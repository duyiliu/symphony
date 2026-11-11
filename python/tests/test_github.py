import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.github import client, workflow_policy  # noqa: E402
from symphony.github import agent_tool  # noqa: E402
from symphony.github.workflow_api import GateError  # noqa: E402
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


class PolicyTest(unittest.TestCase):
    class _T:
        kind = "github"
        required_labels = ["agent"]
        provider = {"repo": "o/r", "token": "t"}
        github_workflow = {}

    def test_glob(self):
        self.assertTrue(workflow_policy.path_matches(
            ["**/*.sql"], "db/migrate/x.sql"))
        self.assertTrue(workflow_policy.path_matches(
            ["deploy/**"], "deploy/foo.service"))
        self.assertTrue(workflow_policy.path_matches(
            ["**/Dockerfile*"], "services/api/Dockerfile"))
        self.assertFalse(workflow_policy.path_matches(
            ["deploy/**"], "src/deploy.go"))
        self.assertTrue(workflow_policy.path_matches(
            ["**/deploy/**"], "src/deploy/x.sh"))

    def test_phase(self):
        self.assertEqual(workflow_policy.phase(["agent"]), "develop")
        self.assertEqual(workflow_policy.phase([]), "waiting")
        self.assertEqual(
            workflow_policy.phase(["agent", "needs-human"]), "invalid")

    def test_allow_request_disabled_is_permissive(self):
        # github_workflow not enabled -> allow_request is a no-op
        workflow_policy.allow_request(self._T, "DELETE", "/repos/o/r/issues/1", None)

    def test_allow_request_enabled_restricts(self):
        tracker = self._T()
        tracker.github_workflow = {
            "enabled": True,
            "state_dir": "/var/lib/symphony/github-workflow",
            "base_branch": "master",
        }
        from symphony.workflow import WorkflowError
        with self.assertRaises(WorkflowError):
            workflow_policy.allow_request(tracker, "DELETE", "/repos/o/r/issues/1", None)
        workflow_policy.allow_request(tracker, "GET", "/repos/o/r/issues/1", None)
        workflow_policy.allow_request(
            tracker, "POST", "/repos/o/r/issues/1/comments", {"body": "hi"})
        with self.assertRaises(WorkflowError):
            workflow_policy.allow_request(
                tracker, "POST", "/repos/o/r/issues/1/comments",
                {"body": "hi", "evil": True})
        with self.assertRaises(WorkflowError):
            workflow_policy.allow_request(
                tracker, "GET", "/repos/o/r/../etc", None)
        with self.assertRaises(WorkflowError):
            workflow_policy.allow_request(
                tracker, "GET", "/repos/other/x/issues/1", None)


class ToolArgumentNormalizeTest(unittest.TestCase):
    """Models sometimes send `body`/`params` as serialized JSON strings."""

    def test_string_body_coerced_to_dict(self):
        _m, _p, _pa, body = agent_tool._normalize_arguments({
            "method": "PATCH", "path": "/repos/o/r/pulls/1",
            "body": '{"body": "hi"}'})
        self.assertEqual(body, {"body": "hi"})

    def test_dict_body_passthrough(self):
        _m, _p, _pa, body = agent_tool._normalize_arguments({
            "method": "PATCH", "path": "/repos/o/r/pulls/1",
            "body": {"body": "hi"}})
        self.assertEqual(body, {"body": "hi"})

    def test_string_params_coerced(self):
        _m, _p, params, _b = agent_tool._normalize_arguments({
            "method": "GET", "path": "/repos/o/r/issues/1",
            "params": '{"per_page": 5}'})
        self.assertEqual(params, {"per_page": 5})

    def test_invalid_body_rejected_clearly(self):
        for bad in (42, "not-json", "[1, 2]"):
            with self.assertRaises(GateError) as ctx:
                agent_tool._normalize_arguments({
                    "method": "POST",
                    "path": "/repos/o/r/issues/1/comments", "body": bad})
            self.assertEqual(ctx.exception.reason, "invalid_body")


class ApprovalLabelTest(unittest.TestCase):
    class _T:
        kind = "github"
        required_labels = ["agent"]
        provider = {"repo": "o/r", "token": "t"}
        github_workflow = {
            "enabled": True,
            "state_dir": "/var/lib/symphony/github-workflow",
            "base_branch": "master",
            "approval_label": "merge-approved",
        }

    def test_approval_label_configured(self):
        self.assertEqual(
            workflow_policy.approval_label(self._T), "merge-approved")

    def test_approval_label_unset(self):
        tracker = self._T()
        tracker.github_workflow = dict(tracker.github_workflow)
        del tracker.github_workflow["approval_label"]
        self.assertIsNone(workflow_policy.approval_label(tracker))

    def test_label_approved(self):
        from symphony.github import workflow as gh_workflow
        self.assertTrue(
            gh_workflow._label_approved(self._T, ["agent", "merge-approved"]))
        self.assertFalse(gh_workflow._label_approved(self._T, ["agent"]))
        tracker = self._T()
        tracker.github_workflow = dict(tracker.github_workflow)
        del tracker.github_workflow["approval_label"]
        self.assertFalse(
            gh_workflow._label_approved(tracker, ["merge-approved"]))

    def test_approval_label_rejects_execution_labels(self):
        from symphony.workflow import WorkflowError
        for bad in ("agent", "needs-human", "high-risk", "", "  "):
            tracker = self._T()
            tracker.github_workflow = dict(tracker.github_workflow)
            tracker.github_workflow["approval_label"] = bad
            with self.assertRaises(WorkflowError):
                workflow_policy.validate(tracker)

    def test_merge_policy(self):
        self.assertEqual(workflow_policy.merge_policy(self._T), "auto")
        tracker = self._T()
        tracker.github_workflow = dict(
            tracker.github_workflow, merge_policy="manual")
        self.assertEqual(workflow_policy.merge_policy(tracker), "manual")
        tracker.github_workflow = dict(
            tracker.github_workflow, merge_policy="bogus")
        from symphony.workflow import WorkflowError
        with self.assertRaises(WorkflowError):
            workflow_policy.validate(tracker)


class ClassifyChecksTest(unittest.TestCase):
    """Transient check-run conclusions must re-poll, not dispatch repairs."""

    SHA = "a" * 40

    def _run(self, conclusion, name="job", status="completed"):
        return {
            "name": name, "status": status, "conclusion": conclusion,
            "head_sha": self.SHA, "id": 1, "app": {"id": 1},
        }

    def test_success_skipped_pass(self):
        from symphony.github import workflow as gh_workflow
        pending, failing = gh_workflow.classify_checks(
            [self._run("success"), self._run("skipped", "mutation")],
            [], self.SHA)
        self.assertEqual(pending, [])
        self.assertEqual(failing, [])

    def test_null_conclusion_is_pending_not_failing(self):
        from symphony.github import workflow as gh_workflow
        pending, failing = gh_workflow.classify_checks(
            [self._run("success"), self._run(None, "mutation")],
            [], self.SHA)
        self.assertEqual(pending, ["mutation"])
        self.assertEqual(failing, [])

    def test_transient_conclusions_pending(self):
        from symphony.github import workflow as gh_workflow
        for conclusion in ("action_required", "startup_failure"):
            pending, failing = gh_workflow.classify_checks(
                [self._run(conclusion)], [], self.SHA)
            self.assertEqual(pending, ["job"])
            self.assertEqual(failing, [])

    def test_real_failures_still_fail(self):
        from symphony.github import workflow as gh_workflow
        for conclusion in ("failure", "timed_out", "cancelled"):
            _pending, failing = gh_workflow.classify_checks(
                [self._run(conclusion)], [], self.SHA)
            self.assertEqual(failing, ["job"])

    def test_in_progress_is_pending(self):
        from symphony.github import workflow as gh_workflow
        pending, failing = gh_workflow.classify_checks(
            [self._run(None, status="in_progress")], [], self.SHA)
        self.assertEqual(pending, ["job"])
        self.assertEqual(failing, [])


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
