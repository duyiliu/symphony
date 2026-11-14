"""Periodic backup and restore of durable scheduler state.

Only JSON records owned by the delivery controller and the Agent Session
reporter are copied. Runtime-only state (MCP scopes, retry queue) is rebuilt
on restart and intentionally excluded.
"""

import datetime
import json
import logging
import os
import threading
import time
import uuid
import zipfile

from . import agent_sessions

log = logging.getLogger("symphony.state_backup")

BACKUP_PREFIX = "state-"
BACKUP_SUFFIX = ".zip"
DELIVERY_PREFIX = "delivery"
SESSION_PREFIX = "agent_sessions"
_TEMP_MARKER = ".tmp."


class StateBackupError(Exception):
    pass


def backup_directory(settings):
    configured = getattr(settings.server, "state_backup_dir", None)
    if configured:
        return os.path.abspath(configured)
    return os.path.join(
        os.path.abspath(settings.workspace.root or os.getcwd()),
        "_state_backups")


def source_directories(settings):
    sources = []
    delivery_dir = getattr(settings.delivery, "state_dir", None)
    if delivery_dir and os.path.isdir(delivery_dir):
        sources.append((DELIVERY_PREFIX, os.path.abspath(delivery_dir)))
    sessions_dir = agent_sessions.state_root(settings)
    if os.path.isdir(sessions_dir):
        sources.append((SESSION_PREFIX, sessions_dir))
    return sources


def _iter_state_files(root):
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories
            if not os.path.islink(os.path.join(current, name))
        ]
        for name in files:
            if name.endswith(".owner.json") or _TEMP_MARKER in name:
                continue
            path = os.path.join(current, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            yield path


def _backup_path(directory, moment):
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    candidate = os.path.join(directory, "%s%s%s" % (
        BACKUP_PREFIX, stamp, BACKUP_SUFFIX))
    if not os.path.exists(candidate):
        return candidate
    return os.path.join(directory, "%s%s-%s%s" % (
        BACKUP_PREFIX, stamp, uuid.uuid4().hex[:8], BACKUP_SUFFIX))


def _remove_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("Unable to remove temporary backup path=%s error=%s",
                    path, error)


def _prune(directory, keep):
    if keep <= 0:
        return 0
    try:
        names = sorted(
            name for name in os.listdir(directory)
            if name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)
        )
    except OSError as error:
        log.warning("Unable to list backup directory path=%s error=%s",
                    directory, error)
        return 0
    removed = 0
    for name in names[:-keep]:
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as error:
            log.warning("Unable to prune backup path=%s error=%s",
                        os.path.join(directory, name), error)
    return removed


def backup_once(settings, now=None):
    """Write one snapshot archive; return ``None`` when there is no state."""
    sources = source_directories(settings)
    if not sources:
        return None
    directory = backup_directory(settings)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    path = _backup_path(directory, moment)
    temporary = "%s%s%s.%s" % (
        path, _TEMP_MARKER, os.getpid(), uuid.uuid4().hex)
    files = 0
    try:
        with zipfile.ZipFile(
                temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for prefix, root in sources:
                for file_path in _iter_state_files(root):
                    relative = os.path.relpath(file_path, root)
                    archive.write(
                        file_path,
                        prefix + "/" + relative.replace(os.sep, "/"))
                    files += 1
        os.replace(temporary, path)
    finally:
        _remove_quietly(temporary)
    if files == 0:
        _remove_quietly(path)
        return None
    _prune(directory, getattr(settings.server, "state_backup_keep", 24))
    log.info("State backup written path=%s files=%d", path, files)
    return {"path": path, "files": files}


def _safe_relative(relative):
    parts = relative.replace("\\", "/").split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise StateBackupError("unsafe_backup_path")
    if any(":" in part for part in parts) or parts[0].startswith("/"):
        raise StateBackupError("unsafe_backup_path")
    return parts


def _restore_targets(settings):
    workspace_root = os.path.abspath(
        settings.workspace.root or os.getcwd())
    delivery_dir = (
        settings.delivery.state_dir
        or os.path.join(workspace_root, "_delivery"))
    return {
        DELIVERY_PREFIX: os.path.abspath(delivery_dir),
        SESSION_PREFIX: agent_sessions.state_root(settings),
    }


def _write_restored_file(target, payload):
    directory = os.path.dirname(target)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = "%s%s%s.%s" % (
        target, _TEMP_MARKER, os.getpid(), uuid.uuid4().hex)
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        _remove_quietly(temporary)


def restore_backup(archive_path, settings):
    """Restore delivery and Agent Session JSON records from an archive.

    Every entry is validated as a JSON object before any file is replaced.
    The service should be stopped before calling this function.
    """
    try:
        backup_once(settings)
    except OSError as error:
        raise StateBackupError("pre_restore_backup_failed") from error
    targets = _restore_targets(settings)
    restored = {DELIVERY_PREFIX: 0, SESSION_PREFIX: 0}
    pending = []
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise StateBackupError("invalid_backup_archive") from error
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            prefix, separator, relative = name.partition("/")
            if prefix not in targets or not separator:
                continue
            parts = _safe_relative(relative)
            target = os.path.join(targets[prefix], *parts)
            payload = archive.read(info)
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise StateBackupError(
                    "invalid_backup_json:%s" % name) from error
            if not isinstance(value, dict):
                raise StateBackupError(
                    "invalid_backup_json:%s" % name)
            pending.append((target, payload))
            restored[prefix] += 1
    if not any(restored.values()):
        raise StateBackupError("empty_backup_archive")
    for target, payload in pending:
        _write_restored_file(target, payload)
    log.info(
        "State restore complete delivery=%d agent_sessions=%d",
        restored[DELIVERY_PREFIX], restored[SESSION_PREFIX])
    return restored


class StateBackup:
    """Background backup thread; failures never stop the scheduler."""

    def __init__(self, settings):
        interval_ms = getattr(
            settings.server, "state_backup_interval_ms", 3_600_000)
        self.interval_s = max(0.0, interval_ms / 1000.0)
        self._settings = settings
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last_success_at = None
        self._last_error = None
        self._last_files = 0
        self._backup_count = 0

    def start(self):
        if self.interval_s <= 0:
            return self
        self._thread = threading.Thread(
            target=self._loop, name="state-backup", daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            self.backup()

    def backup(self):
        with self._lock:
            try:
                result = backup_once(self._settings)
            except Exception as error:  # noqa: BLE001
                self._last_error = str(error) or error.__class__.__name__
                log.warning("State backup failed: %s", self._last_error)
                return None
            if result is None:
                return None
            self._last_success_at = datetime.datetime.now(
                datetime.timezone.utc)
            self._last_error = None
            self._last_files = result["files"]
            self._backup_count += 1
            return result

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if self.interval_s > 0:
            self.backup()

    def snapshot(self):
        with self._lock:
            enabled = self.interval_s > 0
            return {
                "enabled": enabled,
                "directory": backup_directory(self._settings),
                "interval_ms": int(self.interval_s * 1000),
                "keep": getattr(
                    self._settings.server, "state_backup_keep", 24),
                "last_success_at": (
                    self._last_success_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if self._last_success_at else None),
                "last_error": self._last_error,
                "last_files": self._last_files,
                "backup_count": self._backup_count,
                "thread_alive": bool(
                    self._thread and self._thread.is_alive()),
            }
