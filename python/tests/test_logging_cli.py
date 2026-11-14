"""Logging (rotation, UTF-8, redaction) and CLI argument parity tests."""

import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import cli, logging_setup  # noqa: E402


class LoggingSetupTest(unittest.TestCase):
    def test_default_log_file_matches_elixir_layout(self):
        root = os.path.join(tempfile.gettempdir(), "symphony-log-root")
        self.assertEqual(
            logging_setup.default_log_file(root),
            os.path.join(os.path.abspath(root), "log", "symphony.log"))

    def test_setup_uses_utf8_rotating_handler(self):
        root = logging.getLogger()
        previous = root.handlers[:]
        root.handlers[:] = []
        with tempfile.TemporaryDirectory(prefix="symphony-log-") as tmp:
            try:
                logging_setup.setup(
                    "INFO", os.path.join(tmp, "symphony.log"))
                handlers = [
                    h for h in root.handlers
                    if isinstance(h, logging.FileHandler)]
                self.assertEqual(len(handlers), 1)
                handler = handlers[0]
                self.assertEqual(handler.encoding, "utf-8")
                self.assertEqual(handler.maxBytes, 10 * 1024 * 1024)
                self.assertEqual(handler.backupCount, 5)
                logging.getLogger("symphony.test").info("你好")
                handler.flush()
                with open(handler.baseFilename, encoding="utf-8") as stream:
                    self.assertIn("你好", stream.read())
            finally:
                for handler in root.handlers[:]:
                    handler.close()
                root.handlers[:] = previous

    def test_text_formatter_redacts_credentials(self):
        formatter = logging_setup._TextFormat("%(message)s")
        record = logging.LogRecord(
            "symphony.test", logging.INFO, __file__, 1,
            "token=abc api_key: xyz password=hunter2 Bearer s3cr3t",
            None, None)

        rendered = formatter.format(record)

        self.assertEqual(
            rendered,
            "token=*** api_key: *** password=*** Bearer ***")
        for secret in ("abc", "xyz", "hunter2", "s3cr3t"):
            self.assertNotIn(secret, rendered)

    def test_json_formatter_redacts_credentials(self):
        formatter = logging_setup._JsonFormat()
        record = logging.LogRecord(
            "symphony.test", logging.INFO, __file__, 1,
            "authorization: Bearer abc.def secret=s3cr3t", None, None)

        rendered = formatter.format(record)

        self.assertIn(
            '"message": "authorization: *** *** secret=***"', rendered)
        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("s3cr3t", rendered)


class CliParserTest(unittest.TestCase):
    def test_positional_workflow_and_logs_root(self):
        args = cli._build_parser().parse_args(
            ["--logs-root", "/var/log/symphony", "WORKFLOW.md"])

        self.assertEqual(args.workflow_path, "WORKFLOW.md")
        self.assertEqual(args.logs_root, "/var/log/symphony")
        self.assertIsNone(args.workflow)
        self.assertIsNone(args.port)
        self.assertFalse(args.check)

    def test_workflow_flag_port_and_check(self):
        args = cli._build_parser().parse_args(
            ["-w", "custom.md", "--port", "18331", "--check",
             "--log-file", "main.log"])

        self.assertEqual(args.workflow, "custom.md")
        self.assertEqual(args.port, 18331)
        self.assertTrue(args.check)
        self.assertEqual(args.log_file, "main.log")
        self.assertIsNone(args.restore_state)

    def test_restore_state_argument(self):
        args = cli._build_parser().parse_args(
            ["--restore-state", "state.zip", "WORKFLOW.md"])

        self.assertEqual(args.restore_state, "state.zip")
        self.assertEqual(args.workflow_path, "WORKFLOW.md")

    def test_guardrail_flag_and_banner(self):
        args = cli._build_parser().parse_args(
            [cli.GUARDRAIL_FLAG, "WORKFLOW.md"])
        self.assertTrue(args.acknowledge_guardrails)

        banner = cli._acknowledgement_banner()
        self.assertIn("low key engineering preview", banner)
        self.assertIn("without any guardrails", banner)
        self.assertIn(cli.GUARDRAIL_FLAG, banner)

    def test_missing_guardrail_flag_prints_banner_and_exits(self):
        import contextlib
        import io
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main(["--workflow", "missing-WORKFLOW.md"])
        self.assertEqual(code, 1)
        self.assertIn(cli.GUARDRAIL_FLAG, stderr.getvalue())

    def test_check_mode_does_not_require_guardrail_ack(self):
        with tempfile.TemporaryDirectory(prefix="symphony-cli-") as tmp:
            workflow_path = os.path.join(tmp, "WORKFLOW.md")
            with open(workflow_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "tracker:\n"
                    "  kind: github\n"
                    "  provider: {repo: owner/repo, token: token}\n"
                    "  active_states: [open]\n"
                    "  terminal_states: [closed]\n"
                    "runtime:\n"
                    "  kind: pi\n"
                    "  command: python\n"
                    "server:\n"
                    "  port: 18399\n"
                    "workspace:\n"
                    "  root: %s\n"
                    "---\n"
                    "prompt\n" % os.path.join(tmp, "workspaces").replace(
                        "\\", "/"))
            self.assertEqual(cli.main(["--check", workflow_path]), 0)

    def test_resolve_workflow_prefers_explicit_path(self):
        explicit = os.path.join(tempfile.gettempdir(), "explicit.md")
        self.assertEqual(
            cli._resolve_workflow_path(explicit),
            os.path.abspath(explicit))

    def test_resolve_workflow_uses_env_override(self):
        target = os.path.join(tempfile.gettempdir(), "from-env.md")
        with mock.patch.dict(
                os.environ, {"SYMPHONY_WORKFLOW": target}, clear=False):
            self.assertEqual(
                cli._resolve_workflow_path(None), os.path.abspath(target))

    def test_resolve_workflow_falls_back_to_existing_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            default = os.path.join(tmp, "WORKFLOW.md")
            with open(default, "w", encoding="utf-8") as handle:
                handle.write("prompt")
            original = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.dict(
                        os.environ, {"SYMPHONY_WORKFLOW": ""}, clear=False):
                    self.assertEqual(
                        cli._resolve_workflow_path(None),
                        os.path.abspath("WORKFLOW.md"))
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
