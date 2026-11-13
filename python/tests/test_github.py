import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.github import client, workflow_policy  # noqa: E402
from symphony.github import agent_tool  # noqa: E402
from symphony.agent import pi_runtime  # noqa: E402


def _issue_payload(**overrides):
    payload = {
        "number": 89,
        "state": "open",
        "title": "Fix thing",
        "body": "details",
        "html_url": "https://github.com/o/r/issues/89",
        "labels": [{"name": "agent"}],
        "assignee": {"login": "me"},
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        issue = client.normalize_issue(_issue_payload(), "o/r")
        self.assertEqual(issue.id, "89")
        self.assertEqual(issue.identifier, "GH-89")
        self.assertEqual(issue.title, "Fix thing")
        self.assertEqual(issue.description, "details")
        self.assertEqual(issue.state, "open")
        self.assertEqual(issue.labels, ["agent"])
        self.assertEqual(issue.assignee_id, "me")
        self.assertTrue(issue.dispatchable)
        self.assertEqual(issue.native_ref["repo"], "o/r")

    def test_pull_request_not_dispatchable(self):
        payload = _issue_payload()
        payload["pull_request"] = {"url": "x"}
        issue = client.normalize_issue(payload, "o/r")
        self.assertFalse(issue.dispatchable)

    def test_malformed(self):
        self.assertIsNone(client.normalize_issue({"number": 1}, "o/r"))
        self.assertIsNone(client.normalize_issue({"number": "x", "state": "open",
                                                  "title": "t"}, "o/r"))
        self.assertIsNone(client.normalize_issue("junk", "o/r"))

    def test_label_normalisation(self):
        issue = client.normalize_issue(
            _issue_payload(labels=[{"name": "Agent"}, "Bug", {"name": "agent"}]),
            "o/r")
        self.assertEqual(issue.labels, ["agent", "bug"])


class AllowRequestTest(unittest.TestCase):
    """github_api policy: configured-repo boundary + method/body allowlist."""

    _T = SimpleNamespace(
        kind="board", required_labels=[],
        provider={"repo": "o/r", "token": "t", "state_dir": "/var/lib/x"})

    def _ok(self, method, url, body=None):
        return workflow_policy.allow_request(self._T, method, url, body)

    def test_read_endpoints_allowed(self):
        self.assertTrue(self._ok(
            "GET", "https://api.github.com/repos/o/r/issues/1/comments"))
        self.assertTrue(self._ok(
            "GET", "https://api.github.com/repos/o/r/pulls/1/reviews"))
        self.assertTrue(self._ok(
            "GET", "https://api.github.com/repos/o/r/commits/abc/status"))
        self.assertTrue(self._ok(
            "GET", "https://api.github.com/repos/o/r/actions/jobs/9/logs"))

    def test_write_endpoints_scoped(self):
        self.assertTrue(self._ok(
            "POST", "https://api.github.com/repos/o/r/issues/1/comments",
            {"body": "hi"}))
        self.assertFalse(self._ok(
            "PUT", "https://api.github.com/repos/o/r/pulls/1/merge",
            {"sha": "a" * 40, "merge_method": "squash"}))
        self.assertTrue(self._ok(
            "DELETE", "https://api.github.com/repos/o/r/issues/comments/9"))
        self.assertTrue(self._ok(
            "PATCH", "https://api.github.com/repos/o/r/pulls/1",
            {"state": "closed"}))

    def test_repo_boundary_and_traversal_rejected(self):
        self.assertFalse(self._ok(
            "GET", "https://api.github.com/repos/other/x/issues/1"))
        self.assertFalse(self._ok(
            "GET", "https://api.github.com/repos/o/r/../etc"))
        self.assertFalse(self._ok(
            "GET", "https://api.github.com/orgs/o"))
        self.assertFalse(self._ok(
            "GET", "http://api.github.com/repos/o/r/issues/1"))
        self.assertFalse(self._ok(
            "GET", "https://evil.example.com/repos/o/r/issues/1"))

    def test_disallowed_methods_and_bodies(self):
        self.assertFalse(self._ok(
            "DELETE", "https://api.github.com/repos/o/r/pulls/1"))
        self.assertFalse(self._ok(
            "PUT", "https://api.github.com/repos/o/r/pulls/1/merge",
            {"sha": "a" * 40, "merge_method": "squash", "extra": 1}))
        self.assertFalse(self._ok(
            "POST", "https://api.github.com/repos/o/r/issues/1/comments",
            {"body": "hi", "evil": True}))
        self.assertFalse(self._ok(
            "PATCH", "https://api.github.com/repos/o/r/issues/1",
            {"merged": True}))


class ExecuteAgentToolTest(unittest.TestCase):
    """Tool boundary: relative paths only; failures never reach the wire."""

    _T = SimpleNamespace(
        kind="board", required_labels=[],
        provider={"repo": "o/r", "token": "t", "state_dir": "/var/lib/x"})

    def test_full_url_rejected_as_invalid_arguments(self):
        result = agent_tool.execute_agent_tool("github_api", {
            "method": "GET",
            "path": "https://api.github.com/repos/o/r/issues/1"}, self._T)
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_missing_leading_slash_rejected(self):
        result = agent_tool.execute_agent_tool("github_api", {
            "method": "GET", "path": "repos/o/r/issues/1"}, self._T)
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_forbidden_never_calls_request(self):
        with patch.object(client, "request") as call:
            result = agent_tool.execute_agent_tool("github_api", {
                "method": "DELETE",
                "path": "/repos/other/x/issues/comments/9"}, self._T)
        self.assertEqual(result["error"]["code"], "forbidden")
        call.assert_not_called()

    def test_allowed_call_reaches_request_with_api_path(self):
        with patch.object(client, "request",
                          return_value={"status": 200, "body": {"id": 1}}
                          ) as call:
            result = agent_tool.execute_agent_tool("github_api", {
                "method": "GET", "path": "/repos/o/r/issues/1"}, self._T)
        self.assertEqual(result["body"], {"id": 1})
        self.assertEqual(call.call_args.args[1], "/repos/o/r/issues/1")

    def test_non_2xx_surfaces_status_not_exception(self):
        with patch.object(client, "request",
                          return_value={"status": 404, "body": {"m": "x"}}):
            result = agent_tool.execute_agent_tool("github_api", {
                "method": "GET", "path": "/repos/o/r/issues/1"}, self._T)
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["error"]["code"], "github_status")

    def test_params_forwarded_for_pagination(self):
        with patch.object(client, "request",
                          return_value={"status": 200, "body": [{"id": 2}]}
                          ) as call:
            result = agent_tool.execute_agent_tool("github_api", {
                "method": "GET",
                "path": "/repos/o/r/issues/1/comments",
                "params": {"page": 2, "per_page": 100}}, self._T)
        self.assertEqual(result["status"], 200)
        self.assertEqual(call.call_args.args[2],
                         {"page": 2, "per_page": 100})

    def test_params_must_be_scalar_dict(self):
        with patch.object(client, "request") as call:
            for bad in ({"page": {"evil": 1}}, {"page": [1]}, "p=2", 5):
                result = agent_tool.execute_agent_tool("github_api", {
                    "method": "GET",
                    "path": "/repos/o/r/issues/1/comments",
                    "params": bad}, self._T)
                self.assertEqual(result["error"]["code"],
                                 "invalid_arguments")
        call.assert_not_called()

    def test_query_string_in_path_still_rejected(self):
        with patch.object(client, "request") as call:
            result = agent_tool.execute_agent_tool("github_api", {
                "method": "GET",
                "path": "/repos/o/r/issues/1/comments?page=2"}, self._T)
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        call.assert_not_called()


class PiRuntimeMcpInjectionTest(unittest.TestCase):
    """pi >= 1.5 needs builtin:mcp re-enabled and mcp__* in --tools."""

    class _C:
        command = ("pi --mode rpc --no-extensions --approve "
                   "--tools read,bash,edit --session-dir /x")
        mcp = {"enabled": True}

    def test_injects_extension_and_tool_pattern(self):
        out = pi_runtime._inject_mcp_args(self._C.command, self._C)
        self.assertIn("-e builtin:mcp", out)
        self.assertIn("mcp__*", out)
        self.assertIn("read,bash,edit,mcp__*", out)

    def test_disabled_mcp_is_noop(self):
        cfg = self._C()
        cfg.mcp = {"enabled": False}
        self.assertEqual(
            pi_runtime._inject_mcp_args(self._C.command, cfg), self._C.command)

    def test_no_tools_flag_left_alone(self):
        cfg = self._C()
        cfg.command = "pi --mode rpc --no-extensions"
        out = pi_runtime._inject_mcp_args(cfg.command, cfg)
        self.assertIn("-e builtin:mcp", out)
        self.assertNotIn("mcp__*", out)

    def test_already_injected_is_idempotent(self):
        cfg = self._C()
        cfg.command = self._C.command.replace(
            "--tools read", "--tools read,mcp__*") + " -e builtin:mcp"
        self.assertEqual(
            pi_runtime._inject_mcp_args(cfg.command, cfg), cfg.command)


if __name__ == "__main__":
    unittest.main()
