"""Regression tests for behaviours aligned with the Elixir reference during
the 1:1 semantic audit (workflow gates, receipts, mailbox staging, globbing).
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.github import workflow as gh_workflow  # noqa: E402
from symphony.github import workflow_api, workflow_policy  # noqa: E402
from symphony import workspace as workspace_mod  # noqa: E402
from symphony.workspace import workspace_key  # noqa: E402


def _issue(**kw):
    base = dict(
        id="89", identifier="GH-89", title="t", state="open",
        labels=["agent"], dispatchable=True, blocked_by=[],
    )
    base.update(kw)
    return Issue(**base)


def _tracker_settings(**overrides):
    settings = SimpleNamespace(
        kind="github",
        provider={"repo": "o/r", "token": "t"},
        required_labels=[],
        active_states=["open"],
        terminal_states=["closed"],
        github_workflow={
            "enabled": True,
            "state_dir": "/var/lib/symphony/github-workflow",
            "base_branch": "main",
        },
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class EnsureTruthinessTest(unittest.TestCase):
    """Elixir ``if`` treats only nil/false as falsy."""

    def test_falsy_values_raise(self):
        for value in (None, False):
            with self.assertRaises(workflow_api.GateError):
                workflow_api.ensure(value, "boom")

    def test_other_values_pass(self):
        for value in (0, "", [], {}, "x", 1):
            self.assertIsNone(workflow_api.ensure(value, "boom"))


class NumberParseTest(unittest.TestCase):
    def test_valid_numbers(self):
        self.assertEqual(gh_workflow.number_or_fail(89), 89)
        self.assertEqual(gh_workflow.number_or_fail("89"), 89)
        self.assertEqual(gh_workflow.number_or_fail("+89"), 89)

    def test_invalid_numbers(self):
        for value in (True, False, "8x", "89x", " 89", "89 ", 0, -3, "abc",
                      "", None, 8.9, "8.9"):
            with self.assertRaises(workflow_api.GateError) as ctx:
                gh_workflow.number_or_fail(value)
            self.assertEqual(
                ctx.exception.reason, "invalid_github_workflow_issue")


class ReceiptReadTest(unittest.TestCase):
    def test_missing_receipt_returns_empty(self):
        record = gh_workflow._read_record("/nonexistent/dir/99.json")
        self.assertEqual(record, {})

    def test_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "1.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(workflow_api.GateError) as ctx:
                gh_workflow._read_record(path)
            self.assertEqual(ctx.exception.reason, "invalid_workflow_receipt")

    def test_non_dict_json(self):
        for payload in ("[1, 2]", "\"text\"", "5"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "1.json")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                with self.assertRaises(workflow_api.GateError) as ctx:
                    gh_workflow._read_record(path)
                self.assertEqual(
                    ctx.exception.reason, "invalid_workflow_receipt")

    def test_valid_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "1.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"cycle": "develop:1", "attempts": 2}, handle)
            self.assertEqual(
                gh_workflow._read_record(path),
                {"cycle": "develop:1", "attempts": 2})


class WorkspaceKeyTest(unittest.TestCase):
    def test_issue_object_uses_identifier(self):
        issue = _issue()
        self.assertEqual(workspace_key(issue), workspace_key("GH-89"))

    def test_string_identifier(self):
        self.assertEqual(workspace_key("GH-89"), "GH-89")


class CycleTest(unittest.TestCase):
    def test_cycle_value(self):
        self.assertEqual(gh_workflow.cycle({"id": 7}), "develop:7")

    def test_new_cycle_same(self):
        record = {"cycle": "develop:7", "attempts": 2, "blocked": False,
                  "error": "x"}
        self.assertIs(gh_workflow.new_cycle(record, "develop:7"), record)

    def test_new_cycle_resets(self):
        record = {"cycle": "develop:1", "attempts": 5, "blocked": True,
                  "error": "x", "delivery": {}}
        fresh = gh_workflow.new_cycle(record, "develop:9")
        self.assertEqual(
            fresh, {"cycle": "develop:9", "attempts": 0, "blocked": False})


class PhaseTest(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(workflow_policy.phase(["agent"]), "develop")
        self.assertEqual(workflow_policy.phase([]), "waiting")
        self.assertEqual(
            workflow_policy.phase(["agent", "needs-human"]), "invalid")
        self.assertEqual(workflow_policy.phase([" AGENT "]), "develop")


class RoutableTest(unittest.TestCase):
    def test_conflicting_labels_warn_and_block(self):
        settings = _tracker_settings()
        issue = _issue(labels=["agent", "needs-human"])
        with self.assertLogs("symphony.github.workflow", level="WARNING"):
            self.assertFalse(gh_workflow.routable(issue, settings))

    def test_disabled_accepts_any_routable_phase(self):
        settings = _tracker_settings(github_workflow={"enabled": False})
        issue = _issue(labels=["needs-human"])
        # Disabled workflow: labels alone do not gate dispatch.
        self.assertTrue(workflow_policy.routable(issue, settings))


class GetInTest(unittest.TestCase):
    def test_nested_lookup(self):
        data = {"a": {"b": {"c": 5}}}
        self.assertEqual(workflow_api._get_in(data, "a", "b", "c"), 5)

    def test_null_intermediate(self):
        self.assertIsNone(workflow_api._get_in({"a": None}, "a", "b"))

    def test_non_dict_intermediate(self):
        self.assertIsNone(workflow_api._get_in({"a": "x"}, "a", "b"))

    def test_workflow_map_get(self):
        self.assertEqual(
            gh_workflow._map_get({"a": {"b": 1}}, "a", "b"), 1)
        self.assertIsNone(gh_workflow._map_get({"a": None}, "a", "b"))


class PaginationTest(unittest.TestCase):
    def test_page_limit_fails_closed(self):
        full_page = {"status": 200, "body": [{"n": i} for i in range(100)]}

        def fake_client(method, path, params, body, settings):
            return full_page

        with self.assertRaises(workflow_api.GateError) as ctx:
            workflow_api.pages(None, "/x", None, github_client=fake_client)
        self.assertEqual(ctx.exception.reason, "github_pagination_limit")

    def test_params_merge_overrides(self):
        seen = []

        def fake_client(method, path, params, body, settings):
            seen.append(dict(params))
            return {"status": 200, "body": []}

        rows = workflow_api.pages(
            None, "/x", None, github_client=fake_client,
            params={"state": "all", "per_page": 1})
        self.assertEqual(rows, [])
        self.assertEqual(
            seen[0], {"state": "all", "per_page": 100, "page": 1})


class RemoveRecordedTest(unittest.TestCase):
    """Recorded paths only need parent-canonical safety, not the current root."""

    def _settings(self, root, ssh_hosts=None):
        return SimpleNamespace(
            workspace=SimpleNamespace(root=root),
            worker=SimpleNamespace(ssh_hosts=ssh_hosts or []),
            hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
        )

    def test_removes_path_outside_current_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorded = os.path.join(tmp, "legacy", "GH-9")
            os.makedirs(recorded)
            settings = self._settings(os.path.join(tmp, "other-root"))
            self.assertTrue(workspace_mod.remove_recorded(settings, recorded))
            self.assertFalse(os.path.lexists(recorded))

    def test_rejects_relative_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            self.assertFalse(
                workspace_mod.remove_recorded(settings, "relative/GH-1"))
            self.assertFalse(
                workspace_mod.remove_recorded(
                    settings, os.path.join(tmp, "gone")))


class RemoveIssueWorkspacesTest(unittest.TestCase):
    def _settings(self, root, ssh_hosts=None):
        return SimpleNamespace(
            workspace=SimpleNamespace(root=root),
            worker=SimpleNamespace(ssh_hosts=ssh_hosts or []),
            hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
        )

    def test_local_cleanup_without_ssh_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "GH-1")
            os.makedirs(target)
            settings = self._settings(tmp)
            removed = workspace_mod.remove_issue_workspaces(settings, ["GH-1"])
            self.assertEqual(len(removed), 1)
            self.assertFalse(os.path.lexists(target))

    def test_issue_object_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "GH-89"))
            settings = self._settings(tmp)
            removed = workspace_mod.remove_issue_workspaces(
                settings, _issue())
            self.assertEqual(len(removed), 1)

    def test_ssh_hosts_iterate_remote_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "GH-1")
            os.makedirs(target)
            settings = self._settings(tmp, ssh_hosts=["a", "b"])
            removed = workspace_mod.remove_issue_workspaces(settings, ["GH-1"])
            # Remote cleanup is unsupported; the local dir must be untouched.
            self.assertEqual(removed, [])
            self.assertTrue(os.path.isdir(target))


class TickStagingTest(unittest.TestCase):
    """Elixir tick only marks the poll; the cycle runs as a mailbox message."""

    def _orchestrator(self):
        from symphony.orchestrator import Orchestrator

        settings = config.parse_settings({
            "tracker": {"kind": "github",
                        "provider": {"repo": "a/b", "token": "t"}},
        })
        store = SimpleNamespace(settings=lambda: settings, path="x")
        return Orchestrator(store)

    def test_tick_enqueues_run_poll_cycle(self):
        orch = self._orchestrator()
        orch._on_tick()
        self.assertTrue(orch.poll_check_in_progress)
        self.assertIsNone(orch.next_poll_due_at_ms)
        self.assertEqual(orch._mailbox.get_nowait(), ("run_poll_cycle",))


if __name__ == "__main__":
    unittest.main()
