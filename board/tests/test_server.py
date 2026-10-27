"""Offline regression tests; never read deployment credentials or call GitHub."""

import http.client
import importlib.util
import json
import os
from html.parser import HTMLParser
from pathlib import Path
import threading
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("board_server", Path(__file__).parents[1] / "server.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


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
                    ("POST", "/repos/example/repo/issues/1/comments", {"body": "CI 一直红，先人工看看"}),
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
        self.assertEqual(board.pause_reason("example/repo", "", 1), "")

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


if __name__ == "__main__":
    unittest.main()
