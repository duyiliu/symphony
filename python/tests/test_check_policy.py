"""Optional GitHub check policy: config validation, classification, admission.

The classification and configuration cases live in the shared fixture
``tests/fixtures/github-check-policy.json`` and are also consumed by the Elixir
suite, so both implementations must agree on every row.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from symphony.github import workflow as gh_workflow  # noqa: E402
from symphony.github import workflow_api, workflow_policy  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "tests", "fixtures",
    "github-check-policy.json")

with open(FIXTURE, "r", encoding="utf-8") as _handle:
    FIXTURE_DATA = json.load(_handle)

SHA = FIXTURE_DATA["sha"]
OTHER_SHA = FIXTURE_DATA["other_sha"]

_ABSENT = object()


def _settings(checks=_ABSENT):
    config = {
        "enabled": True,
        "state_dir": "/var/lib/symphony/github-workflow",
        "base_branch": "main",
    }
    if checks is not _ABSENT:
        config["checks"] = checks
    return SimpleNamespace(
        kind="github",
        provider={"repo": "o/r", "token": "t"},
        required_labels=[],
        github_workflow=config,
    )


class SharedFixtureTest(unittest.TestCase):
    """Both languages classify every fixture row identically."""

    def test_classification_cases(self):
        self.assertTrue(FIXTURE_DATA["classification"])
        for case in FIXTURE_DATA["classification"]:
            with self.subTest(case=case["name"]):
                pending, failing = gh_workflow.classify_checks(
                    case["runs"], case["statuses"], SHA, case["checks"])
                self.assertEqual(case["pending"], pending)
                self.assertEqual(case["failing"], failing)

    def test_config_valid_cases(self):
        self.assertTrue(FIXTURE_DATA["config"]["valid"])
        for case in FIXTURE_DATA["config"]["valid"]:
            with self.subTest(note=case["note"]):
                checks = _ABSENT if case.get("absent") else case["checks"]
                self.assertIsNone(workflow_policy.validate(_settings(checks)))

    def test_config_invalid_cases(self):
        self.assertTrue(FIXTURE_DATA["config"]["invalid"])
        for case in FIXTURE_DATA["config"]["invalid"]:
            with self.subTest(checks=case["checks"]):
                with self.assertRaises(WorkflowError) as ctx:
                    workflow_policy.validate(_settings(case["checks"]))
                self.assertEqual(ctx.exception.reason, case["error"])


class CheckPolicyTest(unittest.TestCase):
    def test_absent_keeps_previous_behaviour(self):
        self.assertEqual(
            workflow_policy.check_policy(_settings()),
            {"required": [], "allow_skipped": []})
        # Unconfigured: a skipped check still needs repair.
        self.assertEqual(
            gh_workflow.classify_checks(
                [{"id": 1, "name": "docs", "head_sha": SHA,
                  "status": "completed", "conclusion": "skipped"}],
                [], SHA),
            ([], ["docs"]))

    def test_policy_is_normalized(self):
        policy = workflow_policy.check_policy(
            _settings({"required": [" tests ", "tests", "lint"],
                       "allow_skipped": [" docs ", "docs"]}))
        self.assertEqual(policy["required"], ["tests", "lint"])
        self.assertEqual(policy["allow_skipped"], ["docs"])

    def test_allowed_skip_is_not_reported_as_a_pass(self):
        # The classifier has no "passed" list: a tolerated skip is absent from
        # both results, i.e. it does not block and is not claimed as verified.
        pending, failing = gh_workflow.classify_checks(
            [{"id": 1, "name": "docs", "head_sha": SHA,
              "status": "completed", "conclusion": "skipped"}],
            [], SHA, {"required": [], "allow_skipped": ["docs"]})
        self.assertEqual((pending, failing), ([], []))

    def test_invalid_policy_normalizes_to_empty(self):
        # validate/1 rejects this before admission; the accessor still fails safe.
        policy = workflow_policy.check_policy(_settings({"required": "tests"}))
        self.assertEqual(policy, {"required": [], "allow_skipped": []})

    def test_disabled_workflow_does_not_validate_checks(self):
        settings = _settings("not-a-map")
        settings.github_workflow["enabled"] = False
        self.assertIsNone(workflow_policy.validate(settings))

    def test_explicit_empty_policy_equals_default(self):
        empty = {"required": [], "allow_skipped": []}
        for case in FIXTURE_DATA["classification"]:
            if case["checks"] is not None:
                continue
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    gh_workflow.classify_checks(
                        case["runs"], case["statuses"], SHA, empty),
                    gh_workflow.classify_checks(
                        case["runs"], case["statuses"], SHA))


class MergeAdmissionTest(unittest.TestCase):
    """The admission boundary (merge_delivery) must stop on missing/failed
    required checks and on the wrong SHA, and must not call the merge for them.
    """

    def setUp(self):
        self.record = {
            "cycle": "develop:1",
            "attempts": 1,
            "blocked": False,
            "delivery": {"pr": 2, "sha": SHA, "base": "main",
                         "cycle": "develop:1", "published": True},
        }

    def _merge(self, settings, runs, statuses, gate=None):
        pr_data = {
            "state": "open", "draft": False, "merged": False,
            "user": {"login": "author", "type": "User"},
            "head": {"sha": SHA, "repo": {"full_name": "o/r"}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}},
        }
        gate = gate or {
            "headRefOid": SHA, "baseRefName": "main",
            "mergeStateStatus": "CLEAN", "reviewDecision": None,
        }

        def pages(_settings, path, field, **_kw):
            if path.endswith("/check-runs"):
                return runs
            if path.endswith("/statuses"):
                return statuses
            raise AssertionError("unexpected pages path %s" % path)

        with mock.patch.object(gh_workflow, "signal",
                               return_value=({"id": 1}, ["agent"])), \
                mock.patch.object(gh_workflow, "_linked_pr", return_value=2), \
                mock.patch.object(gh_workflow, "fetch_pr",
                                  return_value=pr_data), \
                mock.patch.object(gh_workflow, "_validate_pr"), \
                mock.patch.object(gh_workflow, "pages", side_effect=pages), \
                mock.patch.object(gh_workflow, "merge_gate",
                                  return_value=gate), \
                mock.patch.object(gh_workflow, "pr_files",
                                  return_value=[{"filename": "lib/app.py"}]), \
                mock.patch.object(gh_workflow, "pr_reviews",
                                  return_value=[]), \
                mock.patch.object(gh_workflow, "commit_merge",
                                  return_value={"status": "merged"}) as commit:
            result = gh_workflow.merge_delivery(
                settings, 1, self.record, lambda record: record)
        return result, commit

    @staticmethod
    def _run(name, conclusion, sha=SHA, rid=1, status="completed"):
        return {"id": rid, "name": name, "head_sha": sha,
                "status": status, "conclusion": conclusion}

    def test_missing_required_check_waits_without_merging(self):
        result, commit = self._merge(
            _settings({"required": ["tests"], "allow_skipped": []}),
            [self._run("build", "success")], [])
        self.assertEqual(result["status"], "waiting_checks")
        self.assertIn("tests", result["pending"])
        commit.assert_not_called()

    def test_required_check_wrong_sha_waits_without_merging(self):
        result, commit = self._merge(
            _settings({"required": ["tests"], "allow_skipped": []}),
            [self._run("tests", "success", sha=OTHER_SHA)], [])
        self.assertEqual(result["status"], "waiting_checks")
        self.assertIn("tests", result["pending"])
        commit.assert_not_called()

    def test_required_failure_needs_repair(self):
        result, commit = self._merge(
            _settings({"required": ["tests"], "allow_skipped": []}),
            [self._run("tests", "failure")], [])
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("tests", result["reason"])
        commit.assert_not_called()

    def test_required_skipped_needs_repair(self):
        result, commit = self._merge(
            _settings({"required": ["tests"], "allow_skipped": []}),
            [self._run("tests", "skipped")], [])
        self.assertEqual(result["status"], "needs_repair")
        commit.assert_not_called()

    def test_allowed_optional_skip_admits(self):
        settings = _settings(
            {"required": ["tests"], "allow_skipped": ["docs"]})
        result, commit = self._merge(
            settings,
            [self._run("tests", "success", rid=1),
             self._run("docs", "skipped", rid=2)], [])
        self.assertEqual(result["status"], "merged")
        commit.assert_called_once()

    def test_default_policy_still_repairs_on_skip(self):
        result, commit = self._merge(
            _settings(),
            [self._run("docs", "skipped")], [])
        self.assertEqual(result["status"], "needs_repair")
        commit.assert_not_called()

    def test_neutral_is_never_admitted_by_allow_skipped(self):
        settings = _settings({"allow_skipped": ["docs"]})
        result, commit = self._merge(
            settings, [self._run("docs", "neutral")], [])
        self.assertEqual(result["status"], "needs_repair")
        commit.assert_not_called()


class PolicyValidationThroughGateTest(unittest.TestCase):
    def test_workflow_tool_rejects_invalid_check_policy(self):
        with self.assertRaises(workflow_api.GateError) as ctx:
            gh_workflow.execute(
                _settings({"required": ["tests"], "allow_skipped": ["tests"]}),
                {"action": "status", "issue": "1"})
        self.assertEqual(ctx.exception.reason, "invalid_github_workflow_checks")


if __name__ == "__main__":
    unittest.main()
