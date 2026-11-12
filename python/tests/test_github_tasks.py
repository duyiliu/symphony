"""Board task lifecycle: dispatch by local state, blocked-in-memory,
terminal cleanup, and state transitions driving worker routing."""
import datetime
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import orchestrator  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.tasks import store  # noqa: E402


def _tracker(tmp_path):
    return SimpleNamespace(
        kind="board", required_labels=[], provider={
            "repo": "o/r", "token": "t", "state_dir": str(tmp_path)},
        active_states=["Todo", "In Progress", "Merging", "Rework"],
        terminal_states=["Closed", "Cancelled", "Canceled", "Duplicate",
                         "Done"])


def _settings(tmp_path):
    tracker = _tracker(tmp_path)
    return SimpleNamespace(
        tracker=tracker,
        agent=SimpleNamespace(max_concurrent_agents=2,
                              max_concurrent_agents_by_state={},
                              max_retry_backoff_ms=300000),
        worker=SimpleNamespace(ssh_hosts=[],
                               max_concurrent_agents_per_host=0),
        workspace=SimpleNamespace(root=str(tmp_path / "ws")),
        hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
        codex=SimpleNamespace(stall_timeout_ms=1000),
        polling=SimpleNamespace(interval_ms=10000))


def _store_stub(settings):
    return SimpleNamespace(settings=lambda: settings, path="wf",
                           force_reload=lambda: True)


def _seed(tmp_path, number, state, **fields):
    task_store = store.Store(_tracker(tmp_path))
    record = {
        "schema_version": store.SCHEMA_VERSION, "repo": "o/r",
        "issue_number": number, "identifier": "GH-%d" % number,
        "state": state, "revision": 1, "title": "t%d" % number,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    record.update(fields)
    task_store.locked(number, lambda _r, save: save(record))
    return record


def _orch(tmp_path):
    settings = _settings(tmp_path)
    return orchestrator.Orchestrator(_store_stub(settings)), settings


# --- dispatch by local state --------------------------------------------------

def test_backlog_is_not_a_candidate(tmp_path):
    _seed(tmp_path, 1, "Backlog")
    orch, settings = _orch(tmp_path)
    active = orch._active_state_set(settings)
    terminal = orch._terminal_state_set(settings)
    issue = Issue(id="1", identifier="GH-1", title="t", state="Backlog",
                  dispatchable=True)
    assert not orch._candidate_issue(issue, active, terminal, settings)


def test_human_review_is_inactive(tmp_path):
    _seed(tmp_path, 1, "Human Review")
    orch, settings = _orch(tmp_path)
    active = orch._active_state_set(settings)
    assert "human review" not in active


def test_all_four_active_states_dispatchable(tmp_path):
    from symphony.tasks import adapter
    tracker = _tracker(tmp_path)
    for i, state in enumerate(
            ("Todo", "In Progress", "Merging", "Rework"), start=1):
        _seed(tmp_path, i, state)
    ids = sorted(
        int(i.id) for i in adapter.fetch_candidate_issues(tracker))
    assert ids == [1, 2, 3, 4]


# --- worker_done routing ---------------------------------------------------------

def test_done_normal_schedules_continuation(tmp_path):
    from symphony.agent import runner
    orch, _ = _orch(tmp_path)
    orch.running["1"] = {"ref": 7, "issue": Issue(
        id="1", identifier="GH-1", title="t", state="Todo",
        dispatchable=True, url="u")}
    with patch.object(orch, "_schedule_issue_retry") as schedule:
        orch._on_worker_done("1", 7, runner.DONE_NORMAL)
    assert schedule.call_args.args[2]["delay_type"] == "continuation"
    assert schedule.call_args.args[1] == 1
    assert "1" not in orch.running


def test_done_skipped_releases_claim_without_retry(tmp_path):
    from symphony.agent import runner
    orch, _ = _orch(tmp_path)
    orch.claimed.add("1")
    orch.running["1"] = {"ref": 7, "issue": Issue(
        id="1", identifier="GH-1", title="t", state="Todo",
        dispatchable=True)}
    with patch.object(orch, "_schedule_issue_retry") as schedule:
        orch._on_worker_done("1", 7, runner.DONE_SKIPPED)
    schedule.assert_not_called()
    assert "1" not in orch.running
    assert "1" not in orch.claimed


def test_failure_schedules_retry_with_error(tmp_path):
    orch, _ = _orch(tmp_path)
    orch.running["1"] = {"ref": 7, "retry_attempt": 0,
                         "issue": Issue(id="1", identifier="GH-1",
                                        title="t", state="Todo",
                                        dispatchable=True, url="u")}
    with patch.object(orch, "_schedule_issue_retry") as schedule:
        orch._on_worker_done("1", 7, "turn_failed")
    metadata = schedule.call_args.args[2]
    assert "agent exited" in metadata["error"]
    assert "1" not in orch.running


# --- in-memory blocked ---------------------------------------------------------

def test_blocked_issue_is_not_dispatched_and_not_persisted(tmp_path):
    _seed(tmp_path, 1, "Todo")
    orch, settings = _orch(tmp_path)
    issue = Issue(id="1", identifier="GH-1", title="t", state="Todo",
                  dispatchable=True)
    entry = {"identifier": "GH-1", "issue": issue}
    orch._block_issue_from_entry("1", entry, "agent needs input")
    active = orch._active_state_set(settings)
    terminal = orch._terminal_state_set(settings)
    assert not orch._should_dispatch(issue, active, terminal, settings)
    # Runtime blocked is in-memory only: nothing on disk, cleared by
    # _release_issue_claim (human unblocks via board move + restart).
    assert "blocked" not in store.Store(
        settings.tracker).get(1)
    orch._release_issue_claim("1")
    assert orch._should_dispatch(issue, active, terminal, settings)


# --- terminal cleanup -----------------------------------------------------------

def test_terminal_state_cleans_workspace_and_releases(tmp_path):
    _seed(tmp_path, 1, "Done")
    orch, settings = _orch(tmp_path)
    orch.claimed.add("1")
    issue = Issue(id="1", identifier="GH-1", title="t", state="Done",
                  dispatchable=True)
    with patch.object(orch, "_cleanup_issue_workspace") as cleanup:
        orch._handle_retry_issue_lookup(
            issue, "1", 1, {"delay_type": "continuation"}, settings)
    cleanup.assert_called_once()
    assert "1" not in orch.claimed


def test_gone_issue_releases_claim(tmp_path):
    orch, settings = _orch(tmp_path)
    orch.claimed.add("9")
    orch._handle_retry_issue_lookup(
        None, "9", 1, {"delay_type": "continuation"}, settings)
    assert "9" not in orch.claimed


def test_inactive_state_releases_claim(tmp_path):
    orch, settings = _orch(tmp_path)
    orch.claimed.add("1")
    issue = Issue(id="1", identifier="GH-1", title="t",
                  state="Human Review", dispatchable=True)
    orch._handle_retry_issue_lookup(
        issue, "1", 1, {"delay_type": "continuation"}, settings)
    assert "1" not in orch.claimed


# --- source-closed visibility ---------------------------------------------------

def test_source_closed_keeps_local_state_and_flags(tmp_path):
    from unittest.mock import patch as _patch
    from symphony.github import client as gh_client
    from symphony.tasks import adapter
    tracker = _tracker(tmp_path)
    _seed(tmp_path, 1, "Merging", title="old")

    def fake(method, path, params, body, settings_):
        return {"status": 200, "body": {
            "number": 1, "state": "closed", "title": "new",
            "body": "b", "labels": [],
            "html_url": "https://github.com/o/r/issues/1"}}

    with _patch.object(gh_client, "request", fake):
        issues = adapter.fetch_issues_by_ids(tracker, ["1"])
    assert issues[0].state == "Merging"
    assert issues[0].native_ref["native_state"] == "closed"
    assert store.Store(tracker).get(1)["title"] == "new"
