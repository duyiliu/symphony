"""Task record store for ``tracker.kind: board``.

The JSON record under ``state_dir`` is the single workflow authority
(Linear-style board semantics): ``state`` is the only business state and a
monotonic ``revision`` guards concurrent writes. GitHub issues supply task
identity and content; native open/closed lives on ``native_state`` and must
never overwrite ``state``. One service process owns writes; the board UI and
the agent reach records through the authenticated HTTP API / agent tool,
all serialised by the same per-issue locks.
"""

import datetime
import json
import logging
import os
import re
import threading

from ..github import workflow_policy as policy
from ..workflow import WorkflowError

log = logging.getLogger("symphony.tasks.store")

SCHEMA_VERSION = 2
HISTORY_LIMIT = 50

# Canonical state names keyed by normalised spelling. Official terminal
# aliases Closed/Cancelled/Canceled/Duplicate all keep their own spelling.
_STATE_TABLE = {
    "backlog": "Backlog",
    "todo": "Todo",
    "in progress": "In Progress",
    "in_progress": "In Progress",
    "inprogress": "In Progress",
    "human review": "Human Review",
    "human_review": "Human Review",
    "merging": "Merging",
    "rework": "Rework",
    "done": "Done",
    "closed": "Closed",
    "cancelled": "Cancelled",
    "canceled": "Canceled",
    "duplicate": "Duplicate",
}

STATE_VALUES = tuple(sorted(set(_STATE_TABLE.values())))

# Fields a caller may write through PATCH/task_api. Pointer updates bump
# revision; cached source fields (title/description/labels/native_state)
# are refreshed by discovery/refresh paths and never by clients.
UPDATABLE_FIELDS = {
    "state",
    "branch_name",
    "pr_number",
    "pr_url",
    "pr_head_sha",
    "workpad_comment_id",
}

CACHE_FIELDS = {
    "title", "description", "labels", "url", "native_state",
}

_RECORD_NAME = re.compile(r"^(\d+)\.json$")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class TaskError(WorkflowError):
    """Store failure with a stable code mapped to HTTP status by callers.

    Subclasses WorkflowError so config validation failures follow the
    existing last-known-good reload path."""

    def __init__(self, code, detail=None):
        self.code = code
        super().__init__(code, detail)


def canonical_state(value):
    if not isinstance(value, str):
        return None
    return _STATE_TABLE.get(value.strip().lower())


def is_state(value):
    return canonical_state(value) is not None


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _absolute_private_path(path):
    if not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/")
    if os.name == "nt" and re.match(r"^[A-Za-z]:/", normalized):
        normalized = normalized[2:]
    elif not normalized.startswith("/"):
        return False
    return len([s for s in normalized.split("/") if s]) >= 2


def validate_settings(tracker_settings):
    if not _absolute_private_path(state_dir(tracker_settings)):
        raise TaskError("invalid_state_dir")
    if not policy.repo(tracker_settings):
        raise TaskError("missing_repo")


def state_dir(tracker_settings):
    provider = getattr(tracker_settings, "provider", None) or {}
    value = provider.get("state_dir")
    if isinstance(value, str) and value.startswith("$"):
        value = os.environ.get(value[1:]) or ""
    return value


def _lock_for(path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise TaskError("invalid_task_record", error)
    except OSError as error:
        raise TaskError("task_storage", str(error))
    if not isinstance(record, dict):
        raise TaskError("invalid_task_record")
    return record


def _write_file(path, record):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            log.warning("Unable to back up task record %s", path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _validate_record(record, number, repo):
    """Structure/identity gate for every read and write path.

    A v1-shaped or malformed file must fail loudly (explicit migration or
    report), never silently default to Backlog or poison the batch.
    """
    if not isinstance(record, dict):
        raise TaskError("invalid_task_record")
    if record.get("repo") != repo:
        raise TaskError("receipt_repository_mismatch")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise TaskError("unsupported_schema_version",
                        "run symphony migrate before serving task %s" % number)
    if record.get("issue_number") != number:
        raise TaskError("invalid_task_record",
                        "issue_number %r != file %d"
                        % (record.get("issue_number"), number))
    if record.get("identifier") != "GH-%d" % number:
        raise TaskError("invalid_task_record", "identifier")
    if canonical_state(record.get("state")) is None:
        raise TaskError("invalid_task_record", "state")
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) \
            or revision < 1:
        raise TaskError("invalid_task_record", "revision")
    return record


def public_record(record):
    """Projection served to API/tool callers (internal fields stay whole)."""
    return {key: record.get(key) for key in (
        "schema_version", "repo", "issue_number", "identifier", "state",
        "revision", "branch_name", "pr_number", "pr_url", "pr_head_sha",
        "workpad_comment_id", "title", "url", "native_state", "labels",
        "blocked_reason", "last_error", "updated_at", "created_at",
    ) if key in record}


class Store:
    """Per-process serialised access to ``state_dir/<number>.json`` files."""

    def __init__(self, tracker_settings):
        validate_settings(tracker_settings)
        self.settings = tracker_settings
        self.repo = policy.repo(tracker_settings)
        self.directory = state_dir(tracker_settings)

    # -- paths / locking ------------------------------------------------

    def path_for(self, number):
        return os.path.join(self.directory, "%d.json" % number)

    def locked(self, number, fn):
        """Run ``fn(record_or_None, save)`` under the per-issue lock.

        ``save(record)`` stamps repo/updated_at and atomically replaces the
        file; it returns the stored record.
        """
        path = self.path_for(number)
        lock = _lock_for(path)
        with lock:
            try:
                record = _read_file(path)
            except TaskError:
                raise
            except Exception as error:  # noqa: BLE001
                raise TaskError("task_storage", str(error))
            if record is not None:
                _validate_record(record, number, self.repo)

            def save(value):
                value = {**value, "repo": self.repo,
                         "updated_at": _now()}
                _write_file(path, value)
                return value

            return fn(record, save)

    # -- reads ------------------------------------------------------------

    def get(self, number):
        path = self.path_for(number)
        with _lock_for(path):
            record = _read_file(path)
            if record is not None:
                _validate_record(record, number, self.repo)
            return record

    def list(self):
        """``(records, errors)`` — corrupt/foreign files are reported, not
        silently treated as Backlog and never block healthy candidates."""
        records = []
        errors = []
        if not os.path.isdir(self.directory):
            return records, errors
        for name in sorted(os.listdir(self.directory)):
            match = _RECORD_NAME.match(name)
            if not match:
                continue  # *.bak/*.tmp/non-numeric are ignored on purpose
            number = int(match.group(1))
            path = os.path.join(self.directory, name)
            try:
                record = _read_file(path)
                if record is None:
                    continue
                records.append(_validate_record(record, number, self.repo))
            except Exception as error:  # noqa: BLE001
                errors.append({"issue_number": number,
                               "error": getattr(error, "code", str(error))})
        return records, errors

    # -- writes -----------------------------------------------------------

    def update(self, number, fields, expected_revision, by="api"):
        """Revision-checked field update. Returns the stored record.

        A repeat of the current values is an idempotent success even with a
        stale ``expected_revision``; real changes require the current
        revision so a caller never silently overwrites a human action.
        """
        if not isinstance(fields, dict):
            raise TaskError("invalid_fields")
        unknown = [key for key in fields if key not in UPDATABLE_FIELDS]
        if unknown:
            raise TaskError("invalid_fields", ",".join(sorted(unknown)))
        if "state" in fields:
            state = canonical_state(fields["state"])
            if state is None:
                raise TaskError("invalid_state", fields["state"])
            fields = {**fields, "state": state}
        if "pr_number" in fields and fields["pr_number"] is not None:
            value = fields["pr_number"]
            if not isinstance(value, int) or isinstance(value, bool) \
                    or value <= 0:
                raise TaskError("invalid_fields", "pr_number")
        if not isinstance(expected_revision, int) \
                or isinstance(expected_revision, bool):
            raise TaskError("missing_expected_revision")

        def body(record, save):
            if record is None:
                raise TaskError("task_not_found")
            changed = {
                key: value for key, value in fields.items()
                if record.get(key) != value}
            if not changed:
                return record  # idempotent: no write, no revision bump
            if expected_revision != record.get("revision", 0):
                raise TaskError("revision_conflict")
            updated = dict(record)
            updated.update(changed)
            if "state" in changed:
                history = list(record.get("history") or [])
                history.append({
                    "at": _now(), "from": record.get("state"),
                    "to": changed["state"], "by": by})
                updated["history"] = history[-HISTORY_LIMIT:]
            updated["revision"] = int(record.get("revision") or 0) + 1
            return save(updated)

        return self.locked(number, body)

    def refresh_source(self, number, source):
        """Cache GitHub source fields on an existing record; never touches
        business state or revision. ``source`` keys: title, description,
        labels, url, native_state."""
        updates = {
            key: source[key] for key in CACHE_FIELDS if key in source}

        def body(record, save):
            if record is None:
                return None
            changed = {
                key: value for key, value in updates.items()
                if record.get(key) != value}
            if not changed:
                return record
            updated = dict(record)
            updated.update(changed)
            return save(updated)

        return self.locked(number, body)

    def upsert_source(self, number, source):
        """Discovery upsert: create a Backlog record when missing, else only
        refresh cached source fields. Same lock family as client updates."""
        path = self.path_for(number)
        with _lock_for(path):
            record = _read_file(path)
            if record is not None:
                _validate_record(record, number, self.repo)
                # Existing record: source refresh only, business state stays.
                changed = {
                    key: value for key, value in source.items()
                    if key in CACHE_FIELDS and record.get(key) != value}
                if not changed:
                    return record
                updated = dict(record)
                updated.update(changed)
                updated["updated_at"] = _now()
                _write_file(path, updated)
                return updated
            state = ("Closed" if source.get("native_state") == "closed"
                     else "Backlog")
            record = {
                "schema_version": SCHEMA_VERSION,
                "repo": self.repo,
                "issue_number": number,
                "identifier": "GH-%d" % number,
                "state": state,
                "revision": 1,
                "created_at": _now(),
                "updated_at": _now(),
                "history": [{"at": _now(), "from": None, "to": state,
                             "by": "import"}],
            }
            for key in UPDATABLE_FIELDS - {"state"}:
                record.setdefault(key, None)
            record.update(
                {k: v for k, v in source.items() if k in CACHE_FIELDS})
            _write_file(path, record)
            return record

    def write_failure(self, number, reason):
        """Best-effort error annotation; failure must not kill the caller."""
        try:
            def body(record, save):
                if record is None:
                    return None
                return save({**record, "last_error": str(reason)[:4000]})
            return self.locked(number, body)
        except TaskError:
            log.warning("Cannot persist task failure issue=%s", number)
            return None
