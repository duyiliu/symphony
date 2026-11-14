"""Terminal status dashboard parity tests against upstream fixtures."""

import os
import re
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import status_dashboard  # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
FIXTURES = os.path.join(
    ROOT, "elixir", "test", "fixtures", "status_dashboard_snapshots")


def _settings(port=None):
    return SimpleNamespace(
        tracker=SimpleNamespace(kind="linear", project_slug="project"),
        agent=SimpleNamespace(max_concurrent_agents=10),
        server=SimpleNamespace(host="127.0.0.1", port=port),
        observability=SimpleNamespace(
            dashboard_enabled=True, refresh_ms=1000,
            render_interval_ms=16),
    )


def _fixture(name):
    path = os.path.join(FIXTURES, name + ".snapshot.txt")
    with open(path, encoding="utf-8") as handle:
        return handle.read().replace("\\e", "\x1b").rstrip("\n")


def _turn_completed(status):
    return {
        "event": "notification",
        "message": {
            "method": "turn/completed",
            "params": {"turn": {"status": status}},
        },
    }


def _exec_command(command):
    return {
        "event": "notification",
        "message": {
            "method": "codex/event/exec_command_begin",
            "params": {"msg": {"command": command}},
        },
    }


def _agent_delta(delta):
    return {
        "event": "notification",
        "message": {
            "method": "codex/event/agent_message_delta",
            "params": {"msg": {"payload": {"delta": delta}}},
        },
    }


def _token_usage(input_tokens, output_tokens, total_tokens):
    return {
        "event": "notification",
        "message": {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "total": {
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                        "totalTokens": total_tokens,
                    }
                }
            },
        },
    }


def _idle_snapshot(polling=None):
    return {
        "running": [],
        "retrying": [],
        "codex_totals": {
            "input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "seconds_running": 0,
        },
        "rate_limits": None,
        "polling": polling,
    }


def _running(identifier, tokens, runtime, turns, event, message,
             session="thread-1234567890", pid="4242", state="running"):
    return {
        "identifier": identifier,
        "state": state,
        "session_id": session,
        "codex_app_server_pid": pid,
        "codex_total_tokens": tokens,
        "runtime_seconds": runtime,
        "turn_count": turns,
        "last_codex_event": event,
        "last_codex_message": message,
    }


class SnapshotTest(unittest.TestCase):
    def test_idle_snapshot(self):
        rendered = status_dashboard.render_snapshot(
            _idle_snapshot(), _settings(), 0.0, 115)
        self.assertEqual(rendered, _fixture("idle"))

    def test_idle_with_dashboard_url(self):
        settings = _settings(port=4000)
        rendered = status_dashboard.render_snapshot(
            _idle_snapshot(), settings, 0.0, 115,
            dashboard_url=status_dashboard.dashboard_url(settings))
        self.assertEqual(rendered, _fixture("idle_with_dashboard_url"))

    def test_super_busy_snapshot(self):
        snapshot = {
            "running": [
                _running(
                    "MT-101", 120_450, 785, 11, "turn_completed",
                    _turn_completed("completed")),
                _running(
                    "MT-102", 89_200, 412, 4, "codex/event/task_started",
                    _exec_command("mix test --cover"),
                    session="thread-abcdef1234567890", pid="5252"),
            ],
            "retrying": [],
            "codex_totals": {
                "input_tokens": 250_000, "output_tokens": 18_500,
                "total_tokens": 268_500, "seconds_running": 4_321,
            },
            "rate_limits": {
                "limit_id": "gpt-5",
                "primary": {
                    "remaining": 12_345, "limit": 20_000,
                    "reset_in_seconds": 30,
                },
                "secondary": {
                    "remaining": 45, "limit": 60,
                    "reset_in_seconds": 12,
                },
                "credits": {"has_credits": True, "balance": 9_876.5},
            },
        }
        rendered = status_dashboard.render_snapshot(
            snapshot, _settings(), 1_842.7, 115)
        self.assertEqual(rendered, _fixture("super_busy"))

    def test_backoff_queue_snapshot(self):
        snapshot = {
            "running": [
                _running(
                    "MT-638", 14_200, 1_225, 7, "notification",
                    _agent_delta("waiting on rate-limit backoff window"),
                    state="retrying"),
            ],
            "retrying": [
                {"issue_id": "issue-1", "identifier": "MT-450",
                 "attempt": 4, "due_in_ms": 1_250,
                 "error": "rate limit exhausted"},
                {"issue_id": "issue-2", "identifier": "MT-451",
                 "attempt": 2, "due_in_ms": 3_900,
                 "error": "retrying after API timeout with jitter"},
                {"issue_id": "issue-3", "identifier": "MT-452",
                 "attempt": 6, "due_in_ms": 8_100,
                 "error": "worker crashed\nrestarting cleanly"},
                {"issue_id": "issue-4", "identifier": "MT-453",
                 "attempt": 1, "due_in_ms": 11_000,
                 "error": "fourth queued retry should also render after "
                          "removing the top-three limit"},
            ],
            "codex_totals": {
                "input_tokens": 18_000, "output_tokens": 2_200,
                "total_tokens": 20_200, "seconds_running": 2_700,
            },
            "rate_limits": {
                "limit_id": "gpt-5",
                "primary": {
                    "remaining": 0, "limit": 20_000,
                    "reset_in_seconds": 95,
                },
                "secondary": {
                    "remaining": 0, "limit": 60,
                    "reset_in_seconds": 45,
                },
                "credits": {"has_credits": False},
            },
        }
        rendered = status_dashboard.render_snapshot(
            snapshot, _settings(), 15.4, 115)
        self.assertEqual(rendered, _fixture("backoff_queue"))

    def test_credits_unlimited_snapshot(self):
        snapshot = {
            "running": [
                _running(
                    "MT-777", 3_200, 75, 7, "codex/event/token_count",
                    _token_usage(90, 12, 102)),
            ],
            "retrying": [],
            "codex_totals": {
                "input_tokens": 90, "output_tokens": 12,
                "total_tokens": 102, "seconds_running": 75,
            },
            "rate_limits": {
                "limit_id": "priority-tier",
                "primary": {
                    "remaining": 100, "limit": 100, "reset_in_seconds": 1,
                },
                "secondary": {
                    "remaining": 500, "limit": 500, "reset_in_seconds": 1,
                },
                "credits": {"unlimited": True},
            },
        }
        rendered = status_dashboard.render_snapshot(
            snapshot, _settings(), 42.0, 115)
        self.assertEqual(rendered, _fixture("credits_unlimited"))


class RendererBehaviorTest(unittest.TestCase):
    def test_non_tty_is_disabled_by_default(self):
        import io
        dashboard = status_dashboard.StatusDashboard(
            None, _settings(), stream=io.StringIO())
        self.assertFalse(dashboard.enabled)
        self.assertIs(dashboard.start(), dashboard)
        dashboard.stop()

    def test_offline_status_and_closing_corner(self):
        import io
        stream = io.StringIO()
        dashboard = status_dashboard.StatusDashboard(
            None, _settings(), stream=stream, enabled=True)
        dashboard.render_offline()
        rendered = stream.getvalue()
        self.assertIn("app_status=offline", rendered)
        self.assertNotIn("Timestamp:", rendered)
        self.assertTrue(
            status_dashboard.render_snapshot(
                _idle_snapshot(), _settings(), 0.0, 115
            ).endswith("╰─"))

    def test_dashboard_url_normalizes_hosts(self):
        self.assertEqual(
            status_dashboard.dashboard_url_for_test(
                "0.0.0.0", 0, 43_123),
            "http://127.0.0.1:43123/")
        self.assertEqual(
            status_dashboard.dashboard_url_for_test("::1", 4000, None),
            "http://[::1]:4000/")
        self.assertIsNone(
            status_dashboard.dashboard_url_for_test("127.0.0.1", None, None))

    def test_refresh_countdown_and_checking_marker(self):
        waiting = status_dashboard.render_snapshot(
            _idle_snapshot({"checking": False, "next_poll_in_ms": 2_000}),
            _settings(), 0.0, 115)
        self.assertIn("Next refresh:", waiting)
        self.assertIn("2s", waiting)
        checking = status_dashboard.render_snapshot(
            _idle_snapshot({"checking": True, "next_poll_in_ms": None}),
            _settings(), 0.0, 115)
        self.assertIn("checking now…", checking)

    def test_spacer_before_backoff_queue(self):
        plain = re.sub(
            r"\x1b\[[0-9;]*m", "",
            status_dashboard.render_snapshot(
                _idle_snapshot(), _settings(), 0.0, 115))
        self.assertRegex(
            plain, r"No active agents\n│\s*\n├─ Backoff queue")

    def test_running_row_width_and_message(self):
        entry = _running(
            "MT-598", 123, 15, 1, "notification",
            _turn_completed("completed"))
        row = status_dashboard.format_running_summary_for_test(entry, 140)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", row)
        self.assertEqual(len(plain), 140)
        self.assertIn("turn completed (completed)", plain)

    def test_running_row_strips_ansi_and_control_bytes(self):
        payload = "cmd: \x1b[31mRED\x1b[0m\x00 after\nline"
        entry = _running("MT-898", 12, 15, 1, "notification", payload)
        row = status_dashboard.format_running_summary_for_test(entry, 140)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", row)
        self.assertIn("cmd: RED after line", plain)
        self.assertNotIn("\x1b", plain)
        self.assertNotIn("\x00", plain)

    def test_backoff_row_escapes_escaped_newlines(self):
        snapshot = _idle_snapshot()
        snapshot["retrying"] = [{
            "issue_id": "issue-1", "identifier": "MT-980",
            "attempt": 1, "due_in_ms": 1_500,
            "error": "error with \\nnewline",
        }]
        rendered = status_dashboard.render_snapshot(
            snapshot, _settings(), 0.0, 115)
        rows = [line for line in rendered.split("\n") if "MT-980" in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("error=error with newline", rows[0])
        self.assertNotIn("\\n", rows[0])

    def test_throughput_helpers(self):
        self.assertEqual(status_dashboard.rolling_tps([], 10_000, 0), 0.0)
        self.assertEqual(
            status_dashboard.rolling_tps([(9_000, 20)], 10_000, 40), 20.0)
        self.assertEqual(
            status_dashboard.rolling_tps([(4_900, 10)], 10_000, 90), 0.0)
        self.assertEqual(
            status_dashboard.rolling_tps(
                [(9_500, 10), (9_000, 40), (8_000, 80)], 10_000, 95),
            7.5)
        first = status_dashboard.throttled_tps(
            None, None, 10_000, [(9_000, 20)], 40)
        same = status_dashboard.throttled_tps(
            first[0], first[1], 10_500, [(9_000, 20)], 200)
        self.assertEqual(same, first)
        later = status_dashboard.throttled_tps(
            same[0], same[1], 11_000, [(10_500, 200)], 260)
        self.assertEqual(later[0], 11)
        self.assertNotEqual(later[1], same[1])

    def test_tps_graph(self):
        samples = [(t, t // 100) for t in range(575_000, -1, -25_000)]
        self.assertEqual(
            status_dashboard.tps_graph(samples, 600_000, 6_000),
            "████████████████████████")
        rates = [index * 2 for index in range(1, 25)]
        timestamp = tokens = 0
        ramping = []
        for rate in rates:
            next_timestamp = timestamp + 25_000
            next_tokens = tokens + int(rate * 25_000 / 1000)
            ramping.append((next_timestamp, next_tokens))
            timestamp, tokens = next_timestamp, next_tokens
        self.assertEqual(
            status_dashboard.tps_graph(ramping, 600_000, tokens),
            "▁▂▂▂▃▃▃▃▄▄▄▅▅▅▆▆▆▆▇▇▇██▅")

    def test_humanize_delegates_to_shared_presenter(self):
        message = {
            "event": "notification",
            "message": {
                "method": "codex/event/exec_command_begin",
                "params": {"msg": {"command": "git status --short"}},
            },
        }
        self.assertEqual(
            status_dashboard.humanize_codex_message(message),
            "git status --short")


if __name__ == "__main__":
    unittest.main()
