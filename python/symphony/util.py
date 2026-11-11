"""Shared helpers: time, ISO-8601, shell quoting, state normalisation."""

import os
import re
import shutil
import signal
import time
from datetime import datetime, timezone


def monotonic_ms():
    return int(time.monotonic() * 1000)


def utc_now():
    return datetime.now(timezone.utc)


def iso8601(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        # DateTime.to_iso8601 prints the full microsecond precision.
        text += ".%06d" % dt.microsecond
    return text + "Z"


def iso8601_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (
        dt.microsecond // 1000
    )


def parse_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # DateTime.from_iso8601 requires an explicit offset; naive input is not
    # accepted upstream, so a naive result means malformed input.
    if parsed.tzinfo is None:
        return None
    return parsed


def normalize_issue_state(state_name):
    if not isinstance(state_name, str):
        return ""
    return state_name.strip().lower()


def present_string(value):
    return isinstance(value, str) and value.strip() != ""


def normalize_label(label):
    return label.strip().lower() if isinstance(label, str) else ""


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def sanitize_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))


def posix_shell():
    """Resolve a real POSIX shell for ``-lc`` scripts.

    On Windows, ``bash``/``sh`` on PATH can resolve to a WSL launcher stub
    (``%WINDIR%\\System32\\bash.exe`` or the ``WindowsApps`` app-execution
    alias) which exits immediately when WSL has no installed distro —
    spawning it yields a broken pipe on the first write. Prefer a real
    Git for Windows install, then any PATH hit outside the stub dirs.
    """
    if os.name != "nt":
        return "bash"
    for path in (
        os.path.expandvars(r"%ProgramFiles%\Git\bin\bash.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Git\bin\bash.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
    ):
        if os.path.isfile(path):
            return path
    stub_dirs = [
        os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"), "System32").lower(),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "WindowsApps").lower(),
    ]
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if found and not any(
                os.path.normcase(found).startswith(
                    os.path.normcase(d) + os.sep)
                for d in stub_dirs):
            return found
    return "bash"  # last resort: let the OS report the failure


def signal_process_tree(process, sig):
    """Send ``sig`` to the child's process group.

    POSIX uses ``os.killpg``. On Windows the TERM path calls
    ``process.terminate()`` (best effort) while the KILL path escalates to
    ``taskkill /T /F`` so wrapped grandchildren (sh → node) do not survive
    as orphans — ``process.kill()`` alone only reaps the direct child.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(process.pid, sig)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        if sig == getattr(signal, "SIGKILL", -1):
            if os.name == "nt":
                _taskkill_tree(process.pid)
                return
            process.kill()
        else:
            process.terminate()
    except OSError:
        pass


def _taskkill_tree(pid):
    import subprocess

    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False)
    except OSError:
        pass
