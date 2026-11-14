"""Pi runtime: drives ``pi --mode rpc`` over strict JSONL on stdin/stdout.

One subprocess per session; a single session serves every turn of one agent
run. Only ``agent_settled`` ends a turn — ``agent_end`` may still be followed
by retries, compaction or queued follow-ups.

Events are normalised into the message shape the runner forwards to the
orchestrator: ``event``, ``timestamp`` and ``payload`` (usage is reported as
``method: "turn/completed"`` with an integer token map).
"""

import base64
import datetime
import json
import logging
import os
import queue
import re
import secrets
import signal
import stat
import subprocess
import threading
import time
import uuid

from .. import util
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


class TerminationError(Exception):
    """The Pi process tree could not be confirmed dead."""

    def __init__(self, reason, session=None):
        self.session = session
        super().__init__(reason)


class PiStartupError(Exception):
    """Pi's process started, but session setup failed after ``Popen``."""

    def __init__(self, reason, detail=None, session=None):
        self.reason = reason
        self.detail = detail
        self.session = session
        suffix = ": %s" % (detail,) if detail is not None else ""
        super().__init__("%s%s" % (reason, suffix))


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
    def __init__(self, process, workspace, config, session_id, live_path,
                 mcp_token=None):
        self.process = process
        self.workspace = workspace
        self.config = config
        self.session_id = session_id
        self.live_path = live_path
        self.mcp_token = mcp_token
        self.records = queue.Queue()
        self.reader = None
        self.lock = threading.Lock()


def start_session(workspace, settings, worker_host=None, run_lease=None,
                  issue=None):
    """Spawn the pi subprocess and prepare the workspace ``.pi`` directory."""
    config = settings.runtime
    if worker_host is not None:
        raise WorkflowError("pi_remote_worker_not_supported")
    if not config.command or not config.command.strip():
        raise WorkflowError("missing_pi_command")

    _sanitise_pi_dir(workspace)
    _exclude_pi_dir(workspace)
    mcp_token = _write_mcp_config(workspace, settings, issue)
    try:
        session_id = _new_session_id()
        if run_lease is not None:
            run_lease.prepare_agent(session_id)
    except Exception:
        _revoke_mcp_token(mcp_token)
        raise
    return _open_session(
        workspace, config, settings, session_id, run_lease,
        mcp_token=mcp_token)


def _open_session(workspace, config, settings, session_id, run_lease=None,
                  mcp_token=None):
    secret_names = list(settings.tracker.secret_environment_names or [])
    git_config = None
    gh_config = None
    if settings.tracker.kind == "linear":
        pi_dir = os.path.join(workspace, ".pi")
        gh_config = os.path.join(pi_dir, "gh")
        git_config = os.path.join(pi_dir, "gitconfig")
        os.makedirs(gh_config, mode=0o700, exist_ok=True)
        with open(git_config, "w", encoding="utf-8") as handle:
            handle.write("")
    script = _build_script(
        config, list(dict.fromkeys(secret_names)), session_id,
        git_config=git_config, gh_config=gh_config)
    log.info(
        "Starting pi runtime workspace=%s session=%s command=%s",
        workspace, session_id, config.command)
    try:
        process = subprocess.Popen(
            [util.posix_shell(), "-lc", script],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            start_new_session=True,
            bufsize=0,
        )
    except Exception as error:  # noqa: BLE001 - never leave a live scope behind
        _revoke_mcp_token(mcp_token)
        raise WorkflowError("pi_start_failed", str(error)) from error

    session = PiSession(
        process, workspace, config, session_id, None, mcp_token=mcp_token)
    try:
        if run_lease is not None:
            run_lease.record_agent(process.pid, session_id)
        session.reader = threading.Thread(
            target=_reader_loop, args=(session,),
            name="pi-reader-%s" % session_id[:8], daemon=True)
        _open_live_log(session, workspace, config)
        session.reader.start()
    except TerminationError:
        raise
    except Exception as error:  # noqa: BLE001 - clean up a spawned process
        confirmed = force_terminate_session(session)
        if not confirmed:
            raise TerminationError(
                "pi process tree is still alive after startup failure",
                session=session)
        raise PiStartupError(
            "pi_started_then_failed", repr(error), session=session) from error
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


_TOOLS_RE = re.compile(r"--tools(?:=|\s+)(\"[^\"\n]*\"|'[^'\n]*'|[^\s]+)")
_MCP_TOOL_PATTERN = "mcp__*"


def _inject_mcp_args(command, config):
    """Keep MCP reachable on pi >= 1.5 when the loopback bridge is enabled.

    pi 1.5 implements MCP as a builtin extension, so ``--no-extensions``
    disables it unless ``-e builtin:mcp`` is passed explicitly; and a plain
    ``--tools`` allowlist drops ``mcp__``-prefixed tool names unless the
    pattern is listed. Injecting here keeps WORKFLOW.md commands identical
    across pi versions instead of carrying version-specific flags in config.
    """
    mcp_cfg = getattr(config, "mcp", None)
    if not isinstance(mcp_cfg, dict) or mcp_cfg.get("enabled") is not True:
        return command
    if "builtin:mcp" not in command:
        command += " -e builtin:mcp"
    match = _TOOLS_RE.search(command)
    if match is not None and _MCP_TOOL_PATTERN not in match.group(1):
        raw = match.group(1)
        value = raw[1:-1] if raw[:1] in ("'", '"') and raw.endswith(raw[0]) else raw
        command = (command[:match.start()]
                   + "--tools '%s,%s'" % (value, _MCP_TOOL_PATTERN)
                   + command[match.end():])
    return command


def _build_script(config, secret_names, session_id, git_config=None,
                  gh_config=None):
    unsets = "".join("unset %s;" % name for name in secret_names or [])
    parts = [_inject_mcp_args(config.command, config)]
    if not re.search(r"--session[\s=]|--session-id[\s=]", config.command or ""):
        parts.append("--session-id %s" % _shell_quote(session_id))
    if config.model:
        parts.append("--model %s" % _shell_quote(config.model))
    if config.thinking:
        parts.append("--thinking %s" % _shell_quote(config.thinking))
    isolation = ""
    if git_config:
        isolation += "export GIT_CONFIG_GLOBAL=%s;" % _shell_quote(git_config)
        isolation += "export GIT_CONFIG_NOSYSTEM=1;"
    if gh_config:
        isolation += "export GH_CONFIG_DIR=%s;" % _shell_quote(gh_config)
    return " ".join(
        part for part in [unsets, isolation, "exec", " ".join(parts)] if part)


def _sanitise_pi_dir(workspace):
    """Remove agent-injected project config so ``--approve`` cannot load it."""
    import shutil

    base = os.path.join(workspace, ".pi")
    _ensure_private_directory(base)
    for entry in _PI_PROJECT_ENTRIES:
        path = os.path.join(base, entry)
        try:
            _remove_path_no_follow(path, shutil.rmtree)
        except FileNotFoundError:
            pass
        except OSError as error:
            log.warning("Failed to sanitise .pi entry path=%s error=%s", path, error)


def _exclude_pi_dir(workspace):
    """Keep the bearer-carrying ``.pi/mcp.json`` out of ``git add -A`` commits."""
    git_dir = os.path.join(workspace, ".git")
    if os.path.lexists(git_dir) and not _is_real_directory(git_dir):
        raise WorkflowError(
            "pi_git_metadata_unsafe",
            "workspace .git is not a real directory")
    exclude = os.path.join(workspace, ".git", "info", "exclude")
    entry = ".pi/"
    if not os.path.isdir(os.path.dirname(exclude)):
        return
    _ensure_private_directory(os.path.dirname(exclude), mode=0o755)
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


def _write_mcp_config(workspace, settings, issue):
    from .. import mcp_bridge

    directory = os.path.join(workspace, ".pi")
    path = os.path.join(directory, "mcp.json")
    registered = mcp_bridge.client_config(workspace, settings, issue)
    if registered is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise WorkflowError("pi_mcp_config_failed", str(error))
        return None
    mcp_config, token = registered
    temporary = os.path.join(
        directory, ".mcp-%s.tmp" % uuid.uuid4().hex)
    try:
        _ensure_private_directory(directory)
        with open(temporary, "x", encoding="utf-8") as handle:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            json.dump(mcp_config, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as error:  # noqa: BLE001 - revoke any partially written scope
        _revoke_mcp_token(token)
        raise WorkflowError("pi_mcp_config_failed", str(error)) from error
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return token


def _is_link_or_reparse(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & reparse_flag)


def _is_real_directory(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(path)


def _remove_path_no_follow(path, remove_tree=None):
    """Remove a path without following a final symlink or Windows reparse point."""
    import shutil

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not _is_link_or_reparse(path):
        (remove_tree or shutil.rmtree)(path)
        return
    try:
        os.remove(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)


def _ensure_private_directory(path, mode=0o700):
    if os.path.lexists(path) and not _is_real_directory(path):
        _remove_path_no_follow(path)
    os.makedirs(path, mode=mode, exist_ok=True)
    if not _is_real_directory(path):
        raise WorkflowError(
            "unsafe_private_directory",
            "path is not a real directory: %s" % path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


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
    """Terminate Pi and return only after its process tree is gone."""
    process = session.process
    if process is None:
        return True
    from ..util import signal_process_tree

    known_tree = set()
    force_confirmed = True
    if os.name == "nt":
        # The POSIX shell wrapper may terminate before its Pi child on Windows.
        # Use taskkill /T directly, before the wrapper can exit and orphan Pi.
        force_confirmed = _force_kill_process_tree(process)
    else:
        signal_process_tree(process, signal.SIGTERM)
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        pass
    if _process_tree_alive(process):
        _force_kill_process_tree(process)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    if os.name == "nt" and not force_confirmed:
        known_tree.update(_process_tree_snapshot(process.pid))
    confirmed = not _process_tree_alive(process) and not any(
        _pid_is_alive(pid) for pid in known_tree)
    if confirmed:
        confirmed = _session_scan_confirmed_gone(session)
    if confirmed:
        _close_session_streams(session)
    return confirmed


def force_terminate_session(session):
    """Force-kill a Pi process tree and return whether it is confirmed dead."""
    if session is None or session.process is None:
        return True
    process = session.process
    known_tree = set()
    force_confirmed = _force_kill_process_tree(process)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt" and not force_confirmed:
        known_tree.update(_process_tree_snapshot(process.pid))
    confirmed = not _process_tree_alive(process) and not any(
        _pid_is_alive(pid) for pid in known_tree)
    if confirmed:
        confirmed = _session_scan_confirmed_gone(session)
    if confirmed:
        _close_session_streams(session)
    return confirmed


def _force_kill_process_tree(process):
    if os.name == "nt":
        succeeded = False
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, check=False)
            succeeded = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return succeeded
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except OSError:
        try:
            process.kill()
            return True
        except OSError:
            return False


def _process_tree_snapshot(root_pid):
    """Return known Windows process-tree PIDs, including a dead root."""
    if os.name != "nt":
        return {root_pid}
    script = (
        "Get-CimInstance Win32_Process | ForEach-Object "
        "{ \"$($_.ProcessId),$($_.ParentProcessId)\" }")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {root_pid}
    children = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(",")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        pid, parent = (int(part) for part in parts)
        children.setdefault(parent, []).append(pid)
    pids = {root_pid}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in pids:
                pids.add(child)
                pending.append(child)
    return pids


def _pid_is_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        from .. import workspace

        return workspace._pid_alive(pid)
    except Exception:  # noqa: BLE001 - verification fails closed
        return True


def _session_scan_confirmed_gone(session):
    """Return true only when a session-token scan has no residual process."""
    session_id = getattr(session, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return False
    try:
        from .. import workspace

        remaining = workspace._find_processes_for_session(session_id)
    except Exception:  # noqa: BLE001 - verification fails closed
        return False
    return remaining == []


def _process_tree_alive(process):
    if process.poll() is None:
        return True
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(process.pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False
    return False


def _close_session_streams(session):
    process = session.process
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # noqa: BLE001 - cleanup must reach token revocation
            pass
    _revoke_mcp_token(getattr(session, "mcp_token", None))
    session.mcp_token = None


def _revoke_mcp_token(token):
    if not token:
        return
    try:
        from .. import mcp_bridge

        mcp_bridge.revoke_scope(token)
    except Exception:  # noqa: BLE001 - cleanup must not mask process outcome
        log.exception("Failed to revoke MCP worker scope")


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
