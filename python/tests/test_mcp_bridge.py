"""Security regressions for per-worker MCP credentials and frozen scope."""

import json
import http.client
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, mcp_bridge, tracker
from symphony.agent import pi_runtime
from symphony.linear import IssueScope
from symphony.workflow import WorkflowError


def settings(project_slug="project-a", repo="acme/repo-a"):
    cfg = config.parse_settings({
        "tracker": {
            "kind": "linear",
            "provider": {
                "project_slug": project_slug,
                "api_key": "host-linear-secret",
            },
            "active_states": ["Todo", "In Progress", "Rework"],
            "terminal_states": ["Done", "Cancelled"],
        },
        "delivery": {
            "enabled": True,
            "repo": repo,
            "token": "host-github-secret",
        },
        "runtime": {"kind": "pi", "mcp": {"enabled": True}},
        "workspace": {"root": os.path.join(
            os.path.dirname(__file__), "_mcp_test_workspaces")},
    })
    return cfg


class McpBridgeScopeTests(unittest.TestCase):
    def test_scope_stats_report_usage_without_exposing_tokens(self):
        state = mcp_bridge._State(None, 1234)
        cfg = settings()
        state.register_scope(
            cfg.tracker, cfg.delivery, IssueScope(id="issue-a", identifier="ABC-1"))

        stats = state.stats()

        self.assertTrue(stats["running"])
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["limit"], mcp_bridge.MAX_SCOPES)
        self.assertGreater(stats["utilization"], 0)
        self.assertNotIn("scopes", stats)

    def test_pi_directory_link_cannot_redirect_sanitise_or_mcp_config(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            external = os.path.join(root, "external")
            os.makedirs(workspace)
            os.makedirs(external)
            sentinel = os.path.join(external, "do-not-delete.txt")
            with open(sentinel, "w", encoding="utf-8") as handle:
                handle.write("outside")
            link = os.path.join(workspace, ".pi")
            try:
                if os.name == "nt":
                    import _winapi

                    _winapi.CreateJunction(external, link)
                else:
                    os.symlink(
                        external, link, target_is_directory=True)
            except (AttributeError, OSError) as error:
                self.skipTest("directory links unavailable: %r" % (error,))

            chmod_calls = []
            real_chmod = os.chmod

            def record_chmod(path, mode):
                chmod_calls.append((path, mode))
                return real_chmod(path, mode)

            try:
                with patch.object(mcp_bridge, "_bridge", state), \
                        patch.object(
                            pi_runtime.os, "chmod", side_effect=record_chmod):
                    pi_runtime._sanitise_pi_dir(workspace)
                    token = pi_runtime._write_mcp_config(
                        workspace, cfg, issue)
            finally:
                if os.path.lexists(link) \
                        and pi_runtime._is_link_or_reparse(link):
                    pi_runtime._remove_path_no_follow(link)

            self.assertIsNotNone(token)
            self.assertTrue(os.path.isfile(sentinel))
            self.assertTrue(os.path.isfile(
                os.path.join(workspace, ".pi", "mcp.json")))
            self.assertFalse(os.path.exists(
                os.path.join(external, "mcp.json")))
            self.assertFalse(pi_runtime._is_link_or_reparse(link))
            self.assertIn(
                (os.path.join(workspace, ".pi"), 0o700), chmod_calls)
            self.assertIn(
                (os.path.join(workspace, ".pi", "mcp.json"), 0o600),
                chmod_calls)
            if os.name != "nt":
                self.assertEqual(
                    os.stat(os.path.join(
                        workspace, ".pi")).st_mode & 0o777,
                    0o700,
                )
                self.assertEqual(
                    os.stat(os.path.join(
                        workspace, ".pi", "mcp.json")).st_mode & 0o777,
                    0o600,
                )

    def test_http_requires_a_scoped_bearer(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        bridge = mcp_bridge.start()
        try:
            token = bridge.register_scope(
                cfg.tracker, cfg.delivery,
                IssueScope(id=issue.id, identifier=issue.identifier))
            url = "http://127.0.0.1:%d%s" % (bridge.port, mcp_bridge.MCP_PATH)
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
            }
            body = json.dumps(request).encode("utf-8")

            with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                urllib.request.urlopen(urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json"}), timeout=5)
            self.assertEqual(unauthorized.exception.code, 401)
            self.assertEqual(
                unauthorized.exception.headers.get("Connection"), "close")

            valid = urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                })
            with urllib.request.urlopen(valid, timeout=5) as response:
                payload = json.load(response)
            self.assertEqual(payload["result"]["protocolVersion"],
                             mcp_bridge.PROTOCOL_VERSION)
        finally:
            mcp_bridge.stop()

    def test_http_rejects_malformed_and_oversized_request_lengths(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        bridge = mcp_bridge.start()
        try:
            token = bridge.register_scope(
                cfg.tracker, cfg.delivery,
                IssueScope(id=issue.id, identifier=issue.identifier))
            headers = {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            }
            for value, expected in (
                    ("not-a-number", 400),
                    (str(mcp_bridge.MAX_REQUEST_BYTES + 1), 413)):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", bridge.port, timeout=5)
                connection.putrequest("POST", mcp_bridge.MCP_PATH)
                for name, header_value in headers.items():
                    connection.putheader(name, header_value)
                connection.putheader("Content-Length", value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
                connection.close()
        finally:
            mcp_bridge.stop()

    def test_client_config_contains_scoped_bearer_not_host_tokens(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        with patch.object(mcp_bridge, "_bridge", state):
            value = mcp_bridge.client_config(
                workspace="/tmp/ABC-1", settings=cfg, issue=issue)

        self.assertIsNotNone(value)
        config_value, token_value = value
        encoded = json.dumps(config_value)
        self.assertNotIn("host-linear-secret", encoded)
        self.assertNotIn("host-github-secret", encoded)
        token = config_value["mcpServers"]["symphony"]["headers"]["Authorization"]
        self.assertTrue(token.startswith("Bearer "))
        bearer = token.split(" ", 1)[1]
        self.assertEqual(bearer, token_value)
        self.assertIsNotNone(state.scope_for_token(bearer))

    def test_each_spawn_gets_a_distinct_revocable_scope(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        with patch.object(mcp_bridge, "_bridge", state):
            first = mcp_bridge.client_config(
                workspace="/tmp/ABC-1-a", settings=cfg, issue=issue)
            second = mcp_bridge.client_config(
                workspace="/tmp/ABC-1-b", settings=cfg, issue=issue)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        _, first_token = first
        _, second_token = second
        self.assertNotEqual(first_token, second_token)
        self.assertTrue(state.revoke_scope(first_token))
        self.assertIsNone(state.scope_for_token(first_token))
        self.assertIsNotNone(state.scope_for_token(second_token))

    def test_hot_reload_does_not_mutate_old_worker_scope(self):
        old_cfg = settings(project_slug="project-a", repo="acme/repo-a")
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        scope = IssueScope(id=issue.id, identifier=issue.identifier)
        state = mcp_bridge._State(None, 1234)
        old_token = state.register_scope(
            old_cfg.tracker, old_cfg.delivery, scope)

        new_cfg = settings(project_slug="project-b", repo="acme/repo-b")
        new_token = state.register_scope(
            new_cfg.tracker, new_cfg.delivery, scope)

        self.assertNotEqual(old_token, new_token)
        old_tracker, old_delivery, old_issue = state.scope_for_token(old_token)
        new_tracker, new_delivery, new_issue = state.scope_for_token(new_token)
        self.assertEqual(old_tracker.project_slug, "project-a")
        self.assertEqual(old_delivery.repo, "acme/repo-a")
        self.assertEqual(new_tracker.project_slug, "project-b")
        self.assertEqual(new_delivery.repo, "acme/repo-b")
        self.assertEqual(old_issue, new_issue)

        body = {"method": "GET", "path": "/repos/acme/repo-a/pulls/13"}
        with patch(
                "symphony.github.agent_tool.execute_delivery_tool",
                return_value={"status": 200}) as execute:
            result = tracker.execute_agent_tool(
                "github_api", body, old_tracker,
                issue_scope=old_issue, delivery_settings=old_delivery)
        self.assertEqual(result, {"status": 200})
        self.assertEqual(execute.call_args.args[1].repo, "acme/repo-a")
        self.assertEqual(execute.call_args.kwargs["issue_scope"], old_issue)

    def test_tools_use_frozen_scope_not_live_workflow_store(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        scope = IssueScope(id=issue.id, identifier=issue.identifier)
        token = state.register_scope(cfg.tracker, cfg.delivery, scope)
        frozen = state.scope_for_token(token)

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "linear_graphql", "arguments": {"query": "q"}},
        }
        with patch.object(
                tracker, "execute_agent_tool",
                return_value={"data": {"ok": True}}) as execute:
            response = mcp_bridge._handle_rpc(request, frozen)

        self.assertTrue(response["result"]["content"][0]["text"])
        self.assertEqual(execute.call_args.args[2].project_slug, "project-a")
        self.assertEqual(
            execute.call_args.kwargs["delivery_settings"].repo, "acme/repo-a")
        self.assertEqual(execute.call_args.kwargs["issue_scope"], scope)

    def test_unknown_or_missing_bearer_has_no_scope(self):
        state = mcp_bridge._State(None, 1234)
        self.assertIsNone(state.scope_for_token(""))
        self.assertIsNone(state.scope_for_token("not-a-token"))
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        }
        response = mcp_bridge._handle_rpc(request)
        self.assertEqual(response["error"]["code"], -32602)

    def test_mcp_config_write_failure_revokes_scope(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        with tempfile.TemporaryDirectory() as workspace, \
                patch.object(mcp_bridge, "_bridge", state):
            with patch("builtins.open", side_effect=OSError("disk full")):
                with self.assertRaises(WorkflowError) as raised:
                    pi_runtime._write_mcp_config(
                        workspace, cfg, issue)

        self.assertEqual(raised.exception.reason, "pi_mcp_config_failed")
        self.assertEqual(state._scopes, {})

    def test_scope_is_revoked_on_stop_request(self):
        cfg = settings()
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        token = state.register_scope(
            cfg.tracker, cfg.delivery,
            IssueScope(id=issue.id, identifier=issue.identifier))
        process = Mock()
        process.stdin = Mock()
        process.stdin.close.side_effect = RuntimeError("stream already broken")
        process.stdout = Mock()
        process.wait.return_value = 0
        session = pi_runtime.PiSession(
            process, "", cfg.runtime, "session-a", None, mcp_token=token)

        with patch.object(mcp_bridge, "_bridge", state), \
                patch.object(pi_runtime, "_force_kill_process_tree",
                             return_value=True), \
                patch.object(pi_runtime, "_process_tree_alive",
                             return_value=False), \
                patch.object(pi_runtime, "_session_scan_confirmed_gone",
                             return_value=False):
            self.assertFalse(pi_runtime.stop_session(session))
        self.assertIsNone(state.scope_for_token(token))

        with patch.object(mcp_bridge, "_bridge", state), \
                patch.object(pi_runtime, "_force_kill_process_tree",
                             return_value=True), \
                patch.object(pi_runtime, "_process_tree_alive",
                             return_value=False), \
                patch.object(pi_runtime, "_session_scan_confirmed_gone",
                             return_value=True):
            self.assertTrue(pi_runtime.stop_session(session))
        self.assertIsNone(state.scope_for_token(token))

    def test_startup_failure_revokes_scope_even_when_orphan_unconfirmed(self):
        cfg = settings()
        cfg.runtime.command = "pi --mode rpc"
        issue = SimpleNamespace(id="issue-a", identifier="ABC-1")
        state = mcp_bridge._State(None, 1234)
        token = state.register_scope(
            cfg.tracker, cfg.delivery,
            IssueScope(id=issue.id, identifier=issue.identifier))
        process = Mock()
        process.pid = 4321
        process.stdin = Mock()
        process.stdout = Mock()

        with tempfile.TemporaryDirectory() as workspace, \
                patch.object(mcp_bridge, "_bridge", state), \
                patch.object(pi_runtime.subprocess, "Popen",
                             return_value=process), \
                patch.object(pi_runtime, "_open_live_log",
                             side_effect=RuntimeError("live log failed")), \
                patch.object(pi_runtime, "force_terminate_session",
                             return_value=False):
            with self.assertRaises(pi_runtime.TerminationError) as raised:
                pi_runtime._open_session(
                    workspace, cfg.runtime, cfg, "session-a",
                    mcp_token=token)

        self.assertIsNotNone(raised.exception.session)
        self.assertIsNone(state.scope_for_token(token))
        process.stdin.close.assert_not_called()
        process.stdout.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
