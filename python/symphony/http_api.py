"""Observability HTTP API compatible with the Elixir Phoenix surface.

Routes:
- ``GET  /api/v1/state``            : full scheduler snapshot
- ``GET  /api/v1/{identifier}``     : single-issue detail
- ``POST /api/v1/refresh``          : coalesced poll/reconcile request

The response shape mirrors ``SymphonyElixirWeb.Presenter`` so the existing
Python board can consume it unchanged.
"""

import datetime
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("symphony.http_api")

SNAPSHOT_TIMEOUT_S = 15


def _iso(value):
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value.tzinfo else None
    return None


def _iso_full(value):
    """DateTime.to_iso8601/1 equivalent: microseconds kept when nonzero."""
    if not isinstance(value, datetime.datetime) or not value.tzinfo:
        return None
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(value, maximum=140):
    """String.slice(0, max) <> \"...\" when byte_size > max (Elixir semantics)."""
    if not isinstance(value, str):
        return value
    if len(value.encode("utf-8")) > maximum:
        return value[:maximum] + "..."
    return value


# -- last_codex_message humanisation ------------------------------------------
# Port of SymphonyElixir.StatusDashboard.humanize_codex_message/1 and helpers.

def _summarize_message(message):
    if message is None:
        return None
    return _humanize_codex_message(message)


def _humanize_codex_message(message):
    if isinstance(message, dict):
        if "event" in message and "message" in message:
            payload = _unwrap_payload(message.get("message"))
            text = _humanize_event(
                message.get("event"), message.get("message"), payload)
            if text is None:
                text = _humanize_payload(payload)
            return _truncate(text)
        if "message" in message:
            return _truncate(_humanize_payload(_unwrap_payload(message.get("message"))))
    return _truncate(_humanize_payload(_unwrap_payload(message)))


def _humanize_event(event, message, payload):
    if event == "session_started":
        session_id = _map_value(payload, ["session_id"])
        if isinstance(session_id, str):
            return "session started (%s)" % session_id
        return "session started"
    if event == "turn_input_required":
        return "turn blocked: waiting for user input"
    if event == "approval_auto_approved":
        method = _map_value(payload, ["method"]) \
            or _map_path(message, ["payload", "method"])
        decision = _map_value(message, ["decision"])
        if isinstance(method, str):
            base = "%s (auto-approved)" % _humanize_method(method, payload)
        else:
            base = "approval request auto-approved"
        if isinstance(decision, str):
            return "%s: %s" % (base, decision)
        return base
    if event == "tool_call_completed":
        return _dynamic_tool_event("dynamic tool call completed", payload)
    if event == "tool_call_failed":
        return _dynamic_tool_event("dynamic tool call failed", payload)
    if event == "unsupported_tool_call":
        return _dynamic_tool_event("unsupported dynamic tool call rejected", payload)
    if event == "turn_ended_with_error":
        return "turn ended with error: %s" % _format_reason(message)
    if event == "startup_failed":
        return "startup failed: %s" % _format_reason(message)
    if event == "turn_failed":
        return _humanize_method("turn/failed", payload)
    if event == "turn_cancelled":
        return "turn cancelled"
    if event == "malformed":
        return "malformed JSON event from codex"
    return None


def _unwrap_payload(message):
    if not isinstance(message, dict):
        return message
    for key in ("method", "session_id", "reason"):
        if isinstance(message.get(key), str):
            return message
    payload = message.get("payload")
    return payload if payload is not None else message


def _humanize_payload(payload):
    if isinstance(payload, dict):
        method = payload.get("method")
        if isinstance(method, str):
            return _humanize_method(method, payload)
        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            return "session started (%s)" % session_id
        if "error" in payload:
            return "error: %s" % _format_error_value(payload.get("error"))
        return _clean(_inspect(payload))
    if isinstance(payload, str):
        return _clean(payload)
    return _clean(_inspect(payload))


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ANSI_ESC_RE = re.compile(r"\x1b.")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean(text):
    text = text.replace("\n", " ")
    text = _ANSI_RE.sub("", text)
    text = _ANSI_ESC_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text.strip()


def _inspect(value):
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _humanize_method(method, payload):
    handler = _METHOD_HANDLERS.get(method)
    if handler is not None:
        return handler(payload)
    if method.startswith("codex/event/"):
        return _humanize_wrapper_event(method[len("codex/event/"):], payload)
    msg_type = _map_path(payload, ["params", "msg", "type"])
    if isinstance(msg_type, str):
        return "%s (%s)" % (method, msg_type)
    return method


def _m_thread_started(payload):
    thread_id = _map_path(payload, ["params", "thread", "id"])
    return ("thread started (%s)" % thread_id
            if isinstance(thread_id, str) else "thread started")


def _m_turn_started(payload):
    turn_id = _map_path(payload, ["params", "turn", "id"])
    return ("turn started (%s)" % turn_id
            if isinstance(turn_id, str) else "turn started")


def _m_turn_completed(payload):
    status = _map_path(payload, ["params", "turn", "status"]) or "completed"
    usage = _map_path(payload, ["params", "usage"]) \
        or _map_path(payload, ["params", "tokenUsage"]) \
        or _map_value(payload, ["usage"])
    usage_text = _format_usage_counts(usage)
    suffix = " (%s)" % usage_text if usage_text else ""
    return "turn completed (%s)%s" % (status, suffix)


def _m_turn_failed(payload):
    error_message = _map_path(payload, ["params", "error", "message"])
    return ("turn failed: %s" % error_message
            if isinstance(error_message, str) else "turn failed")


def _m_turn_diff_updated(payload):
    diff = _map_path(payload, ["params", "diff"]) or ""
    if isinstance(diff, str) and diff != "":
        line_count = len([line for line in diff.split("\n") if line.strip()])
        return "turn diff updated (%d lines)" % line_count
    return "turn diff updated"


def _m_turn_plan_updated(payload):
    entries = None
    for key in ("plan", "steps", "items"):
        entries = _map_path(payload, ["params", key])
        if entries is not None:
            break
    if isinstance(entries, list):
        return "plan updated (%d steps)" % len(entries)
    return "plan updated"


def _m_token_usage_updated(payload):
    usage = _map_path(payload, ["params", "tokenUsage", "total"]) \
        or _map_value(payload, ["usage"])
    usage_text = _format_usage_counts(usage)
    return ("thread token usage updated (%s)" % usage_text
            if usage_text else "thread token usage updated")


def _m_item_started(payload):
    return _item_lifecycle("started", payload)


def _m_item_completed(payload):
    return _item_lifecycle("completed", payload)


def _m_command_request_approval(payload):
    command = _extract_command(payload)
    if isinstance(command, str):
        return "command approval requested (%s)" % command
    return "command approval requested"


def _m_file_change_request_approval(payload):
    count = _map_path(payload, ["params", "fileChangeCount"]) \
        or _map_path(payload, ["params", "changeCount"])
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return "file change approval requested (%d files)" % count
    return "file change approval requested"


def _m_request_user_input(payload):
    question = _map_path(payload, ["params", "question"]) \
        or _map_path(payload, ["params", "prompt"])
    if isinstance(question, str) and question.strip() != "":
        return "tool requires user input: %s" % _inline_text(question)
    return "tool requires user input"


def _m_account_updated(payload):
    auth_mode = _map_path(payload, ["params", "authMode"]) or "unknown"
    return "account updated (auth %s)" % auth_mode


def _m_rate_limits_updated(payload):
    rate_limits = _map_path(payload, ["params", "rateLimits"])
    return "rate limits updated: %s" % _rate_limits_summary(rate_limits)


def _m_tool_call(payload):
    tool = _dynamic_tool_name(payload)
    if isinstance(tool, str) and tool.strip() != "":
        return "dynamic tool call requested (%s)" % tool.strip()
    return "dynamic tool call requested"


def _streaming(label):
    def handler(payload):
        preview = _delta_preview(payload)
        return "%s: %s" % (label, preview) if preview is not None else label
    return handler


_METHOD_HANDLERS = {
    "thread/started": _m_thread_started,
    "turn/started": _m_turn_started,
    "turn/completed": _m_turn_completed,
    "turn/failed": _m_turn_failed,
    "turn/cancelled": lambda _p: "turn cancelled",
    "turn/diff/updated": _m_turn_diff_updated,
    "turn/plan/updated": _m_turn_plan_updated,
    "thread/tokenUsage/updated": _m_token_usage_updated,
    "item/started": _m_item_started,
    "item/completed": _m_item_completed,
    "item/agentMessage/delta": _streaming("agent message streaming"),
    "item/plan/delta": _streaming("plan streaming"),
    "item/reasoning/summaryTextDelta": _streaming("reasoning summary streaming"),
    "item/reasoning/summaryPartAdded": _streaming("reasoning summary section added"),
    "item/reasoning/textDelta": _streaming("reasoning text streaming"),
    "item/commandExecution/outputDelta": _streaming("command output streaming"),
    "item/fileChange/outputDelta": _streaming("file change output streaming"),
    "item/commandExecution/requestApproval": _m_command_request_approval,
    "item/fileChange/requestApproval": _m_file_change_request_approval,
    "item/tool/requestUserInput": _m_request_user_input,
    "tool/requestUserInput": _m_request_user_input,
    "account/updated": _m_account_updated,
    "account/rateLimits/updated": _m_rate_limits_updated,
    "account/chatgptAuthTokens/refresh":
        lambda _p: "account auth token refresh requested",
    "item/tool/call": _m_tool_call,
}


def _humanize_wrapper_event(event, payload):
    if event == "mcp_startup_update":
        server = _map_path(payload, ["params", "msg", "server"]) or "mcp"
        state = _map_path(payload, ["params", "msg", "status", "state"]) or "updated"
        return "mcp startup: %s %s" % (server, state)
    if event == "mcp_startup_complete":
        return "mcp startup complete"
    if event == "task_started":
        return "task started"
    if event == "user_message":
        return "user message received"
    if event in ("item_started", "item_completed"):
        state = event.split("_")[1]
        item_type = _wrapper_payload_type(payload)
        if item_type == "token_count":
            return _wrapper_token_count(payload)
        if isinstance(item_type, str):
            return "item %s (%s)" % (state, _humanize_item_type(item_type))
        return "item %s" % state
    if event == "agent_message_delta":
        return _streaming("agent message streaming")(payload)
    if event == "agent_message_content_delta":
        return _streaming("agent message content streaming")(payload)
    if event == "agent_reasoning_delta":
        return _streaming("reasoning streaming")(payload)
    if event == "reasoning_content_delta":
        return _streaming("reasoning content streaming")(payload)
    if event == "agent_reasoning_section_break":
        return "reasoning section break"
    if event == "agent_reasoning":
        focus = _reasoning_focus(payload)
        return "reasoning update: %s" % focus if focus else "reasoning update"
    if event == "turn_diff":
        return "turn diff updated"
    if event == "exec_command_begin":
        command = _map_path(payload, ["params", "msg", "command"]) \
            or _map_path(payload, ["params", "msg", "parsed_cmd"])
        command = _normalise_command(command)
        return command if isinstance(command, str) else "command started"
    if event == "exec_command_end":
        exit_code = _map_path(payload, ["params", "msg", "exit_code"]) \
            or _map_path(payload, ["params", "msg", "exitCode"])
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return "command completed (exit %d)" % exit_code
        return "command completed"
    if event == "exec_command_output_delta":
        return "command output streaming"
    if event == "mcp_tool_call_begin":
        return "mcp tool call started"
    if event == "mcp_tool_call_end":
        return "mcp tool call completed"
    if event == "token_count":
        return _wrapper_token_count(payload)
    msg_type = _map_path(payload, ["params", "msg", "type"])
    return "%s (%s)" % (event, msg_type) if isinstance(msg_type, str) else event


def _wrapper_token_count(payload):
    usage = _extract_first_path(payload, _TOKEN_USAGE_PATHS)
    usage_text = _format_usage_counts(usage)
    return ("token count update (%s)" % usage_text
            if usage_text else "token count update")


_TOKEN_USAGE_PATHS = [
    ["params", "msg", "payload", "info", "total_token_usage"],
    ["params", "msg", "info", "total_token_usage"],
    ["params", "tokenUsage", "total"],
]

_DELTA_PATHS = [
    ["params", "delta"],
    ["params", "msg", "delta"],
    ["params", "textDelta"],
    ["params", "msg", "textDelta"],
    ["params", "outputDelta"],
    ["params", "msg", "outputDelta"],
    ["params", "text"],
    ["params", "msg", "text"],
    ["params", "summaryText"],
    ["params", "msg", "summaryText"],
    ["params", "msg", "content"],
    ["params", "msg", "payload", "delta"],
    ["params", "msg", "payload", "textDelta"],
    ["params", "msg", "payload", "outputDelta"],
    ["params", "msg", "payload", "text"],
    ["params", "msg", "payload", "summaryText"],
    ["params", "msg", "payload", "content"],
]

_REASONING_FOCUS_PATHS = [
    ["params", "reason"],
    ["params", "summaryText"],
    ["params", "summary"],
    ["params", "text"],
    ["params", "msg", "reason"],
    ["params", "msg", "summaryText"],
    ["params", "msg", "summary"],
    ["params", "msg", "text"],
    ["params", "msg", "payload", "reason"],
    ["params", "msg", "payload", "summaryText"],
    ["params", "msg", "payload", "summary"],
    ["params", "msg", "payload", "text"],
]


def _delta_preview(payload):
    delta = _extract_first_path(payload, _DELTA_PATHS)
    if isinstance(delta, str):
        trimmed = delta.strip()
        if trimmed != "":
            return _inline_text(trimmed)
    return None


def _reasoning_focus(payload):
    value = _extract_first_path(payload, _REASONING_FOCUS_PATHS)
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed != "":
            return _inline_text(trimmed)
    return None


def _wrapper_payload_type(payload):
    return _map_path(payload, ["params", "msg", "payload", "type"])


def _dynamic_tool_event(base, payload):
    tool = _dynamic_tool_name(payload)
    if isinstance(tool, str) and tool.strip() != "":
        return "%s (%s)" % (base, tool.strip())
    return base


def _dynamic_tool_name(payload):
    return _map_path(payload, ["params", "tool"]) \
        or _map_path(payload, ["params", "name"])


def _item_lifecycle(state, payload):
    item = _map_path(payload, ["params", "item"]) or {}
    item_type = _humanize_item_type(item.get("type")) \
        if isinstance(item, dict) else "item"
    item_status = item.get("status") if isinstance(item, dict) else None
    item_id = item.get("id") if isinstance(item, dict) else None
    details = []
    short = _short_id(item_id)
    if short:
        details.append(short)
    status = _humanize_status(item_status)
    if status:
        details.append(status)
    suffix = " (%s)" % ", ".join(details) if details else ""
    return "item %s: %s%s" % (state, item_type, suffix)


def _humanize_item_type(item_type):
    if item_type is None:
        return "item"
    if not isinstance(item_type, str):
        return str(item_type)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", item_type)
    return text.replace("_", " ").replace("/", " ").lower().strip()


def _humanize_status(status):
    if not isinstance(status, str):
        return None
    return status.replace("_", " ").replace("-", " ").lower().strip()


def _short_id(item_id):
    if not isinstance(item_id, str):
        return None
    return item_id[:12] if len(item_id.encode("utf-8")) > 12 else item_id


def _extract_command(payload):
    command = _map_path(payload, ["params", "parsedCmd"])
    if command is None:
        for key in ("command", "cmd", "argv", "args"):
            command = _map_path(payload, ["params", key])
            if command is not None:
                break
    return _normalise_command(command)


def _normalise_command(command):
    if isinstance(command, dict):
        binary = _map_value(command, ["parsedCmd", "command", "cmd"])
        args = _map_value(command, ["args", "argv"])
        if isinstance(binary, str) and isinstance(args, list):
            return _normalise_command([binary] + args)
        return _normalise_command(binary if binary is not None else args)
    if isinstance(command, str):
        return _inline_text(command)
    if isinstance(command, list):
        if all(isinstance(part, str) for part in command):
            return _inline_text(" ".join(command))
    return None


def _inline_text(text):
    text = re.sub(r"\s+", " ", str(text).replace("\n", " ")).strip()
    return _truncate(text, 80)


def _format_usage_counts(usage):
    if not isinstance(usage, dict):
        return None
    parts = []
    input_tokens = _parse_int(_map_value(
        usage, ["input_tokens", "prompt_tokens", "inputTokens", "promptTokens"]))
    output_tokens = _parse_int(_map_value(
        usage, ["output_tokens", "completion_tokens",
                "outputTokens", "completionTokens"]))
    total_tokens = _parse_int(_map_value(
        usage, ["total_tokens", "total", "totalTokens"]))
    for label, value in (("in", input_tokens), ("out", output_tokens),
                         ("total", total_tokens)):
        if isinstance(value, int):
            parts.append("%s %s" % (label, _format_count(value)))
    return ", ".join(parts) if parts else None


def _parse_int(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _format_count(value):
    if not isinstance(value, int) or isinstance(value, bool):
        return str(value)
    digits = str(abs(value))
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    grouped = ",".join(groups)
    return grouped if value >= 0 else "-" + grouped


def _rate_limits_summary(rate_limits):
    if not isinstance(rate_limits, dict):
        return "n/a"
    primary = _bucket_summary(rate_limits.get("primary"))
    secondary = _bucket_summary(rate_limits.get("secondary"))
    if primary and secondary:
        return "primary %s; secondary %s" % (primary, secondary)
    if primary:
        return "primary %s" % primary
    if secondary:
        return "secondary %s" % secondary
    return "n/a"


def _bucket_summary(bucket):
    if not isinstance(bucket, dict):
        return None
    used = bucket.get("usedPercent")
    window = bucket.get("windowDurationMins")
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        if isinstance(window, int) and not isinstance(window, bool):
            return "%s%% / %dm" % (used, window)
        return "%s%% used" % used
    return None


def _format_error_value(error):
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return _inspect(error)


def _format_reason(message):
    if isinstance(message, dict):
        reason = _map_value(message, ["reason"])
        if reason is None:
            return _inline_text(_inspect(message))
        return _format_error_value(reason)
    return _format_error_value(message)


def _map_value(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _map_path(data, path):
    value = data
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


def _extract_first_path(payload, paths):
    for path in paths:
        value = _map_path(payload, path)
        if value is not None:
            return value
    return None


def state_payload(orchestrator):
    generated_at = _iso(datetime.datetime.now(datetime.timezone.utc))
    snapshot = orchestrator.snapshot(SNAPSHOT_TIMEOUT_S)
    if snapshot == "timeout":
        return {
            "generated_at": generated_at,
            "error": {"code": "snapshot_timeout", "message": "Snapshot timed out"},
        }
    if snapshot in ("unavailable", None):
        return {
            "generated_at": generated_at,
            "error": {"code": "snapshot_unavailable", "message": "Snapshot unavailable"},
        }
    running = [_running_entry_payload(entry) for entry in snapshot["running"]]
    retrying = [_retry_entry_payload(entry) for entry in snapshot["retrying"]]
    blocked = [_blocked_entry_payload(entry) for entry in snapshot["blocked"]]
    return {
        "generated_at": generated_at,
        "counts": {
            "running": len(running),
            "retrying": len(retrying),
            "blocked": len(blocked),
        },
        "running": running,
        "retrying": retrying,
        "blocked": blocked,
        "codex_totals": snapshot["codex_totals"],
        "rate_limits": snapshot["rate_limits"],
    }


def issue_payload(identifier, orchestrator, settings=None, workflow_path=None):
    snapshot = orchestrator.snapshot(SNAPSHOT_TIMEOUT_S)
    if not isinstance(snapshot, dict):
        return None
    running = next(
        (e for e in snapshot["running"] if e.get("identifier") == identifier), None)
    retry = next(
        (e for e in snapshot["retrying"] if e.get("identifier") == identifier), None)
    blocked = next(
        (e for e in snapshot["blocked"] if e.get("identifier") == identifier), None)
    if running is None and retry is None and blocked is None:
        return None
    return {
        "issue_identifier": identifier,
        "issue_id": (running or retry or blocked or {}).get("issue_id"),
        "status": "running" if running else ("retrying" if retry else "blocked"),
        "workspace": {
            "path": _workspace_path(identifier, running, retry, blocked, settings, workflow_path),
            "host": (running or retry or blocked or {}).get("worker_host"),
        },
        "attempts": {
            "restart_count": max((retry or {}).get("attempt", 0) - 1, 0),
            "current_retry_attempt": (retry or {}).get("attempt", 0),
        },
        "running": running and _running_issue_payload(running),
        "retry": retry and {
            "attempt": retry.get("attempt"),
            "due_at": _due_at(retry.get("due_in_ms")),
            "error": retry.get("error"),
            "worker_host": retry.get("worker_host"),
            "workspace_path": retry.get("workspace_path"),
        },
        "blocked": blocked and {
            "worker_host": blocked.get("worker_host"),
            "workspace_path": blocked.get("workspace_path"),
            "session_id": blocked.get("session_id"),
            "state": blocked.get("state"),
            "error": blocked.get("error"),
            "blocked_at": _iso(blocked.get("blocked_at")),
            "last_event": blocked.get("last_codex_event"),
            "last_message": _summarize_message(blocked.get("last_codex_message")),
            "last_event_at": _iso(blocked.get("last_codex_timestamp")),
        },
        "logs": {"codex_session_logs": []},
        "recent_events": _recent_events(running or blocked),
        "last_error": (blocked or {}).get("error") or (retry or {}).get("error"),
        "tracked": {},
    }


def refresh_payload(orchestrator):
    result = orchestrator.request_refresh()
    if result in ("unavailable", "timeout", None):
        return None
    result = dict(result)
    result["requested_at"] = _iso_full(result.get("requested_at"))
    return result


def _workspace_path(identifier, running, retry, blocked, settings, workflow_path):
    for entry in (running, retry, blocked):
        if entry and entry.get("workspace_path"):
            return entry["workspace_path"]
    if settings is not None:
        from .workspace import workspace_key

        root = settings.workspace.root
        if isinstance(root, str) and root != "":
            return "%s/%s" % (root.rstrip("/"), workspace_key(identifier))
        return workspace_key(identifier)
    return None


def _running_entry_payload(entry):
    return {
        "issue_id": entry.get("issue_id"),
        "issue_identifier": entry.get("identifier"),
        "issue_url": entry.get("issue_url"),
        "state": entry.get("state"),
        "worker_host": entry.get("worker_host"),
        "workspace_path": entry.get("workspace_path"),
        "session_id": entry.get("session_id"),
        "turn_count": entry.get("turn_count", 0),
        "last_event": entry.get("last_codex_event"),
        "last_message": _summarize_message(entry.get("last_codex_message")),
        "started_at": _iso(entry.get("started_at")),
        "last_event_at": _iso(entry.get("last_codex_timestamp")),
        "tokens": {
            "input_tokens": entry.get("codex_input_tokens", 0),
            "output_tokens": entry.get("codex_output_tokens", 0),
            "total_tokens": entry.get("codex_total_tokens", 0),
        },
    }


def _retry_entry_payload(entry):
    return {
        "issue_id": entry.get("issue_id"),
        "issue_identifier": entry.get("identifier"),
        "issue_url": entry.get("issue_url"),
        "attempt": entry.get("attempt"),
        "due_at": _due_at(entry.get("due_in_ms")),
        "error": entry.get("error"),
        "worker_host": entry.get("worker_host"),
        "workspace_path": entry.get("workspace_path"),
    }


def _blocked_entry_payload(entry):
    return {
        "issue_id": entry.get("issue_id"),
        "issue_identifier": entry.get("identifier"),
        "issue_url": entry.get("issue_url"),
        "state": entry.get("state"),
        "error": entry.get("error"),
        "worker_host": entry.get("worker_host"),
        "workspace_path": entry.get("workspace_path"),
        "session_id": entry.get("session_id"),
        "blocked_at": _iso(entry.get("blocked_at")),
        "last_event": entry.get("last_codex_event"),
        "last_message": _summarize_message(entry.get("last_codex_message")),
        "last_event_at": _iso(entry.get("last_codex_timestamp")),
    }


def _running_issue_payload(entry):
    return {
        "worker_host": entry.get("worker_host"),
        "workspace_path": entry.get("workspace_path"),
        "session_id": entry.get("session_id"),
        "turn_count": entry.get("turn_count", 0),
        "state": entry.get("state"),
        "started_at": _iso(entry.get("started_at")),
        "last_event": entry.get("last_codex_event"),
        "last_message": _summarize_message(entry.get("last_codex_message")),
        "last_event_at": _iso(entry.get("last_codex_timestamp")),
        "tokens": {
            "input_tokens": entry.get("codex_input_tokens", 0),
            "output_tokens": entry.get("codex_output_tokens", 0),
            "total_tokens": entry.get("codex_total_tokens", 0),
        },
    }


def _recent_events(entry):
    if not entry:
        return []
    at = _iso(entry.get("last_codex_timestamp"))
    if at is None:
        return []
    return [{
        "at": at,
        "event": entry.get("last_codex_event"),
        "message": _summarize_message(entry.get("last_codex_message")),
    }]


def _due_at(due_in_ms):
    if isinstance(due_in_ms, int):
        return _iso(datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=due_in_ms // 1000))
    return None


class _Api:
    def __init__(self, orchestrator, store, workflow_path):
        self.orchestrator = orchestrator
        self.store = store
        self.workflow_path = workflow_path

    def settings(self):
        try:
            return self.store.settings()
        except Exception:  # noqa: BLE001
            return None


class _Handler(BaseHTTPRequestHandler):
    api = None
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log.debug("http %s", format % args)

    def _json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        self._json(status, {"error": {"code": code, "message": message}})

    def _path(self):
        return self.path.split("?", 1)[0]

    @staticmethod
    def _route_exists(path):
        """Paths that match an Elixir route pattern (405 vs 404 on bad verbs)."""
        if path in ("/", "/api/v1/state", "/api/v1/refresh"):
            return True
        if path.startswith("/api/v1/"):
            identifier = path[len("/api/v1/"):]
            return identifier != "" and "/" not in identifier
        return False

    def do_GET(self):
        path = self._path()
        if path == "/api/v1/state":
            self._json(200, state_payload(self.api.orchestrator))
            return
        if path == "/":
            # Phoenix serves the LiveView dashboard here; the Python port ships
            # no dashboard, so a minimal service document is returned instead.
            self._json(200, {"service": "symphony", "status": "ok"})
            return
        if path.startswith("/api/v1/"):
            identifier = path[len("/api/v1/"):]
            if "/" not in identifier and identifier:
                payload = issue_payload(
                    identifier, self.api.orchestrator,
                    self.api.settings(), self.api.workflow_path)
                if payload is None:
                    self._error(404, "issue_not_found", "Issue not found")
                else:
                    self._json(200, payload)
                return
        self._error(404, "not_found", "Route not found")

    def do_POST(self):
        path = self._path()
        if path == "/api/v1/refresh":
            payload = refresh_payload(self.api.orchestrator)
            if payload is None:
                self._error(503, "orchestrator_unavailable", "Orchestrator is unavailable")
            else:
                self._json(202, payload)
            return
        self._method_not_allowed_or_404()

    def _method_not_allowed_or_404(self):
        if self._route_exists(self._path()):
            self._error(405, "method_not_allowed", "Method not allowed")
        else:
            self._error(404, "not_found", "Route not found")

    do_PUT = _method_not_allowed_or_404
    do_DELETE = _method_not_allowed_or_404
    do_PATCH = _method_not_allowed_or_404
    do_HEAD = _method_not_allowed_or_404
    do_OPTIONS = _method_not_allowed_or_404

    def __getattr__(self, name):
        # Unimplemented verbs route through the same 405/404 split instead of
        # BaseHTTPRequestHandler's default 501.
        if name.startswith("do_"):
            return self._method_not_allowed_or_404
        raise AttributeError(name)


def start(orchestrator, store, workflow_path, host, port):
    """Start the observability HTTP server in a daemon thread."""
    api = _Api(orchestrator, store, workflow_path)

    class BoundHandler(_Handler):
        pass

    BoundHandler.api = api
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, name="http-api", daemon=True)
    thread.start()
    log.info("Observability API listening on http://%s:%d", host, port)
    return server
