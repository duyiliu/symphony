"""In-process MCP tools: ``github_api`` (configured-repo REST) and
``task_api`` (authoritative local task record). Both execute on the host —
the worker environment carries no GitHub token."""

import base64
import logging

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


def execute_delivery_tool(arguments, delivery):
    """Execute github_api for a Linear delivery project.

    ``delivery`` is a :class:`symphony.config.DeliverySettings`; it carries a
    host-only token. The policy allowlist is read/PR/comment only and never
    includes merge.
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
    from urllib.parse import urlsplit
    api_path = urlsplit(url).path
    gh_settings = gh_client.GitHubSettings(
        api_root, delivery.repo, delivery.token)
    try:
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
