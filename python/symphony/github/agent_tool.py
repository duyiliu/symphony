"""In-process MCP tools: ``github_api`` (configured-repo REST) and
``task_api`` (authoritative local task record). Both execute on the host —
the worker environment carries no GitHub token."""

import base64
import logging
import re
from urllib.parse import urlsplit

from .. import workflow_store
from . import client as gh_client
from . import workflow_policy as policy

log = logging.getLogger("symphony.github.agent_tool")

_GITHUB_API_SPEC = {
    "name": "github_api",
    "description": (
        "Call the GitHub REST API for the configured repository. Use this "
        "for issue comments, pull requests, checks, labels and merges "
        "instead of the `gh` CLI — no GitHub token is available in your "
        "shell."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": [
                "GET", "POST", "PATCH", "PUT", "DELETE"]},
            "path": {
                "type": "string",
                "description": "Path relative to https://api.github.com, "
                               "e.g. /repos/{owner}/{repo}/issues/1. Do "
                               "not embed a query string — use params."},
            "params": {
                "type": "object",
                "description": "Optional query parameters (e.g. "
                               "{\"page\": 2, \"per_page\": 100} for "
                               "pagination). Values must be scalars."},
            "body": {"type": ["object", "array", "null"]},
        },
        "required": ["method", "path"],
        "additionalProperties": False,
    },
}


def _tracker_settings():
    store = workflow_store.get_store()
    if store is None:
        return None
    return store.settings().tracker


def tool_specs(tracker_settings=None):
    settings = tracker_settings or _tracker_settings()
    specs = [_GITHUB_API_SPEC]
    if settings is not None and settings.kind == "board":
        from ..tasks import tools as task_tools
        specs.append(task_tools.TASK_API_SPEC)
    return specs


def delivery_tool_specs():
    return [_GITHUB_API_SPEC]


def execute_delivery_tool(arguments, delivery, issue_scope=None):
    """Execute github_api for a Linear delivery project.

    ``delivery`` is a :class:`symphony.config.DeliverySettings`; it carries a
    host-only token. Reads may inspect the configured repository; writes are
    additionally bound to the current issue's open PR and branch.
    """
    if not isinstance(arguments, dict):
        return {"error": {"code": "invalid_arguments"}}
    method = arguments.get("method")
    path = arguments.get("path")
    params = arguments.get("params")
    body = arguments.get("body")
    if not isinstance(method, str) or not isinstance(path, str):
        return {"error": {"code": "invalid_arguments"}}
    if "://" in path or not path.startswith("/") \
            or "?" in path or "#" in path:
        return {"error": {"code": "invalid_arguments"}}
    if params is not None and not _valid_params(params):
        return {"error": {"code": "invalid_arguments"}}
    api_root = (delivery.api_url or "https://api.github.com").rstrip("/")
    url = api_root + path
    try:
        allowed = policy.allow_request_for(
            delivery.repo, method, url, body)
    except Exception:  # noqa: BLE001 - policy failures must not leak detail
        log.exception("delivery github_api policy evaluation failed")
        return {"error": {"code": "forbidden"}}
    if not allowed:
        return {"error": {"code": "forbidden"}}
    api_path = urlsplit(url).path
    gh_settings = gh_client.GitHubSettings(
        api_root, delivery.repo, delivery.token)
    try:
        if not _delivery_write_is_scoped(
                delivery, gh_settings, method, api_path, body, issue_scope):
            return {"error": {"code": "forbidden"}}
        result = gh_client.perform_request(
            method, api_path, params or {}, body, gh_settings)
    except Exception as error:  # noqa: BLE001
        log.exception("delivery github_api request failed")
        return {"error": {"code": "github_request_failed",
                          "detail": getattr(error, "reason", str(error))}}
    response = {
        "status": result["status"],
        "body": _sanitize_body(result["body"]),
    }
    if not (200 <= result["status"] <= 299):
        response["error"] = {
            "code": "github_status",
            "status": result["status"],
        }
    return response


def _delivery_write_is_scoped(
        delivery, gh_settings, method, path, body, issue_scope):
    """Fail closed unless a write targets this worker's issue.

    GET requests remain repository-wide for review and check inspection.
    Mutations must either create a branch whose name contains the issue
    identifier or target an open PR whose head branch is bound to it.
    """
    method = method.upper()
    if method == "GET":
        return True
    identifier = getattr(issue_scope, "identifier", None)
    if not isinstance(identifier, str) or not identifier.strip():
        return False
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "repos" \
            or "/".join(parts[1:3]) != delivery.repo:
        return False
    tail = parts[3:]
    body = body if body is not None else {}

    if method == "POST" and tail == ["pulls"]:
        return isinstance(body, dict) \
            and body.get("base") == delivery.base_branch \
            and _branch_binds_identifier(body.get("head"), identifier)
    if method == "POST" and tail == ["labels"]:
        return isinstance(body, dict) and body.get("name") == "symphony"

    if len(tail) == 3 and tail[0] == "pulls" and tail[2] == "comments" \
            and method == "POST":
        return _pull_is_scoped(
            delivery, gh_settings, tail[1], identifier)
    if len(tail) == 2 and tail[0] == "pulls" \
            and method == "PATCH":
        if not isinstance(body, dict) \
                or not set(body).issubset({"title", "body", "base"}):
            return False
        if "base" in body and body["base"] != delivery.base_branch:
            return False
        return _pull_is_scoped(
            delivery, gh_settings, tail[1], identifier)

    if len(tail) == 3 and tail[0] == "issues" \
            and tail[2] == "comments" and method == "POST":
        return _issue_is_scoped_pull(
            delivery, gh_settings, tail[1], identifier)
    if len(tail) == 2 and tail[0] == "issues" and method == "PATCH":
        if not _pull_identifier_body(body):
            return False
        return _issue_is_scoped_pull(
            delivery, gh_settings, tail[1], identifier)
    if len(tail) == 3 and tail[0] == "issues" \
            and tail[2] == "labels" and method == "POST":
        if not _symphony_label_body(body):
            return False
        return _issue_is_scoped_pull(
            delivery, gh_settings, tail[1], identifier)

    if len(tail) == 3 and tail[0] == "issues" \
            and tail[1] == "comments" \
            and method in {"PATCH", "DELETE"}:
        number = _comment_issue_number(
            delivery, gh_settings, "issues", tail[2])
        return number is not None and _issue_is_scoped_pull(
            delivery, gh_settings, number, identifier)
    if len(tail) == 3 and tail[0] == "pulls" \
            and tail[1] == "comments" and method == "PATCH":
        number = _comment_issue_number(
            delivery, gh_settings, "pulls", tail[2])
        return number is not None and _pull_is_scoped(
            delivery, gh_settings, number, identifier)
    return False


def _pull_identifier_body(body):
    if not isinstance(body, dict) or set(body) != {"labels"}:
        return False
    return _symphony_label_body(body)


def _symphony_label_body(body):
    labels = body.get("labels") if isinstance(body, dict) else None
    return isinstance(labels, list) and labels == ["symphony"]


def _branch_binds_identifier(branch, identifier):
    if not isinstance(branch, str) or not branch:
        return False
    # PR creation accepts either a branch or ``owner:branch``.
    branch = branch.rsplit(":", 1)[-1]
    pattern = r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(
        identifier.strip())
    return re.search(pattern, branch, flags=re.IGNORECASE) is not None


def _pull_is_scoped(delivery, gh_settings, number, identifier):
    payload = _get_github_json(
        gh_settings, _repo_path(delivery.repo, "/pulls/%s" % number))
    if not isinstance(payload, dict) or payload.get("state") != "open":
        return False
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    base_repo = (base.get("repo") or {}).get("full_name")
    head_repo = (head.get("repo") or {}).get("full_name")
    return base_repo == delivery.repo \
        and head_repo == delivery.repo \
        and base.get("ref") == delivery.base_branch \
        and _branch_binds_identifier(head.get("ref"), identifier)


def _issue_is_scoped_pull(delivery, gh_settings, number, identifier):
    payload = _get_github_json(
        gh_settings, _repo_path(delivery.repo, "/issues/%s" % number))
    return isinstance(payload, dict) \
        and bool(payload.get("pull_request")) \
        and _pull_is_scoped(delivery, gh_settings, number, identifier)


def _comment_issue_number(delivery, gh_settings, kind, comment_id):
    suffix = "/issues/comments/%s" % comment_id if kind == "issues" \
        else "/pulls/comments/%s" % comment_id
    payload = _get_github_json(
        gh_settings, _repo_path(delivery.repo, suffix))
    if not isinstance(payload, dict):
        return None
    url = payload.get("pull_request_url") or payload.get("issue_url")
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    expected = [
        "repos", delivery.repo.split("/", 1)[0],
        delivery.repo.split("/", 1)[1],
    ]
    if parts[:3] != expected or len(parts) != 5 \
            or parts[3] not in {"issues", "pulls"}:
        return None
    return parts[4]


def _get_github_json(gh_settings, path):
    try:
        result = gh_client.perform_request("GET", path, {}, None, gh_settings)
    except Exception:  # noqa: BLE001 - verification failures fail closed
        return None
    if not 200 <= result.get("status", 0) <= 299:
        return None
    body = result.get("body")
    return body if isinstance(body, dict) else None


def _repo_path(repo, suffix):
    return "/repos/%s%s" % (gh_client.encoded_repo(repo), suffix)


def execute_agent_tool(tool_name, arguments, tracker_settings=None):
    settings = tracker_settings or _tracker_settings()
    if settings is None:
        return {"error": {"code": "workflow_not_loaded"}}
    if tool_name == "task_api":
        if settings.kind != "board":
            return {"error": {"code": "unknown_tool", "detail": tool_name}}
        from ..tasks import tools as task_tools
        return task_tools.execute(settings, arguments)
    if tool_name != "github_api":
        return {"error": {"code": "unknown_tool", "detail": tool_name}}
    if not isinstance(arguments, dict):
        return {"error": {"code": "invalid_arguments"}}
    method = arguments.get("method")
    path = arguments.get("path")
    params = arguments.get("params")
    body = arguments.get("body")
    if not isinstance(method, str) or not isinstance(path, str):
        return {"error": {"code": "invalid_arguments"}}
    if "://" in path or not path.startswith("/") \
            or "?" in path or "#" in path:
        # Query strings go through `params`; a raw '?' would otherwise be
        # silently stripped by urlsplit and bypass policy validation.
        return {"error": {"code": "invalid_arguments"}}
    if params is not None and not _valid_params(params):
        return {"error": {"code": "invalid_arguments"}}
    url = _API_ROOT + path
    try:
        allowed = policy.allow_request(settings, method, url, body)
    except Exception:  # noqa: BLE001 - a policy failure must not leak detail
        log.exception("github_api policy evaluation failed")
        return {"error": {"code": "forbidden"}}
    if not allowed:
        return {"error": {"code": "forbidden"}}
    from urllib.parse import urlsplit
    api_path = urlsplit(url).path
    try:
        result = gh_client.request(
            method, api_path, params or {}, body, settings)
    except Exception as error:  # noqa: BLE001
        log.exception("github_api request failed")
        return {"error": {"code": "github_request_failed",
                          "detail": getattr(error, "reason", str(error))}}
    response = {
        "status": result["status"],
        "body": _sanitize_body(result["body"]),
    }
    if not (200 <= result["status"] <= 299):
        response["error"] = {
            "code": "github_status",
            "status": result["status"],
        }
    return response


def _valid_params(params):
    """Query params are plain scalars — the repo boundary stays in the
    path, so a nested object or array here is simply malformed input."""
    if not isinstance(params, dict):
        return False
    return all(
        isinstance(key, str)
        and (value is None
             or isinstance(value, (str, int, float, bool)))
        for key, value in params.items())


def _sanitize_body(body):
    """Bound the tool payload; binary-ish bodies degrade to a marker."""
    if isinstance(body, (dict, list)):
        return body
    if body is None:
        return None
    if isinstance(body, bytes):
        return {"_binary": base64.b64encode(body[:256]).decode("ascii")}
    return {"_text": str(body)[:8000]}


_API_ROOT = "https://api.github.com"
