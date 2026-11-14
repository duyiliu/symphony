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
  max_retry_attempts: 4
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

    def test_unterminated_front_matter_keeps_config_and_empty_prompt(self):
        result = workflow.parse("---\ntracker:\n  kind: linear\n")
        self.assertEqual(result.config, {"tracker": {"kind": "linear"}})
        self.assertEqual(result.prompt_template, "")

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
        self.assertEqual(settings.agent.max_retry_attempts, 4)
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
        self.assertEqual(settings.agent.max_retry_attempts, 10)
        self.assertEqual(settings.runtime.kind, "pi")
        self.assertEqual(settings.server.host, "127.0.0.1")
        self.assertEqual(settings.server.state_backup_interval_ms, 3_600_000)
        self.assertEqual(settings.server.state_backup_keep, 24)
        self.assertIsNone(settings.server.state_backup_dir)

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

    def test_invalid_max_retry_attempts(self):
        with self.assertRaises(WorkflowError):
            config.parse_settings({
                "tracker": {"kind": "github",
                            "provider": {"repo": "a/b", "token": "t"}},
                "agent": {"max_retry_attempts": -1},
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

    def test_linear_secret_reference_keeps_provider_literal(self):
        os.environ["SYMP_LINEAR_KEY"] = "resolved-secret"
        try:
            settings = config.parse_settings({
                "tracker": {
                    "kind": "linear",
                    "api_key": "$SYMP_LINEAR_KEY",
                    "project_slug": "project",
                },
            })
            self.assertEqual(settings.tracker.api_key, "resolved-secret")
            self.assertEqual(
                settings.tracker.provider["api_key"], "$SYMP_LINEAR_KEY")
            # Python strips additional host credentials from the Pi process;
            # the first two entries keep the upstream contract.
            self.assertEqual(
                settings.tracker.secret_environment_names[:2],
                ["LINEAR_API_KEY", "SYMP_LINEAR_KEY"])
        finally:
            os.environ.pop("SYMP_LINEAR_KEY", None)

    def test_linear_provider_precedes_compatibility_aliases(self):
        settings = config.parse_settings({
            "tracker": {
                "kind": "linear",
                "api_key": "alias-token",
                "project_slug": "alias-project",
                "provider": {
                    "endpoint": "https://linear.example.test/graphql",
                    "api_key": "provider-token",
                    "project_slug": "provider-project",
                    "extra": {"team": "platform"},
                },
            },
        })
        self.assertEqual(
            settings.tracker.endpoint, "https://linear.example.test/graphql")
        self.assertEqual(settings.tracker.api_key, "provider-token")
        self.assertEqual(settings.tracker.project_slug, "provider-project")
        self.assertEqual(
            settings.tracker.provider["extra"], {"team": "platform"})
        self.assertIn("assignee", settings.tracker.provider)

    def test_non_linear_does_not_inject_linear_defaults(self):
        settings = config.parse_settings({"tracker": {"kind": "future-tracker"}})
        self.assertIsNone(settings.tracker.endpoint)
        self.assertIsNone(settings.tracker.api_key)
        self.assertIsNone(settings.tracker.active_states)
        self.assertIsNone(settings.tracker.terminal_states)
        self.assertEqual(settings.tracker.provider, {})

    def test_legacy_env_prefix_is_not_resolved(self):
        os.environ["SYMP_LEGACY_TOKEN"] = "resolved-secret"
        try:
            settings = config.parse_settings({
                "tracker": {
                    "kind": "linear",
                    "api_key": "env:SYMP_LEGACY_TOKEN",
                    "project_slug": "project",
                },
            })
            self.assertEqual(
                settings.tracker.api_key, "env:SYMP_LEGACY_TOKEN")
        finally:
            os.environ.pop("SYMP_LEGACY_TOKEN", None)

    def test_required_labels_are_trimmed_lowercased_and_deduped(self):
        settings = config.parse_settings({
            "tracker": {
                "kind": "github",
                "provider": {"repo": "a/b", "token": "t"},
                "required_labels": [" Agent ", "agent", "QA", " "],
            },
        })
        self.assertEqual(settings.tracker.required_labels, ["agent", "qa", ""])

    def test_per_state_concurrency_limits(self):
        settings = config.parse_settings({
            "tracker": {
                "kind": "github",
                "provider": {"repo": "a/b", "token": "t"},
            },
            "agent": {
                "max_concurrent_agents": 10,
                "max_concurrent_agents_by_state": {
                    "todo": 1,
                    "In Progress": 4,
                },
            },
        })
        self.assertEqual(settings.agent.max_concurrent_agents, 10)
        self.assertEqual(
            config.max_concurrent_agents_for_state(settings, "Todo"), 1)
        self.assertEqual(
            config.max_concurrent_agents_for_state(settings, "In Progress"), 4)
        self.assertEqual(
            config.max_concurrent_agents_for_state(settings, "Closed"), 10)
        self.assertEqual(
            config.max_concurrent_agents_for_state(settings, None), 10)

    def test_workspace_root_missing_env_falls_back_to_default(self):
        settings = config.parse_settings({
            "tracker": {
                "kind": "github",
                "provider": {"repo": "a/b", "token": "t"},
            },
            "workspace": {"root": "$SYMP_MISSING_WORKSPACE_ROOT"},
        })
        self.assertEqual(
            settings.workspace.root,
            os.path.join(tempfile.gettempdir(), "symphony_workspaces"))


class WorkflowRuntimeValidationTest(unittest.TestCase):
    def _settings(self, runtime):
        return config.parse_settings({
            "tracker": {"kind": "github",
                        "provider": {"repo": "a/b", "token": "t"},
                        "active_states": ["open"],
                        "terminal_states": ["closed"]},
            "runtime": runtime,
        })

    def test_pi_requires_command(self):
        from symphony import workflow_store

        with self.assertRaises(WorkflowError) as ctx:
            workflow_store._validate_settings(self._settings({"kind": "pi"}))
        self.assertEqual(ctx.exception.reason, "missing_pi_command")

    def test_codex_is_rejected(self):
        from symphony import workflow_store

        with self.assertRaises(WorkflowError) as ctx:
            workflow_store._validate_settings(self._settings({
                "kind": "codex",
                "command": "codex app-server",
            }))
        self.assertEqual(ctx.exception.reason, "unsupported_runtime_kind")


class WorkflowStoreParityTest(unittest.TestCase):
    def _write(self, path, project_slug):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "---\n"
                "tracker:\n"
                "  kind: linear\n"
                "  api_key: token\n"
                "  project_slug: %s\n"
                "runtime:\n"
                "  kind: pi\n"
                "  command: pi --mode rpc\n"
                "---\n"
                "prompt\n" % project_slug)

    def test_initial_load_fails_on_invalid_workflow(self):
        from symphony import workflow_store

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "WORKFLOW.md")
            self._write(path, '""')
            with self.assertRaises(WorkflowError) as ctx:
                workflow_store.WorkflowStore(path)
        self.assertEqual(ctx.exception.reason, "missing_linear_project_slug")

    def test_reload_failure_keeps_last_good_settings(self):
        from symphony import workflow_store

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "WORKFLOW.md")
            self._write(path, "project")
            store = workflow_store.WorkflowStore(path)
            self.assertEqual(store.settings().tracker.project_slug, "project")

            self._write(path, '""')
            self.assertEqual(
                store.settings().tracker.project_slug, "project")
            with self.assertRaises(WorkflowError):
                store.force_reload()


if __name__ == "__main__":
    unittest.main()
