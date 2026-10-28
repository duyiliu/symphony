"""Per-issue workspace creation, hook execution and safe removal.

Safety invariant: every path that hooks or the agent can reach must resolve
inside the configured workspace root. Remote workers (``worker.ssh_hosts``)
are not supported by this implementation; ``pi`` runs locally only.
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import time

from .paths import PathSafetyError, canonicalize, expand_workspace_root, safe_join
from .workflow import WorkflowError

log = logging.getLogger("symphony.workspace")

IDENTIFIER_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]")


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


def _cleanup_failed_new_workspace(path):
    try:
        shutil.rmtree(path)
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
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True
    except OSError as error:
        log.warning("workspace remove failed path=%s error=%s", path, error)
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
            ["sh", "-lc", command],
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

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
