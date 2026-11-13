import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, orchestrator  # noqa: E402
from symphony.issue import Issue  # noqa: E402


def _settings(**overrides):
    cfg = {
        "tracker": {"kind": "github",
                    "provider": {"repo": "o/r", "token": "t"},
                    "required_labels": ["agent"],
                    "active_states": ["open"],
                    "terminal_states": ["closed"]},
        "agent": {"max_concurrent_agents": 2, "max_retry_backoff_ms": 300000},
        "polling": {"interval_ms": 10000},
    }
    cfg.update(overrides)
    return config.parse_settings(cfg)


def _issue(id="1", state="open", labels=("agent",), priority=None,
           created_at=None):
    return Issue(
        id=id, identifier="GH-%s" % id, title="t", state=state,
        labels=list(labels), dispatchable=True, priority=priority,
        created_at=created_at, url="https://x/%s" % id)


class _Store:
    def __init__(self, settings):
        self._settings = settings

    def settings(self):
        return self._settings

    def force_reload(self):
        return True


def _orchestrator(settings=None):
    settings = settings or _settings()
    return orchestrator.Orchestrator(_Store(settings))


class SortTest(unittest.TestCase):
    def test_sort_by_priority_then_created(self):
        newer = _issue("2", created_at=datetime.datetime(
            2024, 1, 2, tzinfo=datetime.timezone.utc))
        older = _issue("1", created_at=datetime.datetime(
            2024, 1, 1, tzinfo=datetime.timezone.utc))
        prio = _issue("3", priority=1)
        result = orchestrator.Orchestrator._sort_issues([newer, older, prio])
        self.assertEqual([i.id for i in result], ["3", "1", "2"])


class CandidateTest(unittest.TestCase):
    def test_worker_runtime_info_records_workspace_and_consumes_capacity(self):
        orch = _orchestrator()
        orch.max_concurrent_agents = 1
        orch.running["1"] = {"issue": _issue("1")}
        orch._on_worker_runtime_info("1", {"workspace_path": "worker-workspace",
                                           "worker_host": "h1"})
        self.assertEqual(orch.running["1"]["workspace_path"],
                         "worker-workspace")
        self.assertEqual(orch.running["1"]["worker_host"], "h1")
        self.assertEqual(orch._available_slots(), 0)

    def test_running_entry_consumes_agent_capacity(self):
        orch = _orchestrator()
        orch.max_concurrent_agents = 1
        orch.running["1"] = {"issue": _issue("1")}
        settings = orch._store.settings()
        self.assertEqual(orch._available_slots(), 0)
        active = orch._active_state_set(settings)
        terminal = orch._terminal_state_set(settings)
        self.assertFalse(
            orch._should_dispatch(_issue("2"), active, terminal, settings))

    def test_done_normal_schedules_continuation_check(self):
        from symphony.agent import runner
        orch = _orchestrator()
        orch.running["1"] = {"ref": 7, "issue": _issue("1"),
                             "identifier": "GH-1"}
        with patch.object(orch, "_schedule_issue_retry") as schedule:
            orch._on_worker_done("1", 7, runner.DONE_NORMAL)
        self.assertEqual(schedule.call_args.args[2]["delay_type"],
                         "continuation")
        self.assertNotIn("1", orch.running)

    def test_skip_at_claim_releases_without_worker(self):
        from symphony.agent import runner
        issue = _issue()
        done = []
        with patch.object(runner.tracker_adapter, "begin_run_issue",
                          return_value="skip"), \
             patch.object(runner, "_run_on_worker_host") as worker:
            runner.run(issue, _settings(), lambda *args: None,
                       lambda *args: done.append(args), runner.RunHandle())
        worker.assert_not_called()
        self.assertEqual(done, [(issue.id, runner.DONE_SKIPPED)])

    def test_retry_waits_for_capacity_then_respawns(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        orch.max_concurrent_agents = 1
        orch.running["other"] = {"issue": _issue("other")}
        metadata = {"delay_type": "continuation", "identifier": "GH-1"}
        with patch.object(orch, "_schedule_issue_retry") as retry, \
                patch.object(orch, "_spawn_issue") as spawn:
            orch._handle_active_retry(_issue(), 1, metadata, settings)
        retry.assert_called_once()
        spawn.assert_not_called()
        orch.running.clear()
        with patch.object(orch, "_refresh_issue_for_dispatch",
                          return_value=_issue()), \
                patch.object(orch, "_spawn_issue") as spawn:
            orch._handle_active_retry(_issue(), 1, metadata, settings)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0].id, "1")

    def test_candidate(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        active = orch._active_state_set(settings)
        terminal = orch._terminal_state_set(settings)
        self.assertTrue(orch._candidate_issue(_issue(), active, terminal, settings))
        self.assertFalse(orch._candidate_issue(
            _issue(state="closed"), active, terminal, settings))
        self.assertFalse(orch._candidate_issue(
            _issue(labels=()), active, terminal, settings))
        self.assertFalse(orch._candidate_issue(
            _issue(state="Open"), active, terminal, settings) is False)
        # state normalisation: "Open" is active
        self.assertTrue(orch._candidate_issue(
            _issue(state="Open"), active, terminal, settings))

    def test_should_dispatch_claims(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        active = orch._active_state_set(settings)
        terminal = orch._terminal_state_set(settings)
        issue = _issue()
        self.assertTrue(orch._should_dispatch(issue, active, terminal, settings))
        orch.claimed.add(issue.id)
        self.assertFalse(orch._should_dispatch(issue, active, terminal, settings))


class RetryDelayTest(unittest.TestCase):
    def test_exponential_backoff(self):
        settings = _settings()
        orch = _orchestrator(settings)
        self.assertEqual(
            orch._retry_delay(1, {"delay_type": "continuation"}, settings), 1000)
        self.assertEqual(orch._retry_delay(1, {}, settings), 10000)
        self.assertEqual(orch._retry_delay(2, {}, settings), 20000)
        self.assertEqual(orch._retry_delay(3, {}, settings), 40000)
        self.assertEqual(orch._retry_delay(9, {}, settings), 300000)  # capped

    def test_continuation_only_first(self):
        settings = _settings()
        orch = _orchestrator(settings)
        self.assertEqual(
            orch._retry_delay(2, {"delay_type": "continuation"}, settings), 20000)


class RetryLimitTest(unittest.TestCase):
    def _limited_settings(self, limit):
        return _settings(agent={
            "max_concurrent_agents": 2,
            "max_retry_backoff_ms": 300000,
            "max_retry_attempts": limit,
        })

    def test_retry_agent_down_blocks_at_limit(self):
        orch = _orchestrator(self._limited_settings(2))
        entry = {
            "ref": 1,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "retry_attempt": 2,
            "handle": _FakeHandle(),
        }
        orch.running["1"] = entry
        orch.claimed.add("1")
        orch._retry_agent_down("1", entry, "sess", "boom")
        self.assertIn("1", orch.blocked)
        self.assertIn("retry limit exceeded", orch.blocked["1"]["error"])
        self.assertIn("1", orch.claimed)
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.retry_attempts)

    def test_retry_agent_down_schedules_within_limit(self):
        orch = _orchestrator(self._limited_settings(2))
        entry = {
            "ref": 1,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "retry_attempt": 0,
            "handle": _FakeHandle(),
        }
        with patch.object(orch, "_schedule_issue_retry") as schedule:
            orch._retry_agent_down("1", entry, "sess", "boom")
        schedule.assert_called_once()
        self.assertEqual(schedule.call_args.args[1], 1)
        self.assertNotIn("1", orch.blocked)

    def test_stall_blocks_at_retry_limit(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        settings.runtime.kind = "pi"
        settings.runtime.stall_timeout_ms = 1000
        handle = _FakeHandle()
        entry = {
            "ref": 1,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "retry_attempt": 2,
            "started_at": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=4000),
            "last_codex_timestamp": None,
            "handle": handle,
        }
        orch.running["1"] = entry
        orch.claimed.add("1")
        orch._reconcile_stalled_running_issues(settings)
        self.assertIn("1", orch.blocked)
        self.assertIn("retry limit exceeded", orch.blocked["1"]["error"])
        self.assertTrue(handle.cancelled)
        self.assertNotIn("1", orch.retry_attempts)

    def test_retry_limit_blocks_before_dispatch(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        orch._handle_active_retry(
            _issue("1"), 3, {"identifier": "GH-1", "error": "boom"}, settings)
        self.assertIn("1", orch.blocked)
        self.assertIn("retry limit exceeded", orch.blocked["1"]["error"])

    def test_continuation_is_not_blocked_by_retry_limit(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        orch.max_concurrent_agents = 1
        orch.running["other"] = {"issue": _issue("other")}
        with patch.object(orch, "_schedule_issue_retry") as schedule:
            orch._handle_active_retry(
                _issue("1"), 3,
                {"delay_type": "continuation", "identifier": "GH-1"},
                settings)
        schedule.assert_called_once()
        self.assertNotIn("1", orch.blocked)


class ReconciliationTest(unittest.TestCase):
    def _running(self, orch, handle):
        orch.running["1"] = {
            "ref": 1,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "handle": handle,
        }
        orch.claimed.add("1")

    def test_non_active_state_stops_running_agent(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        handle = _FakeHandle()
        self._running(orch, handle)
        orch._reconcile_issue_state(
            _issue("1", state="human review"),
            orch._active_state_set(settings),
            orch._terminal_state_set(settings),
            settings)
        self.assertTrue(handle.cancelled)
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)

    def test_terminal_state_stops_running_agent(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        handle = _FakeHandle()
        self._running(orch, handle)
        with patch.object(orch, "_cleanup_issue_workspace") as cleanup:
            orch._reconcile_issue_state(
                _issue("1", state="closed"),
                orch._active_state_set(settings),
                orch._terminal_state_set(settings),
                settings)
        cleanup.assert_called_once()
        self.assertTrue(handle.cancelled)
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)


class TokenDeltaTest(unittest.TestCase):
    def test_turn_completed_usage(self):
        orch = _orchestrator()
        entry = {}
        usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        delta = orch._extract_token_delta(entry, {
            "event": "notification",
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
            "payload": {"method": "turn/completed", "usage": usage},
        })
        self.assertEqual(delta["input_tokens"], 100)
        self.assertEqual(delta["output_tokens"], 50)
        # repeated absolute totals produce zero delta
        entry.update({
            "codex_last_reported_input_tokens": 100,
            "codex_last_reported_output_tokens": 50,
            "codex_last_reported_total_tokens": 150,
        })
        delta = orch._extract_token_delta(entry, {
            "event": "notification",
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
            "payload": {"method": "turn/completed", "usage": usage},
        })
        self.assertEqual(delta["total_tokens"], 0)


class StallTest(unittest.TestCase):
    def test_stall_restarts_with_retry(self):
        orch = _orchestrator()
        entry = {
            "ref": 1, "identifier": "GH-1", "issue": _issue(),
            "started_at": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=4000),
            "last_codex_timestamp": None,
            "handle": _FakeHandle(),
        }
        orch.running["1"] = entry
        orch.claimed.add("1")
        settings = orch._store.settings()
        settings.codex.stall_timeout_ms = 1000
        orch._reconcile_stalled_running_issues(settings)
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)
        self.assertIn("1", orch.retry_attempts)
        self.assertIn("stalled", orch.retry_attempts["1"]["error"])
        orch.retry_attempts["1"]["timer"].cancel()

    def test_pi_uses_runtime_stall_timeout(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        settings.runtime.kind = "pi"
        settings.runtime.stall_timeout_ms = 1_500_000
        settings.codex.stall_timeout_ms = 1_000
        orch.running["1"] = {
            "ref": 1,
            "identifier": "GH-1",
            "issue": _issue(),
            "started_at": datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=1_000),
            "last_codex_timestamp": None,
            "handle": _FakeHandle(),
        }
        orch.claimed.add("1")
        orch._reconcile_stalled_running_issues(settings)
        self.assertIn("1", orch.running)
        self.assertNotIn("1", orch.retry_attempts)


class _FakeHandle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class RunnerContinuationTest(unittest.TestCase):
    """The run loop ends on refreshed local state, not on cached data."""

    def _run_with_state(self, refreshed, active=True):
        from types import SimpleNamespace
        from symphony.agent import runner
        issue = _issue("1")
        if refreshed is not None and not active:
            refreshed.state = "Human Review"
        settings = SimpleNamespace(
            tracker=SimpleNamespace(
                active_states=["open", "todo", "in progress", "merging",
                               "rework"], required_labels=[]),
            runtime=SimpleNamespace(kind="pi"),
            agent=SimpleNamespace(max_turns=2),
            worker=SimpleNamespace(ssh_hosts=[]))
        done, turns = [], []
        with patch.object(runner.tracker_adapter, "begin_run_issue",
                          return_value="ok"), \
             patch.object(runner.tracker_adapter, "fetch_issues_by_ids",
                          return_value=[refreshed]
                          if refreshed is not None else []), \
             patch.object(runner.tracker_adapter, "routable",
                          return_value=True), \
             patch.object(runner, "_workflow_path", return_value=None), \
             patch.object(runner.workspace_mod, "create_for_issue",
                          return_value="ws"), \
             patch.object(runner.workspace_mod, "run_before_run",
                          return_value=None), \
             patch.object(runner.workspace_mod, "run_after_run",
                          return_value=None), \
             patch.object(runner, "_build_turn_prompt", return_value="p"), \
             patch.object(runner.pi_runtime, "start_session",
                          return_value="sess"), \
             patch.object(runner.pi_runtime, "stop_session",
                          return_value=None), \
             patch.object(runner.pi_runtime, "run_turn",
                          side_effect=lambda *a, **k:
                          turns.append(1) or {"session_id": "s1"}):
            runner.run(issue, settings, lambda *a: None,
                       lambda *a: done.append(a), runner.RunHandle())
        return done, turns

    def test_issue_gone_ends_after_one_turn(self):
        from symphony.agent import runner
        done, turns = self._run_with_state(None)
        self.assertEqual(done, [("1", runner.DONE_NORMAL)])
        self.assertEqual(len(turns), 1)

    def test_inactive_issue_ends_after_one_turn(self):
        from symphony.agent import runner
        done, turns = self._run_with_state(_issue("1"), active=False)
        self.assertEqual(done, [("1", runner.DONE_NORMAL)])
        self.assertEqual(len(turns), 1)

    def test_active_issue_continues_to_next_turn(self):
        done, turns = self._run_with_state(_issue("1"))
        self.assertEqual(len(turns), 2)  # max_turns=2, issue stays active
        self.assertEqual(done[0][1], "normal")


class SnapshotShapeTest(unittest.TestCase):
    def test_snapshot_keys(self):
        orch = _orchestrator()
        snapshot = orch._snapshot(None)
        for key in ("running", "retrying", "blocked", "codex_totals",
                    "rate_limits", "polling"):
            self.assertIn(key, snapshot)
        self.assertIn("input_tokens", snapshot["codex_totals"])
        self.assertIn("poll_interval_ms", snapshot["polling"])


if __name__ == "__main__":
    unittest.main()
