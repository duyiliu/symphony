"""WorkflowStore: caches the last known-good WORKFLOW.md and reloads on change.

Equivalent to the Elixir GenServer: a 1s poll thread watches file stamp
(mtime, size, content hash); every access re-checks staleness synchronously,
so ``force_reload`` semantics hold for the orchestrator's per-tick validation.
On load or validation failure the previous good config is kept.
"""

import hashlib
import logging
import os
import threading

from . import workflow as workflow_mod
from .config import parse_settings
from .github import workflow_policy
from .workflow import WorkflowError

log = logging.getLogger("symphony.workflow_store")

POLL_INTERVAL_S = 1.0


def _validate_settings(settings):
    if settings.tracker.kind is None:
        raise WorkflowError("missing_tracker_kind")
    if settings.tracker.kind not in ("github",):
        raise WorkflowError("unsupported_tracker_kind", settings.tracker.kind)
    from .github import adapter as github_adapter

    github_adapter.validate_config(settings.tracker)
    workflow_policy.validate_runtime(settings)
    if settings.runtime.kind != "pi":
        raise WorkflowError("unsupported_runtime_kind", settings.runtime.kind)


class WorkflowStore:
    def __init__(self, path):
        self._path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._stamp = None
        self._workflow = None
        self._settings = None
        self._stop = threading.Event()
        self._thread = None
        self.load_initial()

    # -- lifecycle -----------------------------------------------------

    def load_initial(self):
        workflow, settings = self._load_state(self._path)
        self._workflow = workflow
        self._settings = settings
        self._stamp = self._current_stamp(self._path)

    def start(self):
        self._thread = threading.Thread(
            target=self._poll_loop, name="workflow-store", daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    # -- accessors ------------------------------------------------------

    def current(self):
        """Return the cached Workflow, reloading when the file changed.

        Like the Elixir GenServer ``:current`` call: reload failures keep and
        return the last known-good workflow instead of propagating.
        """
        with self._lock:
            self._reload_locked(propagate=False)
            return self._workflow

    def settings(self):
        """Return the last known-good Settings, reloading when changed."""
        with self._lock:
            self._reload_locked(propagate=False)
            return self._settings

    def force_reload(self):
        """Synchronously re-validate the workflow file.

        Unlike ``current``/``settings`` this reports reload failures:
        raises WorkflowError on failure; on success updates the cache.
        """
        with self._lock:
            self._reload_locked(propagate=True)
        return True

    @property
    def path(self):
        return self._path

    # -- internals -------------------------------------------------------

    def _poll_loop(self):
        while not self._stop.wait(POLL_INTERVAL_S):
            try:
                with self._lock:
                    self._reload_locked(propagate=False)
            except Exception:  # noqa: BLE001 - keep last good, log inside
                log.exception("unexpected workflow reload failure")

    def _reload_locked(self, propagate):
        stamp = self._current_stamp(self._path)
        if stamp is not None and stamp == self._stamp:
            return
        try:
            workflow, settings = self._load_state(self._path)
        except WorkflowError as error:
            log.error(
                "Failed to reload workflow path=%s reason=%s; keeping last known good configuration",
                self._path, error.describe())
            if propagate:
                raise
            return
        self._workflow = workflow
        self._settings = settings
        self._stamp = stamp
        log.info("Reloaded workflow path=%s", self._path)

    def _load_state(self, path):
        workflow = workflow_mod.load(path)
        settings = parse_settings(workflow.config)
        _validate_settings(settings)
        return workflow, settings

    @staticmethod
    def _current_stamp(path):
        try:
            stat = os.stat(path)
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            return (stat.st_mtime, stat.st_size, digest)
        except OSError:
            return None


_store = None
_store_lock = threading.Lock()


def start_store(path):
    global _store
    with _store_lock:
        if _store is not None:
            raise RuntimeError("workflow store already started")
        _store = WorkflowStore(path).start()
        return _store


def get_store():
    return _store


def stop_store():
    global _store
    with _store_lock:
        if _store is not None:
            _store.stop()
            _store = None
