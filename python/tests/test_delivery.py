"""Deterministic tests for the host-owned Linear delivery gate."""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, delivery, linear, tracker, workflow_store  # noqa: E402
from symphony.github import agent_tool, workflow_policy  # noqa: E402
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
        "head": {
            "sha": "a" * 40,
            "ref": "abc-1-fix",
            "repo": {"full_name": "o/r"},
        },
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


def comment_page(nodes=None, has_next=False, cursor=None):
    return {
        "data": {
            "issue": {
                "comments": {
                    "nodes": nodes or [],
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": cursor,
                    },
                },
            },
        },
    }


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

    def test_evaluate_rejects_pr_from_different_issue_branch(self):
        cfg = settings()
        value = issue()
        pr = pr_payload()
        pr["head"]["ref"] = "gh-2-unrelated-work"
        result = delivery.evaluate(
            cfg, value, pr, ([], [], ["test"], True), [], [],
            protection(), {},
        )
        self.assertEqual(result[0], "pause")
        self.assertIn("not bound to ABC-1", result[1])

    def test_evaluate_accepts_case_insensitive_issue_branch(self):
        from types import SimpleNamespace

        cfg = settings()
        value = SimpleNamespace(identifier="ABC-1", labels=[])
        pr = pr_payload()
        pr["head"]["ref"] = "feature/abc-1-add-helper"
        result = delivery.evaluate(
            cfg, value, pr, ([], [], ["test"], True),
            [{"filename": "src/x.js"}], [],
            protection(), {},
        )
        self.assertEqual(result[0], "merge")

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

    def test_rate_limit_response_keeps_safe_retry_metadata(self):
        cfg = settings()
        with patch.object(
                delivery.gh_client, "perform_request",
                return_value={
                    "status": 403,
                    "body": {"message": "API rate limit exceeded"},
                    "headers": {
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "200",
                    },
                }):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.GitHub(cfg.delivery).request("GET", "/repos/o/r/pulls/7")
        self.assertEqual(caught.exception.reason, "github_rate_limited")
        self.assertEqual(caught.exception.detail["status"], 403)
        self.assertIsNotNone(caught.exception.detail["retry_after"])

    def test_exhausted_quota_is_not_masked_by_allow_missing(self):
        cfg = settings()
        with patch.object(
                delivery.gh_client, "perform_request",
                return_value={
                    "status": 403,
                    "body": {"message": "Forbidden"},
                    "headers": {
                        "X-RateLimit-Remaining": "0",
                        "Retry-After": "30",
                    },
                }):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.GitHub(cfg.delivery).request(
                    "GET", "/repos/o/r/branches/main/protection",
                    allow_missing=True)
        self.assertEqual(caught.exception.reason, "github_rate_limited")
        self.assertEqual(caught.exception.detail["retry_after"], 30.0)

    def test_non_object_error_response_keeps_structured_status(self):
        cfg = settings()
        with patch.object(
                delivery.gh_client, "perform_request",
                return_value={
                    "status": 502,
                    "body": ["upstream proxy error"],
                    "headers": {},
                }):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.GitHub(cfg.delivery).request(
                    "GET", "/repos/o/r/pulls/7")
        self.assertEqual(caught.exception.reason, "github_status")
        self.assertEqual(caught.exception.detail, {
            "status": 502,
            "method": "GET",
            "path": "/repos/o/r/pulls/7",
            "retry_after": None,
        })


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
        number = self.pr.get("number", 7)
        if method == "GET" and path.endswith("/pulls/%s" % number):
            return self.pr
        if method == "GET" and "/protection" in path:
            return self.protection
        if method == "PUT" and path.endswith("/pulls/%s/merge" % number):
            self.pr = {**self.pr, "merged": True, "state": "closed"}
            return self.merge_result
        raise AssertionError("unexpected request %s %s" % (method, path))

    def pages(self, path, field=None):
        if "/check-runs" in path:
            return [{
                "id": 3, "name": "test",
                "head_sha": (self.pr.get("head") or {}).get("sha"),
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
        reporter = Mock()
        self.controller.session_reporter = reporter
        with patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertEqual(self.transitions, ["Merging", "Done"])
        self.assertTrue(any("已由宿主安全门禁合并" in body
                            for body in self.comments))
        self.assertTrue(any(call[0] == "PUT" for call in fake.calls))
        self.assertEqual(reporter.delivery.call_args.args[1], "merged")

    def test_record_save_failure_cleans_unique_temp_file(self):
        store = delivery.DeliveryRecordStore(self.temp.name)
        issue_value = issue()
        path = store._path(issue_value)
        with patch.object(
                delivery.os, "replace",
                side_effect=OSError("atomic replace failed")):
            with self.assertRaises(OSError):
                store.save(issue_value, {"phase": "review"})
        self.assertFalse(os.path.exists(path))
        self.assertFalse(any(
            name.startswith(os.path.basename(path) + ".tmp.")
            for name in os.listdir(self.temp.name)))

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
        reporter = Mock()
        self.controller.session_reporter = reporter
        with patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertNotIn("Done", self.transitions)
        self.assertTrue(any("自动交付已暂停" in body
                            for body in self.comments))
        self.assertFalse(any(call[0] == "PUT" for call in fake.calls))
        self.assertEqual(reporter.delivery.call_args.args[1], "paused")

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

    def test_new_pr_resets_previous_delivery_fields(self):
        new_sha = "b" * 40
        issue_value = issue(attachments=[{
            "url": "https://github.com/o/r/pull/8",
            "metadata": {"status": "open"},
        }])
        records = delivery.DeliveryRecordStore(self.temp.name)
        records.save(issue_value, {
            "phase": "rework",
            "pr": 7,
            "head_sha": "a" * 40,
            "gated_sha": "c" * 40,
            "rework_attempts": 2,
            "last_rework": {
                "head_sha": "a" * 40,
                "reason": "checks failed: test",
            },
            "last_notice_key": "old-notice",
            "lifecycle_marker": "old-run",
        })
        fake = _FakeGitHub(
            self.cfg.delivery,
            pr=pr_payload(
                number=8,
                head={
                    "sha": new_sha,
                    "ref": "abc-1-fix",
                    "repo": {"full_name": "o/r"},
                },
            ),
        )
        original_pages = fake.pages

        def failed_checks(path, field=None):
            if "/check-runs" in path:
                return [{
                    "id": 8, "name": "test", "head_sha": new_sha,
                    "status": "completed", "conclusion": "failure",
                    "app": {"id": 1},
                }]
            return original_pages(path, field)

        fake.pages = failed_checks
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue_value]), \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()

        record = records.load(issue_value)
        self.assertEqual(record.get("phase"), "rework")
        self.assertEqual(record.get("pr"), 8)
        self.assertEqual(record.get("rework_attempts"), 1)
        self.assertEqual(
            record.get("last_rework"),
            {"head_sha": new_sha, "reason": "checks failed: test"},
        )
        self.assertNotIn("gated_sha", record)
        self.assertNotEqual(record.get("last_notice_key"), "old-notice")
        self.assertTrue(any("返工次数: 1/2" in body
                            for body in self.comments))

    def test_failed_comment_is_retried_before_notice_is_marked_sent(self):
        attempts = []

        def flaky_comment(_settings, _issue_id, body):
            attempts.append(body)
            if len(attempts) == 1:
                raise RuntimeError("simulated Linear comment failure")

        empty_comments = {
            "data": {
                "issue": {
                    "comments": {
                        "nodes": [],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }
        fake = _FakeGitHub(
            self.cfg.delivery,
            files=[{"filename": "auth/login.js"}],
        )
        with patch.object(linear, "create_comment",
                          side_effect=flaky_comment), \
                patch.object(linear, "graphql",
                             return_value=empty_comments), \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()

        records = delivery.DeliveryRecordStore(self.temp.name)
        record = records.load(issue())
        self.assertIsNone(record.get("last_notice_key"))
        self.assertIsNotNone(record.get("pending_notice_key"))
        self.assertEqual(len(attempts), 1)

        with patch.object(linear, "create_comment",
                          side_effect=flaky_comment), \
                patch.object(linear, "graphql",
                          return_value=empty_comments), \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()

        record = records.load(issue())
        self.assertIsNone(record.get("pending_notice_key"))
        self.assertIsNotNone(record.get("last_notice_key"))
        self.assertEqual(len(attempts), 2)

        with patch.object(linear, "create_comment",
                          side_effect=flaky_comment), \
                patch.object(linear, "graphql",
                          return_value=empty_comments), \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()
        self.assertEqual(len(attempts), 2)

    def test_pending_comment_confirmed_existing_is_not_duplicated(self):
        notice_key = "confirmed-notice"
        issue_value = issue()
        records = delivery.DeliveryRecordStore(self.temp.name)
        records.save(issue_value, {
            "phase": "paused",
            "pr": 7,
            "head_sha": "a" * 40,
            "pending_notice_key": notice_key,
            "pending_notice_body": "原通知正文",
            "pending_notice_creation_attempted": True,
        })
        existing_comment = {
            "data": {
                "issue": {
                    "comments": {
                        "nodes": [{
                            "id": "comment-1",
                            "body": (
                                "[symphony-delivery:%s]\n原通知正文"
                                % notice_key),
                        }],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }
        with patch.object(linear, "graphql",
                          return_value=existing_comment), \
                patch.object(linear, "create_comment") as create_comment:
            self.controller.poll_once()

        record = records.load(issue_value)
        self.assertEqual(record.get("last_notice_key"), notice_key)
        self.assertIsNone(record.get("pending_notice_key"))
        create_comment.assert_not_called()

    def test_confirmation_outage_does_not_resend_attempted_comment(self):
        notice_key = "uncertain-notice"
        issue_value = issue()
        records = delivery.DeliveryRecordStore(self.temp.name)
        records.save(issue_value, {
            "phase": "paused",
            "pr": 7,
            "head_sha": "a" * 40,
            "pending_notice_key": notice_key,
            "pending_notice_body": "可能已经发送的通知",
            "pending_notice_creation_attempted": True,
        })
        fake = _FakeGitHub(
            self.cfg.delivery,
            files=[{"filename": "auth/login.js"}],
        )
        with patch.object(
                linear, "graphql",
                side_effect=RuntimeError("Linear temporarily unavailable")), \
                patch.object(linear, "create_comment") as create_comment, \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()

        record = records.load(issue_value)
        self.assertEqual(record.get("pending_notice_key"), notice_key)
        self.assertEqual(
            record.get("pending_notice_body"), "可能已经发送的通知")
        self.assertTrue(record.get("pending_notice_creation_attempted"))
        self.assertNotIn("last_notice_key", record)
        create_comment.assert_not_called()

    def test_rate_limit_applies_global_backoff_without_hot_retry(self):
        now = [100.0]
        controller = delivery.DeliveryController(
            _Store(self.cfg), clock=lambda: now[0])
        first = issue()
        second = Issue(
            id="issue-2", identifier="ABC-2", title="Second",
            state="Human Review", team_id="team-1",
            attachments=[{
                "url": "https://github.com/o/r/pull/8",
                "metadata": {"status": "open"},
            }])
        calls = []

        class RateLimitedGitHub:
            def __init__(self, _settings):
                pass

            def request(self, method, path, **_kwargs):
                calls.append((method, path))
                raise delivery.DeliveryError(
                    "github_rate_limited",
                    {"status": 403, "retry_after": 120})

        fetch = Mock(return_value=[first, second])
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states", fetch), \
                patch.object(delivery, "GitHub", RateLimitedGitHub):
            controller.poll_once()
            snapshot = controller.snapshot()
            self.assertEqual(len(calls), 1)
            self.assertEqual(snapshot["deferred_count"], 1)
            self.assertEqual(snapshot["global_backoff_remaining_ms"], 120000)
            controller.poll_once()
            self.assertEqual(fetch.call_count, 1)

    def test_rate_limit_backoff_resumes_all_issues_after_window(self):
        now = [100.0]
        controller = delivery.DeliveryController(
            _Store(self.cfg), clock=lambda: now[0])
        first = issue()
        second = Issue(
            id="issue-2", identifier="ABC-2", title="Second",
            state="Human Review", team_id="team-1",
            attachments=[{
                "url": "https://github.com/o/r/pull/8",
                "metadata": {"status": "open"},
            }])
        checked = []

        def check_locked(_settings, current, _delivery):
            checked.append(current.id)
            if len(checked) == 1:
                raise delivery.DeliveryError(
                    "github_rate_limited",
                    {"status": 403, "retry_after": 120})

        fetch = Mock(return_value=[first, second])
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states", fetch), \
                patch.object(
                    controller, "_check_locked", side_effect=check_locked):
            controller.poll_once()
            self.assertEqual(checked, [first.id])
            self.assertEqual(fetch.call_count, 1)

            now[0] = 219.0
            controller.poll_once()
            self.assertEqual(checked, [first.id])
            self.assertEqual(fetch.call_count, 1)

            now[0] = 220.0
            controller.poll_once()

        self.assertEqual(checked, [first.id, first.id, second.id])
        self.assertEqual(fetch.call_count, 2)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["deferred_count"], 0)
        self.assertEqual(snapshot["global_backoff_remaining_ms"], 0)
        self.assertIsNone(snapshot["last_error"])

    def test_comment_accepted_before_lost_save_is_not_duplicated(self):
        accepted = []

        def accept_comment(_settings, _issue_id, body):
            accepted.append(body)

        fake = _FakeGitHub(
            self.cfg.delivery,
            files=[{"filename": "auth/login.js"}],
        )
        original_save = delivery.DeliveryRecordStore.save

        def lose_final_notice_save(store_self, save_issue, record):
            if record.get("last_notice_key") \
                    and not record.get("pending_notice_key"):
                raise OSError("simulated crash after Linear accepted comment")
            return original_save(store_self, save_issue, record)

        with patch.object(
                linear, "create_comment", side_effect=accept_comment), \
                patch.object(
                    delivery.DeliveryRecordStore, "save",
                    lose_final_notice_save), \
                patch.object(delivery, "GitHub", return_value=fake):
            self.controller.poll_once()

        records = delivery.DeliveryRecordStore(self.temp.name)
        record = records.load(issue())
        notice_key = record.get("pending_notice_key")
        self.assertEqual(len(accepted), 1)
        self.assertTrue(notice_key)
        self.assertIsNone(record.get("last_notice_key"))

        existing = comment_page([{
            "id": "comment-1",
            "body": "[symphony-delivery:%s]\n%s"
                    % (notice_key, record.get("pending_notice_body")),
        }])
        graphql = Mock(side_effect=[
            comment_page(has_next=True, cursor="page-1"),
            existing,
        ])
        restarted = delivery.DeliveryController(_Store(self.cfg))
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue()]), \
                patch.object(linear, "graphql", graphql), \
                patch.object(linear, "create_comment") as create_comment, \
                patch.object(delivery, "GitHub", return_value=fake):
            restarted.poll_once()

        record = records.load(issue())
        self.assertEqual(graphql.call_count, 2)
        self.assertEqual(record.get("last_notice_key"), notice_key)
        self.assertIsNone(record.get("pending_notice_key"))
        create_comment.assert_not_called()
        self.assertEqual(len(accepted), 1)

    def test_pending_notice_for_old_pr_is_discarded_after_restart(self):
        notice_key = "old-pr-notice"
        current = issue(attachments=[{
            "url": "https://github.com/o/r/pull/8",
            "metadata": {"status": "open"},
        }])
        records = delivery.DeliveryRecordStore(self.temp.name)
        records.save(current, {
            "phase": "paused",
            "pr": 7,
            "head_sha": "a" * 40,
            "pending_notice_key": notice_key,
            "pending_notice_body": "旧 PR 的通知",
            "pending_notice_creation_attempted": True,
        })
        restarted = delivery.DeliveryController(_Store(self.cfg))
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[current]), \
                patch.object(linear, "create_comment") as create_comment, \
                patch.object(linear, "graphql") as graphql:
            restarted.poll_once()

        record = records.load(current)
        self.assertIsNone(record.get("pending_notice_key"))
        self.assertIsNone(record.get("pending_notice_body"))
        self.assertIsNone(record.get("pending_notice_creation_attempted"))
        create_comment.assert_not_called()
        graphql.assert_not_called()

    def test_nested_old_lifecycle_resets_delivery_fields(self):
        old_record = {
            "phase": "paused",
            "pr": 7,
            "head_sha": "a" * 40,
            "gated_sha": "c" * 40,
            "rework_attempts": 2,
            "last_rework": {
                "head_sha": "a" * 40,
                "reason": "previous lifecycle",
            },
            "last_notice_key": "old-notice",
            "agent_session_report": {"lifecycle": "old-run"},
        }
        records = delivery.DeliveryRecordStore(self.temp.name)
        records.save(issue(), old_record)

        class Reporter:
            records = {issue().id: {"marker": "new-run"}}

            def delivery(self, *_args):
                pass

            def delivery_event(self, *_args):
                return {"lifecycle": "new-run"}

        controller = delivery.DeliveryController(
            _Store(self.cfg), session_reporter=Reporter())
        fake = _FakeGitHub(
            self.cfg.delivery,
            files=[{"filename": "auth/login.js"}],
        )
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue()]), \
                patch.object(delivery, "GitHub", return_value=fake):
            controller.poll_once()

        record = records.load(issue())
        self.assertEqual(record.get("phase"), "paused")
        self.assertEqual(record.get("lifecycle_marker"), "new-run")
        self.assertNotIn("gated_sha", record)
        self.assertNotIn("rework_attempts", record)
        self.assertNotIn("last_rework", record)
        self.assertNotEqual(record.get("last_notice_key"), "old-notice")
        self.assertTrue(any("自动交付已暂停" in body
                            for body in self.comments))

    def test_reporter_exception_does_not_abort_delivery_result(self):
        class BrokenReporter:
            def delivery(self, *_args):
                raise RuntimeError("reporter delivery unavailable")

            def delivery_event(self, *_args):
                raise RuntimeError("reporter envelope unavailable")

        controller = delivery.DeliveryController(
            _Store(self.cfg), session_reporter=BrokenReporter())
        fake = _FakeGitHub(self.cfg.delivery)
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue()]), \
                patch.object(delivery, "GitHub", return_value=fake):
            controller.poll_once()

        record = delivery.DeliveryRecordStore(self.temp.name).load(issue())
        self.assertEqual(record.get("phase"), "merged")
        self.assertEqual(self.transitions, ["Merging", "Done"])
        self.assertTrue(any("已由宿主安全门禁合并" in body
                            for body in self.comments))
        snapshot = controller.snapshot()
        self.assertIsNone(snapshot["last_error"])
        self.assertEqual(snapshot["deferred_count"], 0)

    def test_pending_notice_waits_when_lifecycle_lookup_fails(self):
        notice_key = "lifecycle-unknown-notice"
        issue_value = issue()
        records = delivery.DeliveryRecordStore(self.temp.name)
        records.save(issue_value, {
            "phase": "paused",
            "pr": 7,
            "head_sha": "a" * 40,
            "lifecycle_marker": "run-1",
            "pending_notice_key": notice_key,
            "pending_notice_body": "等待可靠 lifecycle 后再发送",
            "pending_notice_creation_attempted": True,
        })

        class BrokenReporter:
            records = {}

            def delivery_event(self, *_args):
                raise RuntimeError("lifecycle lookup unavailable")

        restarted = delivery.DeliveryController(
            _Store(self.cfg), session_reporter=BrokenReporter())
        with patch.object(
                delivery.tracker_adapter, "fetch_issues_by_states",
                return_value=[issue_value]), \
                patch.object(linear, "create_comment") as create_comment:
            restarted.poll_once()

        record = records.load(issue_value)
        self.assertEqual(record.get("pending_notice_key"), notice_key)
        self.assertEqual(
            record.get("pending_notice_body"),
            "等待可靠 lifecycle 后再发送",
        )
        create_comment.assert_not_called()
        self.assertEqual(restarted.snapshot()["deferred_count"], 1)


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
    @staticmethod
    def _scope(identifier="ABC-1"):
        return linear.IssueScope(id="issue-1", identifier=identifier)

    @staticmethod
    def _pull(ref="feature/abc-1-helper"):
        return {
            "state": "open",
            "base": {
                "ref": "main",
                "repo": {"full_name": "o/r"},
            },
            "head": {
                "ref": ref,
                "repo": {"full_name": "o/r"},
            },
        }

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

    def test_delivery_pr_creation_requires_current_issue_branch(self):
        cfg = settings()
        base = {
            "method": "POST",
            "path": "/repos/o/r/pulls",
            "body": {"title": "t", "head": "feature/abc-1-work",
                     "base": "main"},
        }
        with patch.object(agent_tool.gh_client, "perform_request") as call:
            call.return_value = {"status": 201, "body": {"number": 7}}
            allowed = agent_tool.execute_delivery_tool(
                base, cfg.delivery, issue_scope=self._scope())
        self.assertEqual(allowed["status"], 201)

        unrelated = {
            **base,
            "body": {**base["body"], "head": "feature/other-work"},
        }
        with patch.object(agent_tool.gh_client, "perform_request") as call:
            denied = agent_tool.execute_delivery_tool(
                unrelated, cfg.delivery, issue_scope=self._scope())
        self.assertEqual(denied["error"]["code"], "forbidden")
        call.assert_not_called()

    def test_delivery_comment_requires_current_open_pr(self):
        cfg = settings()
        arguments = {
            "method": "POST",
            "path": "/repos/o/r/pulls/7/comments",
            "body": {"body": "review reply"},
        }
        with patch.object(
                agent_tool.gh_client, "perform_request",
                side_effect=[
                {"status": 200, "body": self._pull()},
                {"status": 201, "body": {"id": 9}},
                ]) as call:
            allowed = agent_tool.execute_delivery_tool(
                arguments, cfg.delivery, issue_scope=self._scope())
        self.assertEqual(allowed["status"], 201)
        self.assertEqual([item.args[0] for item in call.call_args_list],
                         ["GET", "POST"])

        with patch.object(
                agent_tool.gh_client, "perform_request",
                return_value={
                    "status": 200,
                    "body": self._pull("feature/other-work"),
                }) as call:
            denied = agent_tool.execute_delivery_tool(
                arguments, cfg.delivery, issue_scope=self._scope())
        self.assertEqual(denied["error"]["code"], "forbidden")
        self.assertEqual(call.call_count, 1)

    def test_delivery_issue_comment_must_target_scoped_pr(self):
        cfg = settings()
        arguments = {
            "method": "POST",
            "path": "/repos/o/r/issues/7/comments",
            "body": {"body": "comment"},
        }
        responses = [
            {"status": 200, "body": {"pull_request": {"url": "x"}}},
            {"status": 200, "body": self._pull()},
            {"status": 201, "body": {"id": 10}},
        ]
        with patch.object(
                agent_tool.gh_client, "perform_request",
                side_effect=responses):
            allowed = agent_tool.execute_delivery_tool(
                arguments, cfg.delivery, issue_scope=self._scope())
        self.assertEqual(allowed["status"], 201)

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
