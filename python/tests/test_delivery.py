"""Deterministic tests for the host-owned Linear delivery gate."""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, delivery, linear, tracker, workflow_store  # noqa: E402
from symphony.github import workflow_policy  # noqa: E402
from symphony.issue import Issue  # noqa: E402


def settings(**overrides):
    raw = {
        "tracker": {
            "kind": "linear",
            "provider": {
                "project_slug": "example",
                "api_key": "linear-token",
            },
            "active_states": ["Todo", "In Progress", "Rework"],
            "terminal_states": ["Done", "Canceled"],
        },
        "delivery": {
            "enabled": True,
            "repo": "o/r",
            "token": "merge-token",
            "base_branch": "main",
            "review_state": "Human Review",
            "merge_state": "Merging",
            "rework_state": "Rework",
            "done_state": "Done",
            "require_branch_protection": True,
            "required_approvals": 0,
            "risk_paths": ["auth/**", ".github/workflows/**", "**/*.sql"],
            "risk_labels": ["high-risk"],
            "max_auto_files": 10,
            "max_rework_attempts": 2,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return config.parse_settings(raw)


def issue(state="Human Review", labels=None, attachments=None,
          updated_at=None):
    return Issue(
        id="issue-uuid", identifier="ABC-1", title="Fix it",
        state=state, labels=labels or [], team_id="team-1",
        attachments=attachments or [{
            "url": "https://github.com/o/r/pull/7",
            "metadata": {"status": "open"},
        }],
        updated_at=updated_at or datetime.datetime(
            2026, 11, 13, tzinfo=datetime.timezone.utc),
    )


def pr_payload(**overrides):
    value = {
        "number": 7,
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "author"},
        "head": {"sha": "a" * 40, "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }
    value.update(overrides)
    return value


def protection(**overrides):
    value = {
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_status_checks": {"contexts": ["test"], "checks": []},
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
        },
    }
    value.update(overrides)
    return value


class DecisionTest(unittest.TestCase):
    def test_low_risk_green_pr_is_mergeable(self):
        cfg = settings()
        action, reason, details = delivery.evaluate(
            cfg, issue(), pr_payload(),
            ([], [], ["test"], True),
            [{"filename": "src/x.js"}], [], protection(), {})
        self.assertEqual(action, "merge")
        self.assertEqual(details["sha"], "a" * 40)
        self.assertIn("gates passed", reason)

    def test_high_risk_path_pauses(self):
        cfg = settings()
        action, reason, _ = delivery.evaluate(
            cfg, issue(), pr_payload(),
            ([], [], ["test"], True),
            [{"filename": "auth/login.js"}], [], protection(), {})
        self.assertEqual(action, "pause")
        self.assertIn("high-risk path", reason)

    def test_high_risk_label_pauses(self):
        cfg = settings()
        action, reason, _ = delivery.evaluate(
            cfg, issue(labels=["High-Risk"]), pr_payload(),
            ([], [], ["test"], True),
            [{"filename": "src/x.js"}], [], protection(), {})
        self.assertEqual(action, "pause")
        self.assertIn("high-risk label", reason)

    def test_failed_checks_request_rework(self):
        cfg = settings()
        action, reason, _ = delivery.evaluate(
            cfg, issue(), pr_payload(),
            ([], ["test"], [], True),
            [{"filename": "src/x.js"}], [], protection(), {})
        self.assertEqual(action, "rework")
        self.assertIn("checks failed", reason)

    def test_missing_checks_pauses(self):
        cfg = settings()
        action, reason, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], [], False),
            [{"filename": "src/x.js"}], [], protection(), {})
        self.assertEqual(action, "pause")
        self.assertIn("no CI checks", reason)

    def test_missing_branch_protection_pauses(self):
        cfg = settings()
        action, reason, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], ["test"], True),
            [{"filename": "src/x.js"}], [], delivery._NOT_FOUND, {})
        self.assertEqual(action, "pause")
        self.assertIn("branch protection", reason)

    def test_missing_branch_protection_can_be_explicitly_allowed(self):
        cfg = settings(delivery={"allow_without_branch_protection": True})
        action, _, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], ["test"], True),
            [{"filename": "src/x.js"}], [], delivery._NOT_FOUND, {})
        self.assertEqual(action, "merge")

    def test_required_review_waits(self):
        cfg = settings()
        action, reason, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], ["test"], True),
            [{"filename": "src/x.js"}], [],
            protection(required_pull_request_reviews={
                "required_approving_review_count": 1}), {})
        self.assertEqual(action, "wait")
        self.assertIn("approval", reason)

    def test_changes_requested_requests_rework(self):
        cfg = settings()
        reviews = [{
            "user": {"login": "reviewer", "type": "User"},
            "state": "CHANGES_REQUESTED",
            "commit_id": "a" * 40,
            "submitted_at": "2026-11-13T00:00:00Z",
        }]
        action, reason, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], ["test"], True),
            [{"filename": "src/x.js"}], reviews, protection(), {})
        self.assertEqual(action, "rework")
        self.assertIn("requested changes", reason)

    def test_stale_approval_does_not_count(self):
        cfg = settings(delivery={"required_approvals": 1})
        reviews = [{
            "user": {"login": "reviewer", "type": "User"},
            "state": "APPROVED",
            "commit_id": "b" * 40,
            "submitted_at": "2026-11-13T00:00:00Z",
        }]
        action, _, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], ["test"], True),
            [{"filename": "src/x.js"}], reviews, protection(), {})
        self.assertEqual(action, "wait")

    def test_current_approval_allows_low_risk_merge(self):
        cfg = settings(delivery={"required_approvals": 1})
        reviews = [{
            "user": {"login": "reviewer", "type": "User"},
            "state": "APPROVED",
            "commit_id": "a" * 40,
            "submitted_at": "2026-11-13T00:00:00Z",
        }]
        action, _, _ = delivery.evaluate(
            cfg, issue(), pr_payload(), ([], [], ["test"], True),
            [{"filename": "src/x.js"}], reviews, protection(), {})
        self.assertEqual(action, "merge")

    def test_merged_pr_is_confirmed(self):
        cfg = settings()
        action, _, details = delivery.evaluate(
            cfg, issue(), pr_payload(merged=True), ([], [], [], False),
            [], [], delivery._NOT_FOUND, {})
        self.assertEqual(action, "confirm")
        self.assertEqual(details["sha"], "a" * 40)


class ChecksTest(unittest.TestCase):
    def test_pending_run_for_other_sha(self):
        pending, failing, successful, present = delivery.classify_checks(
            [{
                "id": 1, "name": "test", "head_sha": "b" * 40,
                "status": "completed", "conclusion": "success",
                "app": {"id": 1},
            }], [], "a" * 40)
        self.assertEqual(pending, ["test"])
        self.assertEqual(failing, [])
        self.assertEqual(successful, [])
        self.assertTrue(present)

    def test_newest_run_per_app_wins(self):
        runs = [
            {"id": 1, "name": "test", "head_sha": "a" * 40,
             "status": "completed", "conclusion": "failure",
             "app": {"id": 1}},
            {"id": 2, "name": "test", "head_sha": "a" * 40,
             "status": "completed", "conclusion": "success",
             "app": {"id": 1}},
        ]
        pending, failing, successful, present = delivery.classify_checks(
            runs, [], "a" * 40)
        self.assertEqual((pending, failing, successful, present),
                         ([], [], ["test"], True))


class DiscoveryTest(unittest.TestCase):
    def test_error_message_tolerates_tuple_detail(self):
        error = delivery.DeliveryError("github_status", (500, "GET", "/x"))
        self.assertEqual(str(error), "github_status: (500, 'GET', '/x')")

    def test_single_pr(self):
        self.assertEqual(delivery.discover_pr(issue(), "o/r"), 7)

    def test_ambiguous_open_prs_fail_closed(self):
        value = issue(attachments=[
            {"url": "https://github.com/o/r/pull/7",
             "metadata": {"status": "open"}},
            {"url": "https://github.com/o/r/pull/8",
             "metadata": {"status": "open"}},
        ])
        with self.assertRaises(delivery.DeliveryError):
            delivery.discover_pr(value, "o/r")

    def test_closed_prs_are_ignored_when_one_open(self):
        value = issue(attachments=[
            {"url": "https://github.com/o/r/pull/7",
             "metadata": {"status": "open"}},
            {"url": "https://github.com/o/r/pull/6",
             "metadata": {"status": "closed"}},
        ])
        self.assertEqual(delivery.discover_pr(value, "o/r"), 7)


class _Store:
    def __init__(self, cfg):
        self._settings = cfg

    def settings(self):
        return self._settings


class _FakeGitHub:
    def __init__(self, settings_value, pr=None, files=None, reviews=None,
                 protection_value=None, merge_result=None):
        self.settings = settings_value
        self.pr = pr or pr_payload()
        self.files = files if files is not None else [
            {"filename": "src/x.js"}]
        self.reviews = reviews or []
        self.protection = protection_value \
            if protection_value is not None else protection()
        self.merge_result = merge_result or {"merged": True}
        self.calls = []

    def request(self, method, path, params=None, body=None,
                allow_missing=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/pulls/7"):
            return self.pr
        if method == "GET" and "/protection" in path:
            return self.protection
        if method == "PUT" and path.endswith("/pulls/7/merge"):
            self.pr = {**self.pr, "merged": True, "state": "closed"}
            return self.merge_result
        raise AssertionError("unexpected request %s %s" % (method, path))

    def pages(self, path, field=None):
        if "/check-runs" in path:
            return [{
                "id": 3, "name": "test", "head_sha": "a" * 40,
                "status": "completed", "conclusion": "success",
                "app": {"id": 1},
            }]
        if "/statuses" in path:
            return []
        if "/files" in path:
            return self.files
        if "/reviews" in path:
            return self.reviews
        raise AssertionError("unexpected pages %s" % path)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="symphony-delivery-")
        self.cfg = settings(delivery={"state_dir": self.temp.name})
        self.controller = delivery.DeliveryController(_Store(self.cfg))
        self.transitions = []
        self.comments = []
        def refresh(_tracker, _ids):
            state = self.transitions[-1] if self.transitions else "Human Review"
            return [issue(state=state)]

        self.patches = [
            patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue()]),
            patch.object(
                delivery.tracker_adapter, "fetch_issues_by_ids",
                side_effect=refresh),
            patch.object(
                linear, "update_issue_state",
                side_effect=lambda _s, _i, _t, state: self.transitions.append(
                    state)),
            patch.object(
                linear, "create_comment",
                side_effect=lambda _s, _i, body: self.comments.append(body)),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_low_risk_pr_is_merged_and_linear_done(self):
        fake = _FakeGitHub(self.cfg.delivery)
        with patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertEqual(self.transitions, ["Merging", "Done"])
        self.assertTrue(any("已由宿主安全门禁合并" in body
                            for body in self.comments))
        self.assertTrue(any(call[0] == "PUT" for call in fake.calls))

    def test_merge_is_not_repeated_after_restart(self):
        fake = _FakeGitHub(
            self.cfg.delivery,
            pr={**pr_payload(), "merged": True, "state": "closed"})
        issue_value = issue(state="Merging")
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue_value]), \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertEqual(self.transitions, ["Done"])
        self.assertFalse(any(call[0] == "PUT" for call in fake.calls))

    def test_high_risk_pr_is_paused_without_merge(self):
        fake = _FakeGitHub(
            self.cfg.delivery, files=[{"filename": "auth/login.js"}])
        with patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertNotIn("Done", self.transitions)
        self.assertTrue(any("自动交付已暂停" in body
                            for body in self.comments))
        self.assertFalse(any(call[0] == "PUT" for call in fake.calls))

    def test_failed_checks_move_to_rework(self):
        fake = _FakeGitHub(self.cfg.delivery)
        fake.pages = lambda path, field=None: (
            [{
                "id": 3, "name": "test", "head_sha": "a" * 40,
                "status": "completed", "conclusion": "failure",
                "app": {"id": 1},
            }] if "/check-runs" in path else []
        )
        with patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertEqual(self.transitions, ["Rework"])
        self.assertFalse(any(call[0] == "PUT" for call in fake.calls))


class ConfigTest(unittest.TestCase):
    def test_delivery_requires_host_token(self):
        with self.assertRaises(config.WorkflowError):
            settings(delivery={"enabled": True, "token": None})

    def test_delivery_states_must_not_be_active(self):
        with self.assertRaises(config.WorkflowError):
            settings(tracker={
                "kind": "linear",
                "provider": {"project_slug": "p", "api_key": "k"},
                "active_states": ["Todo", "Human Review"],
                "terminal_states": ["Done"],
            })


class AgentToolBoundaryTest(unittest.TestCase):
    def test_worker_cannot_merge(self):
        self.assertFalse(workflow_policy.allow_request_for(
            "o/r", "PUT", "https://api.github.com/repos/o/r/pulls/1/merge",
            {"sha": "a" * 40, "merge_method": "squash"}))

    def test_worker_can_create_pr_and_comment(self):
        self.assertTrue(workflow_policy.allow_request_for(
            "o/r", "POST", "https://api.github.com/repos/o/r/pulls",
            {"title": "t", "head": "b", "base": "main"}))
        self.assertTrue(workflow_policy.allow_request_for(
            "o/r", "POST",
            "https://api.github.com/repos/o/r/issues/1/comments",
            {"body": "hi"}))

    def test_linear_agent_gets_github_tool_only_when_delivery_enabled(self):
        cfg = settings()
        store = type("Store", (), {"settings": lambda self: cfg})()
        with patch.object(workflow_store, "get_store", return_value=store):
            names = [
                spec["name"] for spec in tracker.tool_specs(cfg.tracker)
            ]
        self.assertEqual(names, ["linear_graphql", "github_api"])


if __name__ == "__main__":
    unittest.main()
