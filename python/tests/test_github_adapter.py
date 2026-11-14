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
