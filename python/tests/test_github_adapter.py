"""GitHub adapter parity: state validation, pagination and error propagation."""

import unittest
from types import SimpleNamespace
from unittest import mock

from symphony.github import adapter
from symphony.github import client as gh_client
from symphony.workflow import WorkflowError


def _tracker(**overrides):
    values = {
        "kind": "github",
        "active_states": ["open"],
        "terminal_states": ["closed"],
        "required_labels": [],
        "provider": {"repo": "octo/repo", "token": "token"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _raw(number, state="open", **overrides):
    payload = {
        "number": number,
        "title": "Issue %d" % number,
        "body": "Body %d" % number,
        "state": state,
        "html_url": "https://github.test/octo/repo/issues/%d" % number,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class ValidateConfigTest(unittest.TestCase):
    def test_accepts_empty_state_lists(self):
        adapter.validate_config(
            _tracker(active_states=[], terminal_states=[]))

    def test_missing_states_are_reported(self):
        with self.assertRaises(WorkflowError) as raised:
            adapter.validate_config(_tracker(active_states=None))
        self.assertEqual(raised.exception.reason,
                         "missing_github_active_states")

        with self.assertRaises(WorkflowError) as raised:
            adapter.validate_config(_tracker(terminal_states=None))
        self.assertEqual(raised.exception.reason,
                         "missing_github_terminal_states")

    def test_only_open_and_closed_are_allowed(self):
        with self.assertRaises(WorkflowError) as raised:
            adapter.validate_config(_tracker(active_states=["in progress"]))
        self.assertEqual(raised.exception.reason, "invalid_github_states")

    def test_client_settings_errors_are_translated(self):
        with self.assertRaises(WorkflowError) as raised:
            adapter.validate_config(_tracker(provider={"repo": "octo/repo"}))
        self.assertEqual(raised.exception.reason, "missing_github_token")


class LabelStatesTest(unittest.TestCase):
    def _tracker_with_labels(self, **overrides):
        values = {
            "active_states": ["todo", "in-progress"],
            "terminal_states": ["done", "closed"],
            "provider": {
                "repo": "octo/repo",
                "token": "token",
                "use_label_states": True,
            },
        }
        values.update(overrides)
        return _tracker(**values)

    def test_validate_accepts_label_states(self):
        adapter.validate_config(self._tracker_with_labels())

    def test_validate_rejects_non_label_states(self):
        with self.assertRaises(WorkflowError) as raised:
            adapter.validate_config(
                self._tracker_with_labels(
                    active_states=["open"]))
        self.assertEqual(raised.exception.reason, "invalid_github_states")

    def test_validate_rejects_invalid_label_states(self):
        with self.assertRaises(WorkflowError) as raised:
            adapter.validate_config(
                self._tracker_with_labels(
                    active_states=["agent:todo"]))
        self.assertEqual(raised.exception.reason, "invalid_github_states")

    def _raw_with_labels(self, number, labels=None, state="open"):
        payload = _raw(number, state=state)
        if labels is not None:
            payload["labels"] = [{"name": n} for n in labels]
        return payload

    def test_fetch_candidates_uses_label_states(self):
        rows = [
            self._raw_with_labels(1, ["agent:todo"]),
            self._raw_with_labels(2, ["agent:in-progress"]),
            self._raw_with_labels(3, ["agent:done"]),
            self._raw_with_labels(4, []),
        ]
        calls = []

        def fake_request(method, path, params, body, settings):
            calls.append((method, path, dict(params)))
            return {"status": 200, "body": rows}

        with mock.patch.object(
                gh_client, "perform_request", side_effect=fake_request):
            issues = adapter.fetch_candidate_issues(
                self._tracker_with_labels())

        self.assertEqual([i.id for i in issues], ["1", "2"])
        self.assertEqual(
            [i.state for i in issues], ["todo", "in-progress"])
        self.assertEqual(calls[0][2]["state"], "open")

    def test_fetch_terminal_states_includes_done_and_closed(self):
        rows = [
            self._raw_with_labels(1, [], state="closed"),
            self._raw_with_labels(2, ["agent:todo"]),
            self._raw_with_labels(3, ["bug"], state="closed"),
        ]

        def fake_request(method, path, params, body, settings):
            return {"status": 200, "body": rows}

        with mock.patch.object(
                gh_client, "perform_request", side_effect=fake_request):
            issues = adapter.fetch_issues_by_states(
                self._tracker_with_labels(), ["done", "closed"])

        self.assertEqual([i.id for i in issues], ["1", "3"])
        self.assertEqual([i.state for i in issues], ["done", "done"])

    def test_fetch_by_ids_uses_label_states(self):
        def fake_request(method, path, params, body, settings):
            if path.endswith("/1"):
                return {
                    "status": 200,
                    "body": self._raw_with_labels(1, ["agent:todo"]),
                }
            if path.endswith("/2"):
                return {
                    "status": 200,
                    "body": self._raw_with_labels(2, ["bug"]),
                }
            raise AssertionError(path)

        with mock.patch.object(
                gh_client, "perform_request", side_effect=fake_request):
            issues = adapter.fetch_issues_by_ids(
                self._tracker_with_labels(), ["1", "2"])

        self.assertEqual([i.id for i in issues], ["1"])
        self.assertEqual(issues[0].state, "todo")

    def test_multiple_symphony_labels_highest_priority_wins(self):
        row = self._raw_with_labels(1, ["agent:todo", "agent:merging"])
        issue = gh_client.normalize_issue(
            row, "octo/repo", use_label_states=True)
        self.assertEqual(issue.state, "merging")

    def test_unknown_agent_label_is_ignored(self):
        row = self._raw_with_labels(1, ["agent:todo", "agent:unknown"])
        issue = gh_client.normalize_issue(
            row, "octo/repo", use_label_states=True)
        self.assertEqual(issue.state, "todo")

    def test_only_unknown_agent_label_is_not_dispatchable(self):
        row = self._raw_with_labels(1, ["agent:unknown"])
        issue = gh_client.normalize_issue(
            row, "octo/repo", use_label_states=True)
        self.assertIsNone(issue)

    def test_no_symphony_labels_is_not_dispatchable(self):
        row = self._raw_with_labels(1, ["bug", "enhancement"])
        issue = gh_client.normalize_issue(
            row, "octo/repo", use_label_states=True)
        self.assertIsNone(issue)


class FetchTest(unittest.TestCase):
    def test_candidate_issues_paginate_and_filter_requested_states(self):
        first_page = [_raw(number) for number in range(1, 101)]
        second_page = [_raw(101), _raw(102, state="closed")]
        calls = []

        def fake_request(method, path, params, body, settings):
            calls.append((method, path, dict(params)))
            return {
                "status": 200,
                "body": first_page if params["page"] == 1 else second_page,
            }

        with mock.patch.object(
                gh_client, "perform_request", side_effect=fake_request):
            issues = adapter.fetch_candidate_issues(_tracker())

        self.assertEqual([issue.id for issue in issues[:2]], ["1", "2"])
        self.assertEqual([issue.id for issue in issues[-2:]], ["100", "101"])
        self.assertEqual(len(issues), 101)
        self.assertEqual([call[2]["page"] for call in calls], [1, 2])
        self.assertEqual(calls[0][2]["state"], "open")
        self.assertEqual(calls[0][2]["per_page"], 100)

    def test_fetch_by_ids_keeps_order_and_skips_404(self):
        def fake_request(method, path, params, body, settings):
            if path.endswith("/2"):
                return {
                    "status": 200,
                    "body": _raw(2, state="closed"),
                }
            if path.endswith("/1"):
                return {
                    "status": 200,
                    "body": _raw(1),
                }
            return {"status": 404, "body": {"message": "Not Found"}}

        with mock.patch.object(
                gh_client, "perform_request", side_effect=fake_request):
            issues = adapter.fetch_issues_by_ids(
                _tracker(), ["2", "1", "3"])

        self.assertEqual([issue.id for issue in issues], ["2", "1"])

    def test_api_failure_is_not_treated_as_empty_poll(self):
        with mock.patch.object(
                gh_client, "perform_request",
                return_value={"status": 500, "body": {"message": "boom"}}):
            with self.assertRaises(WorkflowError) as raised:
                adapter.fetch_candidate_issues(_tracker())

        self.assertEqual(raised.exception.reason, "github_api_status")


if __name__ == "__main__":
    unittest.main()
