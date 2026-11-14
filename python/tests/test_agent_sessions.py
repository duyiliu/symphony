"""Tests for opt-in Linear Agent Session reporting."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import agent_sessions, config  # noqa: E402
from symphony.issue import Issue  # noqa: E402


def make_settings(state_dir, enabled=True):
    raw = {
        "tracker": {
            "kind": "linear",
            "provider": {
                "project_slug": "p",
                "api_key": "k",
                "agent_sessions": {
                    "enabled": enabled,
                    "state_dir": state_dir,
                },
            },
            "active_states": ["Todo"],
            "terminal_states": ["Done"],
        },
    }
    return config.parse_settings(raw)


def make_issue(issue_id="i-1"):
    return Issue(
        id=issue_id, identifier="YIL-1", title="t", description="",
        state="Todo", url="https://linear.app/x/issue/YIL-1", labels=[],
        blocked_by=[], created_at=None, updated_at=None,
        priority=None, assignee_id=None, team_id="t1",
        attachments=[], branch_name=None)


class AgentSessionTest(unittest.TestCase):
    def test_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(tmp, enabled=False)
            self.assertFalse(agent_sessions.enabled(cfg))
            self.assertIsNone(
                agent_sessions.ensure_session(cfg, make_issue()))

    def test_session_created_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(tmp)
            calls = []

            def fake_graphql(settings, query, variables=None):
                calls.append(query)
                return {"data": {"agentSessionCreateOnIssue": {
                    "success": True,
                    "agentSession": {"id": "sess-1"}}}}

            with patch.object(agent_sessions, "_graphql",
                              side_effect=fake_graphql):
                first = agent_sessions.ensure_session(cfg, make_issue())
                second = agent_sessions.ensure_session(cfg, make_issue())

            self.assertEqual(first, "sess-1")
            self.assertEqual(second, "sess-1")
            self.assertEqual(len(calls), 1)

    def test_finish_posts_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(tmp)
            sent = []

            def fake_graphql(settings, query, variables=None):
                sent.append((query, variables))
                return {"data": {"agentSessionUpdate": {"success": True}}}

            with patch.object(agent_sessions, "_graphql",
                              side_effect=fake_graphql):
                agent_sessions.finish_session(
                    cfg, make_issue(), "sess-1", "done")

            self.assertEqual(len(sent), 1)
            self.assertIn("agentSessionUpdate", sent[0][0])
            self.assertEqual(
                sent[0][1]["input"]["summary"], "done")

    def test_graphql_failure_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(tmp)

            def boom(settings, query, variables=None):
                raise RuntimeError("linear down")

            with patch.object(agent_sessions, "_graphql",
                              side_effect=boom):
                self.assertIsNone(
                    agent_sessions.ensure_session(cfg, make_issue()))

    def test_record_save_failure_cleans_unique_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(tmp)
            issue_value = make_issue()
            path = agent_sessions._state_path(cfg, issue_value.id)
            with patch.object(
                    agent_sessions.os, "replace",
                    side_effect=OSError("atomic replace failed")):
                with self.assertRaises(OSError):
                    agent_sessions._save_record(cfg, {
                        "issue_id": issue_value.id,
                        "identifier": issue_value.identifier,
                    })
            self.assertFalse(os.path.exists(path))
            self.assertFalse(any(
                name.startswith(os.path.basename(path) + ".tmp.")
                for name in os.listdir(os.path.dirname(path))))


class FakeAPI:
    def __init__(self):
        self.sessions = []
        self.activities = {}
        self.updates = []
        self.calls = []
        self.fail_activity_once = False
        self.fail_create_once = False

    def __call__(self, settings, query, variables=None):
        variables = variables or {}
        self.calls.append((query, variables))
        if "SymphonyRecoverAgentSession" in query:
            return {"data": {"agentSessions": {
                "nodes": self.sessions,
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        if "SymphonyRecoverAgentActivity" in query:
            item = self.activities.get(variables["id"])
            return {"data": {"agentActivities": {
                "nodes": [{"id": variables["id"]}] if item else []}}}
        if "agentSessionCreateOnIssue" in query:
            sid = "session-%s" % (len(self.sessions) + 1)
            self.sessions.append({
                "id": sid,
                "issue": {"id": variables["input"]["issueId"]},
                "externalUrls": json.dumps(variables["input"]["externalUrls"]),
            })
            if self.fail_create_once:
                self.fail_create_once = False
                raise agent_sessions.WorkflowError("response_lost")
            return {"data": {"agentSessionCreateOnIssue": {
                "success": True, "agentSession": {"id": sid}}}}
        if "agentActivityCreate" in query:
            fields = variables["input"]
            if fields["id"] in self.activities:
                raise AssertionError("duplicate activity creation")
            self.activities[fields["id"]] = fields
            if self.fail_activity_once:
                self.fail_activity_once = False
                raise agent_sessions.WorkflowError("response_lost")
            return {"data": {"agentActivityCreate": {
                "success": True, "agentActivity": {"id": fields["id"]}}}}
        if "agentSessionUpdate" in query:
            self.updates.append(variables)
            return {"data": {"agentSessionUpdate": {"success": True}}}
        raise AssertionError("Unexpected query " + query)


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = make_settings(self.temp.name)
        self.cfg.delivery.repo = "owner/repo"
        self.now = 100
        self.store = SimpleNamespace(settings=lambda: self.cfg)
        self.reporter = agent_sessions.Reporter(self.store, clock=lambda: self.now)
        self.api = FakeAPI()
        self.patch = patch.object(agent_sessions, "_graphql", side_effect=self.api)
        self.patch.start()
        self.issue = make_issue()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def pump(self):
        self.reporter.process_pending()

    def types(self):
        return [v["content"]["type"] for v in self.api.activities.values()]

    def start(self):
        self.reporter.started(self.issue, model="provider/model")
        self.pump()

    def test_enqueue_performs_no_network_on_caller_thread(self):
        self.reporter.started(self.issue)
        self.assertEqual(self.api.calls, [])
        self.pump()
        self.assertEqual(self.types(), ["thought"])

    def test_worker_finish_is_not_delivery_completion(self):
        self.start()
        self.reporter.worker_finished(self.issue, True)
        self.pump()
        self.assertNotIn("response", self.types())
        self.assertEqual(self.reporter.records[self.issue.id]["phase"], "waiting")

    def test_merge_is_terminal_and_late_worker_does_not_reopen(self):
        self.start()
        self.reporter.delivery(self.issue, "merged", "checks passed", 7, "a" * 40)
        self.pump()
        self.reporter.worker_finished(self.issue, True)
        self.pump()
        self.reporter.stopped(self.issue)
        self.pump()
        self.assertEqual(self.types(), ["thought", "response"])
        self.assertEqual(self.reporter.records[self.issue.id]["phase"], "merged")
        urls = self.api.updates[-1]["input"]["addedExternalUrls"]
        self.assertIn("https://github.com/owner/repo/pull/7",
                      [item["url"] for item in urls])

    def test_paused_remains_awaiting_input_after_worker_finishes(self):
        self.start()
        self.reporter.delivery(self.issue, "paused", "high-risk path", 7, "a" * 40)
        self.pump()
        self.reporter.worker_finished(self.issue, True)
        self.pump()
        self.assertEqual(self.types(), ["thought", "elicitation"])

    def test_summary_has_real_schema_length_and_no_newlines(self):
        agent_sessions.finish_session(
            self.cfg, self.issue, "s1", "hello\n" + "x" * 1000)
        summary = self.api.updates[-1]["input"]["summary"]
        self.assertLessEqual(len(summary), 255)
        self.assertNotIn("\n", summary)

    def test_retry_after_lost_creation_response_recovers_existing_session(self):
        self.api.fail_create_once = True
        self.start()
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(self.reporter.snapshot()["pending_events"], 1)
        self.now += 31
        self.pump()
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(self.types(), ["thought"])

    def test_invalid_recovery_links_do_not_create_duplicate_session(self):
        self.api.fail_create_once = True
        self.start()
        saved_urls = self.api.sessions[0]["externalUrls"]
        self.api.sessions[0]["externalUrls"] = "invalid-json"
        self.now += 31
        self.pump()
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(self.reporter.snapshot()["pending_events"], 1)
        self.api.sessions[0]["externalUrls"] = saved_urls
        self.now += 31
        self.pump()
        self.assertEqual(self.types(), ["thought"])

    def test_retry_after_lost_activity_response_does_not_duplicate(self):
        self.api.fail_activity_once = True
        self.start()
        self.assertEqual(len(self.api.activities), 1)
        self.now += 31
        self.pump()
        self.assertEqual(len(self.api.activities), 1)
        self.assertEqual(self.reporter.snapshot()["pending_events"], 0)

    def test_restart_recovers_durable_pending_event(self):
        self.start()
        self.api.fail_activity_once = True
        self.reporter.delivery(self.issue, "merged", "merged", 7, "a" * 40)
        self.pump()
        restarted = agent_sessions.Reporter(self.store, clock=lambda: self.now)
        restarted._recover()
        restarted.process_pending()
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(self.types(), ["thought", "response"])
        self.assertEqual(restarted.snapshot()["pending_events"], 0)

    def test_reopened_issue_gets_new_session(self):
        self.start()
        self.reporter.delivery(self.issue, "merged", "merged", 7, "a" * 40)
        self.pump()
        self.reporter.started(self.issue)
        self.pump()
        self.assertEqual(len(self.api.sessions), 2)
        self.assertEqual(self.types(), ["thought", "response", "thought"])

    def test_repeated_delivery_poll_does_not_repeat_activity(self):
        self.start()
        for _ in range(3):
            self.reporter.delivery(self.issue, "waiting", "CI pending", 7, "a" * 40)
            self.pump()
        self.assertEqual(self.types(), ["thought", "thought"])

    def test_progress_is_throttled_and_never_reports_tool_arguments(self):
        self.start()
        update = {
            "event": "tool_call_completed", "details": {"tool": "bash"},
            "payload": {"command": "echo private-token"}}
        self.reporter.progress(self.issue, update)
        self.pump()
        self.reporter.progress(self.issue, update)
        self.pump()
        self.assertEqual(self.types(), ["thought", "action"])
        sent = list(self.api.activities.values())[-1]["content"]
        self.assertIn("parameter", sent)
        self.assertNotIn("private-token", json.dumps(sent))

    def test_rate_limit_backs_off_and_keeps_terminal_event(self):
        self.start()
        with patch.object(agent_sessions, "_graphql", side_effect=
                          agent_sessions.WorkflowError(
                              "linear_api_http_error",
                              {"status": 429, "retry_after": "120"})):
            self.reporter.delivery(self.issue, "merged", "merged", 7, "a" * 40)
            self.pump()
        self.now += 31
        calls = len(self.api.calls)
        self.pump()
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual(self.reporter.snapshot()["pending_events"], 1)
        self.now += 90
        self.pump()
        self.assertEqual(self.types(), ["thought", "response"])

    def test_old_delivery_records_do_not_create_sessions(self):
        self.reporter.delivery(self.issue, "paused", "high risk", 7, "a" * 40)
        self.pump()
        self.assertEqual(self.api.sessions, [])


if __name__ == "__main__":
    unittest.main()
