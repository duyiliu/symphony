"""Regression tests for behaviours aligned with the Elixir reference during
the 1:1 semantic audit (workspace keys/removal, mailbox tick staging).
Tracker-side parity coverage moved to test_tasks.py with the v2 record model.
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
from symphony import workspace as workspace_mod  # noqa: E402
from symphony.workspace import workspace_key  # noqa: E402


def _issue(**kw):
    base = dict(
        id="89", identifier="GH-89", title="t", state="open",
        labels=["agent"], dispatchable=True, blocked_by=[],
    )
    base.update(kw)
    return Issue(**base)


class WorkspaceKeyTest(unittest.TestCase):
    def test_issue_object_uses_identifier(self):
        issue = _issue()
        self.assertEqual(workspace_key(issue), workspace_key("GH-89"))

    def test_string_identifier(self):
        self.assertEqual(workspace_key("GH-89"), "GH-89")


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
