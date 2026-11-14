"""Remote Pi startup and MCP reverse-forward tests."""

import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.agent import pi_runtime  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


class FakeProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 0


def _settings(kind="github"):
    return SimpleNamespace(
        runtime=SimpleNamespace(
            command="pi --mode rpc",
            model=None,
            thinking=None,
            turn_timeout_ms=1000,
            stall_timeout_ms=1000,
            mcp={"enabled": True},
            live_log_dir=None,
        ),
        tracker=SimpleNamespace(
            kind=kind, secret_environment_names=[]),
    )


class RemoteOpenSessionTest(unittest.TestCase):
    def test_remote_script_and_mcp_reverse_forward(self):
        process = FakeProcess()
        with mock.patch(
                "symphony.ssh.start_port", return_value=process) as start, \
                mock.patch(
                    "symphony.mcp_bridge.endpoint_port",
                    return_value=12345):
            session = pi_runtime._open_session(
                "/home/u/ws/GH-1", _settings().runtime, _settings(),
                "session-1", mcp_token="token",
                worker_host="worker-a")
        self.assertIs(session.process, process)
        args = start.call_args.args
        self.assertEqual(args[0], "worker-a")
        self.assertIn("cd '/home/u/ws/GH-1' &&", args[1])
        self.assertIn("exec pi --mode rpc", args[1])
        self.assertEqual(
            start.call_args.kwargs["remote_forwards"],
            [(12345, "127.0.0.1", 12345)])
        session.process.stdin.close()
        session.process.stdout.close()

    def test_remote_without_mcp_has_no_forward(self):
        process = FakeProcess()
        with mock.patch(
                "symphony.ssh.start_port", return_value=process) as start:
            session = pi_runtime._open_session(
                "/home/u/ws/GH-1", _settings().runtime, _settings(),
                "session-1", mcp_token=None,
                worker_host="worker-a")
        self.assertIsNone(start.call_args.kwargs["remote_forwards"])
        session.process.stdin.close()
        session.process.stdout.close()

    def test_ssh_start_failure_revokes_scope(self):
        from symphony import ssh

        with mock.patch(
                "symphony.ssh.start_port",
                side_effect=ssh.SSHError("ssh_not_found")), \
                mock.patch(
                    "symphony.agent.pi_runtime._revoke_mcp_token") as revoke:
            with self.assertRaises(WorkflowError) as caught:
                pi_runtime._open_session(
                    "/home/u/ws/GH-1", _settings().runtime, _settings(),
                    "session-1", mcp_token="token",
                    worker_host="worker-a")
        self.assertEqual(caught.exception.reason, "ssh_not_found")
        revoke.assert_called_once_with("token")


class RemotePrepareTest(unittest.TestCase):
    def test_prepare_creates_private_pi_dir_and_excludes_it(self):
        with mock.patch(
                "symphony.ssh.run", return_value=(b"", 0)) as run:
            pi_runtime._prepare_remote_pi_dir(
                "/home/u/ws/GH-1", "worker-a")
        script = run.call_args.args[1]
        self.assertIn("cd '/home/u/ws/GH-1'", script)
        self.assertIn("mkdir -p .pi", script)
        self.assertIn("chmod 700 .pi", script)
        self.assertIn("rm -rf '.pi/extensions'", script)
        self.assertIn("grep -qxF '.pi/' .git/info/exclude", script)

    def test_prepare_rejects_unsafe_git_metadata(self):
        with mock.patch(
                "symphony.ssh.run",
                return_value=(b"PI_GIT_METADATA_UNSAFE\n", 5)):
            with self.assertRaises(WorkflowError) as caught:
                pi_runtime._prepare_remote_pi_dir(
                    "/home/u/ws/GH-1", "worker-a")
        self.assertEqual(
            caught.exception.reason, "pi_git_metadata_unsafe")

    def test_write_mcp_config_uses_private_atomic_file(self):
        with mock.patch(
                "symphony.ssh.run", return_value=(b"", 0)) as run:
            pi_runtime._remote_write_mcp_config(
                "/home/u/ws/GH-1", {"mcpServers": {}}, "worker-a")
        script = run.call_args.args[1]
        self.assertIn("printf '%s'", script)
        self.assertIn("chmod 600 .pi/.mcp.tmp", script)
        self.assertIn("mv .pi/.mcp.tmp .pi/mcp.json", script)

    def test_write_mcp_config_failure_is_explicit(self):
        with mock.patch(
                "symphony.ssh.run", return_value=(b"denied", 1)):
            with self.assertRaises(WorkflowError) as caught:
                pi_runtime._remote_write_mcp_config(
                    "/home/u/ws/GH-1", {"mcpServers": {}}, "worker-a")
        self.assertEqual(caught.exception.reason, "pi_mcp_config_failed")

    def test_remove_mcp_config(self):
        with mock.patch(
                "symphony.ssh.run", return_value=(b"", 0)) as run:
            pi_runtime._remote_remove_mcp_config(
                "/home/u/ws/GH-1", "worker-a")
        self.assertIn("rm -f .pi/mcp.json", run.call_args.args[1])


class RemoteWriteMcpConfigTest(unittest.TestCase):
    def test_enabled_writes_remote_file_and_returns_token(self):
        with mock.patch(
                "symphony.mcp_bridge.client_config",
                return_value=({"mcpServers": {}}, "token")), \
                mock.patch(
                    "symphony.agent.pi_runtime._remote_write_mcp_config"
                ) as write:
            token = pi_runtime._write_mcp_config(
                "/home/u/ws/GH-1", _settings(), object(),
                worker_host="worker-a")
        self.assertEqual(token, "token")
        write.assert_called_once()

    def test_disabled_removes_remote_file(self):
        with mock.patch(
                "symphony.mcp_bridge.client_config", return_value=None), \
                mock.patch(
                    "symphony.agent.pi_runtime._remote_remove_mcp_config"
                ) as remove:
            token = pi_runtime._write_mcp_config(
                "/home/u/ws/GH-1", _settings(), object(),
                worker_host="worker-a")
        self.assertIsNone(token)
        remove.assert_called_once_with("/home/u/ws/GH-1", "worker-a")


class StartSessionRemoteTest(unittest.TestCase):
    def test_remote_start_no_longer_rejects_worker_host(self):
        fake_session = SimpleNamespace(process=FakeProcess())
        with mock.patch(
                "symphony.agent.pi_runtime._prepare_remote_pi_dir") as prep, \
                mock.patch(
                    "symphony.agent.pi_runtime._write_mcp_config",
                    return_value=None), \
                mock.patch(
                    "symphony.agent.pi_runtime._open_session",
                    return_value=fake_session) as open_session:
            session = pi_runtime.start_session(
                "/home/u/ws/GH-1", _settings(), worker_host="worker-a")
        self.assertIs(session, fake_session)
        prep.assert_called_once_with("/home/u/ws/GH-1", "worker-a")
        self.assertEqual(
            open_session.call_args.kwargs["worker_host"], "worker-a")


if __name__ == "__main__":
    unittest.main()
