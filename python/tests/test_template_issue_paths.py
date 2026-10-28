import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import template, workspace  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.paths import PathSafetyError, safe_join  # noqa: E402
from symphony.template import TemplateError  # noqa: E402


class TemplateTest(unittest.TestCase):
    def render(self, source, **ctx):
        return template.render(source, ctx)

    def test_output(self):
        issue = {"identifier": "GH-1", "title": "T", "labels": ["a", "b"]}
        self.assertEqual(
            self.render("{{ issue.identifier }}: {{ issue.title }}", issue=issue),
            "GH-1: T")

    def test_if_else(self):
        self.assertEqual(
            self.render("{% if x %}yes{% else %}no{% endif %}", x=None), "no")
        self.assertEqual(
            self.render("{% if x %}yes{% else %}no{% endif %}", x=""), "yes")

    def test_elsif(self):
        source = "{% if a %}A{% elsif b %}B{% else %}C{% endif %}"
        self.assertEqual(self.render(source, a=None, b=1), "B")
        self.assertEqual(self.render(source, a=None, b=None), "C")

    def test_unless(self):
        self.assertEqual(
            self.render("{% unless x %}no-x{% endunless %}", x=None), "no-x")

    def test_for(self):
        self.assertEqual(
            self.render("{% for i in items %}{{ i }};{% endfor %}",
                        items=[1, 2, 3]), "1;2;3;")
        self.assertEqual(
            self.render("{% for i in items %}x{% else %}empty{% endfor %}",
                        items=[]), "empty")

    def test_comparisons(self):
        self.assertEqual(
            self.render("{% if n > 2 %}big{% endif %}", n=5), "big")
        self.assertEqual(
            self.render("{% if s == 'x' %}eq{% endif %}", s="x"), "eq")

    def test_undefined_raises(self):
        with self.assertRaises(TemplateError):
            self.render("{{ missing.key }}")

    def test_deploy_workflow_shape(self):
        issue = Issue(
            id="42", identifier="GH-42", title="Fix bug",
            description=None, state="open", labels=["agent"],
            dispatchable=True)
        source = (
            "Issue: {{ issue.identifier }}\n"
            "Labels: {{ issue.labels }}\n"
            "{% if issue.description %}{{ issue.description }}\n"
            "{% else %}No issue description provided.\n{% endif %}"
            "{% if attempt %}attempt {{ attempt }}{% endif %}")
        rendered = self.render(
            source, issue=issue.to_template_map(), attempt=None)
        self.assertIn("Issue: GH-42", rendered)
        self.assertIn("No issue description provided.", rendered)
        self.assertNotIn("attempt", rendered.split("provided.")[-1])


class IssueTest(unittest.TestCase):
    def test_routable(self):
        issue = Issue(labels=["Agent"], dispatchable=True)
        self.assertTrue(issue.routable(["agent"]))
        self.assertFalse(issue.routable(["agent", "other"]))
        self.assertFalse(Issue(labels=["agent"], dispatchable=False)
                         .routable(["agent"]))
        self.assertTrue(Issue(dispatchable=True).routable([]))


class PathTest(unittest.TestCase):
    def test_safe_join(self):
        root = tempfile.mkdtemp()
        self.assertTrue(
            safe_join(root, "GH-1").startswith(os.path.realpath(root)))
        with self.assertRaises(PathSafetyError):
            safe_join(root, "..", "escape")

    def test_workspace_key(self):
        self.assertEqual(workspace.workspace_key("GH-89"), "GH-89")
        key = workspace.workspace_key("GH 89/evil")
        self.assertNotEqual(key, "GH 89/evil")
        self.assertIn("--", key)
        self.assertNotIn("/", key)
        # "" stays "" (Elixir parity); non-string falls back to "issue".
        self.assertEqual(workspace.workspace_key(""), "")
        self.assertEqual(workspace.workspace_key(None), "issue")
        # collision-free: two unsafe identifiers never share a directory
        self.assertNotEqual(
            workspace.workspace_key("a b"), workspace.workspace_key("a/b"))


if __name__ == "__main__":
    unittest.main()
