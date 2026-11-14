import datetime
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, orchestrator  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


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
        self.path = "WORKFLOW.md"

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


class MetricsTest(unittest.TestCase):
    def test_dispatch_latency_is_recorded_once(self):
        orch = _orchestrator()
        orch._first_seen_at["1"] = orchestrator._now() - datetime.timedelta(
            seconds=2)

        orch._record_dispatch_latency("1")
        orch._record_dispatch_latency("1")

        self.assertEqual(orch.metrics["dispatch_latency_samples"], 1)
        self.assertGreaterEqual(orch.metrics["dispatch_latency_ms_last"], 1900)

    def test_run_metrics_are_recorded_once(self):
        orch = _orchestrator()
        entry = {
            "started_at": orchestrator._now() - datetime.timedelta(seconds=5),
        }

        orch._record_session_completion_totals(entry)
        orch._record_session_completion_totals(entry)

        self.assertEqual(orch.metrics["run_samples"], 1)
        self.assertGreaterEqual(orch.metrics["run_seconds_total"], 4)

    def test_snapshot_exposes_metrics_and_rates(self):
        orch = _orchestrator()
        orch.metrics["dispatch_count"] = 2
        orch.metrics["retry_count"] = 1
        orch.metrics["blocked_count"] = 1
        orch.running["1"] = {"termination_unconfirmed": True}

        snapshot = orch._snapshot(None)

        self.assertEqual(snapshot["metrics"]["dispatch"]["count"], 2)
        self.assertEqual(
            snapshot["metrics"]["rates"]["retry_per_dispatch"], 0.5)
        self.assertEqual(
            snapshot["metrics"]["rates"]["blocked_per_dispatch"], 0.5)
        self.assertEqual(snapshot["metrics"]["termination_unconfirmed"], 1)
        self.assertFalse(snapshot["paused"])

    def test_paused_scheduler_skips_tracker_fetch(self):
        orch = _orchestrator()
        orch.paused = True
        with patch.object(orch, "_reconcile_running_issues"), \
                patch.object(orch, "_reconcile_blocked_issues"), \
                patch.object(
                    orchestrator.tracker_adapter,
                    "fetch_candidate_issues") as fetch:
            orch._maybe_dispatch()
        fetch.assert_not_called()
        self.assertIsNotNone(orch.last_poll_success_at)

    def test_identifier_active_covers_running_retry_and_blocked(self):
        orch = _orchestrator()
        orch.running["1"] = {"identifier": "GH-1"}
        orch.retry_attempts["2"] = {"identifier": "GH-2"}
        orch.blocked["3"] = {"identifier": "GH-3"}
        self.assertTrue(orch._identifier_active("GH-1"))
        self.assertTrue(orch._identifier_active("GH-2"))
        self.assertTrue(orch._identifier_active("GH-3"))
        self.assertFalse(orch._identifier_active("GH-4"))


class UnconfirmedTerminationTest(unittest.TestCase):
    @staticmethod
    def _handle(force_result=True):
        return SimpleNamespace(
            force_terminate=lambda: force_result,
            termination_is_confirmed=lambda: force_result,
            release_confirmed_lease=lambda: None,
        )

    def test_recheck_releases_confirmed_orphan(self):
        orch = _orchestrator()
        orch.running["1"] = {
            "identifier": "GH-1",
            "termination_unconfirmed": True,
            "handle": self._handle(True),
            "stop_requested": {"kind": "release", "cleanup_workspace": False},
        }
        orch.claimed.add("1")

        orch._recheck_unconfirmed_terminations()

        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)

    def test_recheck_throttles_unconfirmed_orphan(self):
        orch = _orchestrator()
        calls = []
        handle = SimpleNamespace(
            force_terminate=lambda: calls.append(1) or False,
            termination_is_confirmed=lambda: False,
            release_confirmed_lease=lambda: None,
        )
        orch.running["1"] = {
            "identifier": "GH-1",
            "termination_unconfirmed": True,
            "handle": handle,
            "stop_requested": {"kind": "release", "cleanup_workspace": False},
        }

        orch._recheck_unconfirmed_terminations()
        orch._recheck_unconfirmed_terminations()

        self.assertEqual(len(calls), 1)
        self.assertIn("1", orch.running)


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

    def test_lease_failure_does_not_claim_or_start_worker(self):
        from symphony.agent import runner
        issue = _issue()
        done = []
        handle = runner.RunHandle()
        with patch.object(runner.workspace_mod, "acquire_run_lease",
                          return_value=None), \
                patch.object(runner.tracker_adapter, "begin_run_issue") as begin, \
                patch.object(runner, "_run_on_worker_host") as worker:
            runner.run(
                issue, _settings(), lambda *args: None,
                lambda *args: done.append(args), handle)
        begin.assert_not_called()
        worker.assert_not_called()
        self.assertEqual(done, [(issue.id, runner.DONE_DEFERRED)])
        self.assertEqual(handle.deferred_policy, "wait")

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

    def test_control_backoff_is_bounded_only_for_temporary_failures(self):
        settings = _settings()
        orch = _orchestrator(settings)
        bounded = {
            "delay_type": "control",
            "control_policy": "bounded",
            "bounded_attempt": 1,
        }
        self.assertEqual(orch._retry_delay(2, bounded, settings), 10000)
        bounded["bounded_attempt"] = 2
        self.assertEqual(orch._retry_delay(2, bounded, settings), 20000)
        bounded["bounded_attempt"] = 5
        self.assertEqual(orch._retry_delay(2, bounded, settings), 30000)
        waiting = {
            "delay_type": "control",
            "control_policy": "wait",
            "wait_attempt": 100,
        }
        self.assertEqual(orch._retry_delay(2, waiting, settings), 10000)


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

    def test_capacity_deferral_does_not_advance_attempt_or_block(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        orch.max_concurrent_agents = 1
        orch.running["other"] = {"issue": _issue("other")}
        with patch.object(orch, "_schedule_issue_retry") as schedule, \
                patch.object(orch, "_spawn_issue") as spawn:
            orch._handle_active_retry(
                _issue("1"), 3,
                {"identifier": "GH-1", "error": "agent exited"},
                settings)
        schedule.assert_called_once()
        self.assertEqual(schedule.call_args.args[1], 3)
        self.assertEqual(
            schedule.call_args.args[2]["delay_type"], "control")
        self.assertEqual(
            schedule.call_args.args[2]["control_policy"], "wait")
        spawn.assert_not_called()
        self.assertNotIn("1", orch.blocked)

    def test_worker_capacity_deferral_keeps_attempt(self):
        settings = _settings(
            agent={
                "max_concurrent_agents": 2,
                "max_retry_backoff_ms": 300000,
                "max_retry_attempts": 2,
            },
            worker={
                "ssh_hosts": ["worker-a"],
                "max_concurrent_agents_per_host": 1,
            })
        orch = _orchestrator(settings)
        orch.running["other"] = {
            "issue": _issue("other"), "worker_host": "worker-a"}
        with patch.object(orch, "_schedule_issue_retry") as schedule, \
                patch.object(orch, "_spawn_issue") as spawn:
            orch._handle_active_retry(
                _issue("1"), 2,
                {"identifier": "GH-1", "worker_host": "worker-a"},
                settings)
        self.assertEqual(schedule.call_args.args[1], 2)
        self.assertEqual(schedule.call_args.args[2]["delay_type"], "control")
        self.assertEqual(
            schedule.call_args.args[2]["control_policy"], "wait")
        spawn.assert_not_called()
        self.assertNotIn("1", orch.blocked)

    def test_refresh_and_poll_failures_do_not_advance_attempt(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        orch.running.clear()
        metadata = {"identifier": "GH-1", "error": "agent exited"}
        with patch.object(orch, "_schedule_issue_retry") as schedule, \
                patch.object(orch, "_refresh_issue_for_dispatch",
                             return_value=("error", RuntimeError("temporary"))):
            orch._handle_active_retry(_issue("1"), 2, metadata, settings)
        self.assertEqual(schedule.call_args.args[1], 2)
        self.assertEqual(schedule.call_args.args[2]["delay_type"], "control")

        orch.retry_attempts["1"] = {
            "attempt": 2,
            "retry_token": "token",
            "identifier": "GH-1",
            "delay_type": "control",
        }
        with patch.object(orch, "_schedule_issue_retry") as schedule, \
                patch.object(orchestrator.tracker_adapter,
                             "fetch_issues_by_ids",
                             side_effect=RuntimeError("tracker offline")):
            orch._on_retry_issue("1", "token")
        self.assertEqual(schedule.call_args.args[1], 2)
        self.assertEqual(schedule.call_args.args[2]["delay_type"], "control")
        self.assertNotIn("1", orch.blocked)

    def test_thread_start_failure_is_control_deferral(self):
        settings = self._limited_settings(1)
        orch = _orchestrator(settings)
        with patch.object(orch, "_schedule_issue_retry") as schedule, \
                patch.object(orchestrator.threading.Thread, "start",
                             side_effect=RuntimeError("thread unavailable")):
            orch._spawn_issue(_issue("1"), settings, 1, None)
        self.assertEqual(schedule.call_args.args[1], 1)
        self.assertEqual(schedule.call_args.args[2]["delay_type"], "control")
        self.assertNotIn("1", orch.blocked)
        self.assertIn("1", orch.claimed)
        self.assertNotIn("1", orch.running)
        self.assertEqual(
            schedule.call_args.args[2]["control_policy"], "bounded")

    def test_session_completion_seconds_are_recorded_once(self):
        orch = _orchestrator()
        started = datetime.datetime(
            2026, 11, 14, tzinfo=datetime.timezone.utc)
        entry = {"started_at": started}
        with patch.object(
                orchestrator, "_now",
                return_value=started + datetime.timedelta(seconds=17)):
            orch._record_session_completion_totals(entry)
            orch._record_session_completion_totals(entry)
        self.assertEqual(orch.codex_totals["seconds_running"], 17)
        self.assertTrue(entry["session_completion_totals_recorded"])

    def test_fast_worker_done_is_not_lost(self):
        from symphony.agent import runner
        settings = self._limited_settings(1)
        orch = _orchestrator(settings)
        issue = _issue("1")

        def run_immediately(_issue, _settings, _on_update, on_done, _handle,
                            opts=None):
            on_done(_issue.id, runner.DONE_SKIPPED)

        def start_immediately(thread):
            thread._target()

        with patch.object(orchestrator.threading.Thread, "start",
                          new=start_immediately), \
                patch.object(orchestrator.agent_runner, "run",
                             side_effect=run_immediately):
            orch._spawn_issue(issue, settings, 1, None)

        self.assertIn(issue.id, orch.running)
        kind, issue_id, ref, reason = orch._mailbox.get_nowait()
        self.assertEqual((kind, issue_id, reason),
                         ("worker_done", issue.id, runner.DONE_SKIPPED))
        orch._on_worker_done(issue_id, ref, reason)
        self.assertNotIn(issue.id, orch.running)
        self.assertNotIn(issue.id, orch.claimed)

    def test_control_deferrals_have_a_separate_bounded_limit(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        metadata = {
            "identifier": "GH-1",
            "issue": _issue("1"),
            "error": "temporary tracker outage",
        }
        with patch.object(orchestrator, "CONTROL_DEFERRAL_LIMIT", 2):
            self.assertTrue(
                orch._schedule_control_retry("1", 0, metadata, settings))
            self.assertTrue(
                orch._schedule_control_retry("1", 0, metadata, settings))
            self.assertFalse(
                orch._schedule_control_retry("1", 0, metadata, settings))
        self.assertIn("1", orch.blocked)
        self.assertIn("control-plane deferral limit exceeded",
                      orch.blocked["1"]["error"])
        self.assertNotIn("max_retry_attempts", orch.blocked["1"]["error"])
        self.assertNotIn("1", orch.retry_attempts)

    def test_bounded_control_state_survives_redispatch(self):
        from symphony.agent import runner
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        issue = _issue("1")
        with patch.object(orchestrator.threading.Thread, "start"):
            orch._spawn_issue(
                issue, settings, 1, None,
                control_state={
                    "control_attempt": 2,
                    "bounded_attempt": 2,
                    "wait_attempt": 0,
                })
        entry = orch.running["1"]
        self.assertEqual(entry["bounded_attempt"], 2)
        handle = entry["handle"]
        handle.defer(
            "linear_api_request: offline",
            retryable=True,
            code="linear_api_request")
        with patch.object(orchestrator, "CONTROL_DEFERRAL_LIMIT", 2):
            orch._on_worker_done("1", entry["ref"], runner.DONE_DEFERRED)
        self.assertIn("1", orch.blocked)
        self.assertIn("control-plane deferral limit exceeded",
                      orch.blocked["1"]["error"])

    def test_capacity_wait_past_control_limit_still_queues_other_issue(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        orch.max_concurrent_agents = 1
        orch.running["occupied"] = {"issue": _issue("occupied")}
        metadata = {
            "identifier": "GH-1",
            "issue": _issue("1"),
            "error": "no available orchestrator slots",
        }
        for _index in range(31):
            orch._handle_active_retry(_issue("1"), 2, metadata, settings)
        self.assertNotIn("1", orch.blocked)
        retry = orch.retry_attempts["1"]
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["control_policy"], "wait")
        self.assertEqual(retry["wait_attempt"], 31)
        self.assertEqual(retry["bounded_attempt"], 0)
        retry["timer"].cancel()

        orch.running.clear()
        with patch.object(orch, "_dispatch_issue") as dispatch:
            orch._choose_issues([_issue("1"), _issue("2")], settings)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0].id, "2")

    def test_permanent_prestart_error_blocks_without_control_retry(self):
        from symphony.agent import runner
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        issue = _issue("1")
        handle = runner.RunHandle()
        handle.defer(
            "missing_pi_command: None",
            retryable=False,
            code="missing_pi_command")
        orch.running["1"] = {
            "ref": 1,
            "handle": handle,
            "identifier": issue.identifier,
            "issue": issue,
        }
        orch.claimed.add("1")
        with patch.object(orch, "_schedule_control_retry") as schedule:
            orch._on_worker_done("1", 1, runner.DONE_DEFERRED)
        schedule.assert_not_called()
        self.assertIn("1", orch.blocked)
        self.assertIn("blocked before start", orch.blocked["1"]["error"])
        self.assertEqual(
            orch.blocked["1"]["deferred_code"], "missing_pi_command")

    def test_turn_input_required_blocks_instead_of_retrying(self):
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        issue = _issue("1")
        orch.running["1"] = {
            "ref": 1,
            "identifier": issue.identifier,
            "issue": issue,
            "session_id": "session-1",
            "last_codex_event": "turn_input_required",
            "last_codex_message": {
                "event": "turn_input_required",
                "message": {"method": "extension_ui_request"},
            },
            "last_codex_timestamp": datetime.datetime.now(
                datetime.timezone.utc),
        }
        orch.claimed.add("1")

        with patch.object(orch, "_schedule_issue_retry") as retry:
            orch._on_worker_done("1", 1, "turn_input_required")

        retry.assert_not_called()
        self.assertIn("1", orch.blocked)
        self.assertIn("operator input", orch.blocked["1"]["error"])
        self.assertEqual(
            orch.blocked["1"]["last_codex_event"], "turn_input_required")

    def test_live_run_lease_deferral_uses_wait_policy(self):
        from symphony.agent import runner
        settings = self._limited_settings(2)
        orch = _orchestrator(settings)
        issue = _issue("1")
        handle = runner.RunHandle()
        handle.defer(
            "workspace run lease is held by another live process",
            retryable=True,
            code="run_lease_held",
            wait=True)
        orch.running["1"] = {
            "ref": 1,
            "handle": handle,
            "identifier": issue.identifier,
            "issue": issue,
        }
        with patch.object(orch, "_schedule_control_retry") as schedule:
            orch._on_worker_done("1", 1, runner.DONE_DEFERRED)
        self.assertEqual(schedule.call_args.kwargs["policy"], "wait")
        self.assertNotIn("1", orch.blocked)

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


class StopLifecycleTest(unittest.TestCase):
    def test_timeout_forces_pi_and_cleanup_runs_after_runner_exit(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        forced = threading.Event()

        class Handle:
            cancelled = False

            def cancel(self):
                self.cancelled = True

            def force_terminate(self):
                forced.set()
                return True

            def termination_is_confirmed(self):
                return True

        handle = Handle()
        thread = threading.Thread(target=lambda: forced.wait(2), daemon=True)
        thread.start()
        entry = {
            "ref": 1,
            "thread": thread,
            "handle": handle,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "started_at": datetime.datetime.now(datetime.timezone.utc),
        }
        orch.running["1"] = entry
        orch.claimed.add("1")

        def assert_exited_then_cleanup(*_args, **_kwargs):
            self.assertFalse(thread.is_alive())
            self.assertIn("1", orch.running)
            self.assertIn("1", orch.claimed)

        with patch.object(orchestrator, "STOP_GRACE_S", 0.01), \
                patch.object(orchestrator, "STOP_FORCE_WAIT_S", 1.0), \
                patch.object(orch, "_cleanup_issue_workspace",
                             side_effect=assert_exited_then_cleanup):
            self.assertTrue(
                orch._terminate_running_issue("1", True, settings))
        self.assertTrue(handle.cancelled)
        self.assertTrue(forced.is_set())
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)

    def test_exited_runner_rechecks_unconfirmed_pi_termination(self):
        orch = _orchestrator()
        settings = orch._store.settings()

        class Handle:
            def __init__(self):
                self.force_calls = 0
                self.confirmed = False

            def cancel(self):
                pass

            def force_terminate(self):
                self.force_calls += 1
                self.confirmed = True
                return True

            def termination_is_confirmed(self):
                return self.confirmed

        handle = Handle()
        thread = threading.Thread(target=lambda: None, daemon=True)
        thread.start()
        thread.join()
        entry = {
            "ref": 1,
            "thread": thread,
            "handle": handle,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "termination_unconfirmed": True,
            "started_at": datetime.datetime.now(datetime.timezone.utc),
        }
        orch.running["1"] = entry
        orch.claimed.add("1")

        self.assertTrue(
            orch._terminate_running_issue("1", False, settings))
        self.assertEqual(handle.force_calls, 1)
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
        self.assertIn("1", orch.claimed)
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

    def has_started(self):
        return True

    def force_terminate(self):
        return True

    def termination_is_confirmed(self):
        return True


class _FakeLease:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


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
            worker=SimpleNamespace(ssh_hosts=[]),
            workspace=SimpleNamespace(root="unused"))
        done, turns = [], []
        with patch.object(runner.tracker_adapter, "begin_run_issue",
                          return_value="ok"), \
             patch.object(runner.tracker_adapter, "fetch_issues_by_ids",
                          return_value=[refreshed]
                          if refreshed is not None else []), \
             patch.object(runner.tracker_adapter, "routable",
                          return_value=True), \
             patch.object(runner, "_workflow_path", return_value=None), \
             patch.object(runner.workspace_mod, "acquire_run_lease",
                          return_value=_FakeLease()), \
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
                          return_value=True), \
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


class RunnerDeferralTest(unittest.TestCase):
    def test_only_temporary_http_statuses_are_retryable(self):
        from symphony.agent import runner
        self.assertTrue(runner._deferral_is_retryable(
            WorkflowError(
                "linear_api_http_error", {"status": 503})))
        self.assertTrue(runner._deferral_is_retryable(
            WorkflowError(
                "linear_api_http_error", {"status": 429})))
        self.assertFalse(runner._deferral_is_retryable(
            WorkflowError(
                "linear_api_http_error", {"status": 401})))
        self.assertFalse(runner._deferral_is_retryable(
            WorkflowError("unsupported_runtime_kind", "codex")))

    def test_temporary_tracker_error_is_retryable(self):
        from symphony.agent import runner
        issue = _issue("1")
        done = []
        handle = runner.RunHandle()
        lease = _FakeLease()
        with patch.object(
                runner.workspace_mod, "acquire_run_lease",
                return_value=lease), \
                patch.object(
                    runner.tracker_adapter, "begin_run_issue",
                    side_effect=WorkflowError(
                        "linear_api_request", "offline")):
            runner.run(
                issue, _settings(), lambda *args: None,
                lambda *args: done.append(args), handle)
        self.assertEqual(done, [(issue.id, runner.DONE_DEFERRED)])
        self.assertTrue(handle.deferred_retryable)
        self.assertEqual(handle.deferred_policy, "bounded")
        self.assertEqual(handle.deferred_code, "linear_api_request")
        self.assertTrue(lease.released)

    def test_missing_runtime_command_is_permanent(self):
        from symphony.agent import runner
        issue = _issue("1")
        settings = _settings()
        settings.runtime.kind = "pi"
        done = []
        handle = runner.RunHandle()
        lease = _FakeLease()
        with patch.object(
                runner.workspace_mod, "acquire_run_lease",
                return_value=lease), \
                patch.object(
                    runner.tracker_adapter, "begin_run_issue",
                    return_value="ok"), \
                patch.object(
                    runner.workspace_mod, "create_for_issue",
                    return_value="workspace"), \
                patch.object(
                    runner.workspace_mod, "run_before_run",
                    return_value="workspace"), \
                patch.object(
                    runner.workspace_mod, "run_after_run",
                    return_value=None), \
                patch.object(
                    runner.pi_runtime, "start_session",
                    side_effect=WorkflowError("missing_pi_command")):
            runner.run(
                issue, settings, lambda *args: None,
                lambda *args: done.append(args), handle)
        self.assertEqual(done, [(issue.id, runner.DONE_DEFERRED)])
        self.assertFalse(handle.deferred_retryable)
        self.assertEqual(handle.deferred_policy, "permanent")
        self.assertEqual(handle.deferred_code, "missing_pi_command")
        self.assertTrue(lease.released)

    def test_post_popen_setup_failure_consumes_execution_budget(self):
        from symphony.agent import runner
        issue = _issue("1")
        settings = _settings()
        settings.runtime.kind = "pi"
        done = []
        handle = runner.RunHandle()
        lease = _FakeLease()
        with patch.object(
                runner.workspace_mod, "acquire_run_lease",
                return_value=lease), \
                patch.object(
                    runner.tracker_adapter, "begin_run_issue",
                    return_value="ok"), \
                patch.object(
                    runner.workspace_mod, "create_for_issue",
                    return_value="workspace"), \
                patch.object(
                    runner.workspace_mod, "run_before_run",
                    return_value="workspace"), \
                patch.object(
                    runner.workspace_mod, "run_after_run",
                    return_value=None), \
                patch.object(
                    runner.pi_runtime, "start_session",
                    side_effect=runner.pi_runtime.PiStartupError(
                        "pi_started_then_failed", "lease write failed")), \
                patch.object(
                    runner.tracker_adapter, "record_issue_failure"):
            runner.run(
                issue, settings, lambda *args: None,
                lambda *args: done.append(args), handle)
        self.assertEqual(len(done), 1)
        self.assertNotEqual(done[0][1], runner.DONE_DEFERRED)
        self.assertIn("pi_started_then_failed", done[0][1])
        self.assertTrue(handle.has_started())

    def test_post_start_temporary_refresh_failure_is_control_deferral(self):
        from types import SimpleNamespace
        from symphony.agent import runner
        issue = _issue("1")
        settings = SimpleNamespace(
            tracker=SimpleNamespace(
                active_states=["open"], required_labels=[]),
            runtime=SimpleNamespace(kind="pi"),
            agent=SimpleNamespace(max_turns=1),
            worker=SimpleNamespace(ssh_hosts=[]),
            workspace=SimpleNamespace(root="unused"))
        done = []
        handle = runner.RunHandle()
        lease = _FakeLease()
        with patch.object(
                runner.workspace_mod, "acquire_run_lease",
                return_value=lease), \
                patch.object(
                    runner.tracker_adapter, "begin_run_issue",
                    return_value="ok"), \
                patch.object(
                    runner.workspace_mod, "create_for_issue",
                    return_value="workspace"), \
                patch.object(
                    runner.workspace_mod, "run_before_run",
                    return_value=None), \
                patch.object(
                    runner.workspace_mod, "run_after_run",
                    return_value=None), \
                patch.object(
                    runner, "_build_turn_prompt",
                    return_value="prompt"), \
                patch.object(
                    runner.pi_runtime, "start_session",
                    return_value="session"), \
                patch.object(
                    runner.pi_runtime, "stop_session",
                    return_value=True) as stop_session, \
                patch.object(
                    runner.pi_runtime, "run_turn",
                    return_value={"session_id": "s1"}), \
                patch.object(
                    runner.tracker_adapter, "fetch_issues_by_ids",
                    side_effect=WorkflowError(
                        "linear_api_request", "offline")), \
                patch.object(
                    runner.tracker_adapter,
                    "record_issue_failure") as record_failure:
            runner.run(
                issue, settings, lambda *args: None,
                lambda *args: done.append(args), handle)
        self.assertEqual(done, [(issue.id, runner.DONE_DEFERRED)])
        self.assertTrue(handle.has_started())
        self.assertTrue(handle.deferred_retryable)
        self.assertEqual(handle.deferred_policy, "bounded")
        self.assertEqual(handle.deferred_code, "linear_api_request")
        self.assertTrue(lease.released)
        stop_session.assert_called_once_with("session")
        record_failure.assert_not_called()
        self.assertTrue(lease.released)

    def test_successful_stop_restores_termination_confirmation(self):
        from symphony.agent import runner

        issue = _issue("1")
        settings = _settings()
        settings.runtime.kind = "pi"
        handle = runner.RunHandle()
        lease = _FakeLease()
        session = object()
        handle.set_lease(lease)
        handle.mark_termination_unconfirmed()

        with patch.object(
                runner.pi_runtime, "start_session",
                return_value=session), \
                patch.object(
                    runner.pi_runtime, "stop_session",
                    return_value=True), \
                patch.object(
                    runner, "_do_run_turns",
                    return_value=runner.DONE_NORMAL):
            outcome = runner._run_agent_turns(
                "workspace", issue, settings, lambda *args: None,
                handle, None, None)

        self.assertEqual(outcome, runner.DONE_NORMAL)
        self.assertIsNone(handle.session)
        self.assertTrue(handle.termination_is_confirmed())
        handle.release_confirmed_lease()
        self.assertTrue(lease.released)


class ReconcileParityTest(unittest.TestCase):
    def _running(self, orch, handle=None):
        handle = handle or _FakeHandle()
        orch.running["1"] = {
            "ref": 1,
            "identifier": "GH-1",
            "issue": _issue("1"),
            "handle": handle,
            "workspace_path": "/recorded/GH-1",
            "worker_host": None,
        }
        orch.claimed.add("1")
        return handle

    def test_reassigned_issue_stops_without_cleanup(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        handle = self._running(orch)
        reassigned = _issue("1")
        reassigned.dispatchable = False
        with patch.object(orch, "_cleanup_issue_workspace") as cleanup:
            orch._reconcile_issue_state(
                reassigned, orch._active_state_set(settings),
                orch._terminal_state_set(settings), settings)
        self.assertTrue(handle.cancelled)
        cleanup.assert_not_called()
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)

    def test_required_label_removed_stops_running_agent(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        handle = self._running(orch)
        with patch.object(orch, "_cleanup_issue_workspace") as cleanup:
            orch._reconcile_issue_state(
                _issue("1", labels=()),
                orch._active_state_set(settings),
                orch._terminal_state_set(settings), settings)
        self.assertTrue(handle.cancelled)
        cleanup.assert_not_called()
        self.assertNotIn("1", orch.claimed)

    def test_required_label_removed_releases_blocked_issue(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        orch.blocked["1"] = {"issue": _issue("1")}
        orch.claimed.add("1")
        orch._reconcile_blocked_issue_state(
            _issue("1", labels=()),
            orch._active_state_set(settings),
            orch._terminal_state_set(settings), settings)
        self.assertNotIn("1", orch.blocked)
        self.assertNotIn("1", orch.claimed)

    def test_retry_releases_claim_when_label_removed(self):
        orch = _orchestrator()
        orch.claimed.add("1")
        orch.retry_attempts["1"] = {
            "attempt": 1, "retry_token": "tok", "timer": None,
            "identifier": "GH-1",
        }
        with patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                return_value=[_issue("1", labels=())]):
            orch._on_retry_issue("1", "tok")
        self.assertNotIn("1", orch.claimed)
        self.assertNotIn("1", orch.retry_attempts)

    def test_retry_releases_claim_when_issue_disappears(self):
        orch = _orchestrator()
        orch.claimed.add("1")
        orch.retry_attempts["1"] = {
            "attempt": 1, "retry_token": "tok", "timer": None,
            "identifier": "GH-1",
        }
        with patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                return_value=[]):
            orch._on_retry_issue("1", "tok")
        self.assertNotIn("1", orch.claimed)
        self.assertNotIn("1", orch.retry_attempts)

    def test_stale_retry_token_is_ignored(self):
        orch = _orchestrator()
        orch.claimed.add("1")
        orch.retry_attempts["1"] = {
            "attempt": 1, "retry_token": "new", "timer": None,
            "identifier": "GH-1",
        }
        with patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids") as fetch:
            orch._on_retry_issue("1", "old")
        fetch.assert_not_called()
        self.assertIn("1", orch.retry_attempts)
        self.assertIn("1", orch.claimed)

    def test_manual_refresh_coalesces_repeated_requests(self):
        orch = _orchestrator()
        orch.next_poll_due_at_ms = None
        with patch.object(orch, "_schedule_tick") as tick:
            first = orch._request_refresh(None)
        self.assertFalse(first["coalesced"])
        tick.assert_called_once_with(0)

        orch.poll_check_in_progress = True
        with patch.object(orch, "_schedule_tick") as tick:
            second = orch._request_refresh(None)
        self.assertTrue(second["coalesced"])
        tick.assert_not_called()

        orch.poll_check_in_progress = False
        orch.next_poll_due_at_ms = orchestrator._monotonic_ms() - 1
        with patch.object(orch, "_schedule_tick") as tick:
            third = orch._request_refresh(None)
        self.assertTrue(third["coalesced"])
        tick.assert_not_called()

    def test_terminal_cleanup_uses_recorded_workspace(self):
        orch = _orchestrator()
        settings = orch._store.settings()
        lease = _FakeLease()
        with patch.object(
                orchestrator.workspace_mod, "acquire_path_run_lease",
                return_value=lease), \
                patch.object(
                    orchestrator.workspace_mod, "remove_recorded",
                    return_value=True) as remove:
            result = orch._cleanup_issue_workspace(
                _issue("1"), {"workspace_path": "/recorded/GH-1"}, settings)
        self.assertTrue(result)
        self.assertEqual(remove.call_args.args[1], "/recorded/GH-1")
        self.assertTrue(lease.released)


class WorkerHostSelectionTest(unittest.TestCase):
    def _settings(self, hosts, limit=1):
        return _settings(worker={
            "ssh_hosts": hosts,
            "max_concurrent_agents_per_host": limit,
        })

    def test_skips_full_hosts_under_shared_cap(self):
        settings = self._settings(["h1", "h2"])
        orch = _orchestrator(settings)
        orch.running["a"] = {"issue": _issue("a"), "worker_host": "h1"}
        self.assertEqual(orch._select_worker_host(settings, None), "h2")

    def test_returns_no_capacity_when_every_host_is_full(self):
        settings = self._settings(["h1"])
        orch = _orchestrator(settings)
        orch.running["a"] = {"issue": _issue("a"), "worker_host": "h1"}
        self.assertEqual(
            orch._select_worker_host(settings, None), "no_worker_capacity")

    def test_keeps_preferred_host_when_it_has_capacity(self):
        settings = self._settings(["h1", "h2"])
        orch = _orchestrator(settings)
        self.assertEqual(orch._select_worker_host(settings, "h2"), "h2")

    def test_per_state_concurrency_limit(self):
        settings = _settings(agent={
            "max_concurrent_agents": 4,
            "max_concurrent_agents_by_state": {"open": 1},
        })
        orch = _orchestrator(settings)
        orch.running["a"] = {"issue": _issue("a")}
        self.assertFalse(orch._state_slots_available(_issue("b"), settings))
        orch.running["a"]["issue"] = _issue("a", state="closed")
        self.assertTrue(orch._state_slots_available(_issue("b"), settings))


class TokenAccountingParityTest(unittest.TestCase):
    def _running(self, orch):
        orch.running["1"] = {
            "ref": 1,
            "issue": _issue("1"),
            "identifier": "GH-1",
            "codex_input_tokens": 0,
            "codex_output_tokens": 0,
            "codex_total_tokens": 0,
        }
        return orch.running["1"]

    @staticmethod
    def _token_update(info):
        return {
            "event": "notification",
            "timestamp": "2026-11-14T00:00:00Z",
            "payload": {
                "method": "codex/event/token_count",
                "params": {"msg": {"type": "token_count", "info": info}},
            },
        }

    def test_cumulative_token_usage_is_monotonic(self):
        orch = _orchestrator()
        self._running(orch)
        orch._on_worker_update("1", self._token_update({
            "total_token_usage": {
                "input_tokens": "2", "output_tokens": 2, "total_tokens": 4,
            },
        }))
        orch._on_worker_update("1", self._token_update({
            "total_token_usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            },
        }))
        self.assertEqual(orch.running["1"]["codex_input_tokens"], 10)
        self.assertEqual(orch.running["1"]["codex_output_tokens"], 5)
        self.assertEqual(orch.running["1"]["codex_total_tokens"], 15)
        self.assertEqual(orch.codex_totals["total_tokens"], 15)

    def test_total_token_usage_is_preferred_over_last(self):
        orch = _orchestrator()
        self._running(orch)
        orch._on_worker_update("1", {
            "event": "notification",
            "timestamp": "2026-11-14T00:00:00Z",
            "payload": {
                "method": "codex/event/token_count",
                "params": {"msg": {
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "total_tokens": 3,
                        },
                        "total_token_usage": {
                            "input_tokens": 200,
                            "output_tokens": 100,
                            "total_tokens": 300,
                        },
                    }},
                }},
            },
        })
        self.assertEqual(orch.running["1"]["codex_total_tokens"], 300)

    def test_last_token_usage_without_cumulative_is_ignored(self):
        orch = _orchestrator()
        self._running(orch)
        orch._on_worker_update("1", self._token_update({
            "last_token_usage": {
                "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            },
        }))
        self.assertEqual(orch.running["1"]["codex_total_tokens"], 0)
        self.assertEqual(orch.codex_totals["total_tokens"], 0)

    def test_rate_limit_payload_is_exposed_in_snapshot(self):
        orch = _orchestrator()
        self._running(orch)
        rate_limits = {
            "limit_id": "codex",
            "primary": {"remaining": 90, "limit": 100},
            "secondary": None,
            "credits": {
                "has_credits": False, "unlimited": False, "balance": None,
            },
        }
        orch._on_worker_update("1", {
            "event": "notification",
            "timestamp": "2026-11-14T00:00:00Z",
            "payload": {
                "method": "codex/event/token_count",
                "params": {"msg": {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count", "rate_limits": rate_limits,
                    },
                }},
            },
        })
        self.assertEqual(orch._snapshot(None)["rate_limits"], rate_limits)


class SnapshotShapeTest(unittest.TestCase):
    def test_snapshot_keys(self):
        orch = _orchestrator()
        snapshot = orch._snapshot(None)
        for key in ("running", "retrying", "blocked", "codex_totals",
                    "rate_limits", "polling"):
            self.assertIn(key, snapshot)
        self.assertIn("input_tokens", snapshot["codex_totals"])
        self.assertIn("poll_interval_ms", snapshot["polling"])


class RunningRefreshTest(unittest.TestCase):
    def _running(self, orch):
        handle = _FakeHandle()
        orch.running["1"] = {
            "issue_id": "1",
            "identifier": "GH-1",
            "issue": _issue("1"),
            "handle": handle,
            "thread": None,
            "stop_requested": None,
        }
        orch.claimed.add("1")
        return handle

    def test_missing_running_issue_is_stopped_without_cleanup(self):
        orch = _orchestrator()
        handle = self._running(orch)
        with patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                return_value=[]), \
                patch.object(orch, "_cleanup_issue_workspace") as cleanup:
            orch._reconcile_running_issues()
        self.assertTrue(handle.cancelled)
        self.assertNotIn("1", orch.running)
        self.assertNotIn("1", orch.claimed)
        cleanup.assert_not_called()

    def test_refresh_failure_keeps_running_worker(self):
        orch = _orchestrator()
        handle = self._running(orch)
        with patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                side_effect=RuntimeError("tracker down")):
            orch._reconcile_running_issues()
        self.assertFalse(handle.cancelled)
        self.assertIn("1", orch.running)

    def test_active_issue_refreshes_running_entry(self):
        orch = _orchestrator()
        self._running(orch)
        refreshed = _issue("1", state="open")
        with patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                return_value=[refreshed]):
            orch._reconcile_running_issues()
        self.assertIs(orch.running["1"]["issue"], refreshed)


class DispatchRevalidationTest(unittest.TestCase):
    def _dispatch(self, tracker_result):
        orch = _orchestrator()
        if isinstance(tracker_result, Exception):
            patcher = patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                side_effect=tracker_result)
        else:
            patcher = patch.object(
                orchestrator.tracker_adapter, "fetch_issues_by_ids",
                return_value=tracker_result)
        with patcher, patch.object(orch, "_spawn_issue") as spawn:
            orch._dispatch_issue(_issue("1"), orch._store.settings())
        return spawn

    def test_missing_issue_is_not_dispatched(self):
        self._dispatch([]).assert_not_called()

    def test_terminal_issue_is_not_dispatched(self):
        self._dispatch([_issue("1", state="closed")]).assert_not_called()

    def test_refresh_error_is_not_dispatched(self):
        self._dispatch(RuntimeError("tracker down")).assert_not_called()

    def test_refreshed_issue_is_dispatched(self):
        refreshed = _issue("1", state="open")
        spawn = self._dispatch([refreshed])
        spawn.assert_called_once()
        self.assertIs(spawn.call_args.args[0], refreshed)


if __name__ == "__main__":
    unittest.main()
