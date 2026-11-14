"""Observability HTTP API parity tests (state/issue/refresh + errors)."""

import datetime
import http.client
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import http_api  # noqa: E402


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _running_entry():
    return {
        "issue_id": "issue-1",
        "identifier": "GH-1",
        "issue_url": "https://example.test/GH-1",
        "state": "open",
        "worker_host": "worker-a",
        "workspace_path": "/tmp/workspaces/GH-1",
        "session_id": "session-1",
        "turn_count": 2,
        "last_codex_event": "turn_completed",
        "last_codex_message": {
            "event": "turn_completed",
            "message": {"method": "turn/completed"},
        },
        "started_at": _now() - datetime.timedelta(seconds=30),
        "last_codex_timestamp": _now(),
        "codex_input_tokens": 100,
        "codex_output_tokens": 20,
        "codex_total_tokens": 120,
    }


def _blocked_entry():
    return {
        "issue_id": "issue-2",
        "identifier": "GH-2",
        "issue_url": "https://example.test/GH-2",
        "state": "open",
        "error": "codex turn requires operator input",
        "worker_host": None,
        "workspace_path": "/tmp/workspaces/GH-2",
        "session_id": "session-2",
        "blocked_at": _now(),
        "last_codex_event": "turn_input_required",
        "last_codex_message": None,
        "last_codex_timestamp": _now(),
    }


def _snapshot():
    return {
        "running": [_running_entry()],
        "retrying": [],
        "blocked": [_blocked_entry()],
        "codex_totals": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "seconds_running": 30,
        },
        "rate_limits": None,
        "polling": {
            "poll_interval_ms": 30000,
            "last_error": None,
            "last_success_at": _now(),
        },
        "published_at": _now(),
        "scheduler_alive": True,
    }


class _FakeOrchestrator:
    def __init__(self, snapshot=None, refresh=None):
        self._snapshot = snapshot if snapshot is not None else _snapshot()
        self._refresh = (
            refresh if refresh is not None
            else {"queued": True, "coalesced": False,
                  "requested_at": _now()})
        self.paused = False

    def snapshot(self, _timeout):
        return self._snapshot

    def request_refresh(self):
        return self._refresh

    def control_state(self):
        return {
            "paused": self.paused,
            "counts": {"running": 1, "retrying": 0, "blocked": 1},
            "termination_unconfirmed": 0,
        }

    def pause(self):
        self.paused = True
        return {"paused": True, "running": 1}

    def resume(self):
        self.paused = False
        return {"paused": False, "running": 1}

    def release_unconfirmed(self, identifier):
        return {
            "released": False,
            "identifier": identifier,
            "reason": "not_unconfirmed",
        }

    def cleanup_workspace(self, identifier):
        return {
            "cleaned": True,
            "identifier": identifier,
            "reason": None,
        }


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            workspace=SimpleNamespace(root="/tmp/workspaces"))
        self.store = SimpleNamespace(settings=lambda: self.settings)

    def _serve(self, orchestrator):
        server = http_api.start(
            orchestrator, self.store, "WORKFLOW.md", "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def _request(self, base, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            base + path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(
                    response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(
                error.read().decode("utf-8"))

    def test_state_payload_matches_elixir_shape(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(base, "GET", "/api/v1/state")

        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"], {
            "running": 1, "retrying": 0, "blocked": 1})
        self.assertIn("generated_at", payload)
        self.assertEqual(payload["codex_totals"]["total_tokens"], 120)
        running = payload["running"][0]
        for key in ("issue_id", "issue_identifier", "issue_url", "state",
                    "worker_host", "workspace_path", "session_id",
                    "turn_count", "last_event", "last_message",
                    "started_at", "last_event_at", "tokens"):
            self.assertIn(key, running)
        self.assertEqual(running["tokens"], {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        })
        self.assertIn("metrics", payload)
        self.assertIn("alerts", payload["health"])
        self.assertFalse(payload["paused"])

    def test_state_payload_reports_metrics_and_threshold_alerts(self):
        snapshot = _snapshot()
        snapshot["metrics"] = {
            "termination_unconfirmed": 1,
            "dispatch": {"count": 2},
        }
        orchestrator = _FakeOrchestrator(snapshot)
        orchestrator.session_reporter = SimpleNamespace(
            snapshot=lambda: {
                "enabled": True,
                "thread_alive": True,
                "queue_length": 200,
                "pending_events": 100,
            })
        scopes = {
            "running": True,
            "active": 973,
            "limit": 1024,
            "utilization": 0.95,
        }

        with patch.object(http_api.mcp_bridge, "stats", return_value=scopes):
            status, payload = self._request(
                self._serve(orchestrator), "GET", "/api/v1/state")

        self.assertEqual(status, 200)
        self.assertEqual(payload["metrics"]["mcp_scopes"]["active"], 973)
        self.assertEqual(payload["metrics"]["outbox"]["queue_length"], 200)
        codes = {alert["code"] for alert in payload["health"]["alerts"]}
        self.assertIn("termination_unconfirmed", codes)
        self.assertIn("mcp_scope_pressure", codes)
        self.assertIn("agent_session_queue_backlog", codes)
        self.assertIn("agent_session_outbox_backlog", codes)
        self.assertEqual(
            payload["health"]["alert_count"],
            len(payload["health"]["alerts"]))

    def test_state_payload_reports_backup_health_and_failure_alert(self):
        orchestrator = _FakeOrchestrator()
        orchestrator.state_backup = SimpleNamespace(
            snapshot=lambda: {
                "enabled": True,
                "directory": "/tmp/backups",
                "interval_ms": 3_600_000,
                "keep": 24,
                "last_success_at": "2026-01-01T00:00:00Z",
                "last_error": "disk full",
                "last_files": 2,
                "backup_count": 1,
                "thread_alive": False,
            })

        status, payload = self._request(
            self._serve(orchestrator), "GET", "/api/v1/state")

        self.assertEqual(status, 200)
        self.assertEqual(payload["state_backup"]["last_files"], 2)
        codes = {alert["code"] for alert in payload["health"]["alerts"]}
        self.assertIn("state_backup_thread_down", codes)
        self.assertIn("state_backup_failed", codes)

    def test_snapshot_timeout_is_reported_in_band(self):
        base = self._serve(_FakeOrchestrator(snapshot="timeout"))

        status, payload = self._request(base, "GET", "/api/v1/state")

        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], "snapshot_timeout")

    def test_issue_detail_for_running_entry(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(base, "GET", "/api/v1/GH-1")

        self.assertEqual(status, 200)
        self.assertEqual(payload["issue_identifier"], "GH-1")
        self.assertEqual(payload["issue_id"], "issue-1")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["workspace"], {
            "path": "/tmp/workspaces/GH-1", "host": "worker-a"})
        self.assertEqual(payload["attempts"], {
            "restart_count": 0, "current_retry_attempt": 0})
        self.assertEqual(payload["running"]["tokens"]["total_tokens"], 120)
        self.assertEqual(len(payload["recent_events"]), 1)

    def test_issue_detail_for_blocked_entry(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(base, "GET", "/api/v1/GH-2")

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("operator input", payload["last_error"])
        self.assertEqual(
            payload["blocked"]["last_event"], "turn_input_required")

    def test_unknown_issue_returns_404(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(base, "GET", "/api/v1/GH-404")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "issue_not_found")

    def test_refresh_queues_and_returns_202(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(
            base, "POST", "/api/v1/refresh")

        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertFalse(payload["coalesced"])
        self.assertTrue(payload["requested_at"].endswith("Z"))

    def test_control_pause_resume_and_state(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(base, "GET", "/api/v1/control")
        self.assertEqual(status, 200)
        self.assertFalse(payload["paused"])

        status, payload = self._request(
            base, "POST", "/api/v1/control/pause")
        self.assertEqual(status, 200)
        self.assertTrue(payload["paused"])

        status, payload = self._request(
            base, "POST", "/api/v1/control/resume")
        self.assertEqual(status, 200)
        self.assertFalse(payload["paused"])

    def test_control_release_conflict_and_cleanup_success(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(
            base, "POST", "/api/v1/control/release/GH-1")
        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "not_unconfirmed")

        status, payload = self._request(
            base, "POST", "/api/v1/control/cleanup/GH-1")
        self.assertEqual(status, 200)
        self.assertTrue(payload["cleaned"])

        self.assertEqual(
            self._request(base, "GET", "/api/v1/control/pause")[0], 405)

    def test_refresh_unavailable_returns_503(self):
        base = self._serve(_FakeOrchestrator(refresh="unavailable"))

        status, payload = self._request(
            base, "POST", "/api/v1/refresh")

        self.assertEqual(status, 503)
        self.assertEqual(
            payload["error"]["code"], "orchestrator_unavailable")

    def test_wrong_verbs_return_405(self):
        base = self._serve(_FakeOrchestrator())

        self.assertEqual(
            self._request(base, "GET", "/api/v1/refresh")[0], 405)
        status, payload = self._request(
            base, "PUT", "/api/v1/state", body={})
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_error_response_drains_unread_request_body(self):
        base = self._serve(_FakeOrchestrator())
        port = int(base.rsplit(":", 1)[1])
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "PUT", "/api/v1/state", body=b"{}",
                headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            response.read()
            # The drained body must not be parsed as the next request.
            connection.request("GET", "/api/v1/state")
            follow_up = connection.getresponse()
            self.assertEqual(follow_up.status, 200)
            follow_up.read()
        finally:
            connection.close()

    def test_unknown_routes_return_404(self):
        base = self._serve(_FakeOrchestrator())

        status, payload = self._request(base, "GET", "/api/v1/nope/deep")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

        status, payload = self._request(base, "GET", "/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
