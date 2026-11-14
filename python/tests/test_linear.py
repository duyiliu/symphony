"""Offline parity tests for the upstream Linear tracker with Python/Pi."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, linear, tracker, mcp_bridge
from symphony.workflow import WorkflowError


def settings():
    return config.parse_settings({
        "tracker": {
            "kind": "linear",
            "provider": {"project_slug": "example-project", "api_key": "test-key"},
            "active_states": ["Todo", "In Progress", "Merging", "Rework"],
            "terminal_states": ["Done", "Cancelled", "Canceled", "Closed", "Duplicate"],
        },
        "runtime": {"kind": "pi", "mcp": {"enabled": True}},
    })


def row(identifier="ABC-1", state="Todo", assignee=None, blocker=None):
    relations = []
    if blocker is not None:
        relations.append({
            "type": "blocks",
            "issue": {"id": "b1", "identifier": "ABC-0", "state": {"name": blocker}},
        })
    return {
        "id": identifier + "-uuid", "identifier": identifier,
        "title": "Example", "description": "Description",
        "priority": 2, "state": {"name": state}, "branchName": None,
        "url": "https://linear.app/example/issue/" + identifier,
        "assignee": {"id": assignee} if assignee else None,
        "labels": {"nodes": [{"name": "Agent"}]},
        "inverseRelations": {"nodes": relations},
        "createdAt": "2026-11-12T00:00:00Z",
        "updatedAt": "2026-11-12T00:01:00Z",
    }


def issue_scope(identifier="ABC-1"):
    return linear.IssueScope(id=identifier + "-uuid", identifier=identifier)


def live_issue(scope=None):
    scope = scope or issue_scope()
    return {
        "id": scope.id,
        "identifier": scope.identifier,
        "project": {"slugId": "example-project"},
        "team": {"states": {"nodes": [
            {"id": "state-backlog", "name": "Backlog"},
            {"id": "state-todo", "name": "Todo"},
            {"id": "state-progress", "name": "In Progress"},
            {"id": "state-review", "name": "Human Review"},
            {"id": "state-rework", "name": "Rework"},
            {"id": "state-merging", "name": "Merging"},
            {"id": "state-done", "name": "Done"},
            {"id": "state-cancelled", "name": "Cancelled"},
        ]}},
    }


class LinearTrackerTests(unittest.TestCase):
    def test_linear_config_from_provider(self):
        cfg = settings()
        self.assertEqual(cfg.tracker.project_slug, "example-project")
        self.assertEqual(cfg.tracker.endpoint, "https://api.linear.app/graphql")
        self.assertIn("LINEAR_API_KEY", cfg.tracker.secret_environment_names)
        self.assertIn("GITHUB_TOKEN", cfg.tracker.secret_environment_names)
        self.assertIn("SYMPHONY_GITHUB_TOKEN", cfg.tracker.secret_environment_names)
        linear.validate_config(cfg.tracker)

    def test_linear_defaults_and_env_fallbacks(self):
        with patch.dict(os.environ, {"LINEAR_ASSIGNEE": "dev@example.com"}):
            cfg = config.parse_settings({
                "tracker": {"kind": "linear", "provider": {
                    "project_slug": "example-project", "api_key": "test-key"}},
            })
        self.assertEqual(cfg.tracker.active_states, ["Todo", "In Progress"])
        self.assertEqual(cfg.tracker.terminal_states, [
            "Closed", "Cancelled", "Canceled", "Duplicate", "Done"])
        self.assertEqual(cfg.tracker.assignee, "dev@example.com")
        self.assertEqual(cfg.tracker.provider["endpoint"],
                         "https://api.linear.app/graphql")
        self.assertEqual(cfg.tracker.provider["project_slug"],
                         "example-project")
        # The provider keeps the raw config value; the alias resolves env.
        self.assertIsNone(cfg.tracker.provider["assignee"])

    def test_linear_validate_prefers_endpoint_then_token_then_slug(self):
        cfg = settings().tracker
        cfg.endpoint = "http://insecure.example/graphql"
        cfg.api_key = None
        cfg.provider.pop("api_key", None)
        with patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            with self.assertRaises(WorkflowError) as ctx:
                linear.validate_config(cfg)
        self.assertEqual(ctx.exception.reason, "invalid_linear_endpoint")

        cfg.endpoint = "https://api.linear.app/graphql"
        cfg.provider["api_key"] = 123
        with patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            with self.assertRaises(WorkflowError) as ctx:
                linear.validate_config(cfg)
        self.assertEqual(ctx.exception.reason, "missing_linear_api_token")

        cfg.provider["api_key"] = "test-key"
        cfg.project_slug = None
        cfg.provider.pop("project_slug", None)
        with self.assertRaises(WorkflowError) as ctx:
            linear.validate_config(cfg)
        self.assertEqual(ctx.exception.reason, "missing_linear_project_slug")

    def test_empty_states_skip_request(self):
        cfg = settings().tracker
        with patch.object(linear, "graphql") as api:
            self.assertEqual(linear.fetch_issues_by_states(cfg, []), [])
            self.assertEqual(linear.fetch_issues_by_states(cfg, [None]), [])
        api.assert_not_called()

    def test_state_names_are_stringified_and_deduplicated(self):
        cfg = settings().tracker
        page = {"data": {"issues": {
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        with patch.object(linear, "graphql", return_value=page) as api:
            linear.fetch_issues_by_states(cfg, ["Todo", "Todo", 7])
        self.assertEqual(api.call_args.args[2]["stateNames"], ["Todo", "7"])

    def test_malformed_poll_rows_are_dropped_with_warning(self):
        cfg = settings().tracker
        page = {"data": {"issues": {
            "nodes": [row("ABC-1"), {"id": "broken", "title": " "}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        with patch.object(linear, "graphql", return_value=page), \
                self.assertLogs("symphony.linear", level="WARNING") as logs:
            got = linear.fetch_candidate_issues(cfg)
        self.assertEqual([issue.identifier for issue in got], ["ABC-1"])
        self.assertIn("Dropping malformed Linear issue records count=1",
                      "\n".join(logs.output))

    def test_poll_pagination_and_mapping(self):
        cfg = settings().tracker
        pages = [
            {"data": {"issues": {"nodes": [row("ABC-1")],
              "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}},
            {"data": {"issues": {"nodes": [row("ABC-2", state="In Progress")],
              "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
        ]
        with patch.object(linear, "graphql", side_effect=pages) as call:
            got = tracker.fetch_candidate_issues(cfg)
        self.assertEqual([i.identifier for i in got], ["ABC-1", "ABC-2"])
        self.assertEqual(got[0].labels, ["agent"])
        self.assertTrue(got[0].dispatchable)
        self.assertEqual(got[0].priority, 2)
        self.assertEqual(call.call_count, 2)

    def test_todo_blocker_and_assignee(self):
        cfg = settings().tracker
        blocked = linear._normalize(row(blocker="In Progress"), cfg)
        self.assertFalse(blocked.dispatchable)
        completed = linear._normalize(row(blocker="Done"), cfg)
        self.assertTrue(completed.dispatchable)
        ongoing = linear._normalize(row(state="In Progress", blocker="Todo"), cfg)
        self.assertTrue(ongoing.dispatchable)
        self.assertFalse(linear._normalize(row(assignee="other"), cfg, "me-id").dispatchable)

    def test_id_refresh_is_project_scoped_and_ordered(self):
        cfg = settings().tracker
        def response(_settings, query, variables):
            self.assertIn("projectSlug", variables)
            return {"data": {"issues": {"nodes": [row("ABC-2"), row("ABC-1")]}}}
        with patch.object(linear, "graphql", side_effect=response):
            got = linear.fetch_issues_by_ids(cfg, ["ABC-1-uuid", "ABC-2-uuid"])
        self.assertEqual([i.identifier for i in got], ["ABC-1", "ABC-2"])

    def test_invalid_pagination_fails_closed(self):
        cfg = settings().tracker
        with patch.object(linear, "graphql", return_value={
            "data": {"issues": {"nodes": [], "pageInfo": {
                "hasNextPage": True, "endCursor": None}}}}):
            with self.assertRaises(WorkflowError):
                linear.fetch_candidate_issues(cfg)

    def test_pi_exposes_only_linear_tool(self):
        cfg = settings().tracker
        self.assertEqual([s["name"] for s in tracker.tool_specs(cfg)], ["linear_graphql"])
        scope = issue_scope()

        def response(_settings, _query, _variables):
            if "SymphonyLinearWorkerIssueScope" in _query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"viewer": {"id": "me"}}}

        with patch.object(linear, "graphql", side_effect=response):
            response = tracker.execute_agent_tool(
                "linear_graphql", {"query": "query {viewer {id}}"}, cfg,
                issue_scope=scope)
        self.assertEqual(response["data"]["viewer"]["id"], "me")
        self.assertTrue(mcp_bridge._to_mcp_result({"error": {"code": "failed"}})["isError"])

    def test_worker_can_read_only_bound_issue(self):
        cfg = settings().tracker
        scope = issue_scope()
        calls = []

        def response(_settings, query, variables):
            calls.append((query, variables))
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"issue": {
                "id": scope.id, "identifier": scope.identifier,
                "comments": {"nodes": [{"id": "comment-1", "body": "## Codex Workpad"}]},
            }}}

        query = (
            "query Workpad($id: String!) { issue(id: $id) { id identifier "
            "comments(first: 20) { nodes { id body } } } }"
        )
        with patch.object(linear, "graphql", side_effect=response):
            result = tracker.execute_agent_tool(
                "linear_graphql", {"query": query, "variables": {"id": scope.id}},
                cfg, issue_scope=scope)
        self.assertEqual(result["data"]["issue"]["identifier"], scope.identifier)
        self.assertEqual(len(calls), 2)

        with patch.object(linear, "graphql") as api:
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query, "variables": {"id": "OTHER-1-uuid"}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
        api.assert_not_called()

    def test_worker_rejects_cross_project_selection(self):
        cfg = settings().tracker
        scope = issue_scope()
        query = (
            "query Hidden($id: String!) { issue(id: $id) { project { "
            "issues(first: 10) { nodes { identifier } } } } }"
        )
        with patch.object(linear, "graphql") as api:
            result = tracker.execute_agent_tool(
                "linear_graphql", {"query": query, "variables": {"id": scope.id}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
        api.assert_not_called()

    def test_worker_rejects_nested_cross_issue_paths(self):
        cfg = settings().tracker
        scope = issue_scope()
        queries = [
            (
                "query($id: String!) { issue(id: $id) { team { "
                "issues(first: 10) { nodes { id identifier } } } } }"
            ),
            (
                "query($id: String!) { issue(id: $id) { team { "
                "members(first: 10) { nodes { id name } } } } }"
            ),
            (
                "query($id: String!) { issue(id: $id) { comments(first: 10) { "
                "nodes { id body issue { id identifier } } } } }"
            ),
            (
                "query($id: String!) { issue(id: $id) { attachments(first: 10) "
                "{ nodes { id url issue { id identifier } } } } }"
            ),
        ]
        for query in queries:
            with self.subTest(query=query), patch.object(linear, "graphql") as api:
                result = tracker.execute_agent_tool(
                    "linear_graphql",
                    {"query": query, "variables": {"id": scope.id}},
                    cfg, issue_scope=scope)
                self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
                api.assert_not_called()

    def test_mutation_response_cannot_traverse_into_other_issue_paths(self):
        cfg = settings().tracker
        scope = issue_scope()
        query = (
            "mutation($id: String!, $body: String!) { "
            "commentUpdate(id: $id, input: {body: $body}) { success "
            "comment { id issue { id identifier } } } }"
        )
        with patch.object(linear, "graphql") as api:
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query, "variables": {"id": "comment-1", "body": "x"}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
        api.assert_not_called()

    def test_worker_can_read_project_and_team_state_metadata(self):
        cfg = settings().tracker
        scope = issue_scope()
        query = (
            "query State($id: String!) { issue(id: $id) { id "
            "project { id name slugId url } "
            "team { id key name states(first: 50) { "
            "nodes { id name type } } } } }"
        )

        def response(_settings, current, variables):
            if "SymphonyLinearWorkerIssueScope" in current:
                return {"data": {"issue": live_issue(scope)}}
            self.assertEqual(variables, {"id": scope.id})
            return {"data": {"issue": {
                "id": scope.id,
                "project": {
                    "id": "project-1", "name": "Project",
                    "slugId": "example-project", "url": "https://linear.app/project",
                },
                "team": {
                    "id": "team-1", "key": "ABC", "name": "Team",
                    "states": {"nodes": [
                        {"id": "state-progress", "name": "In Progress",
                         "type": "started"},
                    ]},
                },
            }}}

        with patch.object(linear, "graphql", side_effect=response) as api:
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query, "variables": {"id": scope.id}},
                cfg, issue_scope=scope)
        self.assertEqual(
            result["data"]["issue"]["team"]["states"]["nodes"][0]["id"],
            "state-progress")
        self.assertEqual(api.call_count, 2)

    def test_worker_rejects_issue_that_left_bound_project(self):
        cfg = settings().tracker
        scope = issue_scope()
        moved = live_issue(scope)
        moved["project"]["slugId"] = "another-project"
        with patch.object(linear, "graphql", return_value={
                "data": {"issue": moved}}):
            result = tracker.execute_agent_tool(
                "linear_graphql", {"query": "query {viewer {id}}"},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")

    def test_worker_can_create_and_update_current_issue_workpad(self):
        cfg = settings().tracker
        scope = issue_scope()
        queries = []

        def response(_settings, query, variables):
            queries.append(query)
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            if "SymphonyLinearWorkerCommentScope" in query:
                return {"data": {"comment": {
                    "id": "comment-1",
                    "issue": {
                        "id": scope.id, "identifier": scope.identifier,
                        "project": {"slugId": "example-project"},
                    },
                }}}
            return {"data": {"ok": True}}

        create = (
            "mutation Workpad($issueId: String!, $body: String!) { "
            "commentCreate(input: {issueId: $issueId, body: $body}) "
            "{ success comment { id } } }"
        )
        update = (
            "mutation Workpad($id: String!, $body: String!) { "
            "commentUpdate(id: $id, input: {body: $body}) { success } }"
        )
        with patch.object(linear, "graphql", side_effect=response):
            created = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": create,
                 "variables": {"issueId": scope.id, "body": "## Codex Workpad"}},
                cfg, issue_scope=scope)
            updated = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": update,
                 "variables": {"id": "comment-1", "body": "## Codex Workpad\n- done"}},
                cfg, issue_scope=scope)
        self.assertEqual(created, {"data": {"ok": True}})
        self.assertEqual(updated, {"data": {"ok": True}})
        self.assertTrue(any("commentCreate" in query for query in queries))
        self.assertTrue(any("commentUpdate" in query for query in queries))

    def test_comment_mutation_rejects_other_issue(self):
        cfg = settings().tracker
        scope = issue_scope()

        def response(_settings, query, _variables):
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"comment": {
                "id": "comment-other",
                "issue": {
                    "id": "ABC-2-uuid", "identifier": "ABC-2",
                    "project": {"slugId": "example-project"},
                },
            }}}

        query = (
            "mutation Workpad($id: String!, $body: String!) { "
            "commentUpdate(id: $id, input: {body: $body}) { success } }"
        )
        with patch.object(linear, "graphql", side_effect=response):
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query, "variables": {"id": "comment-other", "body": "x"}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")

    def test_comment_update_rejects_comment_from_other_issue(self):
        cfg = settings().tracker
        scope = issue_scope()

        def response(_settings, query, _variables):
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"comment": {
                "id": "comment-other",
                "issue": {
                    "id": "ABC-2-uuid", "identifier": "ABC-2",
                    "project": {"slugId": "example-project"},
                },
            }}}

        query = (
            "mutation Workpad($id: String!, $body: String!) { "
            "commentUpdate(id: $id, input: {body: $body}) { success } }"
        )
        with patch.object(linear, "graphql", side_effect=response) as api:
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query,
                 "variables": {"id": "comment-other", "body": "x"}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
        self.assertTrue(
            all("commentUpdate" not in q for q in
                [c.args[1] for c in api.call_args_list
                 if "SymphonyLinearWorker" not in c.args[1]]),
            "mutation must not be forwarded after scope check fails")

    def test_comment_delete_rejects_comment_not_on_bound_issue(self):
        cfg = settings().tracker
        scope = issue_scope()

        def response(_settings, query, _variables):
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"comment": {
                "id": "comment-gone",
                "issue": {
                    "id": "ABC-2-uuid", "identifier": "ABC-2",
                    "project": {"slugId": "example-project"},
                },
            }}}

        query = (
            "mutation Workpad($id: String!) { "
            "commentDelete(id: $id) { success } }"
        )
        with patch.object(linear, "graphql", side_effect=response):
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query, "variables": {"id": "comment-gone"}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")

    def test_issue_update_rejects_variable_input_with_extra_fields(self):
        cfg = settings().tracker
        scope = issue_scope()

        query = (
            "mutation Move($id: String!, $input: IssueUpdateInput!) { "
            "issueUpdate(id: $id, input: $input) { success } }"
        )
        with patch.object(linear, "graphql") as api:
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query,
                 "variables": {"id": scope.id,
                               "input": {"stateId": "state-todo",
                                         "title": "hijack"}}},
                cfg, issue_scope=scope)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
        forwarded = [
            call.args[1] for call in api.call_args_list
            if "SymphonyLinearWorker" not in call.args[1]]
        self.assertEqual(forwarded, [])

    def test_worker_can_move_to_review_but_not_delivery_terminal_states(self):
        cfg = settings().tracker
        scope = issue_scope()

        def response(_settings, query, _variables):
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"issueUpdate": {"success": True}}}

        query = (
            "mutation Move($id: String!, $stateId: String!) { "
            "issueUpdate(id: $id, input: {stateId: $stateId}) { success } }"
        )
        with patch.object(linear, "graphql", side_effect=response):
            allowed = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query,
                 "variables": {"id": scope.id, "stateId": "state-review"}},
                cfg, issue_scope=scope)
            merging = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query,
                 "variables": {"id": scope.id, "stateId": "state-merging"}},
                cfg, issue_scope=scope)
            done = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query,
                 "variables": {"id": scope.id, "stateId": "state-done"}},
                cfg, issue_scope=scope)
            cancelled = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query,
                 "variables": {"id": scope.id, "stateId": "state-cancelled"}},
                cfg, issue_scope=scope)
        self.assertEqual(allowed["data"]["issueUpdate"]["success"], True)
        self.assertEqual(merging["error"]["code"], "linear_tool_forbidden")
        self.assertEqual(done["error"]["code"], "linear_tool_forbidden")
        self.assertEqual(cancelled["error"]["code"], "linear_tool_forbidden")

    def test_worker_mutation_allowlist_rejects_unknown_before_api(self):
        cfg = settings().tracker
        query = (
            "mutation Delete($id: String!) { "
            "issueArchive(id: $id) { success } }"
        )
        with patch.object(linear, "graphql") as api:
            result = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": query, "variables": {"id": issue_scope().id}},
                cfg, issue_scope=issue_scope())
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")
        api.assert_not_called()

    def test_worker_can_attach_pr_to_current_issue(self):
        cfg = settings()
        cfg.delivery.enabled = True
        cfg.delivery.repo = "duyiliu/symphony"
        scope = issue_scope()

        def response(_settings, query, _variables):
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            field = (
                "attachmentCreate" if "attachmentCreate" in query
                else "attachmentLinkGitHubPR")
            return {"data": {field: {
                "success": True,
                "attachment": {"id": "attachment-1"},
            }}}

        query = (
            "mutation Attach($issueId: String!, $url: String!, "
            "$title: String!, $linkKind: String!) { "
            "attachmentLinkGitHubPR(issueId: $issueId, url: $url, "
            "title: $title, linkKind: $linkKind) "
            "{ success attachment { id } } }"
        )
        variables = {
            "issueId": scope.id,
            "url": "https://github.com/duyiliu/symphony/pull/13",
            "title": "PR #13",
            "linkKind": "links",
        }
        with patch.object(linear, "graphql", side_effect=response):
            result = tracker.execute_agent_tool(
                "linear_graphql", {"query": query, "variables": variables},
                cfg.tracker, issue_scope=scope,
                delivery_settings=cfg.delivery)
        self.assertTrue(result["data"]["attachmentLinkGitHubPR"]["success"])

        create = (
            "mutation Attach($input: AttachmentCreateInput!) { "
            "attachmentCreate(input: $input) { success attachment { id } } }"
        )
        create_variables = {"input": {
            "issueId": scope.id,
            "title": "Pull request",
            "url": "https://github.com/duyiliu/symphony/pull/13",
        }}
        with patch.object(linear, "graphql", side_effect=response):
            generic = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": create, "variables": create_variables},
                cfg.tracker, issue_scope=scope,
                delivery_settings=cfg.delivery)
        self.assertTrue(generic["data"]["attachmentCreate"]["success"])

        variables["url"] = "https://github.com/other/repo/pull/13"
        with patch.object(linear, "graphql", side_effect=response):
            denied = tracker.execute_agent_tool(
                "linear_graphql", {"query": query, "variables": variables},
                cfg.tracker, issue_scope=scope,
                delivery_settings=cfg.delivery)
        self.assertEqual(denied["error"]["code"], "linear_tool_forbidden")

        create_variables["input"]["url"] = (
            "https://github.com/other/repo/pull/13")
        with patch.object(linear, "graphql", side_effect=response):
            denied_create = tracker.execute_agent_tool(
                "linear_graphql",
                {"query": create, "variables": create_variables},
                cfg.tracker, issue_scope=scope,
                delivery_settings=cfg.delivery)
        self.assertEqual(
            denied_create["error"]["code"], "linear_tool_forbidden")

    def test_worker_tool_requires_spawn_issue_scope(self):
        cfg = settings().tracker
        result = tracker.execute_agent_tool(
            "linear_graphql", {"query": "query {viewer {id}}"}, cfg)
        self.assertEqual(result["error"]["code"], "linear_tool_forbidden")

    def test_tool_trims_query_and_validates_variables(self):
        cfg = settings().tracker
        scope = issue_scope()
        calls = []

        def response(_settings, query, variables):
            calls.append((query, variables))
            if "SymphonyLinearWorkerIssueScope" in query:
                return {"data": {"issue": live_issue(scope)}}
            return {"data": {"viewer": {"id": "me"}}}

        with patch.object(linear, "graphql", side_effect=response):
            result = tracker.execute_agent_tool(
                "linear_graphql", "  query {viewer {id}}  ", cfg,
                issue_scope=scope)
        self.assertEqual(result["data"]["viewer"]["id"], "me")
        self.assertEqual(calls[-1][0], "query {viewer {id}}")

        self.assertEqual(
            tracker.execute_agent_tool(
                "linear_graphql", {"query": "   "}, cfg,
                issue_scope=scope)["error"]["code"],
            "missing_query")
        self.assertEqual(
            tracker.execute_agent_tool(
                "linear_graphql",
                {"query": "query {viewer {id}}", "variables": ["bad"]},
                cfg, issue_scope=scope)["error"]["code"],
            "invalid_variables")

    def test_requires_auth(self):
        cfg = settings().tracker
        cfg.api_key = None
        cfg.provider["api_key"] = None
        with patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            with self.assertRaises(WorkflowError):
                linear.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
