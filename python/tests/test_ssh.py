"""SSH transport parity tests for SymphonyElixir.SSH."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import ssh  # noqa: E402


class ParseTargetTest(unittest.TestCase):
    def test_plain_host(self):
        self.assertEqual(ssh.parse_target("worker-a"), ("worker-a", None))

    def test_host_and_port(self):
        self.assertEqual(
            ssh.parse_target("worker-a:2222"), ("worker-a", "2222"))

    def test_user_host_and_port(self):
        self.assertEqual(
            ssh.parse_target("deploy@worker-a:2222"),
            ("deploy@worker-a", "2222"))

    def test_ipv6_without_brackets_is_not_split(self):
        self.assertEqual(ssh.parse_target("::1"), ("::1", None))
        self.assertEqual(
            ssh.parse_target("2001:db8::1"), ("2001:db8::1", None))

    def test_bracketed_ipv6_with_port(self):
        self.assertEqual(
            ssh.parse_target("[::1]:2222"), ("[::1]", "2222"))

    def test_bracketed_ipv6_without_port(self):
        self.assertEqual(ssh.parse_target("[::1]"), ("[::1]", None))

    def test_trimmed_target(self):
        self.assertEqual(
            ssh.parse_target("  worker-a:2200  "), ("worker-a", "2200"))


class RemoteShellCommandTest(unittest.TestCase):
    def test_quotes_single_quotes(self):
        self.assertEqual(
            ssh.remote_shell_command("echo 'hi'"),
            "bash -lc 'echo '\"'\"'hi'\"'\"''")

    def test_plain_command(self):
        self.assertEqual(
            ssh.remote_shell_command("pwd -P"), "bash -lc 'pwd -P'")


class SshArgsTest(unittest.TestCase):
    def test_basic_args(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = ssh.ssh_args("worker-a", "pwd")
        self.assertEqual(
            args, ["-T", "worker-a", "bash -lc 'pwd'"])

    def test_port_and_config(self):
        with mock.patch.dict(
                os.environ, {"SYMPHONY_SSH_CONFIG": "/etc/ssh/config"},
                clear=True):
            args = ssh.ssh_args("worker-a:2222", "pwd")
        self.assertEqual(
            args,
            ["-F", "/etc/ssh/config", "-T", "-p", "2222",
             "worker-a", "bash -lc 'pwd'"])

    def test_remote_forward_and_fail_closed(self):
        args = ssh.ssh_args(
            "worker-a", "pwd",
            remote_forwards=[(18331, "127.0.0.1", 18331)],
            exit_on_forward_failure=True)
        self.assertIn("-o", args)
        self.assertIn("ExitOnForwardFailure=yes", args)
        self.assertIn("-R", args)
        self.assertIn("127.0.0.1:18331:127.0.0.1:18331", args)


class RunTest(unittest.TestCase):
    def test_returns_output_and_status(self):
        completed = mock.Mock(stdout=b"ok\n", returncode=0)
        with mock.patch.object(ssh, "executable", return_value="ssh"), \
                mock.patch("subprocess.run", return_value=completed) as run:
            output, status = ssh.run("worker-a", "pwd", timeout_s=5)
        self.assertEqual(output, b"ok\n")
        self.assertEqual(status, 0)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_timeout_becomes_ssh_error(self):
        import subprocess

        with mock.patch.object(ssh, "executable", return_value="ssh"), \
                mock.patch(
                    "subprocess.run",
                    side_effect=subprocess.TimeoutExpired("ssh", 5)):
            with self.assertRaises(ssh.SSHError) as caught:
                ssh.run("worker-a", "pwd", timeout_s=5)
        self.assertEqual(caught.exception.reason, "ssh_command_timeout")

    def test_missing_ssh(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(ssh.SSHError) as caught:
                ssh.run("worker-a", "pwd")
        self.assertEqual(caught.exception.reason, "ssh_not_found")

    def test_empty_host(self):
        with self.assertRaises(ssh.SSHError) as caught:
            ssh.run("  ", "pwd")
        self.assertEqual(caught.exception.reason, "ssh_host_missing")


class StartPortTest(unittest.TestCase):
    def test_popen_pipes_and_forward(self):
        process = mock.Mock()
        with mock.patch.object(ssh, "executable", return_value="ssh"), \
                mock.patch("subprocess.Popen", return_value=process) as popen:
            result = ssh.start_port(
                "worker-a:2222", "cd '/ws' && pi",
                remote_forwards=[(18331, "127.0.0.1", 18331)])
        self.assertIs(result, process)
        args = popen.call_args.args[0]
        self.assertIn("worker-a", args)
        self.assertIn("2222", args)
        self.assertIn("ExitOnForwardFailure=yes", args)
        self.assertIn("127.0.0.1:18331:127.0.0.1:18331", args)
        kwargs = popen.call_args.kwargs
        self.assertIsNotNone(kwargs["stdin"])
        self.assertIsNotNone(kwargs["stdout"])


if __name__ == "__main__":
    unittest.main()
