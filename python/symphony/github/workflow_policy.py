"""Policy helpers for the GitHub tracker family.

``repo()`` resolves the configured repository; ``allow_request()`` is the
``github_api`` agent-tool boundary: configured repo only, per-method path
prefixes and body key allowlists. The worker environment never carries a
raw GitHub token — the host executes these calls on the agent's behalf.
"""

import os
import re
from urllib.parse import urlsplit

from ..util import present_string
from ..workflow import WorkflowError

_API_HOSTS = ("api.github.com", "uploads.github.com")


def repo(settings):
    provider = settings.provider or {}
    value = provider.get("repo")
    if isinstance(value, str) and value.startswith("$"):
        value = os.environ.get(value[1:])
    if not isinstance(value, str):
        return ""
    return value.strip()


def token(settings):
    provider = settings.provider or {}
    value = provider.get("token")
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:])
    return value


def validate_runtime(settings):
    """State dir must not collide with the workspace root (board kind)."""
    if settings.tracker.kind != "board":
        return
    from ..tasks import store as store_mod
    state_dir = store_mod.state_dir(settings.tracker)
    root = os.path.abspath(settings.workspace.root or "")
    directory = os.path.abspath(state_dir or "")
    if directory == root or directory.startswith(root + os.sep) \
            or root.startswith(directory + os.sep):
        raise WorkflowError(
            "invalid_workflow_config",
            "tracker provider.state_dir must not overlap workspace.root")


# -- github_api allowlist --------------------------------------------------

def _allowed_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in _API_HOSTS


def _path_parts(path):
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    if "%" in path or "?" in path or "#" in path or "\\" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or ".." in parts:
        return None
    return parts


def _keys(body, allowed):
    return isinstance(body, dict) and \
        all(key in allowed for key in body.keys())


# GET prefix set kept broad for read-side flexibility (checks, statuses,
# reviews, comments, refs, actions, labels, contents).
_GET_PREFIXES = (
    "issues", "pulls", "commits", "branches", "compare", "contents",
    "git", "actions", "labels", "rate_limit",
)

_POST_LABEL_KEYS = {"labels"}
_POST_LABEL_CREATE_KEYS = {"name", "color", "description"}
_POST_ISSUE_KEYS = {"title", "body", "labels", "assignees", "milestone"}
_POST_PR_KEYS = {"title", "body", "head", "base", "draft",
                 "maintainer_can_modify", "issue"}
_POST_COMMENT_KEYS = {"body"}
_POST_PR_COMMENT_KEYS = {
    "body", "commit_id", "path", "position", "line", "side",
    "start_line", "start_side", "in_reply_to", "subject_type",
}
_PATCH_ISSUE_KEYS = {"title", "body", "state", "state_reason", "labels"}
_PATCH_PR_KEYS = {"title", "body", "state", "base"}
_PATCH_COMMENT_KEYS = {"body"}
def allow_request_for(repo_name, method, url, body):
    """True when a worker github_api call stays inside ``repo_name``.

    Merge is deliberately absent from every method branch: workers may read,
    create/update PRs, and comment, but only the host delivery controller can
    merge.
    """
    if not _allowed_url(url):
        return False
    parts = _path_parts(urlsplit(url).path)
    if not parts:
        return False
    if parts[0] != "repos" or len(parts) < 3:
        return False
    if "/".join(parts[1:3]) != repo_name:
        return False
    tail = parts[3:]
    method = method.upper()
    body = body if body is not None else {}

    if method == "GET":
        return bool(tail) and tail[0] in _GET_PREFIXES
    if method == "DELETE":
        # Workpad / comment deletion only.
        return len(tail) == 3 and tail[0] == "issues" \
            and tail[1] == "comments"
    if method == "PUT":
        return False
    if method == "PATCH":
        if len(tail) == 3 and tail[0] == "issues" and tail[1] == "comments":
            return _keys(body, _PATCH_COMMENT_KEYS)
        if len(tail) == 3 and tail[0] == "pulls" and tail[1] == "comments":
            return _keys(body, _PATCH_COMMENT_KEYS)
        if len(tail) == 2 and tail[0] == "issues":
            return _keys(body, _PATCH_ISSUE_KEYS)
        if len(tail) == 2 and tail[0] == "pulls":
            return _keys(body, _PATCH_PR_KEYS)
        return False
    if method != "POST":
        return False
    if tail == ["pulls"]:
        return _keys(body, _POST_PR_KEYS)
    if tail == ["issues"]:
        return _keys(body, _POST_ISSUE_KEYS)
    if tail == ["labels"]:
        return _keys(body, _POST_LABEL_CREATE_KEYS)
    if len(tail) == 3 and tail[0] == "issues" and tail[2] == "comments":
        return _keys(body, _POST_COMMENT_KEYS)
    if len(tail) == 3 and tail[0] == "issues" and tail[2] == "labels":
        # Body is either {"labels": [...]} or a bare list of names.
        return _keys(body, _POST_LABEL_KEYS) or (
            isinstance(body, list)
            and all(isinstance(v, str) for v in body))
    if len(tail) == 3 and tail[0] == "pulls" and tail[2] == "comments":
        return _keys(body, _POST_PR_COMMENT_KEYS)
    return False


def allow_request(settings, method, url, body):
    """Backward-compatible call for tracker-backed github_api settings."""
    return allow_request_for(repo(settings), method, url, body)
