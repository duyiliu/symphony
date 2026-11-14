import datetime
import io
import logging
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, http_api, logging_setup, orchestrator
from symphony.workflow import WorkflowError


class HealthTest(unittest.TestCase):
    def setUp(self):
        cfg = config.parse_settings({
            "tracker": {"kind": "linear", "provider": {
                "api_key": "key", "project_slug": "project"},
                "active_states": ["Todo"], "terminal_states": ["Done"]}})
        self.store = SimpleNamespace(settings=lambda: cfg, force_reload=lambda: True)
        self.orch = orchestrator.Orchestrator(self.store)
        self.orch._thread = SimpleNamespace(is_alive=lambda: True)

    def test_failed_poll_is_visible_and_next_success_recovers(self):
        with patch.object(self.orch, "_schedule_tick") as schedule, \
                patch.object(orchestrator.tracker_adapter, "fetch_candidate_issues",
                             side_effect=[WorkflowError("linear_api_request"), []]):
            self.orch._on_run_poll_cycle()
            first = self.orch._snapshot(None)
            self.assertEqual(first["polling"]["last_error"], "linear_api_request")
            self.assertIsNone(first["polling"]["last_success_at"])
            self.orch._on_run_poll_cycle()
        self.assertEqual(schedule.call_count, 2)
        got = self.orch._snapshot(None)["polling"]
        self.assertEqual(got["count"], 2)
        self.assertIsNone(got["last_error"])
        self.assertIsNotNone(got["last_success_at"])

    def test_cached_snapshot_does_not_hide_dead_scheduler(self):
        self.orch._publish_snapshot()
        self.orch._thread = SimpleNamespace(is_alive=lambda: False)
        payload = http_api.state_payload(self.orch)
        self.assertEqual(payload["health"]["status"], "stopped")

    def test_old_snapshot_is_stale_even_with_old_success(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        snapshot = self.orch._snapshot(None)
        snapshot["published_at"] = now - datetime.timedelta(minutes=10)
        snapshot["polling"]["last_success_at"] = snapshot["published_at"]
        self.orch._published = snapshot
        payload = http_api.state_payload(self.orch)
        self.assertEqual(payload["health"]["status"], "stale")
        self.assertGreater(payload["health"]["snapshot_age_seconds"], 500)

    def test_current_error_is_not_reported_healthy(self):
        self.orch.last_poll_success_at = datetime.datetime.now(datetime.timezone.utc)
        self.orch.last_poll_error = "linear_api_request"
        self.orch._publish_snapshot()
        self.assertEqual(http_api.state_payload(self.orch)["health"]["status"], "degraded")

    def test_state_payload_includes_delivery_backoff_health(self):
        self.orch._publish_snapshot()
        self.orch.delivery_controller = SimpleNamespace(snapshot=lambda: {
            "enabled": True,
            "thread_alive": True,
            "deferred_count": 1,
            "global_backoff_remaining_ms": 60000,
        })
        payload = http_api.state_payload(self.orch)
        self.assertEqual(payload["delivery"]["deferred_count"], 1)
        self.assertEqual(
            payload["delivery"]["global_backoff_remaining_ms"], 60000)

    def test_worker_finish_queues_waiting_instead_of_terminal_result(self):
        reporter = Mock()
        self.orch.session_reporter = reporter
        issue = SimpleNamespace(id="one")
        self.orch._finish_agent_session(
            {"issue": issue}, orchestrator.agent_runner.DONE_NORMAL)
        reporter.worker_finished.assert_called_once_with(issue, True)


class FileLoggingTest(unittest.TestCase):
    def test_utf8_log_written_without_shell_redirection(self):
        root = logging.getLogger()
        previous, level = root.handlers[:], root.level
        root.handlers = []
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(sys, "stderr", io.StringIO()):
                path = os.path.join(tmp, "log", "main.log")
                logging_setup.setup("INFO", path)
                logging.getLogger("health-test").info("本机日志 %s", "ready")
                with open(path, encoding="utf-8") as handle:
                    self.assertIn("本机日志 ready", handle.read())
                for handler in root.handlers:
                    handler.close()
                root.handlers = []
        finally:
            root.handlers, root.level = previous, level


if __name__ == "__main__":
    unittest.main()
