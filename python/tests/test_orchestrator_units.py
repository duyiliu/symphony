import datetime
import os
import sys
import unittest

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


class _FakeHandle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


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
