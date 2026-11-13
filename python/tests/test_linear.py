"""Offline parity tests for the upstream Linear tracker with Python/Pi."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, linear, tracker, mcp_bridge
from symphony.workflow import WorkflowError


def settings():
    return config.parse_settings({
        "tracker": {
            "kind": "linear",
            "provider": {"project_slug": "example-project", "api_key": "test-key"},
            "active_states": ["Todo", "In Progress", "Merging", "Rework"],
            "terminal_states": ["Done", "Cancelled", "Canceled", "Closed", "Duplicate"],
        },
        "runtime": {"kind": "pi", "mcp": {"enabled": True}},
    })


def row(identifier="ABC-1", state="Todo", assignee=None, blocker=None):
    relations = []
    if blocker is not None:
        relations.append({
            "type": "blocks",
            "issue": {"id": "b1", "identifier": "ABC-0", "state": {"name": blocker}},
        })
    return {
        "id": identifier + "-uuid", "identifier": identifier,
        "title": "Example", "description": "Description",
        "priority": 2, "state": {"name": state}, "branchName": None,
        "url": "https://linear.app/example/issue/" + identifier,
        "assignee": {"id": assignee} if assignee else None,
        "labels": {"nodes": [{"name": "Agent"}]},
        "inverseRelations": {"nodes": relations},
        "createdAt": "2026-11-12T00:00:00Z",
        "updatedAt": "2026-11-12T00:01:00Z",
    }


class LinearTrackerTests(unittest.TestCase):
    def test_linear_config_from_provider(self):
        cfg = settings()
        self.assertEqual(cfg.tracker.project_slug, "example-project")
        self.assertEqual(cfg.tracker.endpoint, "https://api.linear.app/graphql")
        self.assertIn("LINEAR_API_KEY", cfg.tracker.secret_environment_names)
        self.assertIn("GITHUB_TOKEN", cfg.tracker.secret_environment_names)
        self.assertIn("SYMPHONY_GITHUB_TOKEN", cfg.tracker.secret_environment_names)
        linear.validate_config(cfg.tracker)

    def test_poll_pagination_and_mapping(self):
        cfg = settings().tracker
        pages = [
            {"data": {"issues": {"nodes": [row("ABC-1")],
              "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
            {"data": {"issues": {"nodes": [row("ABC-2", state="In Progress")],
              "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ]
        with patch.object(linear, "graphql", side_effect=pages) as call:
            got = tracker.fetch_candidate_issues(cfg)
        self.assertEqual([i.identifier for i in got], ["ABC-1", "ABC-2"])
        self.assertEqual(got[0].labels, ["agent"])
        self.assertTrue(got[0].dispatchable)
        self.assertEqual(got[0].priority, 2)
        self.assertEqual(call.call_count, 2)

    def test_todo_blocker_and_assignee(self):
        cfg = settings().tracker
        blocked = linear._normalize(row(blocker="In Progress"), cfg)
        self.assertFalse(blocked.dispatchable)
        completed = linear._normalize(row(blocker="Done"), cfg)
        self.assertTrue(completed.dispatchable)
        ongoing = linear._normalize(row(state="In Progress", blocker="Todo"), cfg)
        self.assertTrue(ongoing.dispatchable)
        self.assertFalse(linear._normalize(row(assignee="other"), cfg, "me-id").dispatchable)

    def test_id_refresh_is_project_scoped_and_ordered(self):
        cfg = settings().tracker
        def response(_settings, query, variables):
            self.assertIn("projectSlug", variables)
            return {"data": {"issues": {"nodes": [row("ABC-2"), row("ABC-1")]}}}
        with patch.object(linear, "graphql", side_effect=response):
            got = linear.fetch_issues_by_ids(cfg, ["ABC-1-uuid", "ABC-2-uuid"])
        self.assertEqual([i.identifier for i in got], ["ABC-1", "ABC-2"])

    def test_invalid_pagination_fails_closed(self):
        cfg = settings().tracker
        with patch.object(linear, "graphql", return_value={
            "data": {"issues": {"nodes": [], "pageInfo": {
                "hasNextPage": True, "endCursor": None}}}}):
            with self.assertRaises(WorkflowError):
                linear.fetch_candidate_issues(cfg)

    def test_pi_exposes_only_linear_tool(self):
        cfg = settings().tracker
        self.assertEqual([s["name"] for s in tracker.tool_specs(cfg)], ["linear_graphql"])
        with patch.object(linear, "graphql", return_value={"data": {"viewer": {"id": "me"}}}):
            response = tracker.execute_agent_tool(
                "linear_graphql", {"query": "query {viewer {id}}"}, cfg)
        self.assertEqual(response["data"]["viewer"]["id"], "me")
        self.assertTrue(mcp_bridge._to_mcp_result({"error": {"code": "failed"}})["isError"])

    def test_requires_auth(self):
        cfg = settings().tracker
        cfg.api_key = None
        cfg.provider["api_key"] = None
        with patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            with self.assertRaises(WorkflowError):
                linear.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
