"""Host-side ``jira_rest`` tool exposed to Pi turns."""

import json

from ..workflow import WorkflowError
from . import client

_ALLOWED_METHODS = ["GET", "POST", "PUT", "DELETE"]

_JIRA_REST_SPEC = {
    "name": "jira_rest",
    "description": (
        "Execute a Jira Cloud REST v3 request using Symphony's configured "
        "auth."),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["method", "path"],
        "properties": {
            "method": {
                "type": "string",
                "enum": _ALLOWED_METHODS,
                "description": "Jira REST method.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Jira REST v3 path beginning with /rest/api/3/."),
            },
            "query": {
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


def tool_specs():
    return [_JIRA_REST_SPEC]


def _encode_payload(payload):
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return repr(payload)


def _dynamic_tool_response(success, output):
    return {
        "success": success,
        "output": output,
        "contentItems": [{"type": "inputText", "text": output}],
    }


def _failure(payload):
    return _dynamic_tool_response(False, _encode_payload(payload))


def _tool_error_payload(reason, detail=None):
    messages = {
        "invalid_arguments":
            "jira_rest expects an object with method and path.",
        "invalid_method":
            "jira_rest.method must be GET, POST, PUT, or DELETE.",
        "invalid_path":
            "jira_rest.path must begin with /rest/api/3/.",
        "invalid_query":
            "jira_rest.query must be a JSON object when provided.",
        "missing_jira_api_token":
            "Symphony is missing Jira auth. Set tracker.provider.api_token "
            "or export JIRA_API_TOKEN.",
    }
    if reason == "jira_api_request":
        return {"error": {
            "message": (
                "Jira API request failed before receiving a successful "
                "response."),
            "reason": str(detail),
        }}
    message = messages.get(reason)
    if message:
        return {"error": {"message": message}}
    return {"error": {
        "message": "Jira REST tool execution failed.",
        "reason": str(detail if detail is not None else reason),
    }}


def _normalize_method(value):
    if not isinstance(value, str):
        raise WorkflowError("invalid_method")
    method = value.strip().upper()
    if method not in _ALLOWED_METHODS:
        raise WorkflowError("invalid_method")
    return method


def _normalize_path(value):
    if not isinstance(value, str):
        raise WorkflowError("invalid_path")
    path = value.strip()
    if not path.startswith("/rest/api/3/") \
            or "://" in path or "\n" in path or "\r" in path or "\0" in path:
        raise WorkflowError("invalid_path")
    return path


def _normalize_query(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise WorkflowError("invalid_query")


def _normalize_arguments(arguments):
    if not isinstance(arguments, dict):
        raise WorkflowError("invalid_arguments")
    method = _normalize_method(arguments.get("method"))
    path = _normalize_path(arguments.get("path"))
    query = _normalize_query(arguments.get("query"))
    return method, path, query, arguments.get("body")


def _unsupported_tool(tool_name):
    return _failure({"error": {
        "message": "Unsupported dynamic tool: %r." % (tool_name,),
        "supportedTools": [spec["name"] for spec in tool_specs()],
    }})


def execute_agent_tool(tool_name, arguments, tracker_settings=None,
                       request_fun=None):
    if tool_name != "jira_rest":
        return _unsupported_tool(tool_name)
    try:
        method, path, query, body = _normalize_arguments(arguments)
    except WorkflowError as error:
        return _failure(_tool_error_payload(error.reason))
    try:
        result = client.request(
            method, path, query, body,
            tracker_settings=tracker_settings, request_fun=request_fun)
    except WorkflowError as error:
        return _failure(_tool_error_payload(error.reason, error.detail))
    status = result.get("status") if isinstance(result, dict) else None
    if isinstance(status, bool) or not isinstance(status, int):
        return _failure(_tool_error_payload("jira_unknown_payload"))
    return _dynamic_tool_response(
        200 <= status <= 299,
        _encode_payload({"status": status, "body": result.get("body")}))
