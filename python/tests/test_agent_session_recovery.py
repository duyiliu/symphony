"""Regression tests for agent-session recovery after crashes and scope changes.

These tests assert external behaviour only: terminal delivery events are never
lost, exactly one response is reported after a restart, a stale blocked event
cannot override a completed session, and a pending outbox is never delivered
to a different OAuth identity.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import agent_sessions, config, delivery  # noqa: E402
from tests.test_agent_sessions import FakeAPI, make_issue  # noqa: E402


def make_settings(state_dir, delivery_dir=None, enabled=True,
                  client_id=None):
    agent_sessions_conf = {
        "enabled": enabled,
        "state_dir": state_dir,
        "progress_interval_seconds": 2,
        "max_progress_activities": 50,
    }
    if client_id:
        agent_sessions_conf["oauth"] = {
            "client_id": client_id,
            "client_secret": "secret-%s" % client_id,
            "token_url": "https://auth.example/%s/token" % client_id,
        }
    raw = {
        "tracker": {
            "kind": "linear",
            "provider": {
                "project_slug": "p",
                "api_key": "k",
                "agent_sessions": agent_sessions_conf,
            },
            "active_states": ["Todo"],
            "terminal_states": ["Done"],
        },
    }
    if delivery_dir:
        raw["delivery"] = {
            "enabled": True,
            "repo": "o/r",
            "token": "merge-token",
            "base_branch": "main",
            "review_state": "Human Review",
            "merge_state": "Merging",
            "rework_state": "Rework",
            "done_state": "Done",
            "state_dir": delivery_dir,
        }
    return config.parse_settings(raw)


class _MutableStore:
    def __init__(self, cfg):
        self.cfg = cfg

    def settings(self):
        return self.cfg


class _ReporterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.delivery_dir = tempfile.TemporaryDirectory()
        self.cfg = make_settings(
            self.temp.name, delivery_dir=self.delivery_dir.name)
        self.now = 100
        self.store = _MutableStore(self.cfg)
        self.reporter = agent_sessions.Reporter(
            self.store, clock=lambda: self.now)
        self.api = FakeAPI()
        self.patch = patch.object(
            agent_sessions, "_graphql", side_effect=self.api)
        self.patch.start()
        self.issue = make_issue()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()
        self.delivery_dir.cleanup()

    def pump(self, reporter=None):
        (reporter or self.reporter).process_pending()

    def types(self):
        return [v["content"]["type"] for v in self.api.activities.values()]

    def responses(self):
        return [v for v in self.api.activities.values()
                if v["content"]["type"] == "response"]

    def start(self):
        self.reporter.started(self.issue, model="provider/model")
        self.pump()

    def restart(self):
        return agent_sessions.Reporter(self.store, clock=lambda: self.now)

    def save_delivery_record(self, issue, changes, record=None):
        """Persist a host delivery result the way DeliveryController._save does.

        The controller carries this Reporter as its session_reporter so the
        saved payload gets the same recoverable envelope a real host merge
        would produce.
        """
        records = delivery.DeliveryRecordStore(self.cfg.delivery.state_dir)
        controller = delivery.DeliveryController(
            self.store, session_reporter=self.reporter)
        return controller._save(records, issue, record or {}, changes)


class QueueSaturationRecoveryTest(_ReporterCase):
    def test_merged_survives_full_reporting_queue(self):
        self.start()
        # Saturate the bounded queue with per-issue progress noise; control
        # events must overflow and survive rather than be dropped.
        for index in range(6):
            filler = make_issue("filler-%d" % index)
            for _ in range(50):
                self.now += 60
                self.reporter.progress(filler, {
                    "event": "tool_call_completed",
                    "details": {"tool": "bash"},
                    "payload": {}})
            if self.reporter.queue.full():
                break
        self.assertTrue(self.reporter.queue.full())

        # Host confirms merge while the queue is full: the terminal state is
        # saved atomically in the delivery record by the same helper the real
        # controller uses, and the in-memory event may sit in overflow.
        self.save_delivery_record(self.issue, {
            "phase": "merged",
            "session_phase": "merged",
            "pr": 7,
            "head_sha": "a" * 40,
        })
        self.reporter.delivery(
            self.issue, "merged", "checks passed", 7, "a" * 40)

        # Drop the saturated queue: queued progress noise may be lost.
        while not self.reporter.queue.empty():
            self.reporter.queue.get_nowait()
            self.reporter.queue.task_done()

        restarted = self.restart()
        restarted.process_pending()

        self.assertEqual(len(self.responses()), 1)
        urls = self.api.updates[-1]["input"].get("addedExternalUrls") or []
        self.assertIn(
            "https://github.com/o/r/pull/7",
            [item["url"] for item in urls])
        self.assertEqual(
            restarted.records[self.issue.id]["phase"], "merged")
        self.assertEqual(restarted.snapshot()["pending_events"], 0)


class DurableDeliveryRecordTest(_ReporterCase):
    def test_restart_recovers_merged_from_delivery_record(self):
        """Host merged while no event was ever staged in memory.

        The durable delivery record carries the confirmed terminal phase; a
        fresh Reporter must compensate from it and emit exactly one response
        activity on the existing session.
        """
        self.start()
        self.assertEqual(self.types(), ["thought"])

        self.save_delivery_record(self.issue, {
            "phase": "merged",
            "session_phase": "merged",
            "pr": 7,
            "head_sha": "a" * 40,
        })

        restarted = self.restart()
        restarted.process_pending()

        self.assertEqual(len(self.responses()), 1)
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(restarted.snapshot()["pending_events"], 0)

    def test_merged_pending_sync_is_not_reported_as_done(self):
        self.start()
        self.save_delivery_record(self.issue, {
            "phase": "merged",
            "session_phase": "merged_pending_sync",
            "pr": 7,
            "head_sha": "a" * 40,
        })

        restarted = self.restart()
        restarted.process_pending()

        # The PR is merged on GitHub but Linear is not yet Done: the host
        # must not emit a final "response" that claims completion. An
        # elicitation asking for a state check is acceptable; so is keeping
        # the event pending on disk.
        self.assertEqual(self.responses(), [])
        self.assertNotEqual(
            restarted.records.get(self.issue.id, {}).get("phase"), "merged")

    def test_delivery_record_without_session_creates_no_session(self):
        """Old delivery issues never reported by a session are not retrofitted."""
        self.save_delivery_record(make_issue("old-issue"), {
            "phase": "merged",
            "session_phase": "merged",
            "pr": 7,
            "head_sha": "a" * 40,
        })

        restarted = self.restart()
        restarted.process_pending()

        self.assertEqual(self.api.sessions, [])


class TerminalEventOrderingTest(_ReporterCase):
    def test_blocked_after_merged_cannot_emit_elicitation(self):
        self.start()
        self.save_delivery_record(self.issue, {
            "phase": "merged",
            "session_phase": "merged",
            "pr": 7,
            "head_sha": "a" * 40,
        })
        self.reporter.delivery(
            self.issue, "merged", "checks passed", 7, "a" * 40)
        self.pump()
        self.assertEqual(self.types(), ["thought", "response"])

        self.reporter.blocked(self.issue)
        self.pump()

        self.assertNotIn("elicitation", self.types())
        self.assertEqual(
            self.reporter.records[self.issue.id]["phase"], "merged")

    def test_blocked_after_merged_does_not_reopen_after_restart(self):
        self.start()
        self.save_delivery_record(self.issue, {
            "phase": "merged",
            "session_phase": "merged",
            "pr": 7,
            "head_sha": "a" * 40,
        })
        self.reporter.delivery(
            self.issue, "merged", "checks passed", 7, "a" * 40)
        self.reporter.blocked(self.issue)

        restarted = self.restart()
        restarted.process_pending()
        restarted.process_pending()

        self.assertEqual(self.types(), ["thought", "response"])
        self.assertNotIn("elicitation", self.types())
        self.assertEqual(
            restarted.records[self.issue.id]["phase"], "merged")


class ScopeIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        self.patch = patch.object(
            agent_sessions, "_graphql", side_effect=self.api)
        self.patch.start()
        self.issue = make_issue()
        self.now = 100

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _cfg(self, client_id):
        return make_settings(self.temp.name, client_id=client_id)

    def test_pending_outbox_does_not_leak_to_new_oauth_identity(self):
        cfg_a = self._cfg("client-a")
        reporter = agent_sessions.Reporter(
            _MutableStore(cfg_a), clock=lambda: self.now)
        reporter.started(self.issue, model="provider/model")
        reporter.process_pending()

        # Stage a merged event, then make the network fail before Linear
        # acknowledges it: the durable pending entry stays in scope A.
        reporter.delivery(self.issue, "merged", "merged", 7, "a" * 40)
        with patch.object(
                agent_sessions, "_graphql",
                side_effect=agent_sessions.WorkflowError("offline")):
            reporter.process_pending()
        self.assertEqual(reporter.snapshot()["pending_events"], 1)
        activities_after_a = len(self.api.activities)

        state_path_a = agent_sessions._state_path(cfg_a, self.issue.id)
        self.assertTrue(os.path.exists(state_path_a))

        # Same configured state_dir, different OAuth identity: the scope is
        # derived from endpoint + project_slug + client_id, so the old
        # pending event must not be delivered under the new client.
        cfg_b = self._cfg("client-b")
        restarted = agent_sessions.Reporter(
            _MutableStore(cfg_b), clock=lambda: self.now)
        self.now += 60
        restarted.process_pending()

        self.assertEqual(len(self.api.activities), activities_after_a)
        self.assertNotIn("response", [
            v["content"]["type"] for v in self.api.activities.values()])
        self.assertEqual(len(self.api.sessions), 1)

        # The old scope's pending event stays on disk under scope A and is
        # still recoverable when the original identity is restored.
        self.assertTrue(os.path.exists(state_path_a))
        with open(state_path_a, encoding="utf-8") as handle:
            record_a = json.load(handle)
        self.assertTrue(record_a.get("pending"))


class HotEnableRecoveryTest(_ReporterCase):
    def test_disabled_start_then_hot_enable_recovers_pending(self):
        self.start()
        self.reporter.delivery(
            self.issue, "merged", "merged", 7, "a" * 40)
        with patch.object(
                agent_sessions, "_graphql",
                side_effect=agent_sessions.WorkflowError("offline")):
            self.pump()
        self.assertEqual(self.reporter.snapshot()["pending_events"], 1)

        # Restart while reporting is disabled: nothing is sent.
        self.store.cfg = make_settings(
            self.temp.name, delivery_dir=self.delivery_dir.name,
            enabled=False)
        restarted = self.restart()
        restarted.process_pending()
        self.assertNotIn("response", self.types())

        # Hot-enable in the same process: the next pump must re-activate the
        # scope and drain the durable pending event without any explicit
        # recovery call from the caller.
        self.store.cfg = make_settings(
            self.temp.name, delivery_dir=self.delivery_dir.name)
        self.now += 60
        restarted.process_pending()

        self.assertEqual(len(self.responses()), 1)
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(restarted.snapshot()["pending_events"], 0)

class CrashBeforeFinalSaveTest(_ReporterCase):
    """High 1: Linear moved to Done but the final phase=merged save was lost.

    The durable record still says merged_pending_sync, and the issue no longer
    appears in the active-state poll, so a fresh controller must rediscover it
    via fetch_issues_by_ids and finish the terminal phase exactly once.
    """

    def test_poll_once_recovers_done_issue_and_reports_once(self):
        from symphony import linear
        from tests.test_delivery import _FakeGitHub

        self.start()
        self.assertEqual(self.types(), ["thought"])

        issue = self.issue
        issue.state = "Human Review"
        issue.attachments = [{
            "url": "https://github.com/o/r/pull/7",
            "metadata": {"status": "open"},
        }]

        transitions = []
        original_save = delivery.DeliveryRecordStore.save
        crashed = {"done": False}

        def flaky_save(store_self, save_issue, record):
            # The host already moved Linear to Done and confirmed the merge on
            # GitHub; only the final phase=merged write is lost, as if the
            # process died between the transition and the save.
            if not crashed["done"] and record.get("phase") == "merged" \
                    and record.get("session_phase") == "merged":
                crashed["done"] = True
                raise OSError("simulated crash before final save")
            return original_save(store_self, save_issue, record)

        controller = delivery.DeliveryController(
            self.store, session_reporter=self.reporter)
        fake = _FakeGitHub(self.cfg.delivery)
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue]), \
                patch.object(
                    delivery.tracker_adapter, "fetch_issues_by_ids",
                    return_value=[issue]), \
                patch.object(
                    linear, "update_issue_state",
                    side_effect=lambda _s, _i, _t, state:
                        transitions.append(state)), \
                patch.object(linear, "create_comment"), \
                patch.object(delivery.DeliveryRecordStore, "save", flaky_save), \
                patch.object(delivery, "GitHub", return_value=fake):
            try:
                controller.poll_once()
            except OSError:
                pass  # process dies before the final save lands

        self.assertTrue(crashed["done"])
        self.assertIn("Done", transitions)
        # The intermediate durable record still says pending-sync; the issue
        # object the next poll sees is already terminal.
        pending_record = delivery.DeliveryRecordStore(
            self.cfg.delivery.state_dir).load(issue)
        self.assertEqual(
            pending_record.get("phase"), "merged_pending_sync")
        issue.state = "Done"

        # A new controller starts after the crash. The Done issue is no longer
        # returned by the active-state query; recovery must come through the
        # pending-sync record plus fetch_issues_by_ids.
        fetched_ids = []
        restarted = delivery.DeliveryController(
            self.store, session_reporter=self.restart())
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[]), \
                patch.object(
                    delivery.tracker_adapter, "fetch_issues_by_ids",
                    side_effect=lambda _t, ids:
                        fetched_ids.extend(ids) or [issue]), \
                patch.object(
                    linear, "update_issue_state",
                    side_effect=lambda _s, _i, _t, state:
                        transitions.append(state)), \
                patch.object(linear, "create_comment"), \
                patch.object(delivery, "GitHub", return_value=fake):
            restarted.poll_once()

        # The Done issue is recovered by id even though the active-state poll
        # no longer returns it.
        self.assertIn(issue.id, fetched_ids)
        record = delivery.DeliveryRecordStore(
            self.cfg.delivery.state_dir).load(issue)
        self.assertEqual(record.get("phase"), "merged")

        # Reporter drains the durable envelope exactly once.
        reporter = restarted.session_reporter
        reporter.process_pending()
        reporter.process_pending()
        self.assertEqual(len(self.responses()), 1)
        self.assertEqual(len(self.api.sessions), 1)

class PendingSyncSupersededTest(_ReporterCase):
    """A pending-sync merge must not finish an issue the user reopened."""

    def test_reopened_issue_is_superseded_and_reports_nothing(self):
        from symphony import linear
        from tests.test_delivery import _FakeGitHub

        self.start()
        issue = self.issue
        issue.state = "Human Review"
        issue.attachments = [{
            "url": "https://github.com/o/r/pull/7",
            "metadata": {"status": "closed"},
        }]

        # Durable state left by a crash: GitHub merge confirmed and Linear was
        # still in the review state when the host recorded pre-sync facts.
        self.save_delivery_record(issue, {
            "phase": "merged_pending_sync",
            "session_phase": "merged_pending_sync",
            "reason": "GitHub PR 已确认合并，正在同步 Linear 终态。",
            "pr": 7,
            "head_sha": "a" * 40,
            "gated_sha": "a" * 40,
            "pre_sync_state": "Human Review",
            "pre_sync_updated_at": "2026-11-13T10:00:00+00:00",
        })

        # The user reopens the issue; updated_at moves on.
        import datetime
        issue.state = "Todo"
        issue.updated_at = datetime.datetime(
            2026, 11, 13, 12, 0, tzinfo=datetime.timezone.utc)

        transitions = []
        controller = delivery.DeliveryController(
            self.store, session_reporter=self.reporter)
        fake = _FakeGitHub(self.cfg.delivery)
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[]), \
                patch.object(
                    delivery.tracker_adapter, "fetch_issues_by_ids",
                    return_value=[issue]), \
                patch.object(
                    linear, "update_issue_state",
                    side_effect=lambda _s, _i, _t, state:
                        transitions.append(state)), \
                patch.object(linear, "create_comment"), \
                patch.object(delivery, "GitHub", return_value=fake):
            controller.poll_once()

        # Done must not be applied to a reopened lifecycle.
        self.assertNotIn("Done", transitions)
        record = delivery.DeliveryRecordStore(
            self.cfg.delivery.state_dir).load(issue)
        self.assertEqual(record.get("phase"), "superseded_pending_sync")

        # Moving the reopened issue back into a delivery state must not make
        # the same already-merged PR eligible a second time.
        issue.state = "Human Review"
        self.assertEqual(delivery.discover_pr(issue, "o/r"), 7)
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue]), \
                patch.object(
                    delivery.tracker_adapter, "fetch_issues_by_ids",
                    return_value=[issue]), \
                patch.object(
                    linear, "update_issue_state",
                    side_effect=lambda _s, _i, _t, state:
                        transitions.append(state)), \
                patch.object(linear, "create_comment"), \
                patch.object(delivery, "GitHub", return_value=fake):
            controller.poll_once()

        self.assertNotIn("Done", transitions)
        self.assertEqual(fake.calls, [])
        record = delivery.DeliveryRecordStore(
            self.cfg.delivery.state_dir).load(issue)
        self.assertEqual(record.get("phase"), "superseded_pending_sync")

        self.now += 60
        self.pump()
        self.assertEqual(self.responses(), [])
        self.assertNotIn("elicitation", self.types())


class NewLifecycleDropsOldPendingControlTest(_ReporterCase):
    """A queued pending-sync control belongs to the old session lifecycle."""

    def test_started_after_pending_sync_drops_old_control(self):
        self.start()
        self.assertEqual(self.types(), ["thought"])
        self.assertEqual(len(self.api.sessions), 1)

        # Queue a merged-pending-sync control event but crash before it is
        # ever staged to Linear; then a new task run starts on the same issue.
        self.reporter.delivery(
            self.issue, "merged_pending_sync",
            "Linear 终态同步尚未确认。", 7, "a" * 40)
        self.reporter.started(self.issue, model="provider/model")

        self.pump()
        self.pump()

        # A fresh session is created for the new lifecycle; the stale control
        # must not be delivered on it or the old one.
        self.assertEqual(len(self.api.sessions), 2)
        self.assertNotIn("elicitation", self.types())
        self.assertEqual(self.responses(), [])
        self.assertEqual(self.types(), ["thought", "thought"])


if __name__ == "__main__":
    unittest.main()

