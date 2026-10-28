import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.github import client, workflow_policy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
