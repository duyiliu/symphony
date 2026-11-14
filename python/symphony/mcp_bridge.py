"""Loopback MCP endpoint exposing scoped tracker tools to the Pi runtime.

Transport is Streamable HTTP: one POST per JSON-RPC message, JSON response.
Each worker spawn receives a separate bearer token bound to an immutable copy
of its tracker, issue and delivery scope. The token is only written into that
workspace's ``.pi/mcp.json``; host Linear/GitHub credentials remain stripped
from the agent's environment.
"""

import copy
import hashlib
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("symphony.mcp_bridge")

MCP_PATH = "/mcp"
PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.0.3"
MAX_SCOPES = 1024
MAX_REQUEST_BYTES = 1024 * 1024

_bridge = None
_bridge_lock = threading.Lock()


class _State:
    def __init__(self, server, port):
        self.server = server
        self.port = port
        self.thread = None
        self._lock = threading.Lock()
        self._scopes = {}

    def register_scope(self, tracker_settings, delivery_settings, issue_scope):
        tracker_snapshot = _freeze_tracker_settings(tracker_settings)
        delivery_snapshot = copy.deepcopy(delivery_settings)
        with self._lock:
            if len(self._scopes) >= MAX_SCOPES:
                raise RuntimeError("mcp_scope_limit_reached")
            token = secrets.token_urlsafe(32)
            digest = _token_digest(token)
            self._scopes[digest] = (
                tracker_snapshot,
                delivery_snapshot,
                issue_scope,
            )
            return token

    def scope_for_token(self, token):
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            return self._scopes.get(_token_digest(token))

    def revoke_scope(self, token):
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            return self._scopes.pop(_token_digest(token), None) is not None

    def clear(self):
        with self._lock:
            self._scopes.clear()


def _tools():
    from . import tracker as tracker_adapter

    return tracker_adapter


def _handle_rpc(request, scope=None):
    if not isinstance(request, dict):
        return _error(None, -32600, "Invalid Request")
    method = request.get("method")
    if method == "initialize":
        return _result(request, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "symphony", "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None  # 202 with empty body
    if method == "ping":
        return _result(request, {})
    if method == "tools/list":
        if scope is None:
            return _error(request, -32602, "Missing worker scope")
        tracker_settings, delivery, _issue_scope = scope
        tools = [
            {
                "name": spec.get("name"),
                "description": spec.get("description", ""),
                "inputSchema": spec.get("inputSchema")
                    or spec.get("input_schema") or {"type": "object"},
            }
            for spec in (_tools().tool_specs(
                tracker_settings, delivery_settings=delivery) or [])
        ]
        return _result(request, {"tools": tools})
    if method == "tools/call":
        if scope is None:
            return _error(request, -32602, "Missing worker scope")
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not (isinstance(name, str) and name != ""):
            return _error(request, -32602, "Missing tool name")
        tracker_settings, delivery, issue_scope = scope
        result = _tools().execute_agent_tool(
            name, arguments, tracker_settings,
            issue_scope=issue_scope, delivery_settings=delivery)
        return _result(request, _to_mcp_result(result))
    return _error(request, -32601, "Method not found")


def _result(request, result):
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def _error(request, code, message):
    request_id = request.get("id") if isinstance(request, dict) else None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _to_mcp_result(result):
    if isinstance(result, dict) and "success" in result \
            and isinstance(result.get("output"), str):
        return {
            "content": [{"type": "text", "text": result["output"]}],
            "isError": result.get("success") is not True,
        }
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = repr(result)
    return {"content": [{"type": "text", "text": text}],
            "isError": isinstance(result, dict) and bool(result.get("error") or result.get("errors"))}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.debug("mcp %s", format % args)

    def _respond(self, status, body, close=False):
        data = body.encode("utf-8") if isinstance(body, str) else body or b""
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?", 1)[0] != MCP_PATH:
            self._respond(404, b"", close=True)
            return
        auth = self.headers.get("Authorization") or ""
        scope = None
        if _bridge is not None and auth.startswith("Bearer "):
            scope = _bridge.scope_for_token(auth[len("Bearer "):])
        if _bridge is None or scope is None:
            self._respond(
                401, json.dumps({"error": "unauthorized"}), close=True)
            return
        length_values = self.headers.get_all("Content-Length") or []
        if len(length_values) > 1:
            self._respond(400, b"", close=True)
            return
        try:
            length = int(length_values[0]) if length_values else 0
        except ValueError:
            self._respond(400, b"", close=True)
            return
        if length < 0:
            self._respond(400, b"", close=True)
            return
        if length > MAX_REQUEST_BYTES:
            self._respond(413, b"", close=True)
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._respond(200, json.dumps(_error(None, -32700, "Parse error")))
            return
        if not isinstance(request, dict) or "method" not in request:
            self._respond(200, json.dumps(_error(None, -32600, "Invalid Request")))
            return
        try:
            response = _handle_rpc(request, scope)
        except Exception:  # noqa: BLE001 - never leak a 500 to the agent
            log.exception("mcp dispatch failed method=%r", request.get("method"))
            self._respond(200, json.dumps(_error(request, -32603, "Internal error")))
            return
        if response is None:
            self._respond(202, b"")
        else:
            self._respond(200, json.dumps(response, ensure_ascii=False))

    def do_GET(self):
        self._respond(404, b"")

    def __getattr__(self, name):
        # Any non-POST verb hits the same 404 as the Elixir plug.
        if name.startswith("do_"):
            return lambda: self._respond(404, b"")
        raise AttributeError(name)


def start():
    """Start the loopback bridge. Idempotent; returns the bridge state."""
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            return _bridge
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        port = server.server_address[1]
        state = _State(server, port)
        thread = threading.Thread(
            target=server.serve_forever, name="mcp-bridge", daemon=True)
        thread.start()
        state.thread = thread
        _bridge = state
        log.info("MCP bridge listening on 127.0.0.1:%d%s", port, MCP_PATH)
        return state


def stop():
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            return
        _bridge.clear()
        _bridge.server.shutdown()
        _bridge.server.server_close()
        _bridge = None


def _enabled(settings=None):
    try:
        if settings is None:
            from . import workflow_store

            store = workflow_store.get_store()
            if store is None:
                return False
            settings = store.settings()
        return (
            settings.runtime.kind == "pi"
            and settings.runtime.mcp.get("enabled") is True
        )
    except Exception:  # noqa: BLE001 - disabled beats crashing a session start
        return False


def client_config(workspace, settings, issue):
    """Create one isolated MCP scope and return ``(config, bearer_token)``."""
    bridge = _bridge
    if bridge is None or workspace is None or settings is None or issue is None:
        return None
    if not _enabled(settings):
        return None
    from .linear import IssueScope

    issue_id = getattr(issue, "id", None)
    identifier = getattr(issue, "identifier", None)
    if not isinstance(issue_id, str) or not issue_id \
            or not isinstance(identifier, str) or not identifier:
        return None
    token = bridge.register_scope(
        settings.tracker, settings.delivery,
        IssueScope(id=issue_id, identifier=identifier),
    )
    config = {
        "mcpServers": {
            "symphony": {
                "url": "http://127.0.0.1:%d%s" % (bridge.port, MCP_PATH),
                "headers": {"Authorization": "Bearer %s" % token},
                "exposure": "direct",
                "description": (
                    "Symphony tools scoped to this workspace's Linear issue "
                    "and delivery repository."
                ),
            }
        }
    }
    return config, token


def revoke_scope(token):
    """Revoke one worker scope from the active bridge."""
    bridge = _bridge
    return bool(bridge and bridge.revoke_scope(token))


def endpoint():
    """Compatibility accessor; scoped callers must use ``client_config``."""

    return None


def _token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def _freeze_tracker_settings(settings):
    from .linear import _token

    token = _token(settings)
    snapshot = copy.deepcopy(settings)
    if isinstance(token, str) and token:
        snapshot.api_key = token
        snapshot.provider = dict(snapshot.provider or {})
        snapshot.provider["api_key"] = token
    return snapshot
