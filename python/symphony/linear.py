"""Linear GraphQL tracker and provider-native tool for the Python/Pi runtime.

Follows upstream Symphony's Linear adapter contract: project-scoped candidate
polls, ID refresh, optional assignee routing, blocker checks and raw
linear_graphql calls executed in the trusted host (never in the Pi process).
"""
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from .config import resolve_env_setting
from .issue import Issue
from .util import normalize_issue_state, normalize_label, parse_datetime, present_string
from .workflow import WorkflowError

log = logging.getLogger("symphony.linear")
PAGE_SIZE = 50
FIELDS = """
  id identifier title description priority branchName url createdAt updatedAt
  team { id }
  state { name }
  assignee { id }
  labels { nodes { name } }
  attachments(first: 50) {
    nodes { id title subtitle url sourceType metadata }
  }
  inverseRelations(first: 50) {
    nodes { type issue { id identifier state { name } } }
  }
"""
POLL_QUERY = """
query SymphonyLinearPoll($projectSlug: String!, $stateNames: [String!]!,
                         $first: Int!, $after: String) {
  issues(filter: {project: {slugId: {eq: $projectSlug}},
                  state: {name: {in: $stateNames}}},
         first: $first, after: $after) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}
""" % FIELDS
IDS_QUERY = """
query SymphonyLinearIssuesById($ids: [ID!]!, $projectSlug: String!, $first: Int!) {
  issues(filter: {id: {in: $ids}, project: {slugId: {eq: $projectSlug}}},
         first: $first) { nodes { %s } }
}
""" % FIELDS
VIEWER_QUERY = "query SymphonyLinearViewer { viewer { id } }"
TOOL_SPEC = {
    "name": "linear_graphql",
    "description": (
        "Read the current Linear issue or use an approved Workpad, state, or "
        "attachment mutation. The host enforces project and issue scope."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "variables": {"type": ["object", "null"]},
        },
    },
}

_STATE_CACHE = {}
_QUERY_ROOT_FIELDS = frozenset({"issue", "viewer"})
_MUTATION_ROOT_FIELDS = frozenset({
    "issueUpdate", "commentCreate", "commentUpdate", "commentDelete",
    "attachmentCreate", "attachmentLinkGitHubPR",
})
_PROTECTED_STATES = frozenset({"done", "merging"})
_PAGING_ARGUMENTS = frozenset({"first", "after", "last", "before"})
_SELECTION_RULES = {
    "Issue": {
        "id": None,
        "identifier": None,
        "title": None,
        "description": None,
        "priority": None,
        "priorityLabel": None,
        "branchName": None,
        "url": None,
        "createdAt": None,
        "updatedAt": None,
        "completedAt": None,
        "canceledAt": None,
        "archivedAt": None,
        "state": "WorkflowState",
        "project": "Project",
        "team": "Team",
        "assignee": "User",
        "creator": "User",
        "labels": "IssueLabelConnection",
        "attachments": "AttachmentConnection",
        "comments": "CommentConnection",
    },
    "Project": {
        "id": None,
        "name": None,
        "slugId": None,
        "url": None,
    },
    "Team": {
        "id": None,
        "key": None,
        "name": None,
        "states": "TeamStateConnection",
    },
    "User": {
        "id": None,
        "name": None,
        "displayName": None,
        "initials": None,
        "avatarUrl": None,
    },
    "WorkflowState": {
        "id": None,
        "name": None,
        "type": None,
        "color": None,
        "position": None,
    },
    "IssueLabelConnection": {
        "nodes": "IssueLabel",
        "edges": "IssueLabelEdge",
        "pageInfo": "PageInfo",
    },
    "IssueLabelEdge": {"node": "IssueLabel"},
    "IssueLabel": {"id": None, "name": None, "color": None},
    "AttachmentConnection": {
        "nodes": "Attachment",
        "edges": "AttachmentEdge",
        "pageInfo": "PageInfo",
    },
    "AttachmentEdge": {"node": "Attachment"},
    "Attachment": {
        "id": None,
        "title": None,
        "subtitle": None,
        "url": None,
        "sourceType": None,
        "metadata": None,
        "createdAt": None,
        "updatedAt": None,
    },
    "CommentConnection": {
        "nodes": "Comment",
        "edges": "CommentEdge",
        "pageInfo": "PageInfo",
    },
    "CommentEdge": {"node": "Comment"},
    "Comment": {
        "id": None,
        "body": None,
        "url": None,
        "resolvedAt": None,
        "createdAt": None,
        "updatedAt": None,
        "editedAt": None,
        "user": "User",
    },
    "TeamStateConnection": {
        "nodes": "WorkflowState",
        "edges": "TeamStateEdge",
        "pageInfo": "PageInfo",
    },
    "TeamStateEdge": {"node": "WorkflowState"},
    "PageInfo": {
        "hasNextPage": None,
        "hasPreviousPage": None,
        "startCursor": None,
        "endCursor": None,
    },
}
_FIELD_ARGUMENTS = {
    "comments": _PAGING_ARGUMENTS,
    "attachments": _PAGING_ARGUMENTS,
    "labels": _PAGING_ARGUMENTS,
    "states": _PAGING_ARGUMENTS,
}
_MUTATION_RESULT_CONTEXTS = {
    "issueUpdate": "IssueUpdatePayload",
    "commentCreate": "CommentPayload",
    "commentUpdate": "CommentPayload",
    "commentDelete": "CommentDeletePayload",
    "attachmentCreate": "AttachmentPayload",
    "attachmentLinkGitHubPR": "AttachmentPayload",
}
_SELECTION_RULES.update({
    "IssueUpdatePayload": {"success": None, "issue": "Issue"},
    "CommentPayload": {"success": None, "comment": "Comment"},
    "CommentDeletePayload": {"success": None},
    "AttachmentPayload": {"success": None, "attachment": "Attachment"},
})


@dataclass(frozen=True)
class IssueScope:
    """Immutable identity captured when a worker process is spawned."""

    id: str
    identifier: str


class _GraphQLToken:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value


class _GraphQLField:
    __slots__ = ("name", "arguments", "children")

    def __init__(self, name, arguments, children):
        self.name = name
        self.arguments = arguments
        self.children = children


def _invalid_tool(detail):
    raise WorkflowError("invalid_arguments", detail)


def _forbidden(detail):
    raise WorkflowError("linear_tool_forbidden", detail)


def _tokenize_graphql(query):
    """Tokenize the small GraphQL subset accepted from workers.

    This is intentionally strict. Comments and unnecessary punctuation are
    accepted, while malformed input fails before a host credential is used.
    """

    tokens = []
    index = 0
    length = len(query)
    punctuation = set("!():=@[]{},|")
    while index < length:
        char = query[index]
        if char.isspace() or char == ",":
            index += 1
            continue
        if char == "#":
            newline = query.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if query.startswith('"""', index):
            end = query.find('"""', index + 3)
            if end < 0:
                _invalid_tool("unterminated GraphQL block string")
            tokens.append(_GraphQLToken("string", query[index + 3:end]))
            index = end + 3
            continue
        if char == '"':
            cursor = index + 1
            escaped = False
            while cursor < length:
                value = query[cursor]
                if escaped:
                    escaped = False
                elif value == "\\":
                    escaped = True
                elif value == '"':
                    raw = query[index:cursor + 1]
                    try:
                        tokens.append(_GraphQLToken("string", json.loads(raw)))
                    except ValueError:
                        _invalid_tool("invalid GraphQL string")
                    index = cursor + 1
                    break
                cursor += 1
            else:
                _invalid_tool("unterminated GraphQL string")
            continue
        if char == "$":
            cursor = index + 1
            if cursor >= length or not (query[cursor].isalpha() or query[cursor] == "_"):
                _invalid_tool("invalid GraphQL variable")
            while cursor < length and (query[cursor].isalnum() or query[cursor] == "_"):
                cursor += 1
            tokens.append(_GraphQLToken("variable", query[index + 1:cursor]))
            index = cursor
            continue
        if char.isalpha() or char == "_":
            cursor = index + 1
            while cursor < length and (query[cursor].isalnum() or query[cursor] == "_"):
                cursor += 1
            tokens.append(_GraphQLToken("name", query[index:cursor]))
            index = cursor
            continue
        if char.isdigit() or (char == "-" and index + 1 < length):
            cursor = index + 1
            while cursor < length and (
                    query[cursor].isdigit() or query[cursor] in ".eE+-"):
                cursor += 1
            tokens.append(_GraphQLToken("number", query[index:cursor]))
            index = cursor
            continue
        if query.startswith("...", index):
            tokens.append(_GraphQLToken("punct", "..."))
            index += 3
            continue
        if char in punctuation:
            tokens.append(_GraphQLToken("punct", char))
            index += 1
            continue
        _invalid_tool("unsupported GraphQL syntax")
    return tokens


def _matching_delimiter(tokens, start):
    opening = tokens[start].value
    closing = {"{": "}", "(": ")", "[": "]"}.get(opening)
    if closing is None:
        _invalid_tool("invalid GraphQL delimiter")
    stack = [closing]
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "punct" and token.value in ("{", "(", "["):
            stack.append({"{": "}", "(": ")", "[": "]"}[token.value])
        elif token.kind == "punct" and token.value in ("}", ")", "]"):
            if not stack or stack[-1] != token.value:
                _invalid_tool("unbalanced GraphQL delimiters")
            stack.pop()
            if not stack:
                return index
        index += 1
    _invalid_tool("unbalanced GraphQL delimiters")


def _parse_graphql_value(tokens, index, variables):
    token = tokens[index]
    if token.kind == "variable":
        if token.value not in variables:
            _invalid_tool("undefined GraphQL variable")
        return variables[token.value], index + 1
    if token.kind == "string":
        return token.value, index + 1
    if token.kind == "number":
        try:
            return (
                float(token.value) if any(v in token.value for v in ".eE")
                else int(token.value)
            ), index + 1
        except ValueError:
            _invalid_tool("invalid GraphQL number")
    if token.kind == "name":
        if token.value == "true":
            return True, index + 1
        if token.value == "false":
            return False, index + 1
        if token.value == "null":
            return None, index + 1
        return token.value, index + 1
    if token.kind == "punct" and token.value == "[":
        end = _matching_delimiter(tokens, index)
        values = []
        cursor = index + 1
        while cursor < end:
            value, cursor = _parse_graphql_value(tokens, cursor, variables)
            values.append(value)
        return values, end + 1
    if token.kind == "punct" and token.value == "{":
        end = _matching_delimiter(tokens, index)
        values = {}
        cursor = index + 1
        while cursor < end:
            name = tokens[cursor]
            if name.kind != "name" or cursor + 1 >= end \
                    or tokens[cursor + 1].kind != "punct" \
                    or tokens[cursor + 1].value != ":":
                _invalid_tool("invalid GraphQL input object")
            value, cursor = _parse_graphql_value(tokens, cursor + 2, variables)
            values[name.value] = value
        return values, end + 1
    _invalid_tool("invalid GraphQL value")


def _parse_graphql_arguments(tokens, index, variables):
    if index >= len(tokens) or tokens[index].kind != "punct" \
            or tokens[index].value != "(":
        return {}, index
    end = _matching_delimiter(tokens, index)
    arguments = {}
    cursor = index + 1
    while cursor < end:
        name = tokens[cursor]
        if name.kind != "name" or cursor + 1 >= end \
                or tokens[cursor + 1].kind != "punct" \
                or tokens[cursor + 1].value != ":":
            _invalid_tool("invalid GraphQL argument")
        value, cursor = _parse_graphql_value(tokens, cursor + 2, variables)
        arguments[name.value] = value
    return arguments, end + 1


def _parse_selection_fields(tokens, start, end, variables):
    fields = []
    cursor = start
    while cursor < end:
        first = tokens[cursor]
        if first.kind != "name":
            _invalid_tool("invalid GraphQL selection")
        name = first.value
        cursor += 1
        if cursor < end and tokens[cursor].kind == "punct" \
                and tokens[cursor].value == ":":
            if cursor + 1 >= end or tokens[cursor + 1].kind != "name":
                _invalid_tool("invalid GraphQL alias")
            name = tokens[cursor + 1].value
            cursor += 2
        arguments, cursor = _parse_graphql_arguments(
            tokens, cursor, variables)
        if cursor < end and tokens[cursor].kind == "punct" \
                and tokens[cursor].value == "@":
            _forbidden("GraphQL directives are not allowed")
        children = []
        if cursor < end and tokens[cursor].kind == "punct" \
                and tokens[cursor].value == "{":
            selection_start = cursor
            selection_end = _matching_delimiter(tokens, selection_start)
            children = _parse_selection_fields(
                tokens, selection_start + 1, selection_end, variables)
            cursor = selection_end + 1
        fields.append(_GraphQLField(name, arguments, children))
    return fields


def _parse_root_fields(tokens, start, end, variables):
    return _parse_selection_fields(tokens, start, end, variables)


def _parse_worker_operation(query, variables):
    tokens = _tokenize_graphql(query)
    if not tokens:
        _invalid_tool("missing GraphQL operation")
    if any(token.kind == "name" and token.value == "fragment"
           for token in tokens):
        _forbidden("GraphQL fragments are not allowed")
    if any(token.kind == "punct" and token.value == "@" for token in tokens):
        _forbidden("GraphQL directives are not allowed")
    if any(token.kind == "name" and token.value.startswith("__")
           for token in tokens):
        _forbidden("cross-project or introspection fields are not allowed")

    cursor = 0
    if tokens[0].kind == "punct" and tokens[0].value == "{":
        operation_kind = "query"
        body_start = 0
    elif tokens[0].kind == "name" \
            and tokens[0].value in ("query", "mutation", "subscription"):
        operation_kind = tokens[0].value
        cursor = 1
        parens = 0
        body_start = None
        while cursor < len(tokens):
            token = tokens[cursor]
            if token.kind == "punct" and token.value == "(":
                parens += 1
            elif token.kind == "punct" and token.value == ")":
                parens -= 1
                if parens < 0:
                    _invalid_tool("invalid GraphQL operation")
            elif token.kind == "punct" and token.value == "{" and parens == 0:
                body_start = cursor
                break
            cursor += 1
        if body_start is None:
            _invalid_tool("missing GraphQL selection")
    else:
        _invalid_tool("invalid GraphQL operation")
    if operation_kind == "subscription":
        _forbidden("GraphQL subscriptions are not allowed")

    body_end = _matching_delimiter(tokens, body_start)
    if body_end != len(tokens) - 1:
        _forbidden("one GraphQL operation is allowed per call")
    fields = _parse_root_fields(
        tokens, body_start + 1, body_end, variables)
    return operation_kind, fields


def _same_issue(value, issue_scope):
    return isinstance(value, str) and value in {
        issue_scope.id, issue_scope.identifier,
    }


def _coerce_issue_scope(issue_scope):
    if isinstance(issue_scope, IssueScope):
        scope = issue_scope
    elif isinstance(issue_scope, dict):
        scope = IssueScope(
            str(issue_scope.get("id") or ""),
            str(issue_scope.get("identifier") or ""),
        )
    else:
        return None
    if not present_string(scope.id) or not present_string(scope.identifier):
        return None
    return scope


def _state_is_protected(state_name, settings, delivery=None):
    normalized = normalize_issue_state(state_name)
    protected = {
        normalize_issue_state(state)
        for state in (settings.terminal_states or [])
    } | set(_PROTECTED_STATES)
    if delivery is not None and delivery.enabled:
        protected.update({
            normalize_issue_state(delivery.merge_state),
            normalize_issue_state(delivery.done_state),
        })
    return normalized in protected


def _worker_allowed_states(settings, delivery=None):
    names = list(settings.active_states or [])
    if delivery is not None:
        names.append(delivery.review_state)
    else:
        names.append("Human Review")
    return {
        normalize_issue_state(name) for name in names
        if present_string(name)
        and not _state_is_protected(name, settings, delivery)
    }


def _live_issue_scope(settings, issue_scope):
    payload = graphql(
        settings,
        "query SymphonyLinearWorkerIssueScope($id: String!) {"
        " issue(id: $id) { id identifier project { slugId }"
        " team { id states(first: 100) { nodes { id name } } } } }",
        {"id": issue_scope.id},
    )
    issue = (payload.get("data") or {}).get("issue")
    if not isinstance(issue, dict):
        _forbidden("bound issue is not visible")
    project = issue.get("project") or {}
    team = issue.get("team") or {}
    if issue.get("id") != issue_scope.id \
            or issue.get("identifier") != issue_scope.identifier \
            or project.get("slugId") != settings.project_slug:
        _forbidden("bound issue is outside the configured Linear project")
    states = ((team.get("states") or {}).get("nodes"))
    if not isinstance(states, list):
        _invalid_tool("unable to verify Linear workflow states")
    return states


def _validate_comment_scope(settings, issue_scope, comment_id):
    if not present_string(comment_id):
        _invalid_tool("missing comment id")
    payload = graphql(
        settings,
        "query SymphonyLinearWorkerCommentScope($id: String!) {"
        " comment(id: $id) { id issue { id identifier project { slugId } } } }",
        {"id": comment_id},
    )
    comment = (payload.get("data") or {}).get("comment")
    issue = comment.get("issue") if isinstance(comment, dict) else None
    project = issue.get("project") if isinstance(issue, dict) else None
    if not isinstance(issue, dict) \
            or issue.get("id") != issue_scope.id \
            or issue.get("identifier") != issue_scope.identifier \
            or not isinstance(project, dict) \
            or project.get("slugId") != settings.project_slug:
        _forbidden("comment belongs to another issue or project")


def _validate_github_pr_url(url, delivery):
    if delivery is None or not delivery.enabled or not present_string(url):
        _forbidden("GitHub PR attachment requires delivery scope")
    parsed = urlsplit(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    expected = (delivery.repo or "").split("/", 1)
    if parsed.scheme != "https" or parsed.hostname != "github.com" \
            or parsed.port is not None or parsed.username is not None \
            or parsed.password is not None or len(path_parts) != 4 \
            or path_parts[2] != "pull" or not path_parts[3].isdigit() \
            or len(expected) != 2 \
            or [part.lower() for part in path_parts[:2]] \
            != [part.lower() for part in expected]:
        _forbidden("attachment URL is outside the configured repository")


def _validate_selection(fields, context, path):
    rules = _SELECTION_RULES.get(context)
    if rules is None:
        _forbidden("GraphQL selection context is not allowlisted")
    if not fields:
        _invalid_tool("empty GraphQL selection at %s" % path)
    for field in fields:
        if field.name not in rules:
            _forbidden("GraphQL field path is not allowlisted: %s.%s"
                       % (path, field.name))
        allowed_arguments = _FIELD_ARGUMENTS.get(field.name, frozenset())
        if set(field.arguments) - set(allowed_arguments):
            _forbidden("GraphQL field arguments are not allowlisted: %s.%s"
                       % (path, field.name))
        child_context = rules[field.name]
        child_path = "%s.%s" % (path, field.name)
        if child_context is None:
            if field.children:
                _forbidden("GraphQL scalar field cannot be traversed: %s"
                           % child_path)
            continue
        _validate_selection(field.children, child_context, child_path)


def _validate_worker_query(fields, issue_scope):
    if not fields:
        _invalid_tool("empty GraphQL selection")
    for field in fields:
        if field.name not in _QUERY_ROOT_FIELDS:
            _forbidden("GraphQL query root is not allowed")
        if field.name == "issue":
            if set(field.arguments) != {"id"} \
                    or not _same_issue(field.arguments.get("id"), issue_scope):
                _forbidden("GraphQL query is outside the bound issue")
            _validate_selection(field.children, "Issue", "Issue")
        else:
            if field.arguments:
                _forbidden("viewer arguments are not allowlisted")
            _validate_selection(field.children, "User", "Viewer")


def _validate_worker_mutation(
        fields, issue_scope, settings, delivery, states):
    if len(fields) != 1:
        _forbidden("one approved GraphQL mutation is allowed per call")
    field = fields[0]
    name, arguments = field.name, field.arguments
    if name not in _MUTATION_ROOT_FIELDS:
        _forbidden("GraphQL mutation is not allowlisted")

    if name == "issueUpdate":
        if set(arguments) != {"id", "input"} \
                or not _same_issue(arguments.get("id"), issue_scope) \
                or not isinstance(arguments.get("input"), dict):
            _forbidden("issueUpdate is outside the bound issue")
        update = arguments["input"]
        if set(update) != {"stateId"} or not present_string(update.get("stateId")):
            _forbidden("issueUpdate may only change workflow state")
        target = next(
            (state for state in states
             if isinstance(state, dict)
             and state.get("id") == update["stateId"]),
            None,
        )
        if target is None or not present_string(target.get("name")) \
                or _state_is_protected(target["name"], settings, delivery) \
                or normalize_issue_state(target["name"]) \
                not in _worker_allowed_states(settings, delivery):
            _forbidden("issueUpdate target state is not worker-writable")
        return

    if name == "commentCreate":
        value = arguments.get("input")
        if set(arguments) != {"input"} or not isinstance(value, dict) \
                or set(value) != {"issueId", "body"} \
                or not _same_issue(value.get("issueId"), issue_scope) \
                or not present_string(value.get("body")):
            _forbidden("commentCreate is outside the bound issue")
        return

    if name == "commentUpdate":
        value = arguments.get("input")
        if set(arguments) != {"id", "input"} \
                or not isinstance(value, dict) \
                or set(value) != {"body"} \
                or not present_string(value.get("body")):
            _forbidden("commentUpdate is not allowed")
        _validate_comment_scope(settings, issue_scope, arguments.get("id"))
        return

    if name == "commentDelete":
        if set(arguments) != {"id"}:
            _forbidden("commentDelete is not allowed")
        _validate_comment_scope(settings, issue_scope, arguments.get("id"))
        return

    if name == "attachmentCreate":
        value = arguments.get("input")
        allowed = {"issueId", "title", "url", "subtitle"}
        if set(arguments) != {"input"} or not isinstance(value, dict) \
                or not set(value) <= allowed \
                or not {"issueId", "url"} <= set(value) \
                or not _same_issue(value.get("issueId"), issue_scope) \
                or not present_string(value.get("url")):
            _forbidden("attachmentCreate is outside the bound issue")
        parsed = urlsplit(value["url"])
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.hostname == "github.com" \
                and len(path_parts) >= 3 \
                and path_parts[2] == "pull":
            _validate_github_pr_url(value["url"], delivery)
        return

    if name == "attachmentLinkGitHubPR":
        if set(arguments) != {"issueId", "url", "title", "linkKind"} \
                or not _same_issue(arguments.get("issueId"), issue_scope):
            _forbidden("attachmentLinkGitHubPR is outside the bound issue")
        _validate_github_pr_url(arguments.get("url"), delivery)


def _validate_mutation_root(fields):
    if len(fields) != 1 or fields[0].name not in _MUTATION_ROOT_FIELDS:
        _forbidden("GraphQL mutation is not allowlisted")
    field = fields[0]
    result_context = _MUTATION_RESULT_CONTEXTS[field.name]
    _validate_selection(field.children, result_context, field.name)


def _provider(settings):
    return settings.provider or {}


def _endpoint(settings):
    return settings.endpoint or "https://api.linear.app/graphql"


def _token(settings):
    token = resolve_env_setting(
        _provider(settings).get("api_key", settings.api_key), "LINEAR_API_KEY"
    )
    if not isinstance(token, str) or token.strip() == "":
        return None
    return token


def validate_config(settings):
    endpoint = _endpoint(settings)
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise WorkflowError("invalid_linear_endpoint")
    if not _token(settings):
        raise WorkflowError("missing_linear_api_token")
    if not present_string(settings.project_slug):
        raise WorkflowError("missing_linear_project_slug")
    if settings.assignee is not None and not present_string(settings.assignee):
        raise WorkflowError("invalid_linear_assignee")
    if not settings.active_states or not settings.terminal_states:
        raise WorkflowError("missing_tracker_states")


def graphql(settings, query, variables=None, authorization=None, timeout=30):
    if not isinstance(query, str) or not query.strip():
        raise WorkflowError("missing_linear_query")
    if variables is None:
        variables = {}
    if not isinstance(variables, dict):
        raise WorkflowError("invalid_linear_variables")
    token = authorization if authorization is not None else _token(settings)
    if not token:
        raise WorkflowError("missing_linear_api_token")
    data = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        _endpoint(settings), data=data, method="POST",
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        # Never include response bodies: OAuth errors may contain credentials.
        retry_after = error.headers.get("Retry-After") if error.headers else None
        raise WorkflowError("linear_api_http_error", {
            "status": error.code, "retry_after": retry_after,
        }) from None
    except (URLError, ValueError, OSError) as error:
        raise WorkflowError("linear_api_request", str(error)) from error
    if not isinstance(payload, dict):
        raise WorkflowError("linear_unknown_payload")
    if payload.get("errors"):
        # The session reporter needs machine-readable rate-limit/auth signals;
        # preserve the existing message contract for tracker callers.
        codes = [
            (item.get("extensions") or {}).get("code")
            for item in payload["errors"] if isinstance(item, dict)
        ]
        if "RATELIMITED" in codes:
            raise WorkflowError("linear_rate_limited")
        if "AUTHENTICATION_ERROR" in codes or "UNAUTHENTICATED" in codes:
            raise WorkflowError("linear_unauthenticated")
        raise WorkflowError("linear_graphql_errors", str(payload["errors"])[:500])
    return payload


def _assignee(settings):
    wanted = settings.assignee
    if wanted is None:
        return None
    if not isinstance(wanted, str):
        return None
    wanted = wanted.strip()
    if wanted == "":
        return None
    if wanted != "me":
        return wanted
    payload = graphql(settings, VIEWER_QUERY)
    viewer = (payload.get("data") or {}).get("viewer") or {}
    user_id = viewer.get("id")
    if not present_string(user_id):
        raise WorkflowError("missing_linear_viewer_identity")
    return user_id


def _normalize(row, settings, assignee_id=None):
    if not isinstance(row, dict):
        return None
    state = row.get("state") or {}
    state_name = state.get("name") if isinstance(state, dict) else None
    if not all(present_string(row.get(k)) for k in ("id", "identifier", "title")):
        return None
    if not present_string(state_name):
        return None
    assignee = row.get("assignee") or {}
    assigned = assignee.get("id") if isinstance(assignee, dict) else None
    labels_payload = row.get("labels") or {}
    labels = []
    for item in labels_payload.get("nodes") or []:
        if isinstance(item, dict) and present_string(item.get("name")):
            label = normalize_label(item["name"])
            if label not in labels:
                labels.append(label)
    blockers = []
    relations = row.get("inverseRelations") or {}
    for relation in relations.get("nodes") or []:
        if not isinstance(relation, dict) or normalize_issue_state(relation.get("type")) != "blocks":
            continue
        blocking = relation.get("issue")
        if isinstance(blocking, dict):
            state_object = blocking.get("state") or {}
            blockers.append({
                "id": blocking.get("id"),
                "identifier": blocking.get("identifier"),
                "state": state_object.get("name") if isinstance(state_object, dict) else None,
            })
    terminal = {normalize_issue_state(s) for s in settings.terminal_states or []}
    is_blocked = normalize_issue_state(state_name) == "todo" and any(
        normalize_issue_state(b.get("state")) not in terminal for b in blockers
    )
    priority = row.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        priority = None
    team = row.get("team") or {}
    team_id = team.get("id") if isinstance(team, dict) else None
    attachments = []
    attachment_nodes = (row.get("attachments") or {}).get("nodes") or []
    for item in attachment_nodes:
        if not isinstance(item, dict) or not present_string(item.get("url")):
            continue
        attachments.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "subtitle": item.get("subtitle"),
            "url": item.get("url"),
            "source_type": item.get("sourceType"),
            "metadata": item.get("metadata"),
        })
    return Issue(
        id=row["id"], identifier=row["identifier"], title=row["title"],
        description=row.get("description"), priority=priority, state=state_name,
        branch_name=row.get("branchName"), url=row.get("url"),
        assignee_id=assigned, team_id=team_id, attachments=attachments,
        blocked_by=blockers, labels=labels,
        dispatchable=(not is_blocked and (assignee_id is None or assigned == assignee_id)),
        created_at=parse_datetime(row.get("createdAt")),
        updated_at=parse_datetime(row.get("updatedAt")),
    )


def _rows(payload):
    data = payload.get("data")
    issues = data.get("issues") if isinstance(data, dict) else None
    nodes = issues.get("nodes") if isinstance(issues, dict) else None
    if not isinstance(nodes, list):
        raise WorkflowError("linear_unknown_payload")
    return issues, nodes


def fetch_issues_by_states(settings, state_names):
    if not state_names:
        return []
    wanted_states = list(dict.fromkeys(
        str(name) for name in state_names if name is not None))
    if not wanted_states:
        return []
    wanted = _assignee(settings)
    result, after, visited = [], None, set()
    while True:
        payload = graphql(settings, POLL_QUERY, {
            "projectSlug": settings.project_slug, "stateNames": wanted_states,
            "first": PAGE_SIZE, "after": after,
        })
        issues, nodes = _rows(payload)
        malformed = 0
        for row in nodes:
            item = _normalize(row, settings, wanted)
            if item is not None:
                result.append(item)
            else:
                malformed += 1
        if malformed:
            log.warning(
                "Dropping malformed Linear issue records count=%d", malformed)
        page = issues.get("pageInfo")
        if not isinstance(page, dict):
            raise WorkflowError("linear_unknown_payload")
        if not page.get("hasNextPage"):
            return result
        cursor = page.get("endCursor")
        if not present_string(cursor) or cursor in visited:
            raise WorkflowError("linear_missing_end_cursor")
        visited.add(cursor)
        after = cursor


def fetch_candidate_issues(settings):
    return fetch_issues_by_states(settings, settings.active_states or [])


def fetch_issues_by_ids(settings, issue_ids):
    ids = list(dict.fromkeys(issue_ids))
    if not ids:
        return []
    wanted = _assignee(settings)
    found = {}
    for offset in range(0, len(ids), PAGE_SIZE):
        chunk = ids[offset:offset + PAGE_SIZE]
        _, nodes = _rows(graphql(settings, IDS_QUERY, {
            "ids": chunk, "projectSlug": settings.project_slug, "first": len(chunk)
        }))
        for row in nodes:
            issue = _normalize(row, settings, wanted)
            if issue is None:
                raise WorkflowError("linear_unknown_payload")
            found[issue.id] = issue
    return [found[issue_id] for issue_id in ids if issue_id in found]


def clear_state_cache():
    _STATE_CACHE.clear()


def state_id(settings, team_id, state_name):
    if not present_string(team_id) or not present_string(state_name):
        raise WorkflowError("missing_linear_state_identity")
    key = (_endpoint(settings), team_id)
    states = _STATE_CACHE.get(key)
    if states is None:
        payload = graphql(
            settings,
            "query SymphonyLinearTeamStates($teamId: String!) {"
            " team(id: $teamId) { states(first: 100) { nodes { id name } } } }",
            {"teamId": team_id},
        )
        data = payload.get("data") or {}
        team = data.get("team") if isinstance(data, dict) else None
        nodes = ((team or {}).get("states") or {}).get("nodes")
        if not isinstance(nodes, list):
            raise WorkflowError("linear_unknown_payload")
        states = {}
        for node in nodes:
            if isinstance(node, dict) and present_string(node.get("id")) \
                    and present_string(node.get("name")):
                states[normalize_issue_state(node["name"])] = node["id"]
        _STATE_CACHE[key] = states
    wanted = normalize_issue_state(state_name)
    if wanted not in states:
        raise WorkflowError("unknown_linear_state", state_name)
    return states[wanted]


def update_issue_state(settings, issue_id, team_id, state_name):
    resolved = state_id(settings, team_id, state_name)
    payload = graphql(
        settings,
        "mutation SymphonyLinearUpdateIssue($id: String!, $input: IssueUpdateInput!) {"
        " issueUpdate(id: $id, input: $input) { success issue { id state { name } } } }",
        {"id": issue_id, "input": {"stateId": resolved}},
    )
    result = (payload.get("data") or {}).get("issueUpdate")
    if not isinstance(result, dict) or result.get("success") is not True:
        raise WorkflowError("linear_issue_update_failed")
    return result


def create_comment(settings, issue_id, body):
    if not present_string(issue_id) or not present_string(body):
        raise WorkflowError("invalid_linear_comment")
    payload = graphql(
        settings,
        "mutation SymphonyLinearCreateComment($input: CommentCreateInput!) {"
        " commentCreate(input: $input) { success comment { id } } }",
        {"input": {"issueId": issue_id, "body": body}},
    )
    result = (payload.get("data") or {}).get("commentCreate")
    if not isinstance(result, dict) or result.get("success") is not True:
        raise WorkflowError("linear_comment_create_failed")
    return result


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])


def tool_specs():
    return [TOOL_SPEC]


def execute_agent_tool(
        settings, tool_name, arguments, issue_scope=None, delivery=None):
    if tool_name != "linear_graphql":
        return {"error": {"code": "unknown_tool", "detail": tool_name}}
    if isinstance(arguments, str):
        query, variables = arguments.strip(), {}
    elif isinstance(arguments, dict):
        query = arguments.get("query")
        if isinstance(query, str):
            query = query.strip()
        variables = arguments.get("variables") or {}
    else:
        return {"error": {"code": "invalid_arguments"}}
    if not isinstance(query, str) or query == "":
        return {"error": {"code": "missing_query"}}
    if not isinstance(variables, dict):
        return {"error": {"code": "invalid_variables"}}
    try:
        scope = _coerce_issue_scope(issue_scope)
        if scope is None:
            _forbidden("worker issue scope is required")
        operation, fields = _parse_worker_operation(query, variables)
        if operation == "query":
            _validate_worker_query(fields, scope)
            _live_issue_scope(settings, scope)
        elif operation == "mutation":
            _validate_mutation_root(fields)
            states = _live_issue_scope(settings, scope)
            _validate_worker_mutation(
                fields, scope, settings, delivery, states)
        else:
            _forbidden("GraphQL operation is not allowed")
        return graphql(settings, query, variables)
    except WorkflowError as error:
        return {"error": {"code": error.reason, "detail": str(error.detail)[:500]}}
