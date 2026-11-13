"""Tests for opt-in Linear Agent Session reporting."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

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

            with patch.object(agent_sessions.linear, "graphql",
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

            with patch.object(agent_sessions.linear, "graphql",
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

            with patch.object(agent_sessions.linear, "graphql",
                              side_effect=boom):
                self.assertIsNone(
                    agent_sessions.ensure_session(cfg, make_issue()))


if __name__ == "__main__":
    unittest.main()
