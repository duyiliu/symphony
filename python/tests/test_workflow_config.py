import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import workflow, config  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


WORKFLOW = """\
---
tracker:
  kind: github
  provider:
    repo: "owner/repo"
    token: "$GITHUB_TOKEN"
  required_labels:
    - agent
  active_states:
    - open
  terminal_states:
    - closed
polling:
  interval_ms: 10000
workspace:
  root: /tmp/ws
hooks:
  timeout_ms: 300000
  after_create: |
    git clone git@example.com:repo.git .
agent:
  max_concurrent_agents: 3
  max_turns: 12
runtime:
  kind: pi
  command: pi --mode rpc --tools read,bash
  model: test/model
  mcp:
    enabled: true
server:
  host: 0.0.0.0
  port: 8320
---

Hello {{ issue.identifier }}.
"""


class WorkflowTest(unittest.TestCase):
    def test_parse_front_matter_and_prompt(self):
        result = workflow.parse(WORKFLOW)
        self.assertEqual(result.config["tracker"]["kind"], "github")
        self.assertEqual(result.config["agent"]["max_turns"], 12)
        self.assertIn("git clone", result.config["hooks"]["after_create"])
        self.assertIn("{{ issue.identifier }}", result.prompt_template)

    def test_no_front_matter(self):
        result = workflow.parse("just a prompt")
        self.assertEqual(result.config, {})
        self.assertIn("just a prompt", result.prompt_template)

    def test_missing_file(self):
        with self.assertRaises(WorkflowError) as ctx:
            workflow.load("/nonexistent/WORKFLOW.md")
        self.assertEqual(ctx.exception.reason, "missing_workflow_file")

    def test_front_matter_not_map(self):
        with self.assertRaises(WorkflowError) as ctx:
            workflow.parse("---\n- 1\n- 2\n---\nbody")
        self.assertEqual(ctx.exception.reason, "workflow_front_matter_not_a_map")


class ConfigTest(unittest.TestCase):
    def _settings(self, **env):
        os.environ.update(env)
        try:
            result = workflow.parse(WORKFLOW)
            return config.parse_settings(result.config)
        finally:
            for key in env:
                os.environ.pop(key, None)

    def test_parse_settings(self):
        settings = self._settings(GITHUB_TOKEN="tok")
        self.assertEqual(settings.tracker.kind, "github")
        self.assertEqual(settings.tracker.provider["repo"], "owner/repo")
        self.assertEqual(settings.tracker.required_labels, ["agent"])
        self.assertEqual(settings.tracker.active_states, ["open"])
        self.assertEqual(settings.tracker.terminal_states, ["closed"])
        self.assertEqual(settings.polling.interval_ms, 10000)
        self.assertEqual(settings.agent.max_concurrent_agents, 3)
        self.assertEqual(settings.agent.max_turns, 12)
        self.assertEqual(settings.runtime.kind, "pi")
        self.assertEqual(settings.runtime.model, "test/model")
        self.assertTrue(settings.runtime.mcp["enabled"])
        self.assertEqual(settings.server.port, 8320)
        self.assertEqual(settings.workspace.root, "/tmp/ws")

    def test_defaults(self):
        settings = config.parse_settings({
            "tracker": {"kind": "github",
                        "provider": {"repo": "a/b", "token": "t"}},
        })
        self.assertEqual(settings.polling.interval_ms, 30000)
        self.assertEqual(settings.agent.max_concurrent_agents, 10)
        self.assertEqual(settings.agent.max_turns, 20)
        self.assertEqual(settings.runtime.kind, "codex")
        self.assertEqual(settings.server.host, "127.0.0.1")

    def test_env_indirection(self):
        os.environ["MY_TOKEN"] = "secret-value"
        try:
            self.assertEqual(
                config.resolve_env_setting("$MY_TOKEN"), "secret-value")
            self.assertIsNone(config.resolve_env_setting("$MISSING_VAR"))
            self.assertEqual(config.resolve_env_setting("literal"), "literal")
        finally:
            os.environ.pop("MY_TOKEN", None)

    def test_invalid_max_concurrent(self):
        with self.assertRaises(WorkflowError):
            config.parse_settings({
                "tracker": {"kind": "github",
                            "provider": {"repo": "a/b", "token": "t"}},
                "agent": {"max_concurrent_agents": "three"},
            })

    def test_env_root_resolution(self):
        os.environ["WS_ROOT"] = "/env/root"
        try:
            settings = config.parse_settings({
                "tracker": {"kind": "github",
                            "provider": {"repo": "a/b", "token": "t"}},
                "workspace": {"root": "$WS_ROOT"},
            })
            self.assertEqual(settings.workspace.root, "/env/root")
        finally:
            os.environ.pop("WS_ROOT", None)


if __name__ == "__main__":
    unittest.main()
