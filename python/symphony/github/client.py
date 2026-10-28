"""Thin GitHub REST client for repository issue polling."""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from ..issue import Issue
from ..util import normalize_label, parse_datetime, present_string

log = logging.getLogger("symphony.github")

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
PAGE_SIZE = 100
USER_AGENT = "symphony"
VALID_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


class GitHubError(Exception):
    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        super().__init__("%s%s" % (reason, ": %s" % detail if detail else ""))


class GitHubSettings:
    def __init__(self, api_url, repo, token):
        self.api_url = api_url
        self.repo = repo
        self.token = token


def provider_settings(tracker_settings):
    provider = getattr(tracker_settings, "provider", None)
    return provider if isinstance(provider, dict) else {}


def resolve_setting(value, fallback_env):
    from ..config import resolve_env_setting

    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            return None
    return resolve_env_setting(value, fallback_env)


def settings(tracker_settings):
    provider = provider_settings(tracker_settings)
    api_url = provider.get("api_url")
    if api_url is None or api_url is False:
        api_url = DEFAULT_API_URL
    repo = resolve_setting(provider.get("repo"), "GITHUB_REPO")
    token = resolve_setting(provider.get("token"), "GITHUB_TOKEN")

    parsed = urllib.parse.urlparse(api_url) if isinstance(api_url, str) else None
    if not parsed or parsed.scheme != "https" or not parsed.netloc:
        raise GitHubError("invalid_github_api_url")
    if not present_string(repo):
        raise GitHubError("missing_github_repo")
    if not re.fullmatch(r"[^\s/]+/[^\s/]+", repo):
        raise GitHubError("invalid_github_repo")
    if not present_string(token):
        raise GitHubError("missing_github_token")
    return GitHubSettings(api_url.rstrip("/"), repo, token)


def secret_environment_names(tracker_settings):
    provider = provider_settings(tracker_settings)
    names = [
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_ENTERPRISE_TOKEN",
    ]
    token_ref = provider.get("token")
    if isinstance(token_ref, str) and token_ref.startswith("$"):
        name = token_ref[1:]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            names.append(name)
    return list(dict.fromkeys(names))


def _headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer %s" % token,
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }


def perform_request(method, path, params, body, gh_settings):
    """Execute one HTTP request. Returns ``{"status": int, "body": object}``."""
    if method not in VALID_METHODS:
        raise GitHubError("invalid_github_method", method)
    url = gh_settings.api_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = None
    headers = _headers(gh_settings.token)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            return {
                "status": response.status,
                "body": _decode_payload(payload, response.headers),
            }
    except urllib.error.HTTPError as error:
        payload = error.read()
        return {"status": error.code, "body": _decode_payload(payload, error.headers)}
    except (urllib.error.URLError, OSError) as error:
        raise GitHubError("github_api_request", getattr(error, "reason", error))


def _decode_payload(payload, _headers_map):
    if not payload:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Req keeps the raw body for non-JSON responses; mirror that instead of
    # collapsing to None.
    try:
        return json.loads(text)
    except ValueError:
        return text


def encoded_repo(repo):
    return "/".join(urllib.parse.quote(segment, safe="") for segment in repo.split("/", 1))


def repository_issues_path(gh_settings):
    return "/repos/%s/issues" % encoded_repo(gh_settings.repo)


def repository_issue_path(gh_settings, issue_number):
    return "%s/%s" % (repository_issues_path(gh_settings), issue_number)


def _request_ok(method, path, params, body, gh_settings, allow_not_found=False):
    result = perform_request(method, path, params or {}, body, gh_settings)
    status = result["status"]
    if 200 <= status <= 299:
        return result["body"]
    if status == 404 and allow_not_found:
        return _NotFound
    log.error("GitHub API request failed status=%s method=%s path=%s", status, method, path)
    raise GitHubError("github_api_status", status)


class _NotFoundType:
    pass


_NotFound = _NotFoundType()


def _github_state_query(normalized_states):
    has_open = "open" in normalized_states
    has_closed = "closed" in normalized_states
    if has_open and has_closed:
        return "all"
    if has_open:
        return "open"
    if has_closed:
        return "closed"
    return None


def fetch_issues_by_states(state_names, tracker_settings):
    normalized_states = {
        name.strip().lower() for name in state_names if isinstance(name, str)
    }
    state_query = _github_state_query(normalized_states)
    if state_query is None:
        return []
    gh_settings = settings(tracker_settings)
    issues = []
    page = 1
    while True:
        payload = _request_ok(
            "GET",
            repository_issues_path(gh_settings),
            {
                "state": state_query,
                "per_page": PAGE_SIZE,
                "page": page,
                "sort": "created",
                "direction": "asc",
            },
            None,
            gh_settings,
        )
        if not isinstance(payload, list):
            raise GitHubError("github_unknown_payload")
        page_issues = []
        malformed = 0
        for raw in payload:
            issue = normalize_issue(raw, gh_settings.repo)
            if issue is None:
                malformed += 1
            else:
                page_issues.append(issue)
        if malformed:
            log.warning("Dropping malformed GitHub issue records count=%d", malformed)
        issues.extend(
            issue
            for issue in page_issues
            if issue.state and issue.state.strip().lower() in normalized_states
        )
        if len(payload) < PAGE_SIZE:
            return issues
        page += 1


def _parse_issue_number(value):
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        number = int(value)
        if number > 0:
            return number
    raise GitHubError("invalid_github_issue_id")


def fetch_issues_by_ids(issue_ids, tracker_settings):
    ids = list(dict.fromkeys(issue_ids))
    if not ids:
        return []
    gh_settings = settings(tracker_settings)
    issues = []
    for raw_id in ids:
        number = _parse_issue_number(raw_id)
        payload = _request_ok(
            "GET",
            repository_issue_path(gh_settings, number),
            {},
            None,
            gh_settings,
            allow_not_found=True,
        )
        if payload is _NotFound:
            continue
        if not isinstance(payload, dict):
            raise GitHubError("github_unknown_payload")
        issue = normalize_issue(payload, gh_settings.repo)
        if issue is None:
            raise GitHubError("github_unknown_payload")
        issues.append(issue)
    return issues


def normalize_issue(issue, repo):
    if not isinstance(issue, dict):
        return None
    number = issue.get("number")
    state = issue.get("state")
    if not (isinstance(number, int) and not isinstance(number, bool) and number > 0):
        return None
    if not (present_string(issue.get("title")) and present_string(state)):
        return None
    assignee = issue.get("assignee")
    return Issue(
        id=str(number),
        native_ref=_native_ref(issue, repo),
        identifier="GH-%d" % number,
        title=issue["title"],
        description=issue.get("body"),
        state=state,
        url=issue.get("html_url"),
        assignee_id=assignee.get("login") if isinstance(assignee, dict) else None,
        labels=_extract_labels(issue),
        blocked_by=[],
        dispatchable="pull_request" not in issue,
        created_at=parse_datetime(issue.get("created_at")),
        updated_at=parse_datetime(issue.get("updated_at")),
    )


def _native_ref(issue, repo):
    ref = {
        key: value
        for key, value in (
            ("id", issue.get("id")),
            ("node_id", issue.get("node_id")),
            ("number", issue.get("number")),
            ("repo", repo),
        )
        if value is not None
    }
    return ref or None


def _extract_labels(issue):
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    names = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
        elif isinstance(label, str):
            names.append(label)
    seen = []
    for name in names:
        normalized = normalize_label(name)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def request(method, path, params, body, tracker_settings):
    """Authenticated raw request used by workflow_api and the github_api tool."""
    gh_settings = settings(tracker_settings)
    return perform_request(method, path, params or {}, body, gh_settings)
