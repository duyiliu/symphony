import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, tracker, workflow  # noqa: E402
from symphony.asana import adapter, agent_tool, client  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


def tracker_settings(provider_overrides=None):
    provider = {
        "endpoint": "https://app.asana.com/api/1.0",
        "api_key": "test-pat",
        "project_gid": "PROJ",
    }
    provider.update(provider_overrides or {})
    return SimpleNamespace(
        kind="asana",
        provider=provider,
        active_states=["To Do"],
        terminal_states=["Done"],
        required_labels=[],
        secret_environment_names=[],
    )


def raw_task(gid="10001", name="Task one", section="To Do",
             project_gid="PROJ", **overrides):
    task = {
        "gid": gid,
        "name": name,
        "notes": "First line\nSecond line",
        "completed": False,
        "resource_subtype": "default_task",
        "assignee": {"gid": "user-1"},
        "tags": [{"name": " Bug "}, {"name": "bug"}, {"name": "Platform"}],
        "memberships": [{
            "project": {"gid": project_gid},
            "section": {"gid": "SEC", "name": section},
        }],
        "permalink_url": "https://app.asana.com/0/PROJ/%s" % gid,
        "created_at": "2026-01-01T00:00:00.000Z",
        "modified_at": "2026-01-02T00:00:00.000Z",
    }
    task.update(overrides)
    return task


class AdapterValidationTest(unittest.TestCase):
    def test_validates_states_and_provider_settings(self):
        settings = tracker_settings()
        adapter.validate_config(settings)
        adapter.validate_config(SimpleNamespace(
            **{**settings.__dict__, "active_states": [],
               "terminal_states": []}))

        for value, reason in (
            (None, "missing_asana_active_states"),
            ([" "], "invalid_asana_states"),
            ([42], "invalid_asana_states"),
        ):
            with self.assertRaises(WorkflowError) as ctx:
                adapter.validate_config(SimpleNamespace(
                    **{**settings.__dict__, "active_states": value}))
            self.assertEqual(ctx.exception.reason, reason)

        with self.assertRaises(WorkflowError) as ctx:
            adapter.validate_config(SimpleNamespace(
                **{**settings.__dict__, "terminal_states": None}))
        self.assertEqual(
            ctx.exception.reason, "missing_asana_terminal_states")


class ClientSettingsTest(unittest.TestCase):
    def test_rejects_invalid_provider_settings(self):
        cases = [
            ({"endpoint": "http://asana.test"}, "invalid_asana_endpoint"),
            ({"api_key": 123}, "missing_asana_api_key"),
            ({"project_gid": 123}, "missing_asana_project_gid"),
        ]
        for overrides, reason in cases:
            with self.assertRaises(WorkflowError) as ctx:
                client.validate_settings(tracker_settings(overrides))
            self.assertEqual(ctx.exception.reason, reason)

    def test_uses_default_endpoint(self):
        resolved = client.settings(tracker_settings({"endpoint": None}))
        self.assertEqual(
            resolved.endpoint, "https://app.asana.com/api/1.0")

    def test_declares_token_environment_names(self):
        self.assertEqual(
            client.secret_environment_names(tracker_settings(
                {"api_key": "$SYMPHONY_ASANA_TOKEN"})),
            ["ASANA_PAT", "SYMPHONY_ASANA_TOKEN"])

    def test_falls_back_to_environment(self):
        with patch.dict(
                os.environ, {"ASANA_PAT": "env-pat"}, clear=False):
            resolved = client.settings(tracker_settings({"api_key": None}))
        self.assertEqual(resolved.api_key, "env-pat")


class NormalizeIssueTest(unittest.TestCase):
    def _normalize(self, task, settings=None):
        settings = settings or tracker_settings()
        return client.normalize_issue(task, client.settings(settings))

    def test_normalizes_task_fields(self):
        issue = self._normalize(raw_task())
        self.assertEqual(issue.id, "10001")
        self.assertEqual(issue.identifier, "ASANA-10001")
        self.assertEqual(issue.native_ref, {
            "task_gid": "10001",
            "project_gid": "PROJ",
            "section_gid": "SEC",
        })
        self.assertEqual(issue.title, "Task one")
        self.assertEqual(issue.description, "First line\nSecond line")
        self.assertEqual(issue.state, "To Do")
        self.assertEqual(
            issue.url, "https://app.asana.com/0/PROJ/10001")
        self.assertEqual(issue.assignee_id, "user-1")
        self.assertEqual(issue.labels, ["bug", "platform"])
        self.assertEqual(issue.blocked_by, [])
        self.assertTrue(issue.dispatchable)
        self.assertIsNotNone(issue.created_at)
        self.assertIsNotNone(issue.updated_at)

    def test_completed_or_section_tasks_are_not_dispatchable(self):
        self.assertFalse(
            self._normalize(raw_task(completed=True)).dispatchable)
        self.assertFalse(self._normalize(
            raw_task(resource_subtype="section")).dispatchable)

    def test_rejects_tasks_outside_project_or_malformed(self):
        self.assertIsNone(self._normalize(
            raw_task(project_gid="OTHER")))
        self.assertIsNone(self._normalize(raw_task(name="")))
        self.assertIsNone(self._normalize(raw_task(gid=123)))

    def test_native_ref_omits_missing_section(self):
        task = raw_task()
        task["memberships"] = [{
            "project": {"gid": "PROJ"},
            "section": {"name": "To Do"},
        }]
        issue = self._normalize(task)
        self.assertEqual(issue.native_ref, {
            "task_gid": "10001", "project_gid": "PROJ"})


class FetchTest(unittest.TestCase):
    def test_pages_tasks_filters_states_and_drops_malformed(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append((method, path, dict(query), settings))
            if query.get("offset") is None:
                malformed = raw_task(gid="10002", name="")
                return {"status": 200, "body": {
                    "data": [
                        raw_task(gid="10001"),
                        malformed,
                        raw_task(gid="10003", section="Done"),
                    ],
                    "next_page": {"offset": "next-page"},
                }}
            return {"status": 200, "body": {
                "data": [raw_task(gid="10004")],
                "next_page": None,
            }}

        issues = client.fetch_issues_by_states(
            tracker_settings(), ["To Do"], request_fun=request_fun)
        self.assertEqual(
            [issue.id for issue in issues], ["10001", "10004"])
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][1], "/projects/PROJ/tasks")
        self.assertEqual(calls[0][2]["limit"], 100)
        self.assertIn("memberships.section.name", calls[0][2]["opt_fields"])
        self.assertEqual(calls[1][2]["offset"], "next-page")

    def test_empty_states_skips_request(self):
        def request_fun(*_args):
            raise AssertionError("empty states must not make a request")

        self.assertEqual(
            client.fetch_issues_by_states(
                tracker_settings(), [], request_fun=request_fun), [])

    def test_missing_next_page_offset_is_an_error(self):
        def request_fun(*_args):
            return {"status": 200, "body": {
                "data": [], "next_page": {}}}

        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_states(
                tracker_settings(), ["To Do"], request_fun=request_fun)
        self.assertEqual(
            ctx.exception.reason, "asana_missing_next_page_offset")

    def test_refreshes_ids_in_order_and_omits_404_and_other_projects(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append(path)
            if path.endswith("/404"):
                return {"status": 404, "body": {"errors": []}}
            if path.endswith("/2"):
                return {"status": 200, "body": {
                    "data": raw_task(gid="2", project_gid="OTHER")}}
            gid = path.rsplit("/", 1)[-1]
            return {"status": 200, "body": {"data": raw_task(gid=gid)}}

        issues = client.fetch_issues_by_ids(
            tracker_settings(), ["1", "2", "404", "3"],
            request_fun=request_fun)
        self.assertEqual([issue.id for issue in issues], ["1", "3"])
        self.assertEqual(calls, [
            "/tasks/1", "/tasks/2", "/tasks/404", "/tasks/3"])

    def test_malformed_refresh_is_an_error(self):
        def request_fun(*_args):
            return {"status": 200, "body": {
                "data": raw_task(name="")}}

        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_ids(
                tracker_settings(), ["1"], request_fun=request_fun)
        self.assertEqual(ctx.exception.reason, "asana_unknown_payload")

    def test_non_2xx_refresh_is_an_api_status_error(self):
        def request_fun(*_args):
            return {"status": 500, "body": {}}

        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_ids(
                tracker_settings(), ["1"], request_fun=request_fun)
        self.assertEqual(ctx.exception.reason, "asana_api_status")


class AgentToolTest(unittest.TestCase):
    def test_preserves_status_and_body(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append((method, path, query, body))
            return {"status": 201, "body": {"data": {"gid": "story-1"}}}

        response = agent_tool.execute_agent_tool(
            "asana_api",
            {
                "method": "post",
                "path": " /tasks/1/stories ",
                "query": {"opt_fields": "text"},
                "body": {"data": {"text": "hello"}},
            },
            tracker_settings=tracker_settings(),
            request_fun=request_fun,
        )
        self.assertTrue(response["success"])
        self.assertEqual(
            json.loads(response["output"]),
            {"status": 201, "body": {"data": {"gid": "story-1"}}})
        self.assertEqual(calls, [(
            "POST", "/tasks/1/stories",
            {"opt_fields": "text"},
            {"data": {"text": "hello"}},
        )])
        self.assertEqual(
            response["contentItems"],
            [{"type": "inputText", "text": response["output"]}])

    def test_non_success_status_is_a_tool_failure(self):
        response = agent_tool.execute_agent_tool(
            "asana_api",
            {"method": "GET", "path": "/tasks/404"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": 404, "body": {"errors": []}},
        )
        self.assertFalse(response["success"])

    def test_rejects_unsafe_arguments_without_calling_client(self):
        def request_fun(*_args):
            raise AssertionError("invalid arguments must not call client")

        cases = [
            {"method": "GET", "path": "https://asana.test/tasks/1"},
            {"method": "GET", "path": "tasks/1"},
            {"method": "GET", "path": "/tasks/1", "query": False},
            {"path": "/tasks/1"},
            {"method": "GET", "path": 123},
        ]
        for arguments in cases:
            response = agent_tool.execute_agent_tool(
                "asana_api", arguments,
                tracker_settings=tracker_settings(),
                request_fun=request_fun)
            self.assertFalse(response["success"], arguments)

    def test_unsupported_tool_reports_supported_names(self):
        response = agent_tool.execute_agent_tool(
            "not_asana_api", {}, tracker_settings=tracker_settings())
        payload = json.loads(response["output"])
        self.assertFalse(response["success"])
        self.assertEqual(
            payload["error"]["supportedTools"], ["asana_api"])

    def test_client_failures_return_messages(self):
        def failing(reason, detail=None):
            def request_fun(*_args):
                raise WorkflowError(reason, detail)
            return request_fun

        for reason, detail in (
            ("missing_asana_api_key", None),
            ("asana_api_request", "timeout"),
            ("unexpected", None),
        ):
            response = agent_tool.execute_agent_tool(
                "asana_api",
                {"method": "GET", "path": "/users/me"},
                tracker_settings=tracker_settings(),
                request_fun=failing(reason, detail))
            payload = json.loads(response["output"])
            self.assertFalse(response["success"])
            self.assertIsInstance(payload["error"]["message"], str)

    def test_malformed_response_status_is_a_failure(self):
        response = agent_tool.execute_agent_tool(
            "asana_api",
            {"method": "GET", "path": "/users/me"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": "bad", "body": {}},
        )
        self.assertFalse(response["success"])

    def test_non_json_body_is_rendered(self):
        marker = object()
        response = agent_tool.execute_agent_tool(
            "asana_api",
            {"method": "GET", "path": "/users/me"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": 200, "body": marker},
        )
        self.assertTrue(response["success"])
        self.assertIn("<object", response["output"])


class TrackerFacadeTest(unittest.TestCase):
    def test_binds_asana_specs_and_executes_tools(self):
        settings = tracker_settings()
        self.assertEqual(
            [spec["name"] for spec in tracker.tool_specs(
                tracker_settings=settings)],
            ["asana_api"])
        with patch(
                "symphony.asana.client.request",
                return_value={"status": 200, "body": {"data": {}}}):
            response = tracker.execute_agent_tool(
                "asana_api",
                {"method": "GET", "path": "/users/me"},
                tracker_settings=settings)
        self.assertTrue(response["success"])

    def test_secret_names_come_from_tracker_settings(self):
        settings = tracker_settings(
            {"api_key": "$SYMPHONY_ASANA_TOKEN"})
        settings.secret_environment_names = [
            "ASANA_PAT", "SYMPHONY_ASANA_TOKEN"]
        self.assertEqual(
            tracker.secret_environment_names(settings),
            ["ASANA_PAT", "SYMPHONY_ASANA_TOKEN"])


ASANA_WORKFLOW = """\
---
tracker:
  kind: asana
  provider:
    endpoint: "https://app.asana.com/api/1.0"
    api_key: "$SYMPHONY_ASANA_TOKEN"
    project_gid: "PROJ"
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
    def test_asana_is_supported_and_declares_token_env(self):
        self.assertIn("asana", config.SUPPORTED_TRACKER_KINDS)
        settings = config.parse_settings(
            workflow.parse(ASANA_WORKFLOW).config)
        self.assertEqual(settings.tracker.kind, "asana")
        self.assertEqual(
            settings.tracker.secret_environment_names,
            ["ASANA_PAT", "SYMPHONY_ASANA_TOKEN"])
        self.assertEqual(settings.runtime.kind, "pi")
