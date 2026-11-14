"""Thin Asana REST client for project-scoped task polling."""

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

log = logging.getLogger("symphony.asana")

DEFAULT_ENDPOINT = "https://app.asana.com/api/1.0"
PAGE_SIZE = 100
TASK_FIELDS = [
    "gid",
    "name",
    "notes",
    "completed",
    "resource_subtype",
    "assignee.gid",
    "tags.name",
    "memberships.project.gid",
    "memberships.section.gid",
    "memberships.section.name",
    "permalink_url",
    "created_at",
    "modified_at",
]
TASK_FIELDS_QUERY = ",".join(TASK_FIELDS)
VALID_METHODS = {"GET", "POST", "PUT", "DELETE"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NOT_FOUND = object()


@dataclass
class AsanaSettings:
    endpoint: str
    api_key: str
    project_gid: str


def provider_settings(tracker_settings):
    provider = getattr(tracker_settings, "provider", None)
    return provider if isinstance(provider, dict) else {}


def resolve_setting(value, fallback_env):
    if isinstance(value, str) and value.startswith("$"):
        if not _ENV_NAME.match(value[1:]):
            return None
    return resolve_env_setting(value, fallback_env)


def _valid_endpoint(value):
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def settings(tracker_settings):
    provider = provider_settings(tracker_settings)
    endpoint = provider.get("endpoint") or DEFAULT_ENDPOINT
    api_key = resolve_setting(provider.get("api_key"), "ASANA_PAT")
    project_gid = resolve_setting(provider.get("project_gid"), None)

    if not _valid_endpoint(endpoint):
        raise WorkflowError("invalid_asana_endpoint")
    if not present_string(api_key):
        raise WorkflowError("missing_asana_api_key")
    if not present_string(project_gid):
        raise WorkflowError("missing_asana_project_gid")
    return AsanaSettings(
        endpoint.rstrip("/"), api_key.strip(), project_gid.strip())


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
    names = ["ASANA_PAT"] + _env_reference_names(
        [provider.get("api_key")])
    return list(dict.fromkeys(names))


def _headers(asana_settings):
    return {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % asana_settings.api_key,
    }


def _decode_payload(raw):
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def perform_request(method, path, query, body, asana_settings):
    if method not in VALID_METHODS:
        raise WorkflowError("invalid_asana_method", method)
    url = asana_settings.endpoint + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    data = None
    headers = _headers(asana_settings)
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
        raise WorkflowError("asana_api_request", str(error)) from error


def request(method, path, query, body, tracker_settings=None,
            request_fun=None):
    if tracker_settings is None:
        from ..workflow_store import get_store
        store = get_store()
        tracker_settings = store.settings().tracker if store else None
    asana_settings = settings(tracker_settings)
    return (request_fun or perform_request)(
        method, path, query, body, asana_settings)


def _request_ok(method, path, query, body, asana_settings, request_fun,
                allow_not_found=False):
    result = request_fun(method, path, query, body, asana_settings)
    if not isinstance(result, dict):
        raise WorkflowError("asana_unknown_payload")
    status = result.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise WorkflowError("asana_unknown_payload")
    if 200 <= status <= 299:
        return result.get("body")
    if status == 404 and allow_not_found:
        return _NOT_FOUND
    log.error(
        "Asana API request failed status=%s method=%s path=%s",
        status, method, path)
    raise WorkflowError("asana_api_status", status)


def _task_page(payload):
    if not isinstance(payload, dict) or not isinstance(
            payload.get("data"), list):
        raise WorkflowError("asana_unknown_payload")
    tasks = payload["data"]
    next_page = payload.get("next_page")
    if next_page is None:
        return tasks, None
    if isinstance(next_page, dict):
        offset = next_page.get("offset")
        if isinstance(offset, str) and offset:
            return tasks, offset
        raise WorkflowError("asana_missing_next_page_offset")
    raise WorkflowError("asana_unknown_payload")


def _project_membership(task, project_gid):
    memberships = task.get("memberships") \
        if isinstance(task, dict) else None
    if not isinstance(memberships, list):
        return None
    for membership in memberships:
        project = membership.get("project") \
            if isinstance(membership, dict) else None
        if isinstance(project, dict) and project.get("gid") == project_gid:
            return membership
    return None


def _native_ref(task_gid, project_gid, membership):
    result = {"task_gid": task_gid, "project_gid": project_gid}
    section = membership.get("section") \
        if isinstance(membership, dict) else None
    if isinstance(section, dict) and section.get("gid") is not None:
        result["section_gid"] = section["gid"]
    return result


def _extract_labels(tags):
    if not isinstance(tags, list):
        return []
    result = []
    for tag in tags:
        name = tag.get("name") if isinstance(tag, dict) else None
        if isinstance(name, str):
            normalized = name.strip().lower()
            if normalized and normalized not in result:
                result.append(normalized)
    return result


def _blank_to_nil(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalize_issue(task, asana_settings):
    if not isinstance(task, dict):
        return None
    gid = task.get("gid")
    name = task.get("name")
    if not isinstance(gid, str) or not isinstance(name, str):
        return None
    membership = _project_membership(task, asana_settings.project_gid)
    section = membership.get("section") \
        if isinstance(membership, dict) else None
    state = section.get("name") if isinstance(section, dict) else None
    if not present_string(gid) or not present_string(name) \
            or not present_string(state):
        return None

    assignee = task.get("assignee")
    assignee_id = assignee.get("gid") if isinstance(assignee, dict) else None
    return Issue(
        id=gid,
        native_ref=_native_ref(
            gid, asana_settings.project_gid, membership),
        identifier="ASANA-%s" % gid,
        title=name,
        description=_blank_to_nil(task.get("notes")),
        priority=None,
        state=state,
        branch_name=None,
        url=task.get("permalink_url"),
        assignee_id=assignee_id if isinstance(assignee_id, str) else None,
        labels=_extract_labels(task.get("tags")),
        blocked_by=[],
        dispatchable=task.get("completed") is False
        and task.get("resource_subtype") != "section",
        created_at=parse_datetime(task.get("created_at")),
        updated_at=parse_datetime(task.get("modified_at")),
    )


def _normalize_candidate_page(raw_tasks, asana_settings, states):
    requested = {normalize_issue_state(state) for state in states}
    normalized = [
        normalize_issue(task, asana_settings) for task in raw_tasks]
    malformed = sum(1 for issue in normalized if issue is None)
    if malformed:
        log.warning(
            "Dropping malformed Asana task records count=%s", malformed)
    return [
        issue for issue in normalized
        if issue is not None
        and normalize_issue_state(issue.state) in requested
    ]


def fetch_issues_by_states(tracker_settings, states, request_fun=None):
    if not states:
        return []
    asana_settings = settings(tracker_settings)
    request_fun = request_fun or perform_request
    path = "/projects/%s/tasks" % urllib.parse.quote(
        asana_settings.project_gid, safe="")
    pages = []
    offset = None
    while True:
        query = {"limit": PAGE_SIZE, "opt_fields": TASK_FIELDS_QUERY}
        if offset is not None:
            query["offset"] = offset
        payload = _request_ok(
            "GET", path, query, None, asana_settings, request_fun)
        raw_tasks, offset = _task_page(payload)
        pages.append(_normalize_candidate_page(
            raw_tasks, asana_settings, states))
        if offset is None:
            return [issue for page in pages for issue in page]


def fetch_candidate_issues(tracker_settings):
    return fetch_issues_by_states(
        tracker_settings, tracker_settings.active_states or [])


def _task_outside_project(task, project_gid):
    if not isinstance(task, dict):
        return False
    gid = task.get("gid")
    name = task.get("name")
    memberships = task.get("memberships")
    if not isinstance(gid, str) or not isinstance(name, str) \
            or not isinstance(memberships, list):
        return False
    return present_string(gid) and present_string(name) \
        and _project_membership(task, project_gid) is None


def fetch_issues_by_ids(tracker_settings, issue_ids, request_fun=None):
    if not issue_ids:
        return []
    asana_settings = settings(tracker_settings)
    request_fun = request_fun or perform_request
    issues = []
    for issue_id in dict.fromkeys(issue_ids):
        path = "/tasks/%s" % urllib.parse.quote(issue_id, safe="")
        payload = _request_ok(
            "GET", path, {"opt_fields": TASK_FIELDS_QUERY}, None,
            asana_settings, request_fun, allow_not_found=True)
        if payload is _NOT_FOUND:
            continue
        if not isinstance(payload, dict) \
                or not isinstance(payload.get("data"), dict):
            raise WorkflowError("asana_unknown_payload")
        raw_task = payload["data"]
        if _task_outside_project(raw_task, asana_settings.project_gid):
            continue
        issue = normalize_issue(raw_task, asana_settings)
        if issue is None:
            raise WorkflowError("asana_unknown_payload")
        issues.append(issue)
    return issues


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])
