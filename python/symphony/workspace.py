"""Per-issue workspace creation, hook execution and safe removal.

Safety invariant: every path that hooks or the agent can reach must resolve
inside the configured workspace root. Remote workers (``worker.ssh_hosts``)
are not supported by this implementation; ``pi`` runs locally only.
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

from . import util
from .paths import PathSafetyError, canonicalize, expand_workspace_root, safe_join
from .workflow import WorkflowError

log = logging.getLogger("symphony.workspace")

IDENTIFIER_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")
RUN_LOCK_DIRECTORY = ".symphony-run-locks"
RUN_LOCK_TIMEOUT_S = 3.0
# CIM/WMI process queries can exceed the short lease confirmation window when
# Windows is busy. Keep the fail-closed decision, but allow the observation
# itself enough time to return a usable result.
PROCESS_SCAN_TIMEOUT_S = 10.0

_local_run_leases = {}
_local_run_leases_lock = threading.Lock()


def workspace_key(identifier):
    """Collision-safe directory name for an issue identifier.

    Safe identifiers are used as-is; otherwise the sanitised name gets a
    ``--<sha256[0:16]>`` suffix derived from the original so different
    identifiers can never collide on one directory. Mirrors the Elixir
    behaviour: non-string identifiers become ``"issue"`` while an empty
    string stays empty (rejected later by the equals-root check).
    """
    if hasattr(identifier, "identifier"):
        identifier = identifier.identifier
    if not isinstance(identifier, str):
        return "issue"
    safe = IDENTIFIER_SAFE_RE.sub("_", identifier)
    if safe == identifier:
        return safe
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
    return "%s--%s" % (safe, digest)


class HookError(Exception):
    """Workspace hook failure; ``reason`` mirrors the Elixir error tuple."""

    def __init__(self, hook, status=None, output=None, timed_out=None):
        self.hook = hook
        if timed_out is not None:
            self.reason = ("workspace_hook_timeout", hook, timed_out)
        else:
            self.reason = ("workspace_hook_failed", hook, status, output)
        tag, _hook, *rest = self.reason
        detail = ", ".join(
            '"%s"' % item if isinstance(item, str) else str(item)
            for item in rest)
        super().__init__("{:%s, \"%s\", %s}" % (tag, hook, detail))


class RunLease:
    """Exclusive cross-process ownership of one issue workspace/run."""

    def __init__(self, path, owner_path, handle, owner):
        self.path = path
        self.owner_path = owner_path
        self.handle = handle
        self.owner = owner
        self.token = owner["lease_token"]
        self._released = False
        self._write_lock = threading.Lock()

    def prepare_agent(self, session_id):
        """Persist the Pi session identity before spawning its process."""
        with self._write_lock:
            if self._released:
                raise WorkflowError("run_lease_released")
            self.owner["agent_session_id"] = session_id
            self.owner["agent_pid"] = None
            self.owner["agent_process_identity"] = None
            _write_run_owner(self, self.owner)

    def record_agent(self, pid, session_id):
        """Record the live Pi process after ``Popen`` succeeds."""
        with self._write_lock:
            if self._released:
                raise WorkflowError("run_lease_released")
            self.owner["agent_pid"] = int(pid)
            self.owner["agent_session_id"] = session_id
            self.owner["agent_process_identity"] = _process_identity(int(pid))
            _write_run_owner(self, self.owner)

    def release(self):
        with self._write_lock:
            if self._released:
                return
            self._released = True
            try:
                try:
                    _clear_run_owner(self)
                except OSError as error:
                    log.warning(
                        "Unable to clear released run owner path=%s error=%s",
                        self.owner_path, error)
            finally:
                with _local_run_leases_lock:
                    if _local_run_leases.get(self.path) is self:
                        _local_run_leases.pop(self.path, None)
                _unlock_run_file(self.handle)
                try:
                    self.handle.close()
                except (OSError, ValueError):
                    pass


def acquire_run_lease(settings, issue, workflow_path=None, worker_host=None):
    """Acquire the issue's cross-process run lease, or return ``None``.

    The lock lives outside the removable issue workspace.  A released lock
    with a recorded live Pi is treated as crash debris only when its command
    line still contains the unique Pi session ID; unverifiable ownership is
    kept busy so another host can never execute concurrently.
    """
    if worker_host is not None:
        raise WorkflowError("remote_workspace_not_supported")
    path = _resolve_workspace_path(settings, issue, workflow_path)
    return acquire_path_run_lease(
        settings, path, issue_id=getattr(issue, "id", None),
        identifier=getattr(issue, "identifier", None),
        workflow_path=workflow_path)


def acquire_path_run_lease(settings, workspace, issue_id=None, identifier=None,
                           workflow_path=None):
    """Acquire a run lease for an already resolved workspace path."""
    canonical_workspace = canonicalize(workspace)
    lock_dir = os.path.join(
        expand_workspace_root(settings.workspace.root, workflow_path),
        RUN_LOCK_DIRECTORY)
    os.makedirs(lock_dir, exist_ok=True)
    digest = hashlib.sha256(
        os.path.normcase(canonical_workspace).encode("utf-8")).hexdigest()[:32]
    path = os.path.join(lock_dir, digest + ".lock")
    owner_path = path + ".owner.json"

    with _local_run_leases_lock:
        if path in _local_run_leases:
            log.info("Workspace run lease already held path=%s", path)
            return None
        handle = open(path, "a+b")
        if not _lock_run_file(handle):
            handle.close()
            log.info("Workspace run lease held by another process path=%s", path)
            return None

        _remove_stale_owner_temps(owner_path)
        owner = _read_run_owner(owner_path)
        legacy_owner = None
        if owner is None:
            quarantined = _quarantine_run_owner(owner_path)
            log.warning(
                "Discarding unreadable run owner sidecar after acquiring its "
                "lock path=%s quarantined=%s", owner_path, quarantined)
            owner = {}
        if not owner:
            legacy_owner = _read_run_owner_handle(handle)
            if isinstance(legacy_owner, dict) and legacy_owner:
                owner = legacy_owner
        if owner and not _recover_stale_run_owner(owner):
            _unlock_run_file(handle)
            handle.close()
            log.error(
                "Workspace run lease has a live, unverifiable owner; refusing "
                "execution path=%s owner=%r", path, owner)
            return None

        owner = {
            "version": 2,
            "lease_token": uuid.uuid4().hex,
            "host_pid": os.getpid(),
            "host_process_identity": _process_identity(os.getpid()),
            "workspace": canonical_workspace,
            "issue_id": issue_id,
            "identifier": identifier,
            "agent_pid": None,
            "agent_session_id": None,
            "agent_process_identity": None,
            "created_at": time.time(),
        }
        lease = RunLease(path, owner_path, handle, owner)
        _write_run_owner(lease, owner)
        _reset_lock_file_metadata(handle)
        _local_run_leases[path] = lease
        return lease


def _lock_run_file(handle):
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, IOError):
        return False


def _unlock_run_file(handle):
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError, ValueError):
        pass


def _read_run_owner(owner_path):
    try:
        with open(owner_path, "rb") as handle:
            raw = handle.read()
        if not raw or not raw.strip(b"\0\r\n\t "):
            return {}
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_run_owner_handle(handle):
    try:
        handle.seek(0)
        raw = handle.read()
        if not raw or not raw.strip(b"\0\r\n\t "):
            return {}
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _write_run_owner(lease, owner):
    payload = json.dumps(
        owner, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    temporary = "%s.tmp.%d.%s" % (
        lease.owner_path, os.getpid(), uuid.uuid4().hex)
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, lease.owner_path)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
        except OSError as error:
            log.warning(
                "Unable to remove temporary run owner path=%s error=%s",
                temporary, error)


def _clear_run_owner(lease):
    # Record an explicit release marker before removing the sidecar. If
    # removal fails, a later acquirer can still distinguish an orderly release
    # from a crashed live owner.
    marker_error = None
    try:
        _write_run_owner(lease, {"version": 2, "released": True})
    except OSError as error:
        # Removing the sidecar is sufficient for an orderly release. Keep
        # trying even when the marker write fails, otherwise a same-process
        # reacquire sees this still-live host PID and blocks itself.
        marker_error = error
    try:
        os.remove(lease.owner_path)
    except FileNotFoundError:
        pass
    except OSError:
        if marker_error is None:
            raise
        raise OSError(
            "run owner marker write and sidecar removal both failed: %s"
            % marker_error) from marker_error


def _quarantine_run_owner(owner_path):
    if not os.path.exists(owner_path):
        return None
    target = "%s.corrupt.%d.%s" % (
        owner_path, int(time.time()), uuid.uuid4().hex)
    try:
        os.replace(owner_path, target)
        return target
    except OSError as error:
        log.warning(
            "Unable to quarantine unreadable run owner path=%s error=%s",
            owner_path, error)
        return None


def _remove_stale_owner_temps(owner_path):
    """Remove abandoned atomic-write temps while holding the run lock."""
    prefix = os.path.basename(owner_path) + ".tmp."
    directory = os.path.dirname(owner_path)
    try:
        names = os.listdir(directory)
    except OSError as error:
        log.warning(
            "Unable to inspect run owner directory=%s error=%s",
            directory, error)
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        try:
            os.remove(os.path.join(directory, name))
        except FileNotFoundError:
            pass
        except OSError as error:
            log.warning(
                "Unable to remove stale run owner temp path=%s error=%s",
                os.path.join(directory, name), error)


def _reset_lock_file_metadata(handle):
    """Keep the lock file as a stable one-byte OS lock sentinel."""
    try:
        handle.seek(0)
        handle.write(b"\0")
        handle.truncate(1)
        handle.flush()
    except OSError as error:
        log.warning(
            "Unable to normalize run lock file; sidecar remains authoritative "
            "error=%s", error)


def _recover_stale_run_owner(owner):
    """Return true once an unlocked owner has no matching live Pi."""
    if not isinstance(owner, dict) or not owner:
        return True
    if owner.get("released") is True:
        return True
    host_pid = _positive_pid(owner.get("host_pid"))
    if host_pid is not None and _pid_alive(host_pid):
        recorded_identity = owner.get("host_process_identity")
        current_identity = _process_identity(host_pid)
        # A missing or unreadable identity is ambiguous, so retain the safer
        # busy state. A mismatch proves that Windows/POSIX reused this PID.
        if not isinstance(recorded_identity, str) or not recorded_identity \
                or current_identity is None \
                or current_identity == recorded_identity:
            return False

    pids = []
    agent_pid = _positive_pid(owner.get("agent_pid"))
    session_id = owner.get("agent_session_id")
    if agent_pid is not None and _pid_alive(agent_pid):
        recorded_identity = owner.get("agent_process_identity")
        current_identity = _process_identity(agent_pid) \
            if isinstance(recorded_identity, str) and recorded_identity else None
        if isinstance(recorded_identity, str) and recorded_identity \
                and current_identity != recorded_identity:
            # The recorded PID now belongs to another process. Search for a
            # reparented orphan by the unique session token, but never kill the
            # unrelated process now occupying the old PID.
            matching = _find_processes_for_session(session_id)
            if matching is None:
                return False
            pids.extend(matching)
        else:
            if not _process_matches_session(agent_pid, session_id):
                return False
            pids.append(agent_pid)
    elif isinstance(session_id, str) and session_id:
        # A crash can land between Popen and recording its pid.  The session ID
        # is written first, so scan for an orphan carrying that unique token.
        matching = _find_processes_for_session(session_id)
        if matching is None:
            return False
        pids.extend(matching)

    if not pids:
        return True
    log.warning(
        "Terminating stale Pi process tree pids=%s workspace=%s session_id=%s",
        pids, owner.get("workspace"), session_id)
    for pid in pids:
        _kill_verified_process_tree(pid)
    deadline = time.monotonic() + RUN_LOCK_TIMEOUT_S
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            if isinstance(session_id, str) and session_id:
                remaining = _find_processes_for_session(session_id)
                return remaining == []
            return True
        time.sleep(0.05)
    if any(_pid_alive(pid) for pid in pids):
        return False
    if isinstance(session_id, str) and session_id:
        return _find_processes_for_session(session_id) == []
    return True


def _positive_pid(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            code = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                    process, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    proc_stat = os.path.join("/proc", str(pid), "stat")
    try:
        with open(proc_stat, "r", encoding="utf-8") as handle:
            value = handle.read()
        fields = value[value.rfind(")") + 1:].split()
        return bool(fields) and fields[0] != "Z"
    except OSError:
        return True


def _process_identity(pid):
    """Stable process identity that changes when the OS reuses a PID."""
    pid = _positive_pid(pid)
    if pid is None:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", wintypes.DWORD),
                ("high", wintypes.DWORD),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            created = FileTime()
            exited = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                    process, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return None
            ticks = (int(created.high) << 32) | int(created.low)
            return "windows-start-ticks:%d" % ticks
        finally:
            kernel32.CloseHandle(process)

    proc_stat = os.path.join("/proc", str(pid), "stat")
    try:
        with open(proc_stat, "r", encoding="utf-8") as handle:
            value = handle.read()
        # Fields after the parenthesised command start at process field 3;
        # field 22 (starttime) is therefore index 19 in this suffix.
        fields = value[value.rfind(")") + 1:].split()
        if len(fields) <= 19 or not fields[19].isdigit():
            return None
        return "proc-start-ticks:%s" % fields[19]
    except OSError:
        return None


def _process_command_line(pid):
    if os.name == "nt":
        script = (
            "(Get-CimInstance Win32_Process -Filter \"ProcessId = %d\")"
            ".CommandLine" % pid)
        command = _powershell_command(script)
    else:
        proc_cmdline = os.path.join("/proc", str(pid), "cmdline")
        try:
            with open(proc_cmdline, "rb") as handle:
                return handle.read().replace(b"\0", b" ").decode(
                    "utf-8", "replace")
        except OSError:
            command = ["ps", "-p", str(pid), "-o", "args="]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PROCESS_SCAN_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def _process_matches_session(pid, session_id):
    if not isinstance(session_id, str) or not session_id:
        return False
    command_line = _process_command_line(pid)
    return command_line is not None and session_id in command_line


def _find_processes_for_session(session_id):
    if not isinstance(session_id, str) or not session_id:
        return []
    if os.name == "nt":
        script = (
            "Get-CimInstance Win32_Process | Where-Object "
            "{ $_.CommandLine -like '*%s*' } | "
            "Select-Object -ExpandProperty ProcessId" % session_id)
        command = _powershell_command(script)
    else:
        command = ["ps", "-eo", "pid=,args="]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PROCESS_SCAN_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    pids = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        if session_id not in line:
            continue
        first = line.strip().split(None, 1)[0] if line.strip() else ""
        pid = _positive_pid(first)
        if pid is not None and pid != os.getpid():
            pids.append(pid)
    return pids


def _powershell_command(script):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded,
    ]


def _kill_verified_process_tree(pid):
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=RUN_LOCK_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def workspace_path(settings, issue, workflow_path=None):
    """Deterministic ``<root>/<identifier>`` workspace for an issue."""
    root = expand_workspace_root(settings.workspace.root, workflow_path)
    base = workspace_key(issue.identifier)
    try:
        path = safe_join(root, base)
    except PathSafetyError as error:
        expanded = os.path.abspath(
            os.path.join(os.path.abspath(root), base))
        reason = ("workspace_symlink_escape"
                  if expanded.startswith(root.rstrip(os.sep) + os.sep)
                  else "workspace_outside_root")
        raise WorkflowError(reason, str(error))
    if path == canonicalize(root):
        raise WorkflowError("workspace_equals_root")
    return path


def _resolve_workspace_path(settings, issue, workflow_path):
    return canonicalize(
        workspace_path(settings, issue, workflow_path))


def create_for_issue(settings, issue, workflow_path=None, worker_host=None):
    """Create the workspace and run ``after_create`` hooks (first creation only)."""
    if worker_host is not None:
        raise WorkflowError("remote_workspace_not_supported")
    path = _resolve_workspace_path(settings, issue, workflow_path)
    if os.path.lexists(path) and not os.path.isdir(path):
        os.remove(path)
    created = not os.path.isdir(path)
    if created:
        os.makedirs(path)
        try:
            _run_hook(settings.hooks.after_create, "after_create", path,
                      settings.hooks.timeout_ms)
        except HookError:
            _cleanup_failed_new_workspace(path)
            raise
    return path


def _rmtree_compat(path):
    """``shutil.rmtree`` that survives read-only files.

    Git object files are read-only; on Windows deleting them fails with
    WinError 5. Chmod-and-retry handles it (a no-op on already-writable
    trees). ``onexc`` replaced ``onerror`` in Python 3.12.
    """
    import stat
    import sys

    def _force_writeable(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        func(target)

    kwargs = ({"onexc": _force_writeable}
              if sys.version_info >= (3, 12)
              else {"onerror": _force_writeable})
    shutil.rmtree(path, **kwargs)


def _cleanup_failed_new_workspace(path):
    try:
        _rmtree_compat(path)
    except OSError as error:
        log.warning(
            "workspace cleanup after failed create failed path=%s error=%s",
            path, error)


def run_before_run(settings, issue, workflow_path=None, worker_host=None):
    if worker_host is not None:
        raise WorkflowError("remote_workspace_not_supported")
    path = _resolve_workspace_path(settings, issue, workflow_path)
    _run_hook(settings.hooks.before_run, "before_run", path, settings.hooks.timeout_ms)
    return path


def run_after_run(settings, issue, workflow_path=None, worker_host=None):
    if worker_host is not None:
        raise WorkflowError("remote_workspace_not_supported")
    try:
        path = _resolve_workspace_path(settings, issue, workflow_path)
    except (PathSafetyError, WorkflowError):
        return
    try:
        _run_hook(settings.hooks.after_run, "after_run", path,
                  settings.hooks.timeout_ms)
    except HookError as error:
        log.warning("after_run hook failed workspace=%s error=%s", path, error)


def remove(settings, issue_or_path, workflow_path=None, worker_host=None):
    """Delete an issue workspace, honouring ``before_remove`` hooks.

    Mirrors ``Workspace.remove(workspace, nil)``: ``File.exists?`` is checked
    first — a path that exists (file, dir or live symlink) must canonicalize
    strictly inside the workspace root; a missing or dangling path goes
    straight to ``rm -rf`` (deleting the link itself). ``before_remove``
    failures are logged but never block deletion.
    """
    if worker_host is not None:
        raise WorkflowError("remote_workspace_not_supported")
    root = expand_workspace_root(settings.workspace.root, workflow_path)
    if isinstance(issue_or_path, str):
        path = issue_or_path
    else:
        path = os.path.join(
            root, workspace_key(issue_or_path.identifier))
    if os.path.exists(path):
        candidate = canonicalize(path)
        canonical_root = canonicalize(root)
        if candidate == canonical_root or not candidate.startswith(
                canonical_root.rstrip(os.sep) + os.sep):
            return False
        return _remove_local(settings, candidate)
    if os.path.lexists(path):
        try:
            os.remove(path)
            return True
        except OSError as error:
            log.warning(
                "workspace remove failed path=%s error=%s", path, error)
    return False


def remove_recorded(settings, workspace_path, workflow_path=None,
                    worker_host=None):
    """Remove a previously recorded workspace path.

    Mirrors ``Workspace.remove_recorded``: the path was captured at dispatch
    time and is trusted — it only needs to be absolute and canonical-clean
    (no escaping its own parent via symlinks or ``..``), not necessarily
    under the currently configured workspace root.
    """
    if worker_host is not None:
        raise WorkflowError("remote_workspace_not_supported")
    if not isinstance(workspace_path, str) or not os.path.isabs(workspace_path):
        return False
    if not _validate_recorded_path(workspace_path):
        return False
    return _remove_local(settings, canonicalize(workspace_path))


def _validate_recorded_path(workspace):
    canonical_ws = canonicalize(workspace)
    canonical_parent = canonicalize(os.path.dirname(workspace))
    if canonical_ws == canonical_parent:
        return False
    return (canonical_ws + os.sep).startswith(
        canonical_parent.rstrip(os.sep) + os.sep)


def _remove_local(settings, path):
    if not os.path.lexists(path):
        return False
    # File.dir? follows symlinks; rm_rf deletes the link, never the target.
    if os.path.isdir(path):
        try:
            _run_hook(settings.hooks.before_remove, "before_remove", path,
                      settings.hooks.timeout_ms)
        except HookError as error:
            log.warning(
                "before_remove hook failed workspace=%s error=%s", path, error)
    last_error = None
    for attempt in range(3):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                _rmtree_compat(path)
            else:
                os.remove(path)
            return True
        except OSError as error:
            last_error = error
            if os.name != "nt" or attempt == 2 \
                    or getattr(error, "winerror", None) not in (5, 32):
                break
            # Windows can hold a deleted directory handle briefly after a
            # child process exits. Retry only transient sharing/lock errors.
            time.sleep(0.1 * (attempt + 1))
    log.warning("workspace remove failed path=%s error=%s", path, last_error)
    return False


def remove_issue_workspaces(settings, issue_identifiers, workflow_path=None,
                            worker_host=None):
    """Remove workspaces for issue identifiers.

    ``worker_host`` nil + configured ``ssh_hosts`` iterates every configured
    host (matching the Elixir semantics of "we don't know which host it ran
    on"); remote removal is reported unsupported by :func:`remove`.
    """
    if isinstance(issue_identifiers, (str, bytes)) \
            or hasattr(issue_identifiers, "identifier"):
        identifiers = [issue_identifiers]
    else:
        identifiers = list(issue_identifiers)
    if worker_host is not None:
        targets = [worker_host]
    else:
        hosts = [host for host in (settings.worker.ssh_hosts or []) if host]
        targets = hosts if hosts else [None]
    root = expand_workspace_root(settings.workspace.root, workflow_path)
    removed = []
    for identifier in identifiers:
        base = workspace_key(identifier)
        for host in targets:
            if host is not None:
                try:
                    path = safe_join(root, base)
                    remove(settings, path, workflow_path, worker_host=host)
                except (PathSafetyError, WorkflowError) as error:
                    log.warning(
                        "remote workspace cleanup unsupported host=%s "
                        "path=%s error=%s", host, base, error)
                continue
            try:
                path = safe_join(root, base)
            except PathSafetyError:
                continue
            if remove(settings, path, workflow_path):
                removed.append(path)
    return removed


def _run_hook(command, name, cwd, timeout_ms):
    if not command:
        return
    log.info(
        "Running workspace hook hook=%s workspace=%s worker_host=local",
        name, cwd)
    deadline = time.monotonic() + max(timeout_ms, 1) / 1000.0
    env = dict(os.environ)
    env["SYMPHONY_WORKSPACE"] = cwd
    try:
        process = subprocess.Popen(
            [util.posix_shell(), "-lc", command],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        raise HookError(name, status=-1, output="spawn failed: %s" % error)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_process(process)
            _drain(process)
            log.warning(
                "Workspace hook timed out hook=%s workspace=%s "
                "worker_host=local timeout_ms=%s", name, cwd, timeout_ms)
            raise HookError(name, timed_out=timeout_ms)
        try:
            output, _ = process.communicate(timeout=min(remaining, 1.0))
        except subprocess.TimeoutExpired:
            continue
        if process.returncode == 0:
            return
        text = _decode(output)
        log.warning(
            "Workspace hook failed hook=%s workspace=%s worker_host=local "
            "status=%s output=%r",
            name, cwd, process.returncode, _truncate(text))
        raise HookError(name, status=process.returncode, output=text)


def _drain(process):
    try:
        process.communicate(timeout=2)
    except Exception:  # noqa: BLE001 - best-effort drain
        pass


def _decode(output):
    return (output or b"").decode("utf-8", "replace").strip()


def _truncate(text, limit=2048):
    if len(text) <= limit:
        return text
    return text[:limit] + "... (truncated)"


def _kill_process(process):
    import signal

    from . import util

    util.signal_process_tree(process, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        util.signal_process_tree(
            process, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
