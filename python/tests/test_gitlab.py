"""Offline parity tests for the upstream GitLab tracker with Python/Pi."""
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, tracker  # noqa: E402
from symphony.gitlab import adapter, agent_tool, client  # noqa: E402
from symphony.workflow import WorkflowError  # noqa: E402


def tracker_settings(provider_overrides=None, **overrides):
    provider = {
        "project_path": "group/project",
        "api_key": "test-token",
    }
    provider.update(provider_overrides or {})
    values = {
        "kind": "gitlab",
        "provider": provider,
        "active_states": ["opened"],
        "terminal_states": ["closed"],
        "required_labels": [],
        "secret_environment_names": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def raw_issue(iid, **overrides):
    issue = {
        "iid": iid,
        "id": 1000 + iid,
        "project_id": 77,
        "title": "Issue %s" % iid,
        "description": " Body %s " % iid,
        "state": "opened",
        "web_url": "https://gitlab.test/group/project/-/issues/%s" % iid,
        "assignees": [{"id": 99, "username": "octocat"}],
        "assignee": {"username": "octocat"},
        "labels": [" Bug ", "bug", "Platform"],
        "references": {"full": "group/project#%s" % iid},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    issue.update(overrides)
    return issue


class AdapterValidationTest(unittest.TestCase):
    def test_validates_states_and_provider_settings(self):
        settings = tracker_settings()
        adapter.validate_config(settings)
        adapter.validate_config(tracker_settings(
            active_states=[], terminal_states=[]))

        for value, reason in (
            (None, "missing_gitlab_active_states"),
            (["Todo"], "invalid_gitlab_states"),
            ([42], "invalid_gitlab_states"),
            (["closed"], "invalid_gitlab_states"),
        ):
            with self.assertRaises(WorkflowError) as ctx:
                adapter.validate_config(tracker_settings(active_states=value))
            self.assertEqual(ctx.exception.reason, reason)

        with self.assertRaises(WorkflowError) as ctx:
            adapter.validate_config(
                tracker_settings(terminal_states=["opened"]))
        self.assertEqual(ctx.exception.reason, "invalid_gitlab_states")

        with self.assertRaises(WorkflowError) as ctx:
            adapter.validate_config(
                tracker_settings(terminal_states=None))
        self.assertEqual(
            ctx.exception.reason, "missing_gitlab_terminal_states")


class ClientSettingsTest(unittest.TestCase):
    def test_rejects_invalid_provider_settings(self):
        cases = [
            ({"project_path": 123}, "missing_gitlab_project_path"),
            ({"project_path": "group / project"},
             "invalid_gitlab_project_path"),
            ({"api_key": 123}, "missing_gitlab_api_key"),
            ({"api_url": "not a url"}, "invalid_gitlab_api_url"),
            ({"api_url": "http://gitlab.com/api/v4"},
             "invalid_gitlab_api_url"),
        ]
        for provider, reason in cases:
            with self.assertRaises(WorkflowError) as ctx:
                client.validate_settings(tracker_settings(provider))
            self.assertEqual(ctx.exception.reason, reason)

    def test_secret_environment_names_include_provider_references(self):
        names = client.secret_environment_names(
            tracker_settings({"api_key": "$SYMPHONY_GITLAB_TOKEN"}))
        self.assertEqual(names, [
            "GITLAB_PAT",
            "GITLAB_ACCESS_TOKEN",
            "GITLAB_TOKEN",
            "OAUTH_TOKEN",
            "SYMPHONY_GITLAB_TOKEN",
        ])

    def test_resolves_env_fallbacks_and_trims_settings(self):
        with patch.dict(os.environ, {
                "GITLAB_PROJECT_PATH": " group/project ",
                "GITLAB_PAT": " token "}):
            settings = client.settings(tracker_settings({
                "project_path": None, "api_key": None}))
        self.assertEqual(settings.project_path, "group/project")
        self.assertEqual(settings.api_key, "token")
        self.assertEqual(settings.api_url, "https://gitlab.com/api/v4")

        with patch.dict(os.environ, {"SYMPHONY_GITLAB_TOKEN": "env-token"}):
            settings = client.settings(tracker_settings({
                "api_key": "$SYMPHONY_GITLAB_TOKEN",
                "api_url": "https://gitlab.example/api/v4/"}))
        self.assertEqual(settings.api_key, "env-token")
        self.assertEqual(
            settings.api_url, "https://gitlab.example/api/v4")


class NormalizeIssueTest(unittest.TestCase):
    def test_normalizes_provider_details(self):
        settings = client.settings(tracker_settings())
        issue = client.normalize_issue(raw_issue(42), settings)
        self.assertEqual(issue.id, "42")
        self.assertEqual(issue.identifier, "GL-42")
        self.assertEqual(issue.native_ref, {
            "id": 1042,
            "iid": 42,
            "project_id": 77,
            "project_path": "group/project",
            "references": {"full": "group/project#42"},
        })
        self.assertEqual(issue.title, "Issue 42")
        self.assertEqual(issue.description, "Body 42")
        self.assertEqual(issue.state, "opened")
        self.assertEqual(
            issue.url, "https://gitlab.test/group/project/-/issues/42")
        self.assertEqual(issue.assignee_id, "99")
        self.assertEqual(issue.labels, ["bug", "platform"])
        self.assertEqual(issue.blocked_by, [])
        self.assertTrue(issue.dispatchable)
        self.assertIsNotNone(issue.created_at)
        self.assertIsNotNone(issue.updated_at)

        closed = client.normalize_issue(
            raw_issue(43, state="closed", confidential=True,
                      issue_type="incident"), settings)
        self.assertTrue(closed.dispatchable)

        fallback = client.normalize_issue(
            {key: value for key, value in raw_issue(45).items()
             if key != "assignees"}
            | {"assignee": {"username": "fallback-user"}}, settings)
        self.assertEqual(fallback.assignee_id, "fallback-user")

        self.assertIsNone(client.normalize_issue(
            raw_issue(44, title=" "), settings))
        self.assertIsNone(client.normalize_issue(
            raw_issue(True), settings))


class FetchStatesTest(unittest.TestCase):
    def test_pages_maps_states_and_drops_malformed(self):
        first_page = [raw_issue(iid) for iid in range(1, 99)] + [
            raw_issue(99, state="closed"),
            raw_issue(100, title=""),
        ]
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append((method, path, query, settings))
            return {"status": 200,
                    "body": first_page if query["page"] == 1
                    else [raw_issue(101)]}

        with self.assertLogs("symphony.gitlab", level="WARNING") as logs:
            issues = client.fetch_issues_by_states(
                tracker_settings(), [" OPENED "], request_fun=request_fun)
        self.assertEqual(len(issues), 99)
        self.assertEqual(issues[0].id, "1")
        self.assertEqual(issues[-1].id, "101")
        self.assertNotIn("99", [issue.id for issue in issues])
        self.assertIn("Dropping malformed GitLab issue records count=1",
                      "\n".join(logs.output))
        self.assertEqual(calls[0][1], "/projects/group%2Fproject/issues")
        self.assertEqual(calls[0][2], {
            "state": "opened",
            "per_page": 100,
            "page": 1,
            "order_by": "created_at",
            "sort": "asc",
        })
        self.assertEqual(calls[1][2]["page"], 2)

    def test_state_mapping_and_unsupported_states_skip_requests(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append(query)
            return {"status": 200, "body": []}

        self.assertEqual(client.fetch_issues_by_states(
            tracker_settings(), ["opened", "closed"],
            request_fun=request_fun), [])
        self.assertEqual(calls[0]["state"], "all")

        with patch.object(client, "settings") as settings:
            self.assertEqual(client.fetch_issues_by_states(
                tracker_settings(), ["Todo"]), [])
            self.assertEqual(client.fetch_issues_by_states(
                tracker_settings(), []), [])
        settings.assert_not_called()

    def test_non_list_payload_fails_closed(self):
        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_states(
                tracker_settings(), ["opened"],
                request_fun=lambda *_args: {
                    "status": 200, "body": {"not": "a list"}})
        self.assertEqual(ctx.exception.reason, "gitlab_unknown_payload")


class FetchIdsTest(unittest.TestCase):
    def test_refreshes_iids_in_order_and_skips_404(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append(path)
            if path.endswith("/404"):
                return {"status": 404, "body": {"message": "404"}}
            return {"status": 200, "body": raw_issue(int(path.rsplit("/", 1)[1]))}

        issues = client.fetch_issues_by_ids(
            tracker_settings(), ["2", "1", "404", "2"],
            request_fun=request_fun)
        self.assertEqual([issue.id for issue in issues], ["2", "1"])
        self.assertEqual(calls, [
            "/projects/group%2Fproject/issues/2",
            "/projects/group%2Fproject/issues/1",
            "/projects/group%2Fproject/issues/404",
        ])

    def test_rejects_invalid_ids_and_malformed_refreshes(self):
        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_ids(
                tracker_settings(), ["not-a-number"],
                request_fun=lambda *_args: {"status": 200, "body": {}})
        self.assertEqual(ctx.exception.reason, "invalid_gitlab_issue_id")

        with self.assertRaises(WorkflowError) as ctx:
            client.fetch_issues_by_ids(
                tracker_settings(), ["3"],
                request_fun=lambda *_args: {
                    "status": 200, "body": raw_issue(3, title="")})
        self.assertEqual(ctx.exception.reason, "gitlab_unknown_payload")

        with patch.object(client, "settings") as settings:
            self.assertEqual(
                client.fetch_issues_by_ids(tracker_settings(), []), [])
        settings.assert_not_called()


class AgentToolTest(unittest.TestCase):
    def test_preserves_status_and_body(self):
        calls = []

        def request_fun(method, path, query, body, settings):
            calls.append((method, path, query, body))
            return {"status": 201, "body": {"id": 9}}

        response = agent_tool.execute_agent_tool(
            "gitlab_api",
            {
                "method": "post",
                "path": " /projects/group%2Fproject/issues/42/notes ",
                "query": {"per_page": 10},
                "body": {"body": "hello"},
            },
            tracker_settings=tracker_settings(),
            request_fun=request_fun,
        )
        self.assertTrue(response["success"])
        self.assertEqual(
            json.loads(response["output"]),
            {"status": 201, "body": {"id": 9}})
        self.assertEqual(calls, [(
            "POST", "/projects/group%2Fproject/issues/42/notes",
            {"per_page": 10}, {"body": "hello"},
        )])
        self.assertEqual(
            response["contentItems"],
            [{"type": "inputText", "text": response["output"]}])

    def test_non_success_status_and_unsafe_arguments(self):
        response = agent_tool.execute_agent_tool(
            "gitlab_api",
            {"method": "GET", "path": "/projects/1/issues/404"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": 404, "body": {"message": "404 Not Found"}},
        )
        self.assertFalse(response["success"])

        def request_fun(*_args):
            raise AssertionError("invalid arguments must not call client")

        for arguments in (
                {"method": "GET", "path": "https://gitlab.com/api/v4/user"},
                {"method": "PATCH", "path": "/user"},
                {"method": "GET", "path": "/user", "query": False},
                {"path": "/user"},
                {"method": "GET", "path": 123},
        ):
            invalid = agent_tool.execute_agent_tool(
                "gitlab_api", arguments,
                tracker_settings=tracker_settings(),
                request_fun=request_fun)
            self.assertFalse(invalid["success"])

    def test_unsupported_tools_and_client_failures(self):
        unsupported = agent_tool.execute_agent_tool(
            "not_gitlab_api", {}, tracker_settings=tracker_settings())
        self.assertFalse(unsupported["success"])
        self.assertEqual(
            json.loads(unsupported["output"])["error"]["supportedTools"],
            ["gitlab_api"])

        malformed = agent_tool.execute_agent_tool(
            "gitlab_api", {"method": "GET", "path": "/user"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": "not-an-integer", "body": {}})
        self.assertFalse(malformed["success"])

        missing = agent_tool.execute_agent_tool(
            "gitlab_api", {"method": "GET", "path": "/user"},
            tracker_settings=tracker_settings({"api_key": 123}))
        self.assertFalse(missing["success"])

        for reason in ("gitlab_api_request", "unexpected_failure"):
            def failing(*_args, reason=reason):
                raise WorkflowError(reason, "boom")

            failure = agent_tool.execute_agent_tool(
                "gitlab_api", {"method": "GET", "path": "/user"},
                tracker_settings=tracker_settings(), request_fun=failing)
            self.assertFalse(failure["success"])
            self.assertIsInstance(
                json.loads(failure["output"])["error"]["message"], str)

        non_json = agent_tool.execute_agent_tool(
            "gitlab_api", {"method": "GET", "path": "/user"},
            tracker_settings=tracker_settings(),
            request_fun=lambda *_args: {
                "status": 200, "body": object()})
        self.assertTrue(non_json["success"])
        self.assertIn("object", non_json["output"])


class TrackerFacadeTest(unittest.TestCase):
    def test_binds_gitlab_reads_tools_and_secrets(self):
        settings = tracker_settings()
        tracker.validate_config(SimpleNamespace(tracker=settings))
        self.assertEqual(
            [spec["name"] for spec in tracker.tool_specs(settings)],
            ["gitlab_api"])
        self.assertEqual(
            tracker.secret_environment_names(settings), [])

        with patch.object(
                adapter, "fetch_issues_by_states",
                return_value=["states"]) as states, \
                patch.object(
                    adapter, "fetch_issues_by_ids",
                    return_value=["ids"]) as ids, \
                patch.object(
                    adapter, "execute_agent_tool",
                    return_value={"success": True}) as execute:
            self.assertEqual(
                tracker.fetch_issues_by_states(settings, ["opened"]),
                ["states"])
            self.assertEqual(
                tracker.fetch_issues_by_ids(settings, ["42"]), ["ids"])
            self.assertEqual(
                tracker.execute_agent_tool(
                    "gitlab_api", {"method": "GET", "path": "/user"},
                    settings),
                {"success": True})
        states.assert_called_once_with(settings, ["opened"])
        ids.assert_called_once_with(settings, ["42"])
        execute.assert_called_once()

    def test_config_parses_gitlab_provider_and_secret_names(self):
        with patch.dict(os.environ, {"SYMPHONY_GITLAB_TOKEN": "test-token"}):
            cfg = config.parse_settings({
                "tracker": {
                    "kind": "gitlab",
                    "provider": {
                        "project_path": "group/project",
                        "api_key": "$SYMPHONY_GITLAB_TOKEN",
                    },
                    "active_states": ["opened"],
                    "terminal_states": ["closed"],
                },
            })
            tracker.validate_config(cfg)
            client.validate_settings(cfg.tracker)
        self.assertEqual(cfg.tracker.kind, "gitlab")
        self.assertIn("GITLAB_PAT", cfg.tracker.secret_environment_names)
        self.assertIn(
            "SYMPHONY_GITLAB_TOKEN", cfg.tracker.secret_environment_names)

    def test_routable_uses_required_labels(self):
        settings = tracker_settings(required_labels=["bug"])
        issue = client.normalize_issue(
            raw_issue(7), client.settings(settings))
        self.assertTrue(tracker.routable(issue, settings))
        settings.required_labels = ["missing"]
        self.assertFalse(tracker.routable(issue, settings))


if __name__ == "__main__":
    unittest.main()
