"""Regression: pausing a workflow must persist a blocked receipt.

A receipt ``delivery`` is a mapping. Formatting it through ``_inspect`` used to
raise ``ValueError: unsupported format character '{'`` because ``%{`` was left
unescaped in the format string, so the pause never reached storage.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.github import workflow  # noqa: E402

SHA = "a" * 40


class InspectTest(unittest.TestCase):
    def test_dictionary_uses_elixir_inspect_shape(self):
        value = {"integration_decision": "waiting_checks", "pr": 2}
        self.assertEqual(
            workflow._inspect(value),
            '%{"integration_decision" => waiting_checks, "pr" => 2}')

    def test_nested_structures_do_not_raise(self):
        value = {"delivery": {"pr": 2, "sha": SHA}, "list": [1, None, True]}
        text = workflow._inspect(value)
        self.assertTrue(text.startswith('%{"delivery" => %{'))
        self.assertIn('"sha" => ' + SHA, text)
        self.assertIn('"list" => [1, nil, true]', text)


class PausePersistenceTest(unittest.TestCase):
    def test_pause_saves_blocked_record_and_posts_comment(self):
        saved = {}
        settings = SimpleNamespace(
            github_workflow={"enabled": True, "state_dir": "/tmp/unused"})
        record = {"cycle": "develop:1", "attempts": 1,
                  "delivery": {"pr": 2, "sha": SHA}}
        reason = '%{"integration_decision" => "waiting_checks"}'
        with mock.patch.object(workflow, "comment") as comment, \
                mock.patch.object(workflow, "add_label") as add, \
                mock.patch.object(workflow, "remove_label") as remove:
            workflow.pause(settings, 1, record, saved.update, reason)

        self.assertTrue(saved["blocked"])
        self.assertEqual(saved["error"], reason)
        self.assertNotIn("blocked", record)
        body = comment.call_args[0][2]
        self.assertIn("Paused (needs-human): " + reason, body)
        self.assertIn('Delivery: %{"pr" => 2, "sha" => ' + SHA + '}', body)
        add.assert_called_once_with(settings, 1, "needs-human")
        remove.assert_called_once_with(settings, 1, "agent")


if __name__ == "__main__":
    unittest.main()
