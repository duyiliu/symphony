"""Pi runtime: drives ``pi --mode rpc`` over strict JSONL on stdin/stdout.

One subprocess per session; a single session serves every turn of one agent
run. Only ``agent_settled`` ends a turn — ``agent_end`` may still be followed
by retries, compaction or queued follow-ups.

Events are normalised into the message shape the runner forwards to the
orchestrator: ``event``, ``timestamp`` and ``payload`` (usage is reported as
``method: "turn/completed"`` with an integer token map).
"""

import datetime
import json
import logging
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
import time

from ..workflow import WorkflowError

log = logging.getLogger("symphony.pi")

ABORT_GRACE_S = 5.0
_PI_PROJECT_ENTRIES = (
    "extensions", "skills", "prompts", "themes",
    "SYSTEM.md", "APPEND_SYSTEM.md", "settings.json",
)

_EOF = object()


class TurnError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("pi turn failed: %r" % (reason,))


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _new_session_id():
    raw = secrets.token_bytes(16)
    a = raw[0:4].hex()
    b = raw[4:6].hex()
    c = format(0x4000 | (int.from_bytes(raw[6:8], "big") & 0x0FFF), "04x")
    d = format(0x8000 | (int.from_bytes(raw[8:10], "big") & 0x3FFF), "04x")
    e = raw[10:16].hex()
    return "-".join([a, b, c, d, e])


def _shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def _shell_safe(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


class PiSession:
    def __init__(self, process, workspace, config, session_id, live_path):
        self.process = process
        self.workspace = workspace
        self.config = config
        self.session_id = session_id
        self.live_path = live_path
        self.records = queue.Queue()
        self.reader = None
        self.lock = threading.Lock()


def start_session(workspace, settings, worker_host=None):
    """Spawn the pi subprocess and prepare the workspace ``.pi`` directory."""
    config = settings.runtime
    if worker_host is not None:
        raise WorkflowError("pi_remote_worker_not_supported")
    if not config.command or not config.command.strip():
        raise WorkflowError("missing_pi_command")

    _sanitise_pi_dir(workspace)
    _write_mcp_config(workspace)
    _exclude_pi_dir(workspace)
    return _open_session(workspace, config, settings)


def _open_session(workspace, config, settings):
    session_id = _new_session_id()
    script = _build_script(
        config, settings.tracker.secret_environment_names, session_id)
    log.info(
        "Starting pi runtime workspace=%s session=%s command=%s",
        workspace, session_id, config.command)
    try:
        process = subprocess.Popen(
            ["bash", "-lc", script],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as error:
        raise WorkflowError("pi_start_failed", str(error))

    session = PiSession(process, workspace, config, session_id, None)
    session.reader = threading.Thread(
        target=_reader_loop, args=(session,), name="pi-reader-%s" % session_id[:8],
        daemon=True)
    _open_live_log(session, workspace, config)
    session.reader.start()
    return session


def _reader_loop(session):
    buffer = b""
    stream = session.process.stdout
    try:
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            _mirror_line(session, chunk)
            session.records.put(chunk)
    except (OSError, ValueError):
        pass
    finally:
        session.records.put(_EOF)


def _mirror_line(session, chunk):
    path = session.live_path
    if not path:
        return
    try:
        with open(path, "ab") as handle:
            handle.write(chunk)
    except OSError:
        pass


def _build_script(config, secret_names, session_id):
    unsets = "".join("unset %s;" % name for name in secret_names or [])
    parts = [config.command]
    if not re.search(r"--session[\s=]|--session-id[\s=]", config.command or ""):
        parts.append("--session-id %s" % _shell_quote(session_id))
    if config.model:
        parts.append("--model %s" % _shell_quote(config.model))
    if config.thinking:
        parts.append("--thinking %s" % _shell_quote(config.thinking))
    return " ".join(part for part in [unsets, "exec", " ".join(parts)] if part)


def _sanitise_pi_dir(workspace):
    """Remove agent-injected project config so ``--approve`` cannot load it."""
    import shutil

    base = os.path.join(workspace, ".pi")
    for entry in _PI_PROJECT_ENTRIES:
        path = os.path.join(base, entry)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            log.warning("Failed to sanitise .pi entry path=%s error=%s", path, error)


def _exclude_pi_dir(workspace):
    """Keep the bearer-carrying ``.pi/mcp.json`` out of ``git add -A`` commits."""
    exclude = os.path.join(workspace, ".git", "info", "exclude")
    entry = ".pi/"
    if not os.path.isdir(os.path.dirname(exclude)):
        return
    try:
        with open(exclude, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return
    if entry in content:
        return
    try:
        with open(exclude, "a", encoding="utf-8") as handle:
            handle.write(("\n" if content and not content.endswith("\n") else "")
                         + entry + "\n")
    except OSError:
        pass


def _write_mcp_config(workspace):
    from .. import mcp_bridge

    mcp_config = mcp_bridge.client_config()
    if mcp_config is None:
        return
    directory = os.path.join(workspace, ".pi")
    path = os.path.join(directory, "mcp.json")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(mcp_config, handle, ensure_ascii=False, indent=2)
    except OSError as error:
        raise WorkflowError("pi_mcp_config_failed", str(error))


def _open_live_log(session, workspace, config):
    directory = config.live_log_dir
    if not directory:
        return
    timestamp = _now().strftime("%Y-%m-%dT%H-%M-%S.%f")[:23] + "Z"
    path = os.path.join(
        directory, "%s_%s.live.jsonl" % (timestamp, _shell_safe(session.session_id)))
    header = json.dumps({
        "type": "session",
        "version": 3,
        "id": session.session_id,
        "timestamp": _now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z",
        "cwd": workspace,
    }) + "\n"
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(header)
        log.info("Mirroring pi rpc stream workspace=%s live_log=%s", workspace, path)
        session.live_path = path
    except OSError as error:
        log.warning(
            "Live session mirror disabled workspace=%s reason=%s", workspace, error)


def _send(session, record):
    payload = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        session.process.stdin.write(payload)
        session.process.stdin.flush()
    except (OSError, ValueError) as error:
        raise TurnError(("pi_write_failed", str(error)))


def _next_line(session, timeout_s):
    try:
        return session.records.get(timeout=timeout_s)
    except queue.Empty:
        return None


def _parse(line_bytes):
    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def run_turn(session, prompt, on_message, cancel_event=None):
    """Send one prompt and wait for ``agent_settled``.

    ``on_message`` receives ``{"event", "timestamp", ...}`` dicts.
    Returns ``{"session_id", "turn_id", "usage"}`` on success.
    Raises :class:`TurnError` on failure; ``reason == "turn_aborted"`` means
    the turn was aborted by pi; ``"cancelled"`` means the host interrupted it.
    """
    request_id = "symphony-%s" % secrets.token_hex(8)
    session_id = session.session_id

    _emit(on_message, "session_started", {
        "session_id": session_id,
        "payload": {"method": "session/started", "session_id": session_id},
    })

    _send(session, {"id": request_id, "type": "prompt", "message": prompt})

    usage = {"input": 0, "output": 0}
    deadline = time.monotonic() + max(session.config.turn_timeout_ms, 1) / 1000.0

    while True:
        if cancel_event is not None and cancel_event.is_set():
            _abort_turn(session, request_id)
            _emit(on_message, "turn_cancelled", {
                "session_id": session_id,
                "payload": {"method": "turn/cancelled"},
            })
            raise TurnError("cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _abort_turn(session, request_id)
            _emit_failed(on_message, session_id, "turn_timeout")
            raise TurnError("turn_timeout")
        item = _next_line(session, min(remaining, 0.5))
        if item is None:
            continue
        if item is _EOF:
            code = session.process.poll()
            _emit_failed(on_message, session_id, ("pi_exited", code))
            raise TurnError(("pi_exited", code if code is not None else "eof"))
        record = _parse(item)
        if record is None:
            continue
        outcome, usage = _handle_record(
            record, request_id, session_id, usage, on_message)
        if outcome == "continue":
            continue
        if outcome in ("settled", "handled"):
            final_usage = _finalise_usage(usage)
            if outcome == "settled":
                _emit(on_message, "turn_completed", {
                    "session_id": session_id,
                    "payload": {"method": "turn/completed", "usage": final_usage},
                    "usage": final_usage,
                })
            return {"session_id": session_id, "turn_id": request_id,
                    "usage": final_usage}
        if outcome == "aborted":
            _emit(on_message, "turn_cancelled", {
                "session_id": session_id,
                "payload": {"method": "turn/cancelled"},
            })
            raise TurnError("turn_aborted")
        _emit_failed(on_message, session_id, outcome)
        raise TurnError(outcome)


def _handle_record(record, request_id, session_id, usage, on_message):
    """Returns (outcome, usage). outcome ∈ continue|settled|handled|aborted|reason."""
    rtype = record.get("type")

    if rtype == "response" and record.get("id") == request_id:
        if record.get("success") is False:
            return ("pi_command_failed", record.get("error", "unknown")), usage
        data = record.get("data") or {}
        if data.get("disposition") == "handled":
            return "handled", usage
        return "continue", usage

    if rtype == "agent_settled":
        return ("aborted" if record.get("aborted") is True else "settled"), usage

    if rtype == "message_end" and isinstance(record.get("message"), dict) \
            and record["message"].get("role") == "assistant":
        usage = _accumulate_usage(usage, record["message"].get("usage"))
        # Usage is emitted per assistant message so totals survive a host-side
        # stop that kills pi before agent_settled arrives.
        _emit(on_message, "notification", {
            "session_id": session_id,
            "payload": {"method": "turn/completed", "usage": _finalise_usage(usage)},
            "usage": _finalise_usage(usage),
        })
        return "continue", usage

    if rtype == "tool_execution_end":
        is_error = record.get("isError") is True
        _emit(on_message, "tool_call_failed" if is_error else "tool_call_completed", {
            "session_id": session_id,
            "details": {
                "tool": record.get("toolName"),
                "duration_ms": record.get("durationMs"),
                "error": is_error,
            },
            "payload": {
                "method": "item/tool/call",
                "params": {"tool": record.get("toolName"), "isError": is_error},
            },
        })
        return "continue", usage

    if rtype == "error":
        return ("pi_error", record.get("message", "unknown")), usage

    return "continue", usage


def _abort_turn(session, request_id):
    try:
        _send(session, {"id": "%s-abort" % request_id, "type": "abort"})
    except TurnError:
        return
    deadline = time.monotonic() + ABORT_GRACE_S
    while time.monotonic() < deadline:
        item = _next_line(session, deadline - time.monotonic())
        if item is None:
            break
        if item is _EOF:
            break
        if b'"agent_settled"' in item:
            break


def stop_session(session):
    """Terminate the subprocess (SIGTERM the process group, then SIGKILL)."""
    process = session.process
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _emit(on_message, event, fields):
    if on_message is None:
        return
    message = {"event": event, "timestamp": _now()}
    message.update(fields)
    try:
        on_message(message)
    except Exception:  # noqa: BLE001 - observers must not break the turn
        log.exception("pi on_message callback failed")


def _emit_failed(on_message, session_id, reason):
    _emit(on_message, "turn_failed", {
        "session_id": session_id,
        "payload": {"method": "turn/failed", "error": repr(reason)},
        "reason": repr(reason),
    })


def _accumulate_usage(usage, message_usage):
    if not isinstance(message_usage, dict):
        return usage
    input_tokens = message_usage.get("input")
    output_tokens = message_usage.get("output")
    return {
        "input": max(usage["input"],
                     input_tokens if isinstance(input_tokens, int)
                     and not isinstance(input_tokens, bool)
                     and input_tokens >= 0 else 0),
        "output": usage["output"] + (
            output_tokens if isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0 else 0),
    }


def _finalise_usage(usage):
    return {
        "input_tokens": usage["input"],
        "output_tokens": usage["output"],
        "total_tokens": usage["input"] + usage["output"],
    }
