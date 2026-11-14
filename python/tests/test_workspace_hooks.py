"""Local workspace lifecycle and hook parity tests.

Maps the local half of ``workspace_and_config_test.exs``: deterministic
paths, symlink/root protection, workspace reuse, stale path replacement,
and the four hooks including failure, timeout and output truncation.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import workspace as workspace_mod  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.paths import canonicalize  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


def _issue(identifier="MT-1"):
    return Issue(
        id="issue-1", identifier=identifier, title="t", state="Todo",
        labels=["agent"], dispatchable=True, blocked_by=[],
    )


def _settings(root, **hooks):
    return SimpleNamespace(
        workspace=SimpleNamespace(root=root),
        worker=SimpleNamespace(ssh_hosts=[]),
        hooks=SimpleNamespace(
            after_create=hooks.get("after_create"),
            before_run=hooks.get("before_run"),
            after_run=hooks.get("after_run"),
            before_remove=hooks.get("before_remove"),
            # Git Bash can take a few seconds to start under a loaded
            # full-suite run; keep the production default out of this.
            timeout_ms=hooks.get("timeout_ms", 8000),
        ),
    )


def _make_dir_link(target, link):
    """Create a directory link (junction on Windows, symlink elsewhere)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


class WorkspacePathTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="symphony-ws-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_path_is_deterministic_per_identifier(self):
        workspace_root = os.path.join(self.root, "workspaces")
        settings = _settings(workspace_root)

        first = workspace_mod.create_for_issue(settings, _issue("MT/Det"))
        second = workspace_mod.create_for_issue(settings, _issue("MT/Det"))

        self.assertEqual(first, second)
        self.assertEqual(os.path.basename(first),
                         workspace_mod.workspace_key("MT/Det"))
        self.assertTrue(os.path.basename(first).startswith("MT_Det--"))

    def test_colliding_identifiers_get_distinct_directories(self):
        workspace_root = os.path.join(self.root, "workspaces")
        settings = _settings(workspace_root)

        slash = workspace_mod.create_for_issue(
            settings, _issue("team/a-1"))
        underscore = workspace_mod.create_for_issue(
            settings, _issue("team_a-1"))

        self.assertNotEqual(slash, underscore)
        self.assertEqual(os.path.basename(underscore), "team_a-1")
        self.assertTrue(os.path.basename(slash).startswith("team_a-1--"))

    def test_relative_root_resolves_from_workflow_directory(self):
        workflow_dir = os.path.join(self.root, "workflow")
        launcher_dir = os.path.join(self.root, "launcher")
        os.makedirs(workflow_dir)
        os.makedirs(launcher_dir)
        workflow_path = os.path.join(workflow_dir, "WORKFLOW.md")
        settings = _settings("relative-workspaces")
        original_cwd = os.getcwd()

        try:
            os.chdir(launcher_dir)
            expected = canonicalize(os.path.join(
                workflow_dir, "relative-workspaces", "MT-REL"))
            workspace = workspace_mod.create_for_issue(
                settings, _issue("MT-REL"), workflow_path)
        finally:
            os.chdir(original_cwd)

        self.assertEqual(workspace, expected)
        self.assertFalse(workspace.startswith(
            canonicalize(launcher_dir) + os.sep))

    def test_symlinked_root_is_canonicalized(self):
        actual_root = os.path.join(self.root, "actual-workspaces")
        linked_root = os.path.join(self.root, "linked-workspaces")
        os.makedirs(actual_root)
        try:
            _make_dir_link(actual_root, linked_root)
        except (AttributeError, OSError) as error:
            self.skipTest("directory links unavailable: %r" % (error,))
        settings = _settings(linked_root)

        expected = canonicalize(os.path.join(actual_root, "MT-LINK"))
        workspace = workspace_mod.create_for_issue(settings, _issue("MT-LINK"))

        self.assertEqual(workspace, expected)
        self.assertTrue(os.path.isdir(workspace))

    def test_symlink_escape_under_root_is_rejected(self):
        workspace_root = os.path.join(self.root, "workspaces")
        outside_root = os.path.join(self.root, "outside")
        os.makedirs(workspace_root)
        os.makedirs(outside_root)
        try:
            _make_dir_link(outside_root,
                           os.path.join(workspace_root, "MT-SYM"))
        except (AttributeError, OSError) as error:
            self.skipTest("directory links unavailable: %r" % (error,))
        settings = _settings(workspace_root)

        with self.assertRaises(WorkflowError) as caught:
            workspace_mod.create_for_issue(settings, _issue("MT-SYM"))

        self.assertEqual(
            caught.exception.reason, "workspace_symlink_escape")
        self.assertTrue(os.path.isdir(outside_root))

    def test_remove_rejects_workspace_root_itself(self):
        workspace_root = os.path.join(self.root, "workspaces")
        os.makedirs(workspace_root)
        settings = _settings(workspace_root)

        self.assertFalse(workspace_mod.remove(settings, workspace_root))
        self.assertTrue(os.path.isdir(workspace_root))

    def test_remove_recorded_rejects_symlink_escape_before_hooks(self):
        recorded_root = os.path.join(self.root, "recorded-workspaces")
        outside_root = os.path.join(self.root, "outside")
        recorded = os.path.join(recorded_root, "MT-SYM")
        marker = os.path.join(self.root, "before-remove-ran")
        os.makedirs(recorded_root)
        os.makedirs(outside_root)
        try:
            _make_dir_link(outside_root, recorded)
        except (AttributeError, OSError) as error:
            self.skipTest("directory links unavailable: %r" % (error,))
        settings = _settings(
            os.path.join(self.root, "current-workspaces"),
            before_remove='touch "%s"' % marker)

        self.assertFalse(workspace_mod.remove_recorded(settings, recorded))
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(os.path.isdir(outside_root))

    def test_stale_non_directory_path_is_replaced(self):
        workspace_root = os.path.join(self.root, "workspaces")
        os.makedirs(workspace_root)
        stale = os.path.join(workspace_root, "MT-STALE")
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write("old state\n")
        settings = _settings(workspace_root)

        workspace = workspace_mod.create_for_issue(
            settings, _issue("MT-STALE"))

        self.assertEqual(workspace, canonicalize(stale))
        self.assertTrue(os.path.isdir(workspace))

    def test_existing_workspace_is_reused_without_after_create(self):
        workspace_root = os.path.join(self.root, "workspaces")
        settings = _settings(
            workspace_root, after_create="printf first > README.md")

        first = workspace_mod.create_for_issue(settings, _issue("MT-REUSE"))
        with open(os.path.join(first, "README.md"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "first")
        with open(os.path.join(first, "README.md"), "w", encoding="utf-8") as h:
            h.write("changed\n")
        with open(os.path.join(first, "local-progress.txt"), "w",
                  encoding="utf-8") as h:
            h.write("in progress\n")
        os.makedirs(os.path.join(first, "deps"))
        with open(os.path.join(first, "deps", "cache.txt"), "w",
                  encoding="utf-8") as h:
            h.write("cached deps\n")

        second = workspace_mod.create_for_issue(settings, _issue("MT-REUSE"))

        self.assertEqual(first, second)
        with open(os.path.join(second, "README.md"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "changed\n")
        with open(os.path.join(second, "local-progress.txt"),
                  encoding="utf-8") as h:
            self.assertEqual(h.read(), "in progress\n")
        with open(os.path.join(second, "deps", "cache.txt"),
                  encoding="utf-8") as h:
            self.assertEqual(h.read(), "cached deps\n")


class WorkspaceHookTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="symphony-hook-")
        self.workspace_root = os.path.join(self.root, "workspaces")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_after_create_runs_once_on_first_creation(self):
        settings = _settings(
            self.workspace_root, after_create="printf ready > READY")

        workspace = workspace_mod.create_for_issue(settings, _issue("MT-608"))

        with open(os.path.join(workspace, "READY"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "ready")

    def test_after_create_failure_cleans_partial_workspace_and_retries(self):
        attempts = os.path.join(self.root, "after-create-attempts")
        settings = _settings(
            self.workspace_root,
            after_create=(
                'if [ -f "%s" ]; then count=$(wc -l < "%s"); else count=0; fi\n'
                'printf "attempt\\n" >> "%s"\n'
                'if [ "$count" -eq 0 ]; then printf partial > partial.txt; '
                "exit 17; fi\n"
                "printf ready > READY"
            ) % (attempts, attempts, attempts))

        with self.assertRaises(workspace_mod.HookError) as caught:
            workspace_mod.create_for_issue(settings, _issue("MT-FAIL"))

        self.assertEqual(
            caught.exception.reason[:3],
            ("workspace_hook_failed", "after_create", 17))
        self.assertIsInstance(caught.exception.reason[3], str)
        failed_path = os.path.join(self.workspace_root, "MT-FAIL")
        self.assertFalse(os.path.exists(failed_path))

        workspace = workspace_mod.create_for_issue(settings, _issue("MT-FAIL"))
        with open(os.path.join(workspace, "READY"), encoding="utf-8") as h:
            self.assertEqual(h.read(), "ready")
        with open(attempts, encoding="utf-8") as h:
            self.assertEqual(h.read().split(), ["attempt", "attempt"])

    def test_after_create_timeout_is_surfaced_and_cleaned(self):
        settings = _settings(
            self.workspace_root, after_create="sleep 2", timeout_ms=60)

        with self.assertRaises(workspace_mod.HookError) as caught:
            workspace_mod.create_for_issue(settings, _issue("MT-TIMEOUT"))

        self.assertEqual(
            caught.exception.reason,
            ("workspace_hook_timeout", "after_create", 60))
        self.assertFalse(
            os.path.exists(os.path.join(self.workspace_root, "MT-TIMEOUT")))

    def test_hook_output_is_truncated_only_in_logs(self):
        settings = _settings(
            self.workspace_root,
            after_create="printf 'x%.0s' {1..5000}; exit 3")

        with self.assertLogs("symphony.workspace", level="WARNING") as logs:
            with self.assertRaises(workspace_mod.HookError) as caught:
                workspace_mod.create_for_issue(settings, _issue("MT-BIG"))

        output = caught.exception.reason[3]
        # Git Bash on Windows can prepend ANSI reset sequences.
        self.assertEqual(output.count("x"), 5000)
        self.assertGreater(len(output), 2048)
        self.assertEqual(caught.exception.reason[2], 3)
        self.assertTrue(any("(truncated)" in line for line in logs.output))

    def test_before_run_failure_propagates(self):
        settings = _settings(self.workspace_root)
        workspace = workspace_mod.create_for_issue(
            settings, _issue("MT-BEFORE"))
        settings.hooks.before_run = "exit 9"

        with self.assertRaises(workspace_mod.HookError) as caught:
            workspace_mod.run_before_run(settings, _issue("MT-BEFORE"))

        self.assertEqual(
            caught.exception.reason[:3],
            ("workspace_hook_failed", "before_run", 9))
        self.assertTrue(os.path.isdir(workspace))

    def test_after_run_failure_is_logged_not_raised(self):
        settings = _settings(self.workspace_root)
        workspace_mod.create_for_issue(settings, _issue("MT-AFTER"))
        settings.hooks.after_run = "exit 4"

        with self.assertLogs("symphony.workspace", level="WARNING") as logs:
            workspace_mod.run_after_run(settings, _issue("MT-AFTER"))

        self.assertTrue(any("after_run" in line for line in logs.output))

    def test_before_remove_failure_does_not_block_removal(self):
        settings = _settings(
            self.workspace_root, before_remove="exit 5")
        workspace = workspace_mod.create_for_issue(
            settings, _issue("MT-REMOVE"))

        with self.assertLogs("symphony.workspace", level="WARNING") as logs:
            removed = workspace_mod.remove(settings, workspace)

        self.assertTrue(removed)
        self.assertFalse(os.path.exists(workspace))
        self.assertTrue(any("before_remove" in line for line in logs.output))

    def test_before_remove_is_skipped_for_missing_workspace(self):
        marker = os.path.join(self.root, "before-remove-ran")
        settings = _settings(
            self.workspace_root,
            before_remove='touch "%s"' % marker)
        missing = os.path.join(self.workspace_root, "MT-MISSING")

        self.assertFalse(workspace_mod.remove(settings, missing))
        self.assertFalse(os.path.exists(marker))

    def test_dangling_workspace_link_is_removed(self):
        os.makedirs(self.workspace_root)
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        link = os.path.join(self.workspace_root, "MT-DANGLE")
        try:
            _make_dir_link(outside, link)
        except (AttributeError, OSError) as error:
            self.skipTest("directory links unavailable: %r" % (error,))
        os.rmdir(outside)
        self.assertTrue(os.path.lexists(link))
        self.assertFalse(os.path.exists(link))
        settings = _settings(self.workspace_root)

        self.assertTrue(workspace_mod.remove(settings, link))
        self.assertFalse(os.path.lexists(link))


if __name__ == "__main__":
    unittest.main()
