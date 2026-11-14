"""Offline regression tests; never read deployment credentials or call GitHub."""

import contextlib
import http.client
import importlib.util
import json
import os
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import quote


spec = importlib.util.spec_from_file_location("board_server", Path(__file__).parents[1] / "server.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


GITLAB_PROJECT = {
    "id": "gitlab:gitlab.example.com/group/sub/repo",
    "provider": "gitlab",
    "host": "gitlab.example.com",
    "path": "group/sub/repo",
    "name": "group/sub/repo",
    "scheme": "https",
    "token_env": "GITLAB_TOKEN",
    "added_at": "2026-11-11T00:00:00Z",
    "primary": False,
    "readonly": True,
}

GITLAB_ISSUE = {
    "iid": 17,
    "title": "外部任务",
    "description": "外部项目正文",
    "web_url": "https://gitlab.example.com/group/sub/repo/-/issues/17",
    "state": "opened",
    "labels": ["agent"],
    "updated_at": "2026-11-10T02:00:00Z",
    "closed_at": None,
}

GITHUB_ISSUE = {
    "number": 5,
    "title": "Imported GitHub issue",
    "body": "text",
    "html_url": "https://git.example.com/owner/repo/issues/5",
    "state": "open",
    "labels": [{"name": "needs-human"}],
    "updated_at": "2026-11-10T03:00:00Z",
    "closed_at": None,
}


@contextlib.contextmanager
def project_env(tmp, repo="owner/primary", env_lines=(), projects=None, neutralize_tokens=True):
    """Isolate WORKFLOW / ENV_FILE / registry so no deployment path is touched."""
    workflow = os.path.join(tmp, "WORKFLOW.md")
    env_file = os.path.join(tmp, "env")
    registry = os.path.join(tmp, "projects.json")
    with open(workflow, "w", encoding="utf-8") as handle:
        handle.write('runtime:\n  repo: "%s"\n' % repo)
    with open(env_file, "w", encoding="utf-8") as handle:
        handle.write("\n".join(env_lines))
    overrides = {"SYMPHONY_PROJECTS": registry}
    if neutralize_tokens:
        for name in ("GITHUB_TOKEN", "GITLAB_TOKEN", "GITLAB_PAT", "GITLAB_ACCESS_TOKEN"):
            overrides[name] = ""
    with patch.object(board, "WORKFLOW", workflow), patch.object(board, "ENV_FILE", env_file), patch.dict(
        os.environ, overrides
    ):
        if projects is not None:
            board.save_project_registry(projects)
        board._registry_warned.discard(registry)
        board._project_caches.clear()
        yield registry
    board._project_caches.clear()


def fake_gitlab_request(calls):
    def request(project, path, token=None, timeout=8):
        calls.append((project["id"], path, token))
        if "/issues" in path:
            if "page=1" not in path:
                return []
            if "state=closed" in path:
                return [dict(GITLAB_ISSUE, state="closed", closed_at="2026-11-09T08:00:00Z")]
            return [GITLAB_ISSUE]
        if path.startswith("/projects/"):
            return {"path_with_namespace": project["path"], "name": "repo"}
        raise AssertionError("unexpected GitLab path: %s" % path)

    return request


def fake_github_request(calls):
    def request(project, path, token=None, timeout=8):
        calls.append((project["id"], path, token))
        if path.startswith("/repos/") and "/issues" in path:
            return [GITHUB_ISSUE] if "page=1" in path else []
        if path.startswith("/repos/"):
            return {"full_name": project["path"]}
        raise AssertionError("unexpected GitHub path: %s" % path)

    return request


def serve_board(testcase):
    server = board.ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    testcase.addCleanup(stop)
    return server


def http_request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        connection.close()


def fixture_cards():
    cols = {key: "" for key, _, _ in board.COLUMNS}
    counts = {key: 0 for key in cols}
    cols["awaiting"] = "<div class='bcard' data-issue='GH-1'>Test issue</div>"
    counts["awaiting"] = 1
    return {
        "cols": cols,
        "counts": counts,
        "total": 1,
        "with_runs": 0,
        "repo": "example/repo",
        "writable": True,
    }


class LaneParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lanes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "data-col" in attrs:
            self.lanes[attrs["data-col"]] = attrs.get("class", "").split()


class RenderingTests(unittest.TestCase):
    def test_empty_lane_class_does_not_match_generic_empty_state(self):
        with patch.object(board, "build_cards", return_value=fixture_cards()):
            document = board.issues_page()
        parser = LaneParser()
        parser.feed(document)
        self.assertEqual(len(parser.lanes), 6)
        self.assertEqual(parser.lanes["awaiting"], ["col"])
        for key, classes in parser.lanes.items():
            self.assertNotIn("empty", classes)
            if key != "awaiting":
                self.assertIn("is-empty", classes)
        self.assertIn(".col.is-empty{", board.STYLE)
        self.assertNotIn(".col.empty", board.STYLE)
        # Filtering and periodic refresh must use the same class as initial HTML.
        self.assertIn("classList.toggle('is-empty', empty)", board.BOARD)
        self.assertNotIn("classList.toggle('empty',", board.BOARD)
        # The standalone no-runs view still needs its generic empty-state style.
        self.assertIn(".empty{", board.STYLE)

    def test_all_empty_lanes(self):
        data = fixture_cards()
        data["cols"]["awaiting"] = ""
        data["counts"]["awaiting"] = 0
        data["total"] = 0
        with patch.object(board, "build_cards", return_value=data):
            parser = LaneParser()
            parser.feed(board.issues_page())
        self.assertTrue(all(classes == ["col", "is-empty"] for classes in parser.lanes.values()))

    def test_github_unavailable_renders_local_fallback(self):
        with patch.object(board, "build_cards", return_value=None), patch.object(
            board, "scan_sessions", return_value={}
        ):
            self.assertIn("GitHub 不可用", board.issues_page())

    def test_execution_status_takes_priority_over_labels_and_pr(self):
        for status in ("running", "blocked", "retrying"):
            self.assertEqual(board.status_of("GH-1", ["agent"], status, True), status)
        self.assertEqual(board.status_of("GH-1", ["Agent"], None, True), "ready")
        self.assertEqual(board.status_of("GH-1", [], None, True), "awaiting")
        self.assertEqual(board.status_of("GH-1", [], None, False), "idle")
        self.assertEqual(board.status_of("GH-1", ["needs-human"], None, True), "blocked")
        self.assertEqual(board.status_of("GH-1", ["agent", "needs-human"], None, True), "blocked")
        self.assertEqual(board.status_of("GH-1", ["merge-approved"], None, True), "awaiting")

    def test_cards_render_pause_state_and_reason_from_labels(self):
        issues = [
            {"number": 1, "title": "Paused one", "state": "open", "updated_at": "2026-10-27T01:00:00Z",
             "html_url": "https://github.com/o/r/issues/1",
             "labels": [{"name": "needs-human"}]},
            {"number": 2, "title": "Running one", "state": "open", "updated_at": "2026-10-27T02:00:00Z",
             "html_url": "https://github.com/o/r/issues/2",
             "labels": [{"name": "agent"}, {"name": "high-risk"}]},
            {"number": 3, "title": "Idle one", "state": "open", "updated_at": "2026-10-27T03:00:00Z",
             "html_url": "https://github.com/o/r/issues/3", "labels": []},
        ]

        with patch.object(board, "load_tracker", return_value=("o/r", "t")), patch.object(
            board, "github_issues", return_value=issues
        ), patch.object(board, "scan_sessions", return_value={}), patch.object(
            board, "symphony_state", return_value=None
        ), patch.object(board, "github_pulls", return_value={}), patch.object(
            board, "pause_reason", side_effect=lambda repo, token, n: "Merge gate not satisfied: merge state DIRTY." if n == 1 else ""
        ):
            data = board.build_cards()

        html = "".join(data["cols"].values())
        self.assertIn("data-paused='1' data-pause-reason='Merge gate not satisfied: merge state DIRTY.'", html)
        self.assertIn("等待人工处理", html)
        self.assertIn("data-agent='1'", html)
        # 未暂停的卡片属性为空而不是缺失，前端 getAttribute 不会报错。
        self.assertIn("data-paused='0'", html)
        self.assertEqual(data["counts"]["blocked"], 1)
        self.assertEqual(data["counts"]["ready"], 1)
        self.assertEqual(data["counts"]["idle"], 1)

    def test_board_uses_custom_dialog_instead_of_native_popups(self):
        self.assertNotRegex(board.BOARD, r"\b(?:alert|confirm|prompt)\s*\(")
        self.assertIn("function openDialog", board.BOARD)
        with patch.object(board, "build_cards", return_value=fixture_cards()):
            document = board.issues_page()
        self.assertIn("id='modal'", document)
        self.assertIn("id='dlg-ok'", document)

    def test_symphony_state_uses_background_cache(self):
        cached = {"running": [{"issue_identifier": "GH-1"}]}
        with patch.object(
            board, "_state_cache", {"data": cached, "at": 0.0, "started": True}
        ), patch.object(
            board, "fetch_state", side_effect=AssertionError("unexpected state request")
        ):
            self.assertEqual(board.symphony_state(), cached)

        with patch.object(
            board, "_state_cache", {"data": None, "at": 0.0, "started": False}
        ), patch.object(board, "fetch_state", return_value=cached) as fetch:
            self.assertEqual(board.symphony_state(), cached)
            fetch.assert_called_once()

    def test_count_messages_caches_incremental_live_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "live.jsonl")
            with open(live, "wb") as handle:
                handle.write(b'{"type":"entry_appended","entry":{"type":"message"}}\n')
            self.assertEqual(board.count_messages(live, True), 1)
            self.assertEqual(board.count_messages(live, True), 1)

            with open(live, "ab") as handle:
                handle.write(b'{"type":"entry_appended","entry":{"type":"message"}}\n')
            self.assertEqual(board.count_messages(live, True), 2)

            with open(live, "ab") as handle:
                handle.write(b'{"type":"entry_appended","entry":{"type":"mess')
            self.assertEqual(board.count_messages(live, True), 2)

            with open(live, "ab") as handle:
                handle.write(b'age"}}\n')
            self.assertEqual(board.count_messages(live, True), 3)

            legacy = os.path.join(tmp, "legacy.jsonl")
            with open(legacy, "wb") as handle:
                handle.write(b'{"type":"message"}\n{"type":"other"}\n')
            self.assertEqual(board.count_messages(legacy, False), 1)
            board._message_cache.clear()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.cards = patch.object(board, "build_cards", side_effect=fixture_cards)
        self.cards.start()
        self.addCleanup(self.cards.stop)
        self.github = patch.object(board, "github_request", side_effect=AssertionError("Unexpected GitHub call"))
        self.github_mock = self.github.start()
        self.addCleanup(self.github.stop)
        self.server = board.ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_index_and_api(self):
        with patch.dict(os.environ, {"SYMPHONY_BOARD_TOKEN": "test-secret"}):
            status, body = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("Issue 工作台", body)
            self.assertNotIn("test-secret", body)
            status, body = self.request("GET", "/api/issues")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["counts"]["awaiting"], 1)
        self.assertTrue(payload["writable"])
        self.assertIn("data-writable='1'", self.request("GET", "/")[1])

    def test_label_rejects_cross_site_requests(self):
        for endpoint in ("/api/label", "/api/approve", "/api/intervene"):
            status, body = self.request(
                "POST", endpoint, "{}", {"Origin": "http://evil.example"}
            )
            self.assertEqual(status, 403)
            self.assertIn("跨站", json.loads(body)["error"])
        self.github_mock.assert_not_called()

    def test_label_validates_payload_and_credentials(self):
        status, body = self.request("POST", "/api/label", "not-json")
        self.assertEqual(status, 400)
        bad_payloads = (
            {"issue": "GH-1", "action": "add", "label": "bug"},
            {"issue": "GH-1", "action": "add", "label": "merge-approved"},
            {"issue": "GH-1", "action": "toggle", "label": "agent"},
            {"issue": "jira-1", "action": "add", "label": "agent"},
            {"issue": "GH-x", "action": "add", "label": "agent"},
        )
        for payload in bad_payloads:
            status, _ = self.request(
                "POST", "/api/label", json.dumps(payload)
            )
            self.assertEqual(status, 400)
        with patch.object(board, "load_tracker", return_value=("", "")):
            status, body = self.request(
                "POST",
                "/api/label",
                json.dumps({"issue": "GH-1", "action": "add", "label": "agent"}),
            )
        self.assertEqual(status, 503)
        self.assertIn("GitHub 未配置", json.loads(body)["error"])
        self.github_mock.assert_not_called()

    def test_write_endpoints_enforce_trust_and_open_pr(self):
        outsider = {"author_association": "CONTRIBUTOR", "state": "open", "labels": []}
        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "fetch_issue", return_value=outsider
        ):
            status, body = self.request(
                "POST",
                "/api/label",
                json.dumps({"issue": "GH-1", "action": "add", "label": "agent"}),
            )
            self.assertEqual(status, 403)
            status, body = self.request("POST", "/api/approve", json.dumps({"issue": "GH-1"}))
            self.assertEqual(status, 410)
            self.assertIn("merge-approved", json.loads(body)["error"])
        member = {"author_association": "MEMBER", "state": "open", "labels": []}
        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "fetch_issue", return_value=member
        ), patch.object(board, "github_pulls", return_value={}):
            status, body = self.request("POST", "/api/approve", json.dumps({"issue": "GH-1"}))
        self.assertEqual(status, 410)
        self.github_mock.assert_not_called()

    def test_label_add_swaps_mutually_exclusive_label(self):
        member = {
            "author_association": "MEMBER",
            "state": "open",
            "labels": [{"name": "needs-human"}],
        }
        calls = []

        def fake_request(method, path, token, body=None, timeout=10):
            calls.append((method, path, body))
            return {}

        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "fetch_issue", return_value=member
        ), patch.object(board, "github_request", side_effect=fake_request):
            status, body = self.request(
                "POST",
                "/api/label",
                json.dumps({"issue": "GH-1", "action": "add", "label": "agent"}),
            )
        self.assertEqual(status, 200)
        self.assertIn("agent", json.loads(body)["message"])
        self.assertEqual(
            calls,
            [
                ("DELETE", "/repos/example/repo/issues/1/labels/needs-human", None),
                ("POST", "/repos/example/repo/issues/1/labels", {"labels": ["agent"]}),
            ],
        )
        self.assertIsNone(board._cache["issues"])

    def test_leftover_legacy_labels_do_not_block_execution(self):
        member = {
            "author_association": "MEMBER",
            "state": "open",
            "labels": [{"name": "agent"}, {"name": "merge-approved"}],
        }
        calls = []

        def fake_request(method, path, token, body=None, timeout=10):
            calls.append((method, path, body))
            return {}

        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "fetch_issue", return_value=member
        ), patch.object(board, "github_request", side_effect=fake_request):
            status, body = self.request(
                "POST",
                "/api/label",
                json.dumps({"issue": "GH-1", "action": "add", "label": "agent"}),
            )
        self.assertEqual(status, 200)
        # merge-approved carries no meaning: adding agent touches only agent.
        self.assertEqual(
            calls,
            [("POST", "/repos/example/repo/issues/1/labels", {"labels": ["agent"]})],
        )

    def test_approve_endpoint_is_gone(self):
        status, body = self.request("POST", "/api/approve", json.dumps({"issue": "GH-1"}))
        self.assertEqual(status, 410)
        self.assertIn("needs-human", json.loads(body)["error"])
        self.github_mock.assert_not_called()

    def test_label_add_needs_human_swaps_out_agent(self):
        member = {
            "author_association": "MEMBER",
            "state": "open",
            "labels": [{"name": "agent"}],
        }
        calls = []

        def fake_request(method, path, token, body=None, timeout=10):
            calls.append((method, path, body))
            return {}

        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "fetch_issue", return_value=member
        ), patch.object(board, "github_request", side_effect=fake_request):
            status, body = self.request(
                "POST",
                "/api/label",
                json.dumps({"issue": "GH-1", "action": "add", "label": "needs-human"}),
            )
        self.assertEqual(status, 200)
        self.assertIn("needs-human", json.loads(body)["message"])
        self.assertEqual(
            calls,
            [
                ("DELETE", "/repos/example/repo/issues/1/labels/agent", None),
                ("POST", "/repos/example/repo/issues/1/labels", {"labels": ["needs-human"]}),
            ],
        )

    def test_intervene_pauses_and_resumes_in_auto_mode(self):
        member = {"author_association": "MEMBER", "state": "open", "labels": [{"name": "agent"}]}
        calls = []

        def fake_request(method, path, token, body=None, timeout=10):
            calls.append((method, path, body))
            return {}

        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "fetch_issue", return_value=member
        ), patch.object(board, "github_request", side_effect=fake_request):
            status, body = self.request(
                "POST",
                "/api/intervene",
                json.dumps({"issue": "GH-1", "op": "pause", "comment": "CI 一直红，先人工看看"}),
            )
            self.assertEqual(status, 200)
            self.assertIn("needs-human", json.loads(body)["message"])
            # 顺序与宿主一致：先评论，再加 needs-human，最后才摘 agent。
            self.assertEqual(
                calls,
                [
                    ("POST", "/repos/example/repo/issues/1/comments",
                     {"body": "[board] 人工暂停（needs-human）：CI 一直红，先人工看看"}),
                    ("POST", "/repos/example/repo/issues/1/labels", {"labels": ["needs-human"]}),
                    ("DELETE", "/repos/example/repo/issues/1/labels/agent", None),
                ],
            )

            calls.clear()
            paused = {
                "author_association": "MEMBER",
                "state": "open",
                "labels": [{"name": "needs-human"}],
            }
            with patch.object(board, "fetch_issue", return_value=paused):
                status, body = self.request(
                    "POST", "/api/intervene", json.dumps({"issue": "GH-1", "op": "resume"})
                )
            self.assertEqual(status, 200)
            self.assertIn("agent", json.loads(body)["message"])
            self.assertEqual(
                calls,
                [
                    ("DELETE", "/repos/example/repo/issues/1/labels/needs-human", None),
                    ("POST", "/repos/example/repo/issues/1/labels", {"labels": ["agent"]}),
                ],
            )

        # Cross-site guards still apply.
        status, _ = self.request(
            "POST", "/api/intervene", json.dumps({"issue": "GH-1", "op": "pause"}), {"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        status, body = self.request("POST", "/api/intervene", json.dumps({"issue": "GH-1", "op": "toggle"}))
        self.assertEqual(status, 400)
        self.github_mock.assert_not_called()

    def test_pause_reason_extracts_the_latest_host_pause_comment(self):
        comments = [
            {"body": "[agent] Delivery PR #7"},
            {"body": "[agent] Paused: Merge gate not satisfied: merge state DIRTY.\nDelivery: %{\"pr\" => 7}"},
            {"body": "普通评论"},
        ]

        def fake_request(method, path, token, body=None, timeout=10):
            self.assertIn("comments", path)
            return comments

        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "github_request", side_effect=fake_request
        ):
            self.assertEqual(
                board.pause_reason("example/repo", "t", 1), "Merge gate not satisfied: merge state DIRTY."
            )

        board_comments = [
            {"body": "[agent] Delivery PR #7"},
            {"body": "[board] 人工暂停（needs-human）：CI 一直红，先人工看看"},
        ]

        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "github_request", return_value=board_comments
        ):
            self.assertEqual(board.pause_reason("example/repo", "t", 1), "CI 一直红，先人工看看")

        host_comments = [{"body": "[agent] Paused (needs-human): 验证完成，无需改动\nDelivery: nil"}]
        with patch.object(board, "load_tracker", return_value=("example/repo", "t")), patch.object(
            board, "github_request", return_value=host_comments
        ):
            self.assertEqual(board.pause_reason("example/repo", "t", 1), "验证完成，无需改动")
        self.assertEqual(board.pause_reason("example/repo", "", 1), "")

    def test_write_endpoints_reject_non_object_json(self):
        for body in ('"text"', "[]", "123", "null", "true"):
            for endpoint in ("/api/label", "/api/intervene"):
                status, payload = self.request(
                    "POST", endpoint, body, {"Content-Type": "application/json"}
                )
                self.assertEqual(status, 400, "%s %s" % (endpoint, body))
                self.assertIn("JSON", json.loads(payload)["error"])
        self.github_mock.assert_not_called()

    def test_completed_is_inline_and_history_bookmark_redirects(self):
        with patch.object(board, "github_history", return_value=([], "")):
            status, body = self.request("GET", "/api/completed")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["issues"], [])
        self.assertEqual(self.request("GET", "/history")[0], 302)
        status, document = self.request("GET", "/")
        self.assertIn("data-view='today'", document)
        self.assertIn("data-view='history'", document)
        self.assertIn("id='completed-panel'", document)
        self.assertNotIn("href='/history'", document)
        self.assertIn("@media(max-width:640px)", document)
        self.github_mock.assert_not_called()

    def test_invalid_run_reference_and_unknown_route(self):
        self.assertEqual(self.request("GET", "/api/run/GH-1")[0], 400)
        self.assertEqual(self.request("GET", "/not-found")[0], 404)


class ProjectUrlTests(unittest.TestCase):
    def test_parses_supported_url_shapes(self):
        expected = [
            ("https://github.com/owner/repo", None, ("github", "github.com", "owner/repo", "https")),
            ("https://github.com/owner/repo.git", None, ("github", "github.com", "owner/repo", "https")),
            ("git@github.com:owner/repo.git", None, ("github", "github.com", "owner/repo", "https")),
            ("ssh://git@github.com/owner/repo.git", None, ("github", "github.com", "owner/repo", "https")),
            ("github.com/owner/repo", None, ("github", "github.com", "owner/repo", "https")),
            ("https://gitlab.com/group/sub/repo", None, ("gitlab", "gitlab.com", "group/sub/repo", "https")),
            ("https://gitlab.com/group/sub/repo/", None, ("gitlab", "gitlab.com", "group/sub/repo", "https")),
            ("https://git.example.com/owner/repo", "github", ("github", "git.example.com", "owner/repo", "https")),
            ("git@git.example.com:owner/repo.git", "github", ("github", "git.example.com", "owner/repo", "https")),
            ("https://gl.example.com:8443/group/sub/repo", "gitlab",
             ("gitlab", "gl.example.com:8443", "group/sub/repo", "https")),
            ("http://127.0.0.1:3000/group/repo", "gitlab", ("gitlab", "127.0.0.1:3000", "group/repo", "http")),
            ("http://localhost/group/repo", "gitlab", ("gitlab", "localhost", "group/repo", "http")),
        ]
        for url, provider, want in expected:
            parsed = board.parse_project_url(url, provider)
            self.assertEqual(
                (parsed["provider"], parsed["host"], parsed["path"], parsed["scheme"]), want, url
            )

    def test_rejects_malformed_urls(self):
        rejected = [
            "", "   ", "github.com", "https://github.com/owner", "https://gitlab.com/only-group",
            "https://github.com/owner/repo/extra", "https://github.com/owner/repo?tab=issues",
            "https://gitlab.com/group/sub/repo#frag", "https://user:pass@github.com/owner/repo",
            "https://someone@github.com/owner/repo", "https://github.com/owner//repo",
            "https://github.com/owner/../repo", "ftp://github.com/owner/repo",
            "http://gitlab.example.com/group/repo", "https://github.com/owner/re po",
            "https://gitlab.com/group/repo?", "https://GITLAB.com/group/repo/../x",
        ]
        for url in rejected:
            with self.assertRaises(board.ProjectUrlError, msg=url):
                board.parse_project_url(url, "github" if "github" in url else "gitlab")
        with self.assertRaises(board.ProjectUrlError):
            board.parse_project_url("https://github.com/" + "a" * board.PROJECT_URL_LIMIT + "/repo")
        with self.assertRaises(board.ProjectUrlError):
            board.parse_project_url("https://github.com/owner\n/repo")
        # Surrounding whitespace from a paste is tolerated.
        self.assertEqual(board.parse_project_url("  https://github.com/owner/repo\n")["path"], "owner/repo")

    def test_self_hosted_needs_explicit_provider_and_conflicts_are_rejected(self):
        with self.assertRaises(board.ProjectUrlError):
            board.parse_project_url("https://git.example.com/owner/repo")
        with self.assertRaises(board.ProjectUrlError):
            board.parse_project_url("https://github.com/owner/repo", "gitlab")
        with self.assertRaises(board.ProjectUrlError):
            board.parse_project_url("https://gitlab.com/group/repo", "github")
        with self.assertRaises(board.ProjectUrlError):
            board.parse_project_url("https://git.example.com/owner/repo", "gitea")
        # "auto" behaves like no explicit choice.
        self.assertEqual(board.parse_project_url("https://gitlab.com/g/r", "auto")["provider"], "gitlab")

    def test_api_base_and_identifier_prefixes(self):
        github = board.PROVIDERS["github"]
        gitlab = board.PROVIDERS["gitlab"]
        self.assertEqual(
            github.api_base({"host": "github.com", "path": "o/r", "scheme": "https"}), "https://api.github.com"
        )
        self.assertEqual(
            github.api_base({"host": "git.example.com", "path": "o/r", "scheme": "https"}),
            "https://git.example.com/api/v3",
        )
        self.assertEqual(
            gitlab.api_base({"host": "gitlab.com", "path": "g/r", "scheme": "https"}),
            "https://gitlab.com/api/v4",
        )
        self.assertEqual(
            gitlab.api_base({"host": "gl.example.com", "path": "g/r", "scheme": "https"}),
            "https://gl.example.com/api/v4",
        )
        # Subgroup paths must collapse into one URL-encoded segment.
        self.assertEqual(gitlab.project_ref({"path": "group/sub/repo"}), "projects/group%2Fsub%2Frepo")
        self.assertEqual(github.project_ref({"path": "owner/repo"}), "repos/owner/repo")
        # The primary project keeps GH-; imported projects never reuse that namespace.
        self.assertEqual(github.identifier({"number": 5}, board.primary_project()), "GH-5")
        self.assertEqual(github.identifier({"number": 5}), "GHX-5")
        self.assertEqual(gitlab.identifier({"number": 17}), "GL-17")
        self.assertNotEqual(board.IMPORTED_GITHUB_PREFIX, board.IDENTIFIER_PREFIX)

    def test_issues_normalize_to_one_shape(self):
        project = dict(GITLAB_PROJECT)
        gitlab_issue = board.PROVIDERS["gitlab"].normalize(GITLAB_ISSUE, project)
        github_issue = board.PROVIDERS["github"].normalize(GITHUB_ISSUE, project)
        shape = {"number", "identifier", "title", "body", "url", "state", "labels", "updated_at", "closed_at"}
        for issue in (gitlab_issue, github_issue):
            self.assertEqual(set(issue), shape)
        self.assertEqual(gitlab_issue["identifier"], "GL-17")
        self.assertEqual(gitlab_issue["number"], 17)
        self.assertEqual(gitlab_issue["url"], GITLAB_ISSUE["web_url"])
        self.assertEqual(gitlab_issue["labels"], [{"name": "agent"}])
        self.assertEqual(github_issue["identifier"], "GHX-5")
        self.assertEqual(github_issue["labels"], [{"name": "needs-human"}])
        self.assertEqual(github_issue["url"], GITHUB_ISSUE["html_url"])


class RegistryTests(unittest.TestCase):
    def test_round_trip_and_metadata_only_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp, env_lines=["GITLAB_TOKEN=glpat-secret-value"], projects=[GITLAB_PROJECT]) as registry:
                with open(registry, encoding="utf-8") as handle:
                    raw = handle.read()
                payload = json.loads(raw)
                self.assertEqual(payload["version"], 1)
                self.assertNotIn("glpat-secret-value", raw)
                self.assertEqual(set(payload["projects"][0]), set(board.PROJECT_FIELDS))
                self.assertEqual(payload["projects"][0]["token_env"], "GITLAB_TOKEN")

                loaded = board.load_project_registry()
                self.assertEqual([entry["id"] for entry in loaded], [GITLAB_PROJECT["id"]])
                entry = loaded[0]
                self.assertTrue(entry["readonly"])
                self.assertFalse(entry["primary"])
                self.assertNotIn("token", entry)
                self.assertEqual(entry["provider"], "gitlab")
                self.assertEqual(entry["path"], "group/sub/repo")
                self.assertEqual(board.resolve_project(GITLAB_PROJECT["id"])["id"], GITLAB_PROJECT["id"])
                self.assertIsNone(board.resolve_project("gitlab:gitlab.example.com/other/repo"))

    def test_broken_registry_is_ignored_and_never_blocks_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp) as registry:
                with open(registry, "w", encoding="utf-8") as handle:
                    handle.write("{not json")
                self.assertEqual(board.load_project_registry(), [])
                self.assertEqual([project["id"] for project in board.all_projects()], ["primary"])

                board.save_project_registry([GITLAB_PROJECT])
                with open(registry, encoding="utf-8") as handle:
                    payload = json.load(handle)
                payload["projects"].append({"provider": "gitea", "host": "x.example", "path": "a/b"})
                payload["projects"].append({"provider": "gitlab", "host": "", "path": ""})
                payload["projects"].append(dict(GITLAB_PROJECT))
                payload["projects"].append("nope")
                with open(registry, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                loaded = board.load_project_registry()
                self.assertEqual([entry["id"] for entry in loaded], [GITLAB_PROJECT["id"]])

    def test_primary_project_is_dynamic_and_never_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp, repo="owner/primary", projects=[GITLAB_PROJECT]):
                projects = board.all_projects()
                self.assertEqual([project["id"] for project in projects], ["primary", GITLAB_PROJECT["id"]])
                primary = projects[0]
                self.assertEqual(primary["path"], "owner/primary")
                self.assertFalse(primary["readonly"])
                with open(board.projects_file(), encoding="utf-8") as handle:
                    stored = json.load(handle)["projects"]
                self.assertEqual([entry["id"] for entry in stored], [GITLAB_PROJECT["id"]])
                self.assertEqual(board.load_tracker(), ("owner/primary", None))

                public = board.public_project(primary)
                self.assertEqual(public["web_url"], "https://github.com/owner/primary")
                self.assertEqual(public["token_env"], "GITHUB_TOKEN")
                self.assertFalse(public["token_configured"])
                self.assertNotIn("token", public)

    def test_token_resolution_prefers_process_env_then_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp, env_lines=["GITLAB_TOKEN=from-env-file", "GITHUB_TOKEN=gh-from-file"]):
                with patch.dict(os.environ, {"GITLAB_TOKEN": "from-process"}):
                    self.assertEqual(board.token_for(GITLAB_PROJECT), "from-process")
                self.assertEqual(board.token_for(GITLAB_PROJECT), "from-env-file")
                self.assertEqual(board.token_for(board.primary_project()), "gh-from-file")
            with project_env(tmp, env_lines=["GITLAB_ACCESS_TOKEN=fallback-value"]):
                self.assertEqual(board.token_for(GITLAB_PROJECT), "fallback-value")
                custom = dict(GITLAB_PROJECT, token_env="MY_TOKEN")
                self.assertEqual(board.token_for(custom), "fallback-value")
                with patch.dict(os.environ, {"MY_TOKEN": "custom-value"}):
                    self.assertEqual(board.token_for(custom), "custom-value")
            with project_env(tmp):
                self.assertEqual(board.token_for(GITLAB_PROJECT), "")
                self.assertEqual(
                    board.token_names({"provider": "gitlab", "token_env": "not a var!"})[:1], ["GITLAB_TOKEN"]
                )


class ImportedProjectTests(unittest.TestCase):
    def test_build_cards_keeps_its_no_argument_primary_contract(self):
        import inspect

        self.assertEqual(list(inspect.signature(board.build_cards).parameters), [])
        issues = [dict(GITHUB_ISSUE, number=1, labels=[], html_url="https://github.com/o/r/issues/1")]
        with patch.object(board, "load_tracker", return_value=("o/r", "t")), patch.object(
            board, "github_issues", return_value=issues
        ), patch.object(board, "scan_sessions", return_value={}), patch.object(
            board, "symphony_state", return_value=None
        ), patch.object(board, "github_pulls", return_value={}), patch.object(
            board, "pause_reason", return_value=""
        ):
            data = board.build_cards()
        self.assertEqual(set(data), {"cols", "counts", "total", "with_runs", "repo", "writable"})
        self.assertEqual(data["repo"], "o/r")
        self.assertTrue(data["writable"])
        self.assertEqual(set(data["cols"]), {key for key, _label, _sub in board.COLUMNS})
        self.assertIn("data-issue='GH-1'", "".join(data["cols"].values()))

    def test_imported_status_maps_labels_only(self):
        self.assertEqual(board.imported_status(["needs-human"]), "blocked")
        self.assertEqual(board.imported_status(["Agent"]), "ready")
        self.assertEqual(board.imported_status(["agent", "needs-human"]), "blocked")
        self.assertEqual(board.imported_status(["bug"]), "idle")
        self.assertEqual(board.imported_status([]), "idle")

    def test_imported_cards_are_read_only_anchors(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp, projects=[GITLAB_PROJECT]), patch.object(
                board, "provider_request", side_effect=fake_gitlab_request(calls)
            ):
                data = board.build_project_cards(GITLAB_PROJECT["id"])
        html = "".join(data["cols"].values())
        self.assertIn("<a class='bcard ready'", html)
        self.assertIn("GL-17", html)
        self.assertIn("href='https://gitlab.example.com/group/sub/repo/-/issues/17'", html)
        self.assertIn("rel='noopener noreferrer'", html)
        self.assertIn("data-project='%s'" % GITLAB_PROJECT["id"], html)
        # No data-issue and no nested anchor: board handlers must ignore these cards.
        self.assertNotIn("data-issue", html)
        self.assertNotIn("<a class='ext'", html)
        self.assertEqual(set(data["cols"]), {"blocked", "ready", "idle"})
        self.assertEqual(data["counts"]["ready"], 1)
        self.assertEqual(data["total"], 1)
        self.assertFalse(data["writable"])
        self.assertTrue(data["readonly"])
        self.assertEqual(data["project"], GITLAB_PROJECT["id"])
        self.assertEqual(data["repo_url"], "https://gitlab.example.com/group/sub/repo")
        self.assertEqual(calls[0][1], "/projects/group%2Fsub%2Frepo/issues?state=opened&per_page=100&page=1")

    def test_imported_github_project_uses_scoped_identifier(self):
        project = dict(
            GITLAB_PROJECT,
            id="github:git.example.com/owner/repo",
            provider="github",
            host="git.example.com",
            path="owner/repo",
            name="owner/repo",
            token_env="GITHUB_TOKEN",
        )
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp), patch.object(board, "provider_request", side_effect=fake_github_request(calls)):
                board.save_project_registry([project])
                data = board.build_project_cards(project["id"])
        html = "".join(data["cols"].values())
        self.assertIn("GHX-5", html)
        self.assertNotIn("'GH-5'", html)
        self.assertEqual(data["counts"]["blocked"], 1)
        self.assertEqual(calls[0][1], "/repos/owner/repo/issues?state=open&per_page=100&page=1")

    def test_project_cache_is_isolated_and_survives_provider_failure(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp, projects=[GITLAB_PROJECT]):
                project = board.resolve_project(GITLAB_PROJECT["id"])
                with patch.object(board, "provider_request", side_effect=fake_gitlab_request(calls)):
                    first, warning = board.provider_issues(project)
                    second, _ = board.provider_issues(board.resolve_project(GITLAB_PROJECT["id"]))
                self.assertEqual(warning, "")
                self.assertEqual(first, second)
                self.assertEqual(len(calls), 1)
                # Imported projects never touch the primary global caches.
                self.assertIsNone(board._cache["issues"])
                self.assertEqual(board._pulls_cache["data"], None)
                self.assertEqual(list(board._project_caches), [(GITLAB_PROJECT["id"], "open")])

                board.clear_project_cache(GITLAB_PROJECT["id"])
                self.assertEqual(board._project_caches, {})
                with patch.object(board, "provider_request", side_effect=fake_gitlab_request(calls)):
                    board.provider_issues(project)
                self.assertEqual(len(calls), 2)

                board._project_caches[(GITLAB_PROJECT["id"], "open")]["at"] = 0.0
                with patch.object(board, "provider_request", side_effect=OSError("network down")):
                    data = board.build_project_cards(GITLAB_PROJECT["id"])
                self.assertIn("失败", data["warning"])
                self.assertEqual(data["counts"]["ready"], 1)
                self.assertFalse(data["writable"])

    def test_completed_data_adds_identifier_for_both_project_kinds(self):
        events = [{"number": 3, "title": "t", "at": "2026-11-10T02:00:00Z", "url": "u", "body": "b"}]
        with tempfile.TemporaryDirectory() as tmp:
            with project_env(tmp, projects=[GITLAB_PROJECT]):
                with patch.object(board, "github_history", return_value=(events, "")):
                    data = board.completed_data()
                self.assertEqual(data["project"], "primary")
                self.assertFalse(data["readonly"])
                self.assertEqual(data["issues"][0]["identifier"], "GH-3")
                self.assertEqual(data["issues"][0]["day"], "2026-11-10")

                calls = []
                with patch.object(board, "provider_request", side_effect=fake_gitlab_request(calls)):
                    data = board.completed_data(GITLAB_PROJECT["id"])
                self.assertEqual(data["project"], GITLAB_PROJECT["id"])
                self.assertTrue(data["readonly"])
                self.assertEqual(data["issues"][0]["identifier"], "GL-17")
                self.assertEqual(data["issues"][0]["number"], 17)
                self.assertEqual(data["issues"][0]["day"], "2026-11-09")
                self.assertIn("state=closed", calls[0][1])

                self.assertEqual(board.completed_data("nope")["issues"], [])
                self.assertIn("未知项目", board.completed_data("nope")["warning"])


class ProjectHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(project_env(tmp.name, projects=[GITLAB_PROJECT]))
        self.stack.enter_context(patch.object(board, "build_cards", side_effect=fixture_cards))
        self.stack.enter_context(
            patch.object(board, "github_request", side_effect=AssertionError("Unexpected GitHub call"))
        )
        self.server = serve_board(self)

    def request(self, method, path, body=None, headers=None):
        return http_request(self.server, method, path, body, headers)

    def post_project(self, payload, headers=None):
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        return self.request("POST", "/api/projects", json.dumps(payload), merged)

    def test_projects_list_never_leaks_tokens(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-secret"}):
            status, body = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertNotIn("glpat-secret", body)
        payload = json.loads(body)
        self.assertEqual([project["id"] for project in payload["projects"]], ["primary", GITLAB_PROJECT["id"]])
        self.assertEqual([provider["kind"] for provider in payload["providers"]], ["github", "gitlab"])
        primary, imported = payload["projects"]
        self.assertFalse(primary["readonly"])
        self.assertEqual(primary["provider_label"], "GitHub")
        self.assertFalse(primary["token_configured"])
        self.assertTrue(imported["readonly"])
        self.assertTrue(imported["token_configured"])
        self.assertEqual(imported["provider_label"], "GitLab")
        self.assertEqual(imported["web_url"], "https://gitlab.example.com/group/sub/repo")
        self.assertEqual(imported["token_env"], "GITLAB_TOKEN")
        self.assertEqual(
            set(imported),
            {"id", "provider", "provider_label", "host", "path", "name", "primary", "readonly",
             "web_url", "token_env", "token_configured"},
        )

    def test_import_persists_metadata_and_rejects_duplicates(self):
        calls = []
        with patch.object(board, "provider_request", side_effect=fake_github_request(calls)):
            status, body = self.post_project({"url": "https://git.example.com/owner/repo.git", "provider": "github"})
        self.assertEqual(status, 201, body)
        project = json.loads(body)["project"]
        self.assertEqual(project["id"], "github:git.example.com/owner/repo")
        self.assertEqual(project["name"], "owner/repo")
        self.assertEqual(project["token_env"], "GITHUB_TOKEN")
        self.assertTrue(project["readonly"])
        self.assertNotIn("token", project)
        self.assertEqual(calls[0][1], "/repos/owner/repo")
        with open(board.projects_file(), encoding="utf-8") as handle:
            stored = json.load(handle)["projects"]
        self.assertEqual(
            [entry["id"] for entry in stored], [GITLAB_PROJECT["id"], "github:git.example.com/owner/repo"]
        )
        self.assertEqual(set(stored[1]), set(board.PROJECT_FIELDS))

        with patch.object(board, "provider_request", side_effect=fake_github_request(calls)):
            status, body = self.post_project({"url": "https://git.example.com/owner/repo", "provider": "github"})
        self.assertEqual(status, 409)
        self.assertIn("已导入", json.loads(body)["error"])

        status, body = self.post_project({"url": "https://github.com/owner/primary"})
        self.assertEqual(status, 409)
        self.assertIn("主项目", json.loads(body)["error"])

        status, body = self.post_project({"url": "gitlab:gitlab.example.com/group/sub/repo"})
        self.assertEqual(status, 400)

    def test_import_validates_input_and_reports_provider_errors(self):
        status, body = self.post_project({"url": "https://gl.example.com/group/repo"})
        self.assertEqual(status, 400)
        self.assertIn("平台类型", json.loads(body)["error"])

        status, body = self.post_project({"url": "https://gitlab.com/group/repo", "token_env": "not a var!"})
        self.assertEqual(status, 400)
        self.assertIn("环境变量", json.loads(body)["error"])

        status, body = self.post_project({"url": "https://gitlab.com/group/repo", "name": "x" * 121})
        self.assertEqual(status, 400)

        def deny(project, path, token=None, timeout=8):
            raise urllib.error.HTTPError("https://gitlab.example.com", 404, "Not Found", {}, None)

        with patch.object(board, "provider_request", side_effect=deny):
            status, body = self.post_project({"url": "https://gitlab.com/group/repo"})
        self.assertEqual(status, 400)
        self.assertIn("token", json.loads(body)["error"])

        with patch.object(board, "provider_request", side_effect=OSError("boom")):
            status, body = self.post_project({"url": "https://gitlab.com/group/repo"})
        self.assertEqual(status, 502)
        self.assertIn("失败", json.loads(body)["error"])

        with open(board.projects_file(), encoding="utf-8") as handle:
            stored = json.load(handle)["projects"]
        self.assertEqual([entry["id"] for entry in stored], [GITLAB_PROJECT["id"]])

    def test_write_guards_apply_to_project_endpoints(self):
        status, body = self.post_project({"url": "https://gitlab.com/g/r"}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("跨站", json.loads(body)["error"])
        status, body = self.request(
            "DELETE", "/api/projects/" + quote(GITLAB_PROJECT["id"], safe=""), None,
            {"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.request("POST", "/api/projects", "x" * 5000, {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/projects", "not-json", {"Content-Type": "application/json"})[0], 400)
        with open(board.projects_file(), encoding="utf-8") as handle:
            stored = json.load(handle)["projects"]
        self.assertEqual([entry["id"] for entry in stored], [GITLAB_PROJECT["id"]])

    def test_delete_project(self):
        encoded = quote(GITLAB_PROJECT["id"], safe="")
        status, body = self.request("DELETE", "/api/projects/" + encoded)
        self.assertEqual(status, 200)
        self.assertEqual([project["id"] for project in json.loads(body)["projects"]], ["primary"])
        with open(board.projects_file(), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["projects"], [])
        self.assertEqual(self.request("DELETE", "/api/projects/" + encoded)[0], 404)

        status, body = self.request("DELETE", "/api/projects/primary")
        self.assertEqual(status, 400)
        self.assertIn("主项目", json.loads(body)["error"])
        self.assertEqual(self.request("DELETE", "/api/projects/")[0], 400)
        self.assertEqual(self.request("DELETE", "/api/other")[0], 404)

    def test_imported_projects_reject_issue_write_actions(self):
        payload = {"issue": "GL-17", "action": "add", "label": "agent", "project": GITLAB_PROJECT["id"]}
        status, body = self.request("POST", "/api/label", json.dumps(payload), {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIn("只读", json.loads(body)["error"])

        payload = {"issue": "GL-17", "op": "pause", "project": GITLAB_PROJECT["id"]}
        status, body = self.request("POST", "/api/intervene", json.dumps(payload), {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

        # Without a project field the imported identifier still cannot address the primary repo.
        status, body = self.request(
            "POST", "/api/label",
            json.dumps({"issue": "GL-17", "action": "add", "label": "agent"}),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("标识不合法", json.loads(body)["error"])
        status, _ = self.request(
            "POST", "/api/label",
            json.dumps({"issue": "GHX-5", "action": "add", "label": "agent"}),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)

    def test_project_scoped_issue_endpoint(self):
        calls = []
        encoded = quote(GITLAB_PROJECT["id"], safe="")
        with patch.object(board, "provider_request", side_effect=fake_gitlab_request(calls)):
            status, body = self.request("GET", "/api/issues?project=" + encoded)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["project"], GITLAB_PROJECT["id"])
        self.assertEqual(payload["project_name"], "group/sub/repo")
        self.assertTrue(payload["readonly"])
        self.assertFalse(payload["writable"])
        self.assertEqual(payload["repo_url"], "https://gitlab.example.com/group/sub/repo")
        self.assertIn("GL-17", payload["cols"]["ready"])
        self.assertNotIn("data-issue", "".join(payload["cols"].values()))
        self.assertEqual(payload["summary"], "1 进行中")
        self.assertTrue(calls[0][1].startswith("/projects/group%2Fsub%2Frepo/issues"))

    def test_default_endpoints_still_serve_the_primary_project(self):
        status, body = self.request("GET", "/api/issues")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["project"], "primary")
        self.assertFalse(payload["readonly"])
        self.assertTrue(payload["writable"])
        self.assertEqual(payload["counts"]["awaiting"], 1)
        self.assertIn("data-issue='GH-1'", "".join(payload["cols"].values()))

        events = [{"number": 3, "title": "t", "at": "2026-11-10T02:00:00Z", "url": "u", "body": "b"}]
        with patch.object(board, "github_history", return_value=(events, "")):
            status, body = self.request("GET", "/api/completed")
        payload = json.loads(body)
        self.assertEqual(payload["project"], "primary")
        self.assertEqual(payload["issues"][0]["identifier"], "GH-3")

        self.assertEqual(self.request("GET", "/api/issues?project=nope")[0], 404)
        self.assertEqual(self.request("GET", "/api/completed?project=nope")[0], 404)
        status, document = self.request("GET", "/?project=nope")
        self.assertEqual(status, 200)
        self.assertIn("data-project='primary'", document)

    def test_primary_page_keeps_writable_lanes_and_adds_project_controls(self):
        status, document = self.request("GET", "/")
        self.assertEqual(status, 200)
        for marker in (
            "id='projectSel'", "id='importProject'", "id='projectModal'", "id='pm-url'",
            "id='pm-provider'", "id='pm-name'", "id='pm-token'", "id='pm-error'", "id='pm-ok'",
            "data-project='primary'", "data-readonly='0'", "data-writable='1'",
            "a.bcard{display:block;color:inherit}", "function switchProject", "function openProjectDialog",
            "<button class='chip' id='removeProject' type='button' hidden>",
            "<span class='chip ro' id='readonlyBadge' hidden>",
        ):
            self.assertIn(marker, document)
        self.assertIn("owner/primary · 主项目", document)
        self.assertIn("group/sub/repo · GitLab", document)
        self.assertIn("业务状态来自 Symphony", document)
        self.assertNotIn("外部项目只读", document)
        for key, _label, _sub in board.COLUMNS:
            self.assertIn("data-col='%s'" % key, document)

    def test_imported_page_is_read_only(self):
        calls = []
        with patch.object(board, "provider_request", side_effect=fake_gitlab_request(calls)):
            status, document = self.request("GET", "/?project=" + quote(GITLAB_PROJECT["id"], safe=""))
        self.assertEqual(status, 200)
        self.assertIn("data-project='%s'" % GITLAB_PROJECT["id"], document)
        self.assertIn("data-readonly='1'", document)
        self.assertIn("data-writable='0'", document)
        self.assertIn("外部项目只读", document)
        self.assertIn("group/sub/repo · GitLab", document)
        self.assertIn(
            "value='%s' data-provider='gitlab' data-readonly='1' selected" % GITLAB_PROJECT["id"],
            document,
        )
        board_slice = document.split("<main class='board'")[1].split("</main>")[0]
        self.assertIn("<a class='bcard ready'", board_slice)
        self.assertIn("GL-17", board_slice)
        self.assertNotIn("data-issue", board_slice)
        # Only label-derived lanes are rendered for an external project.
        for key, _label, _sub in board.IMPORTED_COLUMNS:
            self.assertIn("data-col='%s'" % key, document)
        for key in ("running", "retrying", "awaiting"):
            self.assertNotIn("data-col='%s'" % key, document)


if __name__ == "__main__":
    unittest.main()
