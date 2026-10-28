"""``github_api`` / ``github_workflow`` agent tools exposed over the MCP bridge."""

import json
import logging

from ..workflow import WorkflowError
from ..workflow_store import get_store
from . import client as gh_client
from . import workflow as gh_workflow
from . import workflow_policy
from .workflow_api import GateError

log = logging.getLogger("symphony.github.tools")

_ALLOWED_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")

_GITHUB_API_SPEC = {
    "name": "github_api",
    "description": "Execute a GitHub REST API request using Symphony's configured auth.\n",
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["method", "path"],
        "properties": {
            "method": {
                "type": "string",
                "enum": list(_ALLOWED_METHODS),
                "description": "GitHub REST method.",
            },
            "path": {
                "type": "string",
                "description": "GitHub REST path such as /repos/owner/repo/issues/1/comments.",
            },
            "params": {
                "type": ["object", "null"],
                "description": "Optional query parameters.",
                "additionalProperties": True,
            },
            "body": {
                "description": "Optional JSON request body.",
            },
        },
    },
}

_GITHUB_WORKFLOW_SPEC = {
    "name": "github_workflow",
    "description": (
        "Host-enforced GitHub workflow. Register delivery before handoff. "
        "`agent` alone carries develop through host-guarded merge: only the "
        "host merges, after re-checking labels, SHA, reported checks, risk "
        "rules and review approvals. Issue is its numeric GitHub number. "
        "Never manipulate execution labels through github_api."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "issue"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "deliver", "merge", "pause"],
            },
            "issue": {"type": "string"},
            "pr": {"type": "integer"},
            "tests": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
}


def tool_specs():
    specs = [_GITHUB_API_SPEC]
    settings = _tracker_settings()
    if settings is not None and workflow_policy.enabled(settings):
        specs.append(_GITHUB_WORKFLOW_SPEC)
    return specs


def _tracker_settings():
    store = get_store()
    if store is None:
        return None
    settings = store.settings()
    return getattr(settings, "tracker", None) if settings else None


def execute(tool, arguments):
    if tool == "github_api":
        return _execute_github_api(arguments)
    if tool == "github_workflow":
        return _execute_workflow(arguments)
    return _unsupported_tool_response(tool)


def _execute_github_api(arguments):
    try:
        method, path, params, body = _normalize_arguments(arguments)
    except GateError as error:
        return _failure_response(_tool_error_payload(error.reason))
    settings = _tracker_settings()
    if settings is None:
        return _failure_response(_tool_error_payload("github_workflow_disabled"))
    try:
        workflow_policy.allow_request(settings, method, path, body)
        result = gh_client.request(method, path, params, body, settings)
    except WorkflowError as error:
        return _failure_response(_tool_error_payload(error.reason))
    except GateError as error:
        return _failure_response(_tool_error_payload(error.reason))
    except gh_client.GitHubError as error:
        return _failure_response(
            _tool_error_payload(("github_api_request", error.reason)))
    except Exception:  # noqa: BLE001 - tools must not crash the bridge
        log.exception("github_api tool failed")
        return _failure_response(_tool_error_payload("tool_error"))
    status = result.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return _failure_response(_tool_error_payload("github_unknown_payload"))
    return _rest_response(status, result.get("body"))


def _execute_workflow(arguments):
    if not isinstance(arguments, dict):
        return _failure_response(_tool_error_payload("invalid_arguments"))
    settings = _tracker_settings()
    if settings is None:
        return _failure_response(_tool_error_payload("github_workflow_disabled"))
    try:
        result = gh_workflow.execute(settings, arguments)
    except GateError as error:
        return _failure_response(_tool_error_payload(error.reason))
    except WorkflowError as error:
        return _failure_response(_tool_error_payload(error.reason))
    except Exception:  # noqa: BLE001 - tools must not crash the bridge
        log.exception("github_workflow tool failed")
        return _failure_response(_tool_error_payload("tool_error"))
    return _tool_response(True, _encode_payload(result))


def _normalize_arguments(arguments):
    if not isinstance(arguments, dict):
        raise GateError("invalid_arguments")
    method = _normalize_method(arguments.get("method"))
    path = _normalize_path(arguments.get("path"))
    params = _normalize_params(arguments.get("params"))
    return method, path, params, arguments.get("body")


def _normalize_method(method):
    if not isinstance(method, str):
        raise GateError("invalid_method")
    normalized = method.strip().upper()
    if normalized in _ALLOWED_METHODS:
        return normalized
    raise GateError("invalid_method")


def _normalize_path(path):
    if not isinstance(path, str):
        raise GateError("invalid_path")
    trimmed = path.strip()
    if trimmed.startswith("/") and not any(
            token in trimmed for token in ("://", "\n", "\r", "\x00")):
        return trimmed
    raise GateError("invalid_path")


def _normalize_params(params):
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    raise GateError("invalid_params")


def _rest_response(status, body):
    return _tool_response(
        200 <= status <= 299,
        _encode_payload({"status": status, "body": body}))


def _failure_response(payload):
    return _tool_response(False, _encode_payload(payload))


def _tool_response(success, output):
    return {
        "success": success,
        "output": output,
        "contentItems": [{"type": "inputText", "text": output}],
    }


def _encode_payload(payload):
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return repr(payload)


def _unsupported_tool_response(tool):
    return _failure_response({
        "error": {
            "message": "Unsupported dynamic tool: %s." % gh_workflow._inspect(tool),
            "supportedTools": [spec["name"] for spec in tool_specs()],
        }
    })


def _tool_error_payload(reason):
    if reason == "invalid_arguments":
        return {"error": {"message": "`github_api` expects an object with `method` and `path`."}}
    if reason == "invalid_method":
        return {"error": {"message": "`github_api.method` must be GET, POST, PATCH, PUT, or DELETE."}}
    if reason == "invalid_path":
        return {"error": {"message": "`github_api.path` must be a relative GitHub REST path."}}
    if reason == "invalid_params":
        return {"error": {"message": "`github_api.params` must be a JSON object when provided."}}
    if reason == "missing_github_token":
        return {"error": {"message": "Symphony is missing GitHub auth. Set `tracker.provider.token` in `WORKFLOW.md` or export `GITHUB_TOKEN`."}}
    if isinstance(reason, tuple) and reason and reason[0] == "github_api_request":
        return {"error": {
            "message": "GitHub API request failed before receiving a successful response.",
            "reason": gh_workflow._inspect(reason[1]),
        }}
    return {"error": {
        "message": "GitHub API tool execution failed.",
        "reason": gh_workflow._inspect(reason),
    }}
