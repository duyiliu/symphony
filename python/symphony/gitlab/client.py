"""Thin GitLab REST client for project-scoped issue polling."""

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

log = logging.getLogger("symphony.gitlab")

DEFAULT_API_URL = "https://gitlab.com/api/v4"
PAGE_SIZE = 100
VALID_METHODS = {"GET", "POST", "PUT", "DELETE"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IID = re.compile(r"^[0-9]+$")
_NOT_FOUND = object()


@dataclass
class GitLabSettings:
    api_url: str
    api_key: str
    project_path: str


def provider_settings(tracker_settings):
    provider = getattr(tracker_settings, "provider", None)
    return provider if isinstance(provider, dict) else {}


def resolve_setting(value, fallback_env=None):
    if isinstance(value, str) and value.startswith("$"):
        if not _ENV_NAME.match(value[1:]):
            return None
    resolved = resolve_env_setting(value, fallback_env)
    return _normalize_string(resolved) if isinstance(resolved, str) else None


def _normalize_string(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _valid_api_url(value):
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _valid_project_path(value):
    return isinstance(value, str) and not any(
        marker in value for marker in (" ", "\t", "\n", "\r", "\0"))


def settings(tracker_settings):
    provider = provider_settings(tracker_settings)
    api_url = provider.get("api_url") or DEFAULT_API_URL
    project_path = resolve_setting(
        provider.get("project_path"), "GITLAB_PROJECT_PATH")
    api_key = resolve_setting(provider.get("api_key"), "GITLAB_PAT")

    if not _valid_api_url(api_url):
        raise WorkflowError("invalid_gitlab_api_url")
    if not present_string(project_path):
        raise WorkflowError("missing_gitlab_project_path")
    if not _valid_project_path(project_path):
        raise WorkflowError("invalid_gitlab_project_path")
    if not present_string(api_key):
        raise WorkflowError("missing_gitlab_api_key")
    return GitLabSettings(
        api_url=api_url.rstrip("/"),
        api_key=api_key.strip(),
        project_path=project_path.strip(),
    )


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
    names = [
        "GITLAB_PAT",
        "GITLAB_ACCESS_TOKEN",
        "GITLAB_TOKEN",
        "OAUTH_TOKEN",
    ] + _env_reference_names([provider.get("api_key")])
    return list(dict.fromkeys(names))


def _headers(gitlab_settings):
    return {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % gitlab_settings.api_key,
    }


def _decode_payload(raw):
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def perform_request(method, path, query, body, gitlab_settings):
    if method not in VALID_METHODS:
        raise WorkflowError("invalid_gitlab_method", method)
    url = gitlab_settings.api_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    data = None
    headers = _headers(gitlab_settings)
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
        raise WorkflowError("gitlab_api_request", str(error)) from error


def request(method, path, query, body, tracker_settings=None,
            request_fun=None):
    if tracker_settings is None:
        from ..workflow_store import get_store
        store = get_store()
        tracker_settings = store.settings().tracker if store else None
    gitlab_settings = settings(tracker_settings)
    return (request_fun or perform_request)(
        method, path, query, body, gitlab_settings)


def _request_ok(method, path, query, body, gitlab_settings, request_fun,
                allow_not_found=False):
    result = request_fun(method, path, query, body, gitlab_settings)
    if not isinstance(result, dict):
        raise WorkflowError("gitlab_unknown_payload")
    status = result.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise WorkflowError("gitlab_unknown_payload")
    if 200 <= status <= 299:
        return result.get("body")
    if status == 404 and allow_not_found:
        return _NOT_FOUND
    log.error(
        "GitLab API request failed status=%s method=%s path=%s",
        status, method, path)
    raise WorkflowError("gitlab_api_status", status)


def _project_issues_path(gitlab_settings):
    return "/projects/%s/issues" % urllib.parse.quote(
        gitlab_settings.project_path, safe="")


def _project_issue_path(gitlab_settings, iid):
    return "%s/%s" % (_project_issues_path(gitlab_settings), iid)


def _state_query(states):
    opened = "opened" in states
    closed = "closed" in states
    if opened and closed:
        return "all"
    if opened:
        return "opened"
    if closed:
        return "closed"
    return None


def _parse_iid(value):
    if not isinstance(value, str) or not _IID.match(value) \
            or int(value) <= 0:
        raise WorkflowError("invalid_gitlab_issue_id", value)
    return int(value)


def _assignee_id(issue):
    assignees = issue.get("assignees")
    assignee = assignees[0] if isinstance(assignees, list) and assignees \
        else issue.get("assignee")
    if not isinstance(assignee, dict):
        return None
    identifier = assignee.get("id")
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        return str(identifier)
    username = assignee.get("username")
    return username if isinstance(username, str) else None


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


def _blank_to_nil(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _native_ref(issue, project_path):
    result = {
        "id": issue.get("id"),
        "iid": issue.get("iid"),
        "project_id": issue.get("project_id"),
        "project_path": project_path,
        "references": issue.get("references"),
    }
    return {key: value for key, value in result.items() if value is not None}


def normalize_issue(issue, gitlab_settings):
    if not isinstance(issue, dict):
        return None
    iid = issue.get("iid")
    title = issue.get("title")
    state = issue.get("state")
    if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0:
        return None
    if not isinstance(title, str) or not isinstance(state, str):
        return None
    if not present_string(title) or not present_string(state):
        return None
    return Issue(
        id=str(iid),
        native_ref=_native_ref(issue, gitlab_settings.project_path),
        identifier="GL-%s" % iid,
        title=title,
        description=_blank_to_nil(issue.get("description")),
        priority=None,
        state=state,
        branch_name=None,
        url=issue.get("web_url"),
        assignee_id=_assignee_id(issue),
        labels=_extract_labels(issue.get("labels")),
        blocked_by=[],
        dispatchable=True,
        created_at=parse_datetime(issue.get("created_at")),
        updated_at=parse_datetime(issue.get("updated_at")),
    )


def _normalize_state_page(raw_issues, gitlab_settings, requested_states):
    normalized = [
        normalize_issue(issue, gitlab_settings) for issue in raw_issues]
    malformed = sum(1 for issue in normalized if issue is None)
    if malformed:
        log.warning(
            "Dropping malformed GitLab issue records count=%s", malformed)
    return [
        issue for issue in normalized
        if issue is not None
        and normalize_issue_state(issue.state) in requested_states
    ]


def fetch_issues_by_states(tracker_settings, states, request_fun=None):
    normalized_states = {
        normalize_issue_state(state) for state in states or []}
    state_query = _state_query(normalized_states)
    if state_query is None:
        return []
    gitlab_settings = settings(tracker_settings)
    request_fun = request_fun or perform_request
    path = _project_issues_path(gitlab_settings)
    pages = []
    page = 1
    while True:
        query = {
            "state": state_query,
            "per_page": PAGE_SIZE,
            "page": page,
            "order_by": "created_at",
            "sort": "asc",
        }
        payload = _request_ok(
            "GET", path, query, None, gitlab_settings, request_fun)
        if not isinstance(payload, list):
            raise WorkflowError("gitlab_unknown_payload")
        pages.append(_normalize_state_page(
            payload, gitlab_settings, normalized_states))
        if len(payload) < PAGE_SIZE:
            return [issue for issues in pages for issue in issues]
        page += 1


def fetch_candidate_issues(tracker_settings):
    return fetch_issues_by_states(
        tracker_settings, tracker_settings.active_states or [])


def fetch_issues_by_ids(tracker_settings, issue_ids, request_fun=None):
    ids = list(dict.fromkeys(issue_ids or []))
    if not ids:
        return []
    gitlab_settings = settings(tracker_settings)
    request_fun = request_fun or perform_request
    issues = []
    for issue_id in ids:
        iid = _parse_iid(issue_id)
        payload = _request_ok(
            "GET", _project_issue_path(gitlab_settings, iid), {}, None,
            gitlab_settings, request_fun, allow_not_found=True)
        if payload is _NOT_FOUND:
            continue
        if not isinstance(payload, dict):
            raise WorkflowError("gitlab_unknown_payload")
        issue = normalize_issue(payload, gitlab_settings)
        if issue is None:
            raise WorkflowError("gitlab_unknown_payload")
        issues.append(issue)
    return issues


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])
