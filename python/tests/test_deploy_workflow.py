import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from symphony import workflow, config
from symphony.github import workflow_policy

ROOT = Path(__file__).resolve().parents[2]

class DeployWorkflowTest(unittest.TestCase):
    def test_templates_parse_and_validate(self):
        env = {'SYMPHONY_GITHUB_REPO': 'example/test', 'SYMPHONY_GITHUB_TOKEN': 'test',
               'SYMPHONY_WORKSPACE_ROOT': '/tmp/symphony-test/workspaces'}
        with patch.dict(os.environ, env):
            for name in ('WORKFLOW.md', 'WORKFLOW.test.md'):
                with self.subTest(name=name):
                    parsed = workflow.load(str(ROOT / 'deploy' / name))
                    settings = config.parse_settings(parsed.config)
                    workflow_policy.validate(settings.tracker)
                    workflow_policy.validate_runtime(settings)
                    self.assertEqual(settings.runtime.kind, 'pi')
                    self.assertTrue(workflow_policy.enabled(settings.tracker))
