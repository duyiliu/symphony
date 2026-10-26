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
    return {"cols": cols, "counts": counts, "total": 1, "with_runs": 0, "repo": "example/repo"}


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
        self.assertFalse(payload["writable"])

    def test_legacy_write_endpoints_are_gone_even_with_valid_token(self):
        with patch.dict(os.environ, {"SYMPHONY_BOARD_TOKEN": "test-secret"}):
            for endpoint in ("/api/label", "/api/approve"):
                for headers in ({}, {"X-Board-Token": "test-secret"}):
                    status, body = self.request("POST", endpoint, "{}", headers)
                    self.assertEqual(status, 410)
                    self.assertIn("merge-approved", json.loads(body)["error"])
        self.github_mock.assert_not_called()

    def test_invalid_run_reference_and_unknown_route(self):
        self.assertEqual(self.request("GET", "/api/run/GH-1")[0], 400)
        self.assertEqual(self.request("GET", "/not-found")[0], 404)


if __name__ == "__main__":
    unittest.main()
