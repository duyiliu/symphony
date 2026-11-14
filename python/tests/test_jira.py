import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, tracker, workflow  # noqa: E402
from symphony.jira import adapter, agent_tool, client  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


def tracker_settings(provider_overrides=None, terminal_states=None):
    provider = {
        "base_url": "https://jira.example.test",
        "email": "agent@example.test",
        "api_token": "test-token",
        "project_key": "SYM",
    }
    provider.update(provider_overrides or {})
    return SimpleNamespace(
        kind="jira",
        provider=provider,
        active_states=["To Do"],
        terminal_states=(
            ["Done"] if terminal_states is None else terminal_states),
        required_labels=[],
        secret_environment_names=[],
    )


def raw_issue(issue_id, key, state="To Do", project_key="SYM"):
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "summary": "Issue %s" % key,
            "description": {
                "type": "doc",
                "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "First line"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "Second line"},
                    ],
                }],
            },
            "status": {"name": state},
            "labels": [" Bug ", "bug", "Platform"],
            "assignee": {"accountId": "account-1"},
            "created": "2026-01-01T00:00:00.000+0000",
            "updated": "2026-01-02T00:00:00.000+0000",
            "project": {"key": project_key},
        },
    }


def linked_issue(issue_id, key, state):
    return {
        "id": issue_id,
        "key": key,
        "fields": {"status": {"name": state}},
    }


class AdapterValidationTest(unittest.TestCase):
    def test_validates_states_and_provider_settings(self):
        settings = tracker_settings()
        adapter.validate_config(settings)
        adapter.validate_config(SimpleNamespace(
            **{**settings.__dict__, "active_states": [],
               "terminal_states": []}))

        for value, reason in (
            (None, "missing_jira_active_states"),
            ([" "], "invalid_jira_states"),
            ([42], "invalid_jira_states"),
        ):
            with self.assertRaises(WorkflowError) as ctx:
                adapter.validate_config(SimpleNamespace(
                    **{**settings.__dict__, "active_states": value}))
            self.assertEqual(ctx.exception.reason, reason)

        with self.assertRaises(WorkflowError) as ctx:
            adapter.validate_config(SimpleNamespace(
                **{**settings.__dict__, "terminal_states": None}))
        self.assertEqual(
            ctx.exception.reason, "missing_jira_terminal_states")


class ClientSettingsTest(unittest.TestCase):
    def test_rejects_invalid_provider_settings(self):
        cases = [
            ({"base_url": "http://jira.test"}, "invalid_jira_base_url"),
            ({"email": 123}, "missing_jira_email"),
            ({"api_token": 123}, "missing_jira_api_token"),
            ({"project_key": 123}, "missing_jira_project_key"),
        ]
        for overrides, reason in cases:
            with self.assertRaises(WorkflowError) as ctx:
                client.validate_settings(tracker_settings(overrides))
            self.assertEqual(ctx.exception.reason, reason)

    def test_declares_token_environment_names(self):
        self.assertEqual(
            client.secret_environment_names(tracker_settings(
                {"api_token": "$SYMPHONY_JIRA_TOKEN"})),
            ["JIRA_API_TOKEN", "SYMPHONY_JIRA_TOKEN"])

    def test_falls_back_to_environment(self):
        env = {
            "JIRA_BASE_URL": "https://env-jira.example.test",
            "JIRA_EMAIL": "env@example.test",
            "JIRA_API_TOKEN": "env-token",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = tracker_settings({
                "base_url": None,
                "email": None,
                "api_token": None,
                "project_key": "SYM",
            })
            resolved = client.settings(settings)
        self.assertEqual(
            resolved.base_url, "https://env-jira.example.test")
        self.assertEqual(resolved.email, "env@example.test")
        self.assertEqual(resolved.api_token, "env-token")


class NormalizeIssueTest(unittest.TestCase):
    def _normalize(self, raw, settings=None):
        settings = settings or tracker_settings()
        return client.normalize_issue(raw, client.settings(settings))

    def test_normalizes_fields_and_adf_description(self):
        issue = self._normalize(raw_issue("10001", "SYM-1"))
        self.assertEqual(issue.id, "10001")
        self.assertEqual(issue.identifier, "SYM-1")
        self.assertIsNone(issue.native_ref)
        self.assertEqual(issue.title, "Issue SYM-1")
        self.assertEqual(issue.description, "First line\nSecond line")
        self.assertIsNone(issue.priority)
        self.assertEqual(issue.state, "To Do")
        self.assertIsNone(issue.branch_name)
        self.assertEqual(
            issue.url, "https://jira.example.test/browse/SYM-1")
        self.assertEqual(issue.assignee_id, "account-1")
        self.assertEqual(issue.labels, ["bug", "platform"])
        self.assertEqual(issue.blocked_by, [])
        self.assertTrue(issue.dispatchable)
        self.assertIsNotNone(issue.created_at)
        self.assertIsNotNone(issue.updated_at)

    def test_rejects_malformed_and_other_projects(self):
        missing_summary = raw_issue("10002", "SYM-2")
        missing_summary["fields"]["summary"] = ""
        self.assertIsNone(self._normalize(missing_summary))
        self.assertIsNone(self._normalize(
            raw_issue("10003", "OTHER-3", project_key="OTHER")))

    def test_matches_project_key_case_insensitively(self):
        issue = self._normalize(
            raw_issue("10001", "SYM-1"),
            tracker_settings({"project_key": "sym"}))
        self.assertIsNotNone(issue)

    def test_projects_rich_adf_attributes(self):
        rich = {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "mention", "attrs": {"text": "@Alex"}},
                    {"type": "emoji", "attrs": {"shortName": ":wave:"}},
                    {"type": "status", "attrs": {"text": "Ready"}},
                    {"type": "inlineCard",
                     "attrs": {"url": "https://example.test/card"}},
                ],
            }],
        }
        raw = raw_issue("10003", "SYM-3")
        raw["fields"]["description"] = rich
        self.assertEqual(
            self._normalize(raw).description,
            "@Alex:wave:Readyhttps://example.test/card")

    def test_projects_nested_panel_text(self):
        panel = {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "panel",
                "attrs": {"panelType": "info"},
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Panel text"}],
                }],
            }],
        }
        raw = raw_issue("10004", "SYM-4")
        raw["fields"]["description"] = panel
        self.assertEqual(self._normalize(raw).description, "Panel text")

    def test_encodes_issue_key_in_url(self):
        issue = self._normalize(raw_issue("10005", "SYM 5"))
        self.assertEqual(
            issue.url, "https://jira.example.test/browse/SYM%205")


class BlockerTest(unittest.TestCase):
    def _normalize(self, raw, terminal_states=None):
        settings = tracker_settings(terminal_states=terminal_states)
        return client.normalize_issue(raw, client.settings(settings))

    def test_retains_inward_blocks_and_gates_ready_issues(self):
        active = linked_issue("20001", "SYM-10", "In Progress")
        terminal = linked_issue("20002", "OTHER-20", "Ready for Release")
        raw = raw_issue("10001", "SYM-1")
        raw["fields"]["issuelinks"] = [
            {"type": {"name": " Blocks "}, "inwardIssue": active},
            {"type": {"name": "blocks"}, "inwardIssue": terminal},
            {"type": {"name": "Relates"},
             "inwardIssue": linked_issue("20003", "SYM-30", "In Progress")},
            {"type": {"name": "Blocks"},
             "outwardIssue": linked_issue("20004", "SYM-40", "In Progress")},
        ]
        issue = self._normalize(raw)
        self.assertEqual(issue.blocked_by, [
            {"id": "20001", "identifier": "SYM-10",
             "state": "In Progress"},
            {"id": "20002", "identifier": "OTHER-20",
             "state": "Ready for Release"},
        ])
        self.assertFalse(issue.dispatchable)

    def test_status_category_new_gates_dispatch(self):
        raw = raw_issue("10001", "SYM-1")
        raw["fields"]["status"] = {
            "name": "Open", "statusCategory": {"key": "new"}}
        raw["fields"]["issuelinks"] = [{
            "type": {"name": "Blocks"},
            "inwardIssue": linked_issue(
                "20001", "SYM-10", "In Progress"),
        }]
        self.assertFalse(self._normalize(raw).dispatchable)

    def test_terminal_blocker_allows_dispatch(self):
        raw = raw_issue("10001", "SYM-1")
        raw["fields"]["issuelinks"] = [{
            "type": {"name": "Blocks"},
            "inwardIssue": linked_issue(
                "20002", "OTHER-20", "Ready for Release"),
        }]
        self.assertTrue(self._normalize(
            raw, terminal_states=["Ready for Release"]).dispatchable)

    def test_unknown_blocker_keeps_nulls_and_blocks(self):
        raw = raw_issue("10001", "SYM-1")
        raw["fields"]["status"]["name"] = "Todo"
        raw["fields"]["issuelinks"] = [{
            "type": {"name": "Blocks"},
            "inwardIssue": {"id": "20005"},
        }]
        issue = self._normalize(raw)
        self.assertEqual(
            issue.blocked_by,
            [{"id": "20005", "identifier": None, "state": None}])
        self.assertFalse(issue.dispatchable)

    def test_indeterminate_status_is_dispatchable(self):
        raw = raw_issue("10001", "SYM-1")
        raw["fields"]["status"] = {
            "name": "In Progress",
            "statusCategory": {"key": "indeterminate"},
        }
        self.assertTrue(self._normalize(raw).dispatchable)


class FetchTest(unittest.TestCase):
    def test_pages_search_filters_states_and_drops_malformed(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append((method, path, dict(body), settings))
            if body.get("nextPageToken") is None:
                malformed = raw_issue("10002", "SYM-2")
                malformed["fields"]["summary"] = ""
                return {"status": 200, "body": {
                    "issues": [
                        raw_issue("10001", "SYM-1"),
                        malformed,
                        raw_issue("10003", "SYM-3", "Done"),
                    ],
                    "isLast": False,
                    "nextPageToken": "next-page",
                }}
            return {"status": 200, "body": {
                "issues": [raw_issue("10004", "SYM-4")],
                "isLast": True,
            }}

        issues = client.fetch_issues_by_states(
            tracker_settings(), ["To Do"], request_fun=request_fun)
        self.assertEqual([issue.id for issue in issues],
                         ["10001", "10004"])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/rest/api/3/search/jql")
        self.assertEqual(
            calls[0][2]["jql"],
            'project = "SYM" AND status IN ("To Do")')
        self.assertIn("issuelinks", calls[0][2]["fields"])
        self.assertEqual(calls[0][2]["maxResults"], 100)
        self.assertEqual(calls[1][2]["nextPageToken"], "next-page")

    def test_empty_states_skips_request(self):
        def request_fun(*_args):
            raise AssertionError("empty states must not make a request")

        self.assertEqual(
            client.fetch_issues_by_states(
                tracker_settings(), [], request_fun=request_fun), [])

    def test_missing_next_page_token_is_an_error(self):
        def request_fun(*_args):
            return {"status": 200, "body": {
                "issues": [], "isLast": False}}

        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_states(
                tracker_settings(), ["To Do"], request_fun=request_fun)
        self.assertEqual(
            ctx.exception.reason, "jira_missing_next_page_token")

    def test_refreshes_ids_in_batches_and_preserves_order(self):
        ids = [str(number) for number in range(1, 102)]
        batches = []

        def request_fun(method, path, query, body, settings):
            batches.append(list(body["issueIdsOrKeys"]))
            self.assertIn("issuelinks", body["fields"])
            return {"status": 200, "body": {
                "issues": [raw_issue(issue_id, "SYM-%s" % issue_id)
                           for issue_id in body["issueIdsOrKeys"]],
            }}

        issues = client.fetch_issues_by_ids(
            tracker_settings(), ids, request_fun=request_fun)
        self.assertEqual([issue.id for issue in issues], ids)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 100)
        self.assertEqual(batches[1], ["101"])

    def test_filters_other_projects_and_omits_missing(self):
        def request_fun(*_args):
            return {"status": 200, "body": {"issues": [
                raw_issue("1", "SYM-1"),
                raw_issue("2", "OTHER-2", project_key="OTHER"),
            ]}}

        issues = client.fetch_issues_by_ids(
            tracker_settings(), ["1", "2", "404"],
            request_fun=request_fun)
        self.assertEqual([issue.id for issue in issues], ["1"])

    def test_malformed_requested_issue_is_an_error(self):
        malformed = raw_issue("1", "SYM-1")
        malformed["fields"]["summary"] = ""

        def request_fun(*_args):
            return {"status": 200, "body": {"issues": [malformed]}}

        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_ids(
                tracker_settings(), ["1"], request_fun=request_fun)
        self.assertEqual(ctx.exception.reason, "jira_unknown_payload")

    def test_refreshes_blockers_before_dispatch(self):
        raw = raw_issue("10001", "SYM-1")
        raw["fields"]["issuelinks"] = [{
            "type": {"name": "Blocks"},
            "inwardIssue": linked_issue(
                "20001", "SYM-10", "In Progress"),
        }]

        def request_fun(*_args):
            return {"status": 200, "body": {"issues": [raw]}}

        issues = client.fetch_issues_by_ids(
            tracker_settings(), ["10001"], request_fun=request_fun)
        self.assertEqual(issues[0].blocked_by, [{
            "id": "20001", "identifier": "SYM-10",
            "state": "In Progress",
        }])
        self.assertFalse(issues[0].dispatchable)


class AgentToolTest(unittest.TestCase):
    def test_preserves_status_and_body(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append((method, path, query, body))
            return {"status": 201, "body": {"id": "comment-1"}}

        response = agent_tool.execute_agent_tool(
            "jira_rest",
            {
                "method": "post",
                "path": " /rest/api/3/issue/10001/comment ",
                "query": {"expand": "renderedBody"},
                "body": {"body": {"type": "doc"}},
            },
            tracker_settings=tracker_settings(),
            request_fun=request_fun,
        )
        self.assertTrue(response["success"])
        self.assertEqual(
            json.loads(response["output"]),
            {"status": 201, "body": {"id": "comment-1"}})
        self.assertEqual(calls, [(
            "POST", "/rest/api/3/issue/10001/comment",
            {"expand": "renderedBody"}, {"body": {"type": "doc"}},
        )])
        self.assertEqual(
            response["contentItems"],
            [{"type": "inputText", "text": response["output"]}])

    def test_non_success_status_is_a_tool_failure(self):
        response = agent_tool.execute_agent_tool(
            "jira_rest",
            {"method": "GET", "path": "/rest/api/3/issue/404"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": 404,
                "body": {"errorMessages": ["Not Found"]},
            },
        )
        self.assertFalse(response["success"])
        self.assertEqual(
            json.loads(response["output"]),
            {"status": 404,
             "body": {"errorMessages": ["Not Found"]}})

    def test_rejects_unsafe_arguments_without_calling_client(self):
        def request_fun(*_args):
            raise AssertionError("invalid arguments must not call client")

        cases = [
            {"method": "GET",
             "path": "https://jira.test/rest/api/3/myself"},
            {"method": "GET", "path": "/rest/api/2/myself"},
            {"method": "GET", "path": "/rest/api/3/myself",
             "query": False},
            {"path": "/rest/api/3/myself"},
            {"method": "GET", "path": 123},
        ]
        for arguments in cases:
            response = agent_tool.execute_agent_tool(
                "jira_rest", arguments,
                tracker_settings=tracker_settings(),
                request_fun=request_fun)
            self.assertFalse(response["success"], arguments)

    def test_unsupported_tool_reports_supported_names(self):
        response = agent_tool.execute_agent_tool(
            "not_jira_rest", {}, tracker_settings=tracker_settings())
        payload = json.loads(response["output"])
        self.assertFalse(response["success"])
        self.assertEqual(
            payload["error"]["supportedTools"], ["jira_rest"])

    def test_client_failures_return_messages(self):
        def failing(reason, detail=None):
            def request_fun(*_args):
                raise WorkflowError(reason, detail)
            return request_fun

        for reason, detail in (
            ("missing_jira_api_token", None),
            ("jira_api_request", "timeout"),
            ("unexpected", None),
        ):
            response = agent_tool.execute_agent_tool(
                "jira_rest",
                {"method": "GET", "path": "/rest/api/3/myself"},
                tracker_settings=tracker_settings(),
                request_fun=failing(reason, detail))
            payload = json.loads(response["output"])
            self.assertFalse(response["success"])
            self.assertIsInstance(
                payload["error"]["message"], str)

    def test_malformed_response_status_is_a_failure(self):
        response = agent_tool.execute_agent_tool(
            "jira_rest",
            {"method": "GET", "path": "/rest/api/3/myself"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": "bad", "body": {}},
        )
        self.assertFalse(response["success"])

    def test_non_json_body_is_rendered(self):
        marker = object()
        response = agent_tool.execute_agent_tool(
            "jira_rest",
            {"method": "GET", "path": "/rest/api/3/myself"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": 200, "body": marker},
        )
        self.assertTrue(response["success"])
        self.assertIn("<object", response["output"])


class TrackerFacadeTest(unittest.TestCase):
    def test_binds_jira_specs_and_executes_tools(self):
        settings = tracker_settings()
        self.assertEqual(
            [spec["name"] for spec in tracker.tool_specs(
                tracker_settings=settings)],
            ["jira_rest"])
        with patch(
                "symphony.jira.client.request",
                return_value={"status": 200, "body": {"ok": True}}):
            response = tracker.execute_agent_tool(
                "jira_rest",
                {"method": "GET", "path": "/rest/api/3/myself"},
                tracker_settings=settings)
        self.assertTrue(response["success"])
        self.assertEqual(
            json.loads(response["output"]),
            {"status": 200, "body": {"ok": True}})

    def test_secret_names_come_from_tracker_settings(self):
        settings = tracker_settings(
            {"api_token": "$SYMPHONY_JIRA_TOKEN"})
        settings.secret_environment_names = [
            "JIRA_API_TOKEN", "SYMPHONY_JIRA_TOKEN"]
        self.assertEqual(
            tracker.secret_environment_names(settings),
            ["JIRA_API_TOKEN", "SYMPHONY_JIRA_TOKEN"])


JIRA_WORKFLOW = """\
---
tracker:
  kind: jira
  provider:
    base_url: "https://jira.example.test"
    email: "agent@example.test"
    api_token: "$SYMPHONY_JIRA_TOKEN"
    project_key: "SYM"
  active_states:
    - To Do
  terminal_states:
    - Done
runtime:
  kind: pi
  command: pi --mode rpc
---

Body
"""


class ConfigTest(unittest.TestCase):
    def test_jira_is_supported_and_declares_token_env(self):
        self.assertIn("jira", config.SUPPORTED_TRACKER_KINDS)
        settings = config.parse_settings(workflow.parse(JIRA_WORKFLOW).config)
        self.assertEqual(settings.tracker.kind, "jira")
        self.assertEqual(
            settings.tracker.secret_environment_names,
            ["JIRA_API_TOKEN", "SYMPHONY_JIRA_TOKEN"])
        self.assertEqual(settings.runtime.kind, "pi")
