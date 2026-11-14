"""Tests for the Python ports of upstream mix dev tasks.

- ``mix pr_body.check`` -> ``python -m symphony.pr_body_check``
- ``mix workspace.before_remove`` -> ``python -m symphony.workspace_before_remove``
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from symphony import pr_body_check  # noqa: E402
from symphony import workspace_before_remove  # noqa: E402


TEMPLATE = """\
## Summary

## Checklist
- [ ] Tests pass
- [ ] Docs updated
"""

BODY_OK = """\
## Summary

Did a thing.

## Checklist
- [x] Tests pass
- [x] Docs updated
"""

BODY_MISSING_HEADING = """\
## Summary
- [x] Tests pass
- [x] Docs updated
"""

BODY_UNCHECKED = """\
## Summary

## Checklist
- [ ] Tests pass
- [x] Docs updated
"""


class PrBodyCheckTest(unittest.TestCase):
    def test_ok_body_passes(self):
        self.assertEqual(pr_body_check.check(TEMPLATE, BODY_OK), [])

    def test_missing_heading_is_reported(self):
        problems = pr_body_check.check(TEMPLATE, BODY_MISSING_HEADING)
        self.assertIn("missing heading: ## Checklist", problems)

    def test_unchecked_checkbox_is_reported(self):
        problems = pr_body_check.check(TEMPLATE, BODY_UNCHECKED)
        self.assertIn("checkbox not ticked: Tests pass", problems)

    def test_missing_template_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            body_path = os.path.join(tmp, "body.md")
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write(BODY_OK)
            rc = pr_body_check.main([
                "--file", body_path,
                "--template", os.path.join(tmp, "missing.md"),
            ])
        self.assertEqual(rc, 2)

    def test_cli_ok_returns_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            template_path = os.path.join(tmp, "template.md")
            body_path = os.path.join(tmp, "body.md")
            with open(template_path, "w", encoding="utf-8") as handle:
                handle.write(TEMPLATE)
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write(BODY_OK)
            rc = pr_body_check.main([
                "--file", body_path, "--template", template_path,
            ])
        self.assertEqual(rc, 0)

    def test_cli_failure_returns_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            template_path = os.path.join(tmp, "template.md")
            body_path = os.path.join(tmp, "body.md")
            with open(template_path, "w", encoding="utf-8") as handle:
                handle.write(TEMPLATE)
            with open(body_path, "w", encoding="utf-8") as handle:
                handle.write(BODY_MISSING_HEADING)
            rc = pr_body_check.main([
                "--file", body_path, "--template", template_path,
            ])
        self.assertEqual(rc, 1)


class WorkspaceBeforeRemoveTest(unittest.TestCase):
    def test_no_branch_is_noop(self):
        with mock.patch.object(
                workspace_before_remove, "_close_pr") as close:
            workspace_before_remove.maybe_close_open_pull_requests(
                "o/r", None)
        close.assert_not_called()

    def test_gh_missing_is_noop(self):
        with mock.patch.object(
                workspace_before_remove, "_gh_available",
                return_value=False), \
             mock.patch.object(
                 workspace_before_remove, "_close_pr") as close:
            workspace_before_remove.maybe_close_open_pull_requests(
                "o/r", "feature/x")
        close.assert_not_called()

    def test_gh_unauthenticated_is_noop(self):
        with mock.patch.object(
                workspace_before_remove, "_gh_available",
                return_value=True), \
             mock.patch.object(
                 workspace_before_remove, "_gh_authenticated",
                 return_value=False), \
             mock.patch.object(
                 workspace_before_remove, "_close_pr") as close:
            workspace_before_remove.maybe_close_open_pull_requests(
                "o/r", "feature/x")
        close.assert_not_called()

    def test_closes_each_open_pr(self):
        with mock.patch.object(
                workspace_before_remove, "_gh_available",
                return_value=True), \
             mock.patch.object(
                 workspace_before_remove, "_gh_authenticated",
                 return_value=True), \
             mock.patch.object(
                 workspace_before_remove, "_open_pr_numbers",
                 return_value=[7, 9]), \
             mock.patch.object(
                 workspace_before_remove, "_close_pr") as close:
            workspace_before_remove.maybe_close_open_pull_requests(
                "o/r", "feature/x")
        self.assertEqual(
            [c.args for c in close.call_args_list],
            [("o/r", "feature/x", 7), ("o/r", "feature/x", 9)])

    def test_open_pr_numbers_ignores_malformed_rows(self):
        result = mock.Mock()
        result.returncode = 0
        result.stdout = '[{"number": 3}, {"number": "x"}, {}]'
        with mock.patch.object(
                workspace_before_remove, "_run", return_value=result):
            numbers = workspace_before_remove._open_pr_numbers(
                "o/r", "b")
        self.assertEqual(numbers, [3])

    def test_open_pr_numbers_empty_on_failure(self):
        result = mock.Mock()
        result.returncode = 1
        result.stdout = ""
        with mock.patch.object(
                workspace_before_remove, "_run", return_value=result):
            self.assertEqual(
                workspace_before_remove._open_pr_numbers("o/r", "b"), [])


if __name__ == "__main__":
    unittest.main()
