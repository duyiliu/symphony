"""Remote workspace lifecycle parity tests."""

import os
import posixpath
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.issue import Issue  # noqa: E402
from symphony import workspace as workspace_mod  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


def _issue(identifier="GH-1"):
    return Issue(
        id="1", identifier=identifier, title="t", state="open",
        labels=["agent"], dispatchable=True, blocked_by=[],
    )


def _settings(root="~/symphony-workspaces", hosts=None, **hooks):
    return SimpleNamespace(
        workspace=SimpleNamespace(root=root),
        worker=SimpleNamespace(ssh_hosts=hosts or ["worker-a"]),
        polling=SimpleNamespace(interval_ms=1000),
        agent=SimpleNamespace(max_concurrent_agents=1),
        hooks=SimpleNamespace(
            after_create=hooks.get("after_create"),
            before_run=hooks.get("before_run"),
            after_run=hooks.get("after_run"),
            before_remove=hooks.get("before_remove"),
            timeout_ms=hooks.get("timeout_ms", 1000),
        ),
    )


MARKER = "__SYMPHONY_WORKSPACE__\t1\t/home/u/ws/GH-1\n"


class RemoteWorkspacePathTest(unittest.TestCase):
    def test_keeps_root_and_key_without_local_canonicalisation(self):
        settings = _settings(root="~/symphony-workspaces")
        self.assertEqual(
            workspace_mod.workspace_path(
                settings, _issue(), worker_host="worker-a"),
            "~/symphony-workspaces/GH-1")

    def test_rejects_invalid_remote_root(self):
        settings = _settings(root="/srv/bad\nroot")
        with self.assertRaises(WorkflowError) as caught:
            workspace_mod.workspace_path(
                settings, _issue(), worker_host="worker-a")
        self.assertEqual(
            caught.exception.reason, "workspace_path_unreadable")


class RemoteCreateTest(unittest.TestCase):
    def test_creates_and_parses_marker(self):
        settings = _settings()
        with mock.patch(
                "symphony.ssh.run",
                return_value=(MARKER.encode(), 0)) as run:
            path = workspace_mod.create_for_issue(
                settings, _issue(), worker_host="worker-a")
        self.assertEqual(path, "/home/u/ws/GH-1")
        host, script = run.call_args.args
        self.assertEqual(host, "worker-a")
        self.assertIn(
            "workspace='~/symphony-workspaces/GH-1'", script)
        self.assertIn('case "$workspace" in', script)
        self.assertIn("__SYMPHONY_WORKSPACE__", script)
        self.assertIn('pwd -P', script)

    def test_existing_workspace_skips_after_create(self):
        settings = _settings(after_create="echo created")
        marker = "__SYMPHONY_WORKSPACE__\t0\t/home/u/ws/GH-1\n"
        with mock.patch(
                "symphony.ssh.run",
                return_value=(marker.encode(), 0)) as run:
            path = workspace_mod.create_for_issue(
                settings, _issue(), worker_host="worker-a")
        self.assertEqual(path, "/home/u/ws/GH-1")
        self.assertEqual(run.call_count, 1)

    def test_runs_after_create_on_first_creation(self):
        settings = _settings(after_create="echo created")
        with mock.patch(
                "symphony.ssh.run",
                side_effect=[(MARKER.encode(), 0), (b"", 0)]) as run:
            workspace_mod.create_for_issue(
                settings, _issue(), worker_host="worker-a")
        self.assertEqual(run.call_count, 2)
        hook_script = run.call_args.args[1]
        self.assertIn("cd '/home/u/ws/GH-1' && echo created", hook_script)

    def test_after_create_failure_cleans_partial_workspace(self):
        settings = _settings(after_create="false")
        with mock.patch(
                "symphony.ssh.run",
                side_effect=[
                    (MARKER.encode(), 0),
                    (b"boom", 1),
                    (b"", 0),
                ]) as run:
            with self.assertRaises(workspace_mod.HookError):
                workspace_mod.create_for_issue(
                    settings, _issue(), worker_host="worker-a")
        cleanup_script = run.call_args.args[1]
        self.assertIn('rm -rf "$workspace"', cleanup_script)

    def test_prepare_failure_is_explicit(self):
        settings = _settings()
        with mock.patch(
                "symphony.ssh.run", return_value=(b"denied", 255)):
            with self.assertRaises(WorkflowError) as caught:
                workspace_mod.create_for_issue(
                    settings, _issue(), worker_host="worker-a")
        self.assertEqual(
            caught.exception.reason, "workspace_prepare_failed")

    def test_invalid_marker_is_explicit(self):
        settings = _settings()
        with mock.patch(
                "symphony.ssh.run", return_value=(b"noise", 0)):
            with self.assertRaises(WorkflowError) as caught:
                workspace_mod.create_for_issue(
                    settings, _issue(), worker_host="worker-a")
        self.assertEqual(
            caught.exception.reason, "workspace_prepare_failed")


class RemoteHooksTest(unittest.TestCase):
    def test_before_run_uses_remote_cd(self):
        settings = _settings(before_run="echo before")
        with mock.patch(
                "symphony.ssh.run", return_value=(b"", 0)) as run:
            path = workspace_mod.run_before_run(
                settings, _issue(), worker_host="worker-a")
        self.assertEqual(path, "~/symphony-workspaces/GH-1")
        self.assertIn(
            "cd '~/symphony-workspaces/GH-1' && echo before",
            run.call_args.args[1])

    def test_after_run_failure_is_logged_not_raised(self):
        settings = _settings(after_run="false")
        with mock.patch(
                "symphony.ssh.run", return_value=(b"boom", 1)):
            workspace_mod.run_after_run(
                settings, _issue(), worker_host="worker-a")

    def test_hook_timeout_is_hook_error(self):
        from symphony import ssh

        settings = _settings(before_run="sleep 10")
        with mock.patch(
                "symphony.ssh.run",
                side_effect=ssh.SSHError("ssh_command_timeout", 1)):
            with self.assertRaises(workspace_mod.HookError) as caught:
                workspace_mod.run_before_run(
                    settings, _issue(), worker_host="worker-a")
        self.assertEqual(caught.exception.reason[0], "workspace_hook_timeout")


class RemoteRemoveTest(unittest.TestCase):
    def test_remove_runs_before_remove_then_rm_rf(self):
        settings = _settings(before_remove="echo bye")
        with mock.patch(
                "symphony.ssh.run",
                side_effect=[(b"", 0), (b"", 0)]) as run:
            self.assertTrue(workspace_mod.remove(
                settings, "/home/u/ws/GH-1", worker_host="worker-a"))
        self.assertEqual(run.call_count, 2)
        before_remove = run.call_args_list[0].args[1]
        self.assertIn('if [ -d "$workspace" ]; then', before_remove)
        self.assertIn("cd \"$workspace\"", before_remove)
        self.assertIn("echo bye", before_remove)
        self.assertIn(
            'rm -rf "$workspace"', run.call_args_list[1].args[1])

    def test_remove_failure_returns_false(self):
        settings = _settings()
        with mock.patch(
                "symphony.ssh.run", return_value=(b"denied", 255)):
            self.assertFalse(workspace_mod.remove(
                settings, "/home/u/ws/GH-1", worker_host="worker-a"))

    def test_remove_recorded_uses_remote_remove(self):
        settings = _settings()
        with mock.patch(
                "symphony.ssh.run", return_value=(b"", 0)) as run:
            self.assertTrue(workspace_mod.remove_recorded(
                settings, "~/symphony-workspaces/GH-1",
                worker_host="worker-a"))
        self.assertIn('rm -rf "$workspace"', run.call_args.args[1])

    def test_remove_issue_workspaces_iterates_hosts(self):
        settings = _settings(root="/srv/symphony", hosts=["a", "b"])
        with mock.patch(
                "symphony.ssh.run", return_value=(b"", 0)) as run:
            removed = workspace_mod.remove_issue_workspaces(
                settings, ["GH-1"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            removed, ["/srv/symphony/GH-1", "/srv/symphony/GH-1"])
        self.assertEqual(
            [call.args[0] for call in run.call_args_list], ["a", "b"])


class RemoteLeaseTest(unittest.TestCase):
    def test_remote_lease_is_acquired_and_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                    os.environ, {"SYMPHONY_LEASE_ROOT": tmp}, clear=False):
                settings = _settings(root=tmp)
                lease = workspace_mod.acquire_run_lease(
                    settings, _issue(), worker_host="worker-a")
                self.assertIsNotNone(lease)
                self.assertEqual(
                    lease.owner["workspace"], posixpath.join(tmp, "GH-1"))
                self.assertTrue(lease.path.startswith(tmp))
                self.assertIsNone(workspace_mod.acquire_run_lease(
                    settings, _issue(), worker_host="worker-a"))
                lease.release()
                again = workspace_mod.acquire_run_lease(
                    settings, _issue(), worker_host="worker-a")
                self.assertIsNotNone(again)
                again.release()

    def test_different_hosts_use_different_leases(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                    os.environ, {"SYMPHONY_LEASE_ROOT": tmp}, clear=False):
                settings = _settings(root=tmp)
                first = workspace_mod.acquire_run_lease(
                    settings, _issue(), worker_host="worker-a")
                second = workspace_mod.acquire_run_lease(
                    settings, _issue(), worker_host="worker-b")
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                first.release()
                second.release()


class _FakeLease:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class RemoteOrchestratorCleanupTest(unittest.TestCase):
    def test_remote_metadata_uses_remote_remove_recorded(self):
        from symphony.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(root=tmp)
            store = SimpleNamespace(settings=lambda: settings, path=None)
            orchestrator = Orchestrator(store)
            metadata = {
                "worker_host": "worker-a",
                "workspace_path": "/home/u/ws/GH-1",
            }
            lease = _FakeLease()
            with mock.patch.dict(
                    os.environ, {"SYMPHONY_LEASE_ROOT": tmp}, clear=False), \
                    mock.patch(
                        "symphony.workspace.acquire_path_run_lease",
                        return_value=lease) as acquire, \
                    mock.patch(
                        "symphony.workspace.remove_recorded",
                        return_value=True) as remove:
                result = orchestrator._cleanup_issue_workspace(
                    _issue(), metadata, settings)
        self.assertTrue(result)
        self.assertTrue(lease.released)
        self.assertFalse(acquire.call_args.kwargs["canonical"])
        remove.assert_called_once()
        self.assertEqual(
            remove.call_args.kwargs["worker_host"], "worker-a")


if __name__ == "__main__":
    unittest.main()
