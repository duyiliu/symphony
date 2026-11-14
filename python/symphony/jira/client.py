"""Thin Jira Cloud REST client for project-scoped issue polling."""

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .. import http_client
from ..config import resolve_env_setting
from ..issue import Issue
from ..util import normalize_issue_state, parse_datetime, present_string
from ..workflow import WorkflowError

log = logging.getLogger("symphony.jira")

PAGE_SIZE = 100
ISSUE_FIELDS = [
    "summary",
    "description",
    "status",
    "labels",
    "assignee",
    "created",
    "updated",
    "project",
    "issuelinks",
]
VALID_METHODS = {"GET", "POST", "PUT", "DELETE"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class JiraSettings:
    base_url: str
    email: str
    api_token: str
    project_key: str
    terminal_states: list


def provider_settings(tracker_settings):
    provider = getattr(tracker_settings, "provider", None)
    return provider if isinstance(provider, dict) else {}


def resolve_setting(value, fallback_env):
    if isinstance(value, str) and value.startswith("$"):
        if not _ENV_NAME.match(value[1:]):
            return None
    return resolve_env_setting(value, fallback_env)


def _valid_base_url(value):
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) \
        and not parsed.query and not parsed.fragment


def settings(tracker_settings):
    provider = provider_settings(tracker_settings)
    base_url = resolve_setting(provider.get("base_url"), "JIRA_BASE_URL")
    email = resolve_setting(provider.get("email"), "JIRA_EMAIL")
    api_token = resolve_setting(provider.get("api_token"), "JIRA_API_TOKEN")
    project_key = resolve_setting(provider.get("project_key"), None)

    if not _valid_base_url(base_url):
        raise WorkflowError("invalid_jira_base_url")
    if not present_string(email):
        raise WorkflowError("missing_jira_email")
    if not present_string(api_token):
        raise WorkflowError("missing_jira_api_token")
    if not present_string(project_key):
        raise WorkflowError("missing_jira_project_key")

    terminal_states = []
    for state in getattr(tracker_settings, "terminal_states", None) or []:
        normalized = normalize_issue_state(state)
        if normalized:
            terminal_states.append(normalized)
    return JiraSettings(
        base_url.rstrip("/"), email.strip(), api_token.strip(),
        project_key.strip(), terminal_states)


def validate_settings(tracker_settings):
    settings(tracker_settings)


def _env_reference_names(values):
    names = []
    for value in values:
        if isinstance(value, str) and value.startswith("$") \
                and _ENV_NAME.match(value[1:]):
            names.append(value[1:])
    return names


def secret_environment_names(tracker_settings):
    provider = provider_settings(tracker_settings)
    names = ["JIRA_API_TOKEN"] + _env_reference_names(
        [provider.get("api_token")])
    return list(dict.fromkeys(names))


def _headers(jira_settings):
    credentials = "%s:%s" % (
        jira_settings.email, jira_settings.api_token)
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return {
        "Accept": "application/json",
        "Authorization": "Basic %s" % encoded,
    }


def _decode_payload(raw):
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def perform_request(method, path, query, body, jira_settings):
    if method not in VALID_METHODS:
        raise WorkflowError("invalid_jira_method", method)
    url = jira_settings.base_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    data = None
    headers = _headers(jira_settings)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method)
    try:
        with http_client.urlopen(request, timeout=30) as response:
            return {
                "status": response.status,
                "body": _decode_payload(response.read()),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "body": _decode_payload(error.read()),
        }
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise WorkflowError("jira_api_request", str(error)) from error


def request(method, path, query, body, tracker_settings=None,
            request_fun=None):
    if tracker_settings is None:
        from ..workflow_store import get_store
        store = get_store()
        tracker_settings = store.settings().tracker if store else None
    jira_settings = settings(tracker_settings)
    return (request_fun or perform_request)(
        method, path, query, body, jira_settings)


def _request_ok(method, path, query, body, jira_settings, request_fun):
    result = request_fun(method, path, query, body, jira_settings)
    if not isinstance(result, dict):
        raise WorkflowError("jira_unknown_payload")
    status = result.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise WorkflowError("jira_unknown_payload")
    if 200 <= status <= 299:
        return result.get("body")
    log.error(
        "Jira API request failed status=%s method=%s path=%s",
        status, method, path)
    raise WorkflowError("jira_api_status", status)


def _state_page(payload):
    if not isinstance(payload, dict):
        raise WorkflowError("jira_unknown_payload")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise WorkflowError("jira_unknown_payload")
    if payload.get("isLast") is True:
        return issues, None
    token = payload.get("nextPageToken")
    if payload.get("isLast") is False and isinstance(token, str) and token:
        return issues, token
    if payload.get("isLast") is False:
        raise WorkflowError("jira_missing_next_page_token")
    raise WorkflowError("jira_unknown_payload")


def _normalize_candidate_page(raw_issues, jira_settings, states):
    requested = {normalize_issue_state(state) for state in states}
    normalized = [normalize_issue(raw, jira_settings) for raw in raw_issues]
    malformed = sum(1 for issue in normalized if issue is None)
    if malformed:
        log.warning(
            "Dropping malformed Jira issue records count=%s", malformed)
    return [
        issue for issue in normalized
        if issue is not None
        and normalize_issue_state(issue.state) in requested
    ]


def fetch_issues_by_states(tracker_settings, states, request_fun=None):
    if not states:
        return []
    jira_settings = settings(tracker_settings)
    request_fun = request_fun or perform_request
    pages = []
    next_token = None
    while True:
        body = {
            "jql": _state_jql(jira_settings.project_key, states),
            "fields": list(ISSUE_FIELDS),
            "maxResults": PAGE_SIZE,
        }
        if next_token is not None:
            body["nextPageToken"] = next_token
        payload = _request_ok(
            "POST", "/rest/api/3/search/jql", {}, body,
            jira_settings, request_fun)
        raw_issues, next_token = _state_page(payload)
        pages.append(_normalize_candidate_page(
            raw_issues, jira_settings, states))
        if next_token is None:
            return [issue for page in pages for issue in page]


def fetch_candidate_issues(tracker_settings):
    return fetch_issues_by_states(
        tracker_settings, tracker_settings.active_states or [])


def _normalize_requested_issues(raw_issues, requested_ids, jira_settings):
    requested = set(requested_ids)
    issues_by_id = {}
    for raw in raw_issues:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise WorkflowError("jira_unknown_payload")
        issue_id = raw["id"]
        if issue_id not in requested:
            continue
        project_key = _issue_project_key(raw)
        if not isinstance(project_key, str):
            raise WorkflowError("jira_unknown_payload")
        if not _same_project_key(project_key, jira_settings.project_key):
            continue
        issue = normalize_issue(raw, jira_settings)
        if issue is None:
            raise WorkflowError("jira_unknown_payload")
        issues_by_id[issue_id] = issue
    return [issues_by_id[issue_id] for issue_id in requested_ids
            if issue_id in issues_by_id]


def fetch_issues_by_ids(tracker_settings, issue_ids, request_fun=None):
    if not issue_ids:
        return []
    jira_settings = settings(tracker_settings)
    request_fun = request_fun or perform_request
    unique_ids = list(dict.fromkeys(issue_ids))
    issues = []
    for start in range(0, len(unique_ids), PAGE_SIZE):
        batch = unique_ids[start:start + PAGE_SIZE]
        payload = _request_ok(
            "POST", "/rest/api/3/issue/bulkfetch", {},
            {"issueIdsOrKeys": batch, "fields": list(ISSUE_FIELDS)},
            jira_settings, request_fun)
        if not isinstance(payload, dict) \
                or not isinstance(payload.get("issues"), list):
            raise WorkflowError("jira_unknown_payload")
        issues.extend(_normalize_requested_issues(
            payload["issues"], batch, jira_settings))
    return issues


def _issue_project_key(raw):
    fields = raw.get("fields") if isinstance(raw, dict) else None
    project = fields.get("project") if isinstance(fields, dict) else None
    return project.get("key") if isinstance(project, dict) else None


def _same_project_key(left, right):
    return isinstance(left, str) and isinstance(right, str) \
        and left.strip().lower() == right.strip().lower()


def _optional_string(value):
    return value if isinstance(value, str) and value.strip() else None


def _blank_to_nil(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _adf_text(value):
    if not isinstance(value, dict):
        return ""
    if value.get("type") == "hardBreak":
        return "\n"
    text = value.get("text")
    if isinstance(text, str):
        return text
    node_type = value.get("type")
    content = value.get("content")
    if node_type in ("paragraph", "heading", "blockquote") \
            and isinstance(content, list):
        return "".join(_adf_text(item) for item in content) + "\n"
    if isinstance(content, list):
        return "".join(_adf_text(item) for item in content)
    attrs = value.get("attrs")
    if isinstance(attrs, dict):
        for key in ("text", "shortName", "url"):
            if key in attrs and isinstance(attrs[key], str):
                return attrs[key]
        return ""
    return ""


def description_text(value):
    if value is None:
        return None
    if isinstance(value, str):
        return _blank_to_nil(value)
    if isinstance(value, dict):
        return _blank_to_nil(_adf_text(value))
    return None


def _extract_labels(labels):
    if not isinstance(labels, list):
        return []
    result = []
    for label in labels:
        if isinstance(label, str):
            normalized = label.strip().lower()
            if normalized and normalized not in result:
                result.append(normalized)
    return result


def _blocker_ref(blocker_issue):
    fields = blocker_issue.get("fields") \
        if isinstance(blocker_issue, dict) else None
    status = fields.get("status") if isinstance(fields, dict) else None
    state = status.get("name") if isinstance(status, dict) else None
    return {
        "id": _optional_string(blocker_issue.get("id"))
        if isinstance(blocker_issue, dict) else None,
        "identifier": _optional_string(blocker_issue.get("key"))
        if isinstance(blocker_issue, dict) else None,
        "state": _optional_string(state),
    }


def _extract_blockers(links):
    if not isinstance(links, list):
        return []
    result = []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type")
        blocker = link.get("inwardIssue")
        if not isinstance(link_type, dict) \
                or not isinstance(link_type.get("name"), str) \
                or not isinstance(blocker, dict):
            continue
        if normalize_issue_state(link_type["name"]) == "blocks":
            result.append(_blocker_ref(blocker))
    return result


def _blocks_gate_dispatch(state, status_category):
    if isinstance(status_category, str) and status_category.strip():
        return normalize_issue_state(status_category) == "new"
    return normalize_issue_state(state) in ("todo", "to do")


def _terminal_blocker(blocker, terminal_states):
    state = blocker.get("state") if isinstance(blocker, dict) else None
    return isinstance(state, str) \
        and normalize_issue_state(state) in terminal_states


def _dispatchable(state, status_category, blockers, terminal_states):
    return not _blocks_gate_dispatch(state, status_category) \
        or all(_terminal_blocker(blocker, terminal_states)
               for blocker in blockers)


def _parse_jira_datetime(value):
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    return parse_datetime(normalized)


def normalize_issue(raw, jira_settings):
    if not isinstance(raw, dict):
        return None
    issue_id = raw.get("id")
    key = raw.get("key")
    fields = raw.get("fields")
    if not isinstance(issue_id, str) or not isinstance(key, str) \
            or not isinstance(fields, dict):
        return None

    status = fields.get("status")
    status = status if isinstance(status, dict) else {}
    status_category = status.get("statusCategory")
    status_category = status_category.get("key") \
        if isinstance(status_category, dict) else None
    title = fields.get("summary")
    state = status.get("name")
    blockers = _extract_blockers(fields.get("issuelinks"))

    if not _same_project_key(
            _issue_project_key(raw), jira_settings.project_key) \
            or not present_string(issue_id) \
            or not present_string(key) \
            or not present_string(title) \
            or not present_string(state):
        return None

    assignee = fields.get("assignee")
    assignee_id = assignee.get("accountId") \
        if isinstance(assignee, dict) else None
    encoded_key = urllib.parse.quote(key, safe="")
    return Issue(
        id=issue_id,
        native_ref=None,
        identifier=key,
        title=title,
        description=description_text(fields.get("description")),
        priority=None,
        state=state,
        branch_name=None,
        url="%s/browse/%s" % (jira_settings.base_url, encoded_key),
        assignee_id=assignee_id if isinstance(assignee_id, str) else None,
        labels=_extract_labels(fields.get("labels")),
        blocked_by=blockers,
        dispatchable=_dispatchable(
            state, status_category, blockers,
            jira_settings.terminal_states),
        created_at=_parse_jira_datetime(fields.get("created")),
        updated_at=_parse_jira_datetime(fields.get("updated")),
    )


def _jql_string(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


def _state_jql(project_key, states):
    quoted = ", ".join(_jql_string(state) for state in states)
    return "project = %s AND status IN (%s)" % (
        _jql_string(project_key), quoted)


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])
