import datetime
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import workflow, workflow_store  # noqa: E402
from symphony.agent import runner  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.template import TemplateError  # noqa: E402


class PromptBuilderTest(unittest.TestCase):
    def _issue(self, **kw):
        base = dict(
            id="1", identifier="MT-697", title="Live smoke",
            description="Prompt should serialize datetimes",
            state="Todo", url="https://example.org/issues/MT-697",
            labels=[],
        )
        base.update(kw)
        return Issue(**base)

    def _build(self, prompt, issue=None, attempt=None, turn_number=1):
        store = SimpleNamespace(
            current=lambda: workflow.Workflow({}, prompt))
        with mock.patch.object(workflow_store, "get_store", return_value=store):
            return runner._build_turn_prompt(
                issue or self._issue(), None, turn_number, 20, attempt)

    def test_renders_issue_labels_and_attempt(self):
        prompt = self._build(
            "Ticket {{ issue.identifier }} {{ issue.title }} "
            "labels={{ issue.labels }} attempt={{ attempt }}",
            issue=self._issue(labels=["backend"]),
            attempt=3)
        self.assertEqual(
            prompt,
            "Ticket MT-697 Live smoke labels=backend attempt=3")

    def test_renders_datetime_fields_as_iso8601(self):
        issue = self._issue(
            created_at=datetime.datetime(
                2026, 2, 26, 18, 6, 48, tzinfo=datetime.timezone.utc),
            updated_at=datetime.datetime(
                2026, 2, 26, 18, 7, 3, tzinfo=datetime.timezone.utc))
        prompt = self._build(
            "Ticket {{ issue.identifier }} created={{ issue.created_at }} "
            "updated={{ issue.updated_at }}", issue=issue)
        self.assertIn("created=2026-02-26T18:06:48Z", prompt)
        self.assertIn("updated=2026-02-26T18:07:03Z", prompt)

    def test_normalizes_nested_maps_and_dates(self):
        issue = self._issue(attachments=[{
            "title": "log",
            "created_at": datetime.datetime(
                2026, 2, 27, 12, 34, 56, tzinfo=datetime.timezone.utc),
        }])
        prompt = self._build(
            "{{ issue.attachments.0.title }} "
            "{{ issue.attachments.0.created_at }}", issue=issue)
        self.assertEqual(prompt, "log 2026-02-27T12:34:56Z")

    def test_strict_variables_raise_on_render(self):
        with self.assertRaises(TemplateError):
            self._build("Work on {{ missing.ticket_id }}")

    def test_invalid_template_reports_parse_context(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._build("{% if issue.identifier %}")
        message = str(ctx.exception)
        self.assertIn("template_parse_error:", message)
        self.assertIn("template=", message)

    def test_blank_prompt_uses_default_template(self):
        prompt = self._build("   \n")
        self.assertIn(
            "You are working on an issue from the configured tracker.", prompt)
        self.assertIn("Identifier: MT-697", prompt)
        self.assertIn("Title: Live smoke", prompt)
        self.assertIn("Body:", prompt)

    def test_default_template_handles_missing_description(self):
        prompt = self._build("", issue=self._issue(description=None))
        self.assertIn("Identifier: MT-697", prompt)
        self.assertIn("No description provided.", prompt)

    def test_attempt_condition(self):
        prompt = self._build(
            "{% if attempt %}Retry #{{ attempt }}{% endif %}", attempt=2)
        self.assertEqual(prompt, "Retry #2")

    def test_continuation_turn_does_not_render_template(self):
        prompt = self._build("ignored", turn_number=2)
        self.assertIn("Continuation guidance:", prompt)
        self.assertIn("continuation turn #2 of 20", prompt)


if __name__ == "__main__":
    unittest.main()
