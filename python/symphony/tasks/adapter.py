"""Tracker adapter for ``tracker.kind: board``.

Local JSON records are the workflow authority. Candidates come from the
record set filtered by local state; a by-ID refresh additionally performs a
live source check so a confirmed 404 makes the task invisible while native
open/closed is only reported as ``native_state`` and never rewrites the
local state (SPEC §11.1 boundary semantics).
"""

import logging

from ..github import client as gh_client
from ..github import workflow_policy as policy
from ..github.client import GitHubError
from ..issue import Issue
from ..util import normalize_issue_state, parse_datetime
from . import importer, store as store_mod

log = logging.getLogger("symphony.tasks.adapter")


def validate_config(tracker_settings):
    """Local states must be board states, never GitHub open/closed."""
    store_mod.validate_settings(tracker_settings)
    if not tracker_settings.active_states:
        raise store_mod.TaskError("missing_active_states")
    if not tracker_settings.terminal_states:
        raise store_mod.TaskError("missing_terminal_states")
    for name in list(tracker_settings.active_states) + \
            list(tracker_settings.terminal_states):
        if store_mod.canonical_state(name) is None:
            raise store_mod.TaskError("invalid_tracker_state", name)


def _store(tracker_settings):
    return store_mod.Store(tracker_settings)


def _identifier(number):
    return "GH-%d" % number


def _issue_number(issue_id):
    try:
        return int(str(issue_id))
    except (TypeError, ValueError):
        text = str(issue_id).upper()
        if text.startswith("GH-"):
            try:
                return int(text[3:])
            except ValueError:
                return None
        return None


def _to_issue(record):
    issue = Issue(
        id=str(record["issue_number"]),
        identifier=record.get("identifier")
        or _identifier(record["issue_number"]),
        title=record.get("title") or "",
        description=record.get("description") or "",
        state=record.get("state") or "Backlog",
        url=record.get("url"),
        labels=list(record.get("labels") or []),
        blocked_by=[],
        created_at=parse_datetime(record.get("created_at")),
        updated_at=parse_datetime(record.get("updated_at")),
    )
    issue.dispatchable = True
    issue.native_ref = {
        "repo": record.get("repo"),
        "number": record.get("issue_number"),
        "native_state": record.get("native_state"),
        "url": record.get("url"),
        "title": record.get("title"),
    }
    return issue


def fetch_issues_by_states(tracker_settings, state_names):
    """Local scan only — cheap per-tick read, independent of GitHub."""
    wanted = {normalize_issue_state(s) for s in state_names}
    records, errors = _store(tracker_settings).list()
    for error in errors:
        log.warning("Task record skipped issue=%s error=%s",
                    error["issue_number"], error["error"])
    return [issue for issue in (_to_issue(r) for r in records)
            if normalize_issue_state(issue.state) in wanted]


def fetch_issues_by_ids(tracker_settings, issue_ids):
    """Refresh by IDs: local record + live source liveness.

    Confirmed 404 → omitted (not visible). Transport/HTTP failures raise
    (fetch failure ≠ 404). Native ``closed`` keeps the local state and is
    surfaced via ``native_state`` only.
    """
    tracker = tracker_settings
    task_store = _store(tracker)
    repo = policy.repo(tracker)
    issues = []
    for issue_id in issue_ids:
        number = _issue_number(issue_id)
        if number is None:
            continue
        record = task_store.get(number)
        if record is None:
            continue  # never imported → not a tracked task
        result = gh_client.request(
            "GET", "/repos/%s/issues/%d" % (repo, number), {}, None,
            tracker)
        if result["status"] == 404:
            continue
        if not (200 <= result["status"] <= 299):
            raise GitHubError(
                "github_status_%s" % result["status"],
                "issue %d refresh failed" % number)
        refreshed = task_store.refresh_source(
            number, importer.source_fields(result["body"])) or record
        issues.append(_to_issue(refreshed))
    return issues


def fetch_candidate_issues(tracker_settings):
    return fetch_issues_by_states(
        tracker_settings, tracker_settings.active_states or [])


def import_sources(tracker_settings):
    """Poll-tick discovery: paginate all open issues, upsert records."""
    return importer.import_open_issues(tracker_settings)


def import_issue_by_number(tracker_settings, number):
    """Explicit import, the only path that can import a closed issue."""
    return importer.import_issue_by_number(tracker_settings, number)


def begin_run_issue(issue, tracker_settings):
    """Claim-time recheck: still a tracked, active task → ``ok``."""
    number = _issue_number(issue.id)
    active = {normalize_issue_state(s)
              for s in (tracker_settings.active_states or [])}
    try:
        record = _store(tracker_settings).get(number)
    except store_mod.TaskError as error:
        raise GitHubError("task_storage", str(error))
    if record is None:
        return "skip"
    if normalize_issue_state(record.get("state")) not in active:
        return "skip"
    return "ok"


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])


def record_issue_failure(issue, tracker_settings, reason):
    number = _issue_number(issue.id)
    if number is not None:
        _store(tracker_settings).write_failure(number, reason)
