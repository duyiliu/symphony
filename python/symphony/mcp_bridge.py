"""Loopback MCP endpoint exposing tracker agent tools to runtimes that cannot
register custom tools natively (pi).

Transport is Streamable HTTP: one POST per JSON-RPC message, JSON response.
The bearer token is generated per service start and only ever written into
the workspace ``.pi/mcp.json``, so it never enters the agent's environment.
"""

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("symphony.mcp_bridge")

MCP_PATH = "/mcp"
PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.0.3"

_bridge = None
_bridge_lock = threading.Lock()


class _State:
    def __init__(self, server, port, token):
        self.server = server
        self.port = port
        self.token = token
        self.thread = None


def _tools():
    from .github import adapter as tracker_adapter

    return tracker_adapter


def _handle_rpc(request):
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
        tools = [
            {
                "name": spec.get("name"),
                "description": spec.get("description", ""),
                "inputSchema": spec.get("inputSchema")
                    or spec.get("input_schema") or {"type": "object"},
            }
            for spec in (_tools().tool_specs() or [])
        ]
        return _result(request, {"tools": tools})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not (isinstance(name, str) and name != ""):
            return _error(request, -32602, "Missing tool name")
        result = _tools().execute_agent_tool(name, arguments)
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
    return {"content": [{"type": "text", "text": text}], "isError": False}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.debug("mcp %s", format % args)

    def _respond(self, status, body):
        data = body.encode("utf-8") if isinstance(body, str) else body or b""
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?", 1)[0] != MCP_PATH:
            self._respond(404, b"")
            return
        auth = self.headers.get("Authorization") or ""
        expected = "Bearer " + (_bridge.token if _bridge else "")
        if not _bridge or not secrets.compare_digest(
                auth.encode(), expected.encode()):
            self._respond(401, json.dumps({"error": "unauthorized"}))
            return
        length = int(self.headers.get("Content-Length") or 0)
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
            response = _handle_rpc(request)
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
        token = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        port = server.server_address[1]
        state = _State(server, port, token)
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
        _bridge.server.shutdown()
        _bridge.server.server_close()
        _bridge = None


def _enabled():
    try:
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


def client_config():
    """MCP client configuration for a workspace, or ``None`` when disabled."""
    bridge = _bridge
    if bridge is None or not _enabled():
        return None
    return {
        "mcpServers": {
            "symphony": {
                "url": "http://127.0.0.1:%d%s" % (bridge.port, MCP_PATH),
                "headers": {"Authorization": "Bearer %s" % bridge.token},
                "exposure": "direct",
                "description": "Symphony tracker tools (issues, comments, pull requests).",
            }
        }
    }


def endpoint():
    if _bridge is None:
        return None
    return ("http://127.0.0.1:%d%s" % (_bridge.port, MCP_PATH), _bridge.token)
