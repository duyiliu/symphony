"""Linear GraphQL tracker and provider-native tool for the Python/Pi runtime.

Follows upstream Symphony's Linear adapter contract: project-scoped candidate
polls, ID refresh, optional assignee routing, blocker checks and raw
linear_graphql calls executed in the trusted host (never in the Pi process).
"""
import json
import logging
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
    "description": "Execute Linear GraphQL queries or mutations with host-side credentials.",
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


def _provider(settings):
    return settings.provider or {}


def _endpoint(settings):
    return settings.endpoint or "https://api.linear.app/graphql"


def _token(settings):
    return resolve_env_setting(
        _provider(settings).get("api_key", settings.api_key), "LINEAR_API_KEY"
    )


def validate_config(settings):
    if not present_string(settings.project_slug):
        raise WorkflowError("missing_linear_project_slug")
    if not _token(settings):
        raise WorkflowError("missing_linear_api_token")
    endpoint = _endpoint(settings)
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise WorkflowError("invalid_linear_endpoint")
    if settings.assignee is not None and not present_string(settings.assignee):
        raise WorkflowError("invalid_linear_assignee")
    if not settings.active_states or not settings.terminal_states:
        raise WorkflowError("missing_tracker_states")


def graphql(settings, query, variables=None):
    if not isinstance(query, str) or not query.strip():
        raise WorkflowError("missing_linear_query")
    if variables is None:
        variables = {}
    if not isinstance(variables, dict):
        raise WorkflowError("invalid_linear_variables")
    token = _token(settings)
    if not token:
        raise WorkflowError("missing_linear_api_token")
    data = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        _endpoint(settings), data=data, method="POST",
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (HTTPError, URLError, ValueError, OSError) as error:
        raise WorkflowError("linear_api_request", str(error)) from error
    if not isinstance(payload, dict):
        raise WorkflowError("linear_unknown_payload")
    if payload.get("errors"):
        raise WorkflowError("linear_graphql_errors", str(payload["errors"])[:500])
    return payload


def _assignee(settings):
    wanted = settings.assignee
    if wanted is None:
        return None
    wanted = wanted.strip()
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
    wanted = _assignee(settings)
    result, after, visited = [], None, set()
    while True:
        payload = graphql(settings, POLL_QUERY, {
            "projectSlug": settings.project_slug, "stateNames": list(state_names),
            "first": PAGE_SIZE, "after": after,
        })
        issues, nodes = _rows(payload)
        for row in nodes:
            item = _normalize(row, settings, wanted)
            if item is not None:
                result.append(item)
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


def execute_agent_tool(settings, tool_name, arguments):
    if tool_name != "linear_graphql":
        return {"error": {"code": "unknown_tool", "detail": tool_name}}
    if isinstance(arguments, str):
        query, variables = arguments, {}
    elif isinstance(arguments, dict):
        query = arguments.get("query")
        variables = arguments.get("variables") or {}
    else:
        return {"error": {"code": "invalid_arguments"}}
    if not isinstance(query, str) or not query.strip() or not isinstance(variables, dict):
        return {"error": {"code": "invalid_arguments"}}
    try:
        return graphql(settings, query, variables)
    except WorkflowError as error:
        return {"error": {"code": error.reason, "detail": str(error.detail)[:500]}}
