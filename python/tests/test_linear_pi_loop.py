"""Local control-plane test for the Linear -> Python -> Pi loop.

This uses an in-process Linear GraphQL stub and a small Pi RPC process. It
does not claim to be a real Linear/Pi integration test; it exercises the
orchestrator, workspace, runner, and MCP-independent handoff deterministically.
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import mcp_bridge, orchestrator, tracker, workflow_store  # noqa: E402
from symphony import workspace as workspace_mod  # noqa: E402


class _LinearState:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "Todo"
        self.poll_calls = 0
        self.id_calls = 0
        self.finish_calls = 0
        self.tool_calls = 0

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "poll_calls": self.poll_calls,
                "id_calls": self.id_calls,
                "finish_calls": self.finish_calls,
                "tool_calls": self.tool_calls,
            }

    def finish(self):
        with self.lock:
            self.finish_calls += 1
            self.state = "Human Review"


class _LinearHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None
    issue_id = "issue-1"
    identifier = "LOC-1"

    def log_message(self, _format, *_args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/finish":
            self.state.finish()
            self._reply(200, {"ok": True})
            return
        if self.path != "/graphql":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        query = request.get("query", "")
        variables = request.get("variables") or {}
        if "SymphonyLinearWorkerIssueScope" in query:
            self._reply(200, {"data": {"issue": {
                "id": self.issue_id,
                "identifier": self.identifier,
                "project": {"slugId": "local-project"},
                "team": {"states": {"nodes": [
                    {"id": "progress", "name": "In Progress"},
                    {"id": "review", "name": "Human Review"},
                    {"id": "rework", "name": "Rework"},
                    {"id": "merging", "name": "Merging"},
                    {"id": "done", "name": "Done"},
                ]}},
            }}})
            return
        if "SymphonyLinearViewer" in query:
            self._reply(200, {"data": {"viewer": {"id": "viewer-1"}}})
            return
        if "SymphonyLinearIssuesById" in query:
            with self.state.lock:
                self.state.id_calls += 1
                state = self.state.state
            issue = self._issue(state)
            wanted = variables.get("ids") or []
            nodes = [issue] if issue["id"] in wanted else []
            self._reply(200, {"data": {"issues": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}})
            return
        if "SymphonyLinearPoll" in query:
            with self.state.lock:
                self.state.poll_calls += 1
                state = self.state.state
            wanted_states = variables.get("stateNames") or []
            nodes = [self._issue(state)] if state in wanted_states else []
            self._reply(200, {"data": {"issues": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}})
            return
        if "issueUpdate" in query:
            with self.state.lock:
                self.state.tool_calls += 1
                self.state.state = "Human Review"
            self._reply(200, {"data": {"issueUpdate": {"success": True}}})
            return
        self._reply(200, {"data": {}})

    def _issue(self, state):
        return {
            "id": self.issue_id,
            "identifier": self.identifier,
            "title": "Local Pi loop",
            "description": "Exercise the local control plane.",
            "priority": 2,
            "state": {"name": state},
            "branchName": None,
            "url": "https://linear.example/" + self.identifier,
            "assignee": None,
            "labels": {"nodes": []},
            "inverseRelations": {"nodes": []},
            "createdAt": "2026-11-13T00:00:00Z",
            "updatedAt": "2026-11-13T00:01:00Z",
        }


class _FakePiServer:
    def __init__(self, directory):
        self.state = _LinearState()
        handler = type(
            "BoundLinearHandler", (_LinearHandler,),
            {"state": self.state},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-linear", daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


_FAKE_PI = r"""
import json
import os
import pathlib
import sys
import time
import urllib.request

# The host sends one JSON-RPC prompt line. The fake deliberately consumes only
# that line and does not implement a model.
sys.stdin.readline()
marker = os.environ.get("FAKE_PI_MARKER")
if marker:
    pathlib.Path(marker).write_text(os.getcwd(), encoding="utf-8")
delay = float(os.environ.get("FAKE_PI_DELAY", "0"))
if delay:
    time.sleep(delay)
finish = os.environ.get("FAKE_LINEAR_FINISH")
if finish:
    urllib.request.urlopen(finish, data=b"{}", timeout=5).read()
print(json.dumps({"type": "agent_settled", "aborted": False}), flush=True)
"""


class LinearPiLoopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="symphony-loop-", ignore_cleanup_errors=True)
        self.root = self.temp.name
        self.fake = _FakePiServer(self.root)
        self.workflow_path = os.path.join(self.root, "WORKFLOW.md")
        self.script_path = os.path.join(self.root, "fake_pi.py")
        self.marker = os.path.join(self.root, "pi-started.txt")
        with open(self.script_path, "w", encoding="utf-8") as handle:
            handle.write(_FAKE_PI)
        python = sys.executable.replace("\\", "/")
        command = '"%s" "%s"' % (
            python, self.script_path.replace("\\", "/"))
        command_yaml = "'" + command.replace("'", "''") + "'"
        self.workflow = """\
---
tracker:
  kind: linear
  provider:
    project_slug: local-project
    api_key: local-token
    endpoint: http://127.0.0.1:%d/graphql
  active_states:
    - Todo
  terminal_states:
    - Human Review
    - Done
polling:
  interval_ms: 50
workspace:
  root: %s
agent:
  max_concurrent_agents: 1
  max_turns: 1
runtime:
  kind: pi
  command: %s
  turn_timeout_ms: 5000
  stall_timeout_ms: 0
  mcp:
    enabled: false
server:
  host: 127.0.0.1
  port: 0
---
Handle {{ issue.identifier }} in the local test.
""" % (self.fake.port, os.path.join(self.root, "workspaces"), command_yaml)
        with open(self.workflow_path, "w", encoding="utf-8") as handle:
            handle.write(self.workflow)
        os.environ["FAKE_PI_MARKER"] = self.marker
        os.environ["FAKE_LINEAR_FINISH"] = (
            "http://127.0.0.1:%d/finish" % self.fake.port)
        os.environ["FAKE_PI_DELAY"] = "0"
        self.store = None
        self.orch = None
        self.linear_validation = patch.object(
            tracker.linear_adapter, "validate_config", return_value=None)
        self.linear_validation.start()

    def tearDown(self):
        if self.orch is not None:
            self.orch.stop()
        if self.store is not None:
            workflow_store.stop_store()
        self.fake.close()
        self.linear_validation.stop()
        for key in ("FAKE_PI_MARKER", "FAKE_LINEAR_FINISH", "FAKE_PI_DELAY"):
            os.environ.pop(key, None)
        self.temp.cleanup()

    def _start(self):
        self.store = workflow_store.start_store(self.workflow_path)
        self.orch = orchestrator.Orchestrator(self.store)
        self.orch.start()
        return self.orch

    def _wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_todo_claim_runs_pi_and_releases_after_review_state(self):
        orch = self._start()
        self.assertTrue(self._wait(
            lambda: os.path.exists(self.marker) and self.fake.state.snapshot()[
                "finish_calls"] == 1))
        settings = self.store.settings()
        issue = type("Issue", (), {"identifier": "LOC-1"})()
        path = workspace_mod.workspace_path(settings, issue, self.workflow_path)
        with open(self.marker, "r", encoding="utf-8") as handle:
            self.assertEqual(os.path.normcase(os.path.abspath(handle.read())),
                             os.path.normcase(os.path.abspath(path)))
        self.assertTrue(self._wait(
            lambda: self.fake.state.snapshot()["state"] == "Human Review"
            and not orch.running))
        self.assertTrue(self._wait(
            lambda: "issue-1" not in orch.claimed, timeout=3))
        snapshot = self.fake.state.snapshot()
        self.assertEqual(snapshot["finish_calls"], 1)
        self.assertGreaterEqual(snapshot["poll_calls"], 1)
        self.assertGreaterEqual(snapshot["id_calls"], 2)

    def test_repeated_refresh_while_running_does_not_claim_twice(self):
        os.environ["FAKE_PI_DELAY"] = "0.8"
        orch = self._start()
        self.assertTrue(self._wait(lambda: os.path.exists(self.marker)))
        orch.request_refresh()
        orch.request_refresh()
        self.assertTrue(self._wait(
            lambda: self.fake.state.snapshot()["finish_calls"] == 1,
            timeout=10))
        self.assertEqual(self.fake.state.snapshot()["finish_calls"], 1)

    @unittest.skipUnless(
        os.environ.get("SYMPHONY_REAL_PI") == "1",
        "set SYMPHONY_REAL_PI=1 to run against the installed pi runtime",
    )
    def test_real_pi_can_call_host_linear_tool(self):
        with open(self.workflow_path, "r", encoding="utf-8") as handle:
            workflow = handle.read()
        workflow = workflow.replace(
            "  mcp:\n    enabled: false\n",
            "  mcp:\n    enabled: true\n",
        )
        workflow = re.sub(
            r"(?m)^  command: .*$",
            '  command: \'"C:/nvm4w/nodejs/pi.cmd" --mode rpc --no-session '
            "--no-extensions --tools read,bash,edit,write,grep,find,ls "
            "--approve'",
            workflow,
            count=1,
        )
        workflow = workflow.replace(
            "Handle {{ issue.identifier }} in the local test.",
            "Use the linear_graphql tool exactly once with this query:\n"
            "mutation { issueUpdate(id: \"issue-1\", "
            "input: {stateId: \"review\"}) { success } }\n"
            "After it succeeds, reply DONE. Do not edit files.",
        )
        with open(self.workflow_path, "w", encoding="utf-8") as handle:
            handle.write(workflow)
        mcp_bridge.start()
        try:
            orch = self._start()
            self.assertTrue(self._wait(
                lambda: self.fake.state.snapshot()["tool_calls"] == 1,
                timeout=90))
            self.assertTrue(self._wait(
                lambda: self.fake.state.snapshot()["state"] == "Human Review"
                and not orch.running, timeout=30))
        finally:
            mcp_bridge.stop()


if __name__ == "__main__":
    unittest.main()
