"""Offline regression tests; never read deployment credentials or call GitHub."""

import http.client
import importlib.util
import json
import os
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("board_server", Path(__file__).parents[1] / "server.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


def record(number, state="Todo", **extra):
    base = {
        "issue_number": number,
        "identifier": "GH-%d" % number,
        "state": state,
        "revision": 3,
        "repo": "o/r",
        "title": "Task %d" % number,
        "url": "https://github.com/o/r/issues/%d" % number,
        "native_state": "open",
        "labels": [],
        "updated_at": "2026-10-27T0%d:00:00Z" % (number % 10),
    }
    base.update(extra)
    return base


def fixture_cards():
    cols = {key: "" for key, _, _ in board.COLUMNS}
    counts = {key: 0 for key in cols}
    cols["human_review"] = "<div class='bcard' data-issue='GH-1'>Test issue</div>"
    counts["human_review"] = 1
    return {
        "cols": cols,
        "counts": counts,
        "total": 1,
        "with_runs": 0,
        "repo": "o/r",
        "writable": True,
        "facts": {},
    }


class LaneParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lanes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "data-col" in attrs:
            self.lanes[attrs["data-col"]] = attrs.get("class", "").split()


class LaneTests(unittest.TestCase):
    def test_columns_are_the_seven_local_states(self):
        self.assertEqual(
            [key for key, _label, _sub in board.COLUMNS],
            ["backlog", "todo", "in_progress", "human_review",
             "merging", "rework", "done"])

    def test_state_to_lane_maps_canonical_and_terminal_aliases(self):
        expected = {
            "Backlog": "backlog", "Todo": "todo",
            "In Progress": "in_progress", "Human Review": "human_review",
            "Merging": "merging", "Rework": "rework",
            "Done": "done", "Closed": "done", "Cancelled": "done",
            "Canceled": "done", "Duplicate": "done",
        }
        for state, lane in expected.items():
            self.assertEqual(board.lane_of_state(state), lane, state)
        self.assertEqual(board.lane_of_state(None), "backlog")
        self.assertEqual(board.lane_of_state("bogus"), "backlog")

    def test_task_card_state_runtime_badges_do_not_change_lane(self):
        self.assertEqual(
            board.task_card_state(record(1, "In Progress"), "running")[0], "running")
        self.assertEqual(
            board.task_card_state(record(1, "Todo"), "retrying")[0], "retrying")
        self.assertEqual(
            board.task_card_state(record(1, "Human Review"), None)[0], "awaiting")
        self.assertEqual(
            board.task_card_state(record(1, "Done"), None)[0], "done")
        self.assertEqual(
            board.task_card_state(record(1, "Rework"), None)[0], "retrying")


class BuildCardsTests(unittest.TestCase):
    def build(self, tasks, state=None):
        with patch.object(board, "board_tasks", return_value=tasks), patch.object(
            board, "scan_sessions", return_value={}
        ), patch.object(board, "symphony_state", return_value=state or {}):
            return board.build_cards()

    def test_records_land_in_their_state_lane(self):
        tasks = [
            record(1, "Backlog"), record(2, "Todo"), record(3, "In Progress"),
            record(4, "Human Review"), record(5, "Merging"),
            record(6, "Rework"), record(7, "Done"), record(8, "Cancelled"),
        ]
        data = self.build(tasks)
        self.assertEqual(data["counts"], {
            "backlog": 1, "todo": 1, "in_progress": 1, "human_review": 1,
            "merging": 1, "rework": 1, "done": 2})
        self.assertIn("data-task-state='Rework'", data["cols"]["rework"])
        self.assertIn("data-task-state='Cancelled'", data["cols"]["done"])

    def test_source_closed_is_a_badge_not_a_state_override(self):
        tasks = [record(1, "In Progress", native_state="closed")]
        data = self.build(tasks)
        self.assertEqual(data["counts"]["in_progress"], 1)
        self.assertIn("源已关闭", data["cols"]["in_progress"])
        self.assertTrue(data["facts"]["GH-1"]["closed"])

    def test_card_carries_revision_pr_and_workpad(self):
        tasks = [record(9, "Merging", revision=7, pr_number=42,
                        pr_url="https://github.com/o/r/pull/42",
                        workpad_comment_id=555, branch_name="agent/issue-9")]
        data = self.build(tasks)
        html = data["cols"]["merging"]
        self.assertIn("data-rev='7'", html)
        self.assertIn("PR #42", html)
        facts = data["facts"]["GH-9"]
        self.assertEqual(facts["rev"], 7)
        self.assertEqual(facts["workpad"], 555)
        self.assertEqual(facts["branch"], "agent/issue-9")

    def test_card_carries_blocked_reason_for_drawer(self):
        tasks = [record(9, "Rework", blocked_reason="评审要求重做")]
        facts = self.build(tasks)["facts"]["GH-9"]
        self.assertEqual(facts["blocked"], "评审要求重做")

    def test_done_lane_is_capped(self):
        tasks = [record(n, "Done", updated_at="2026-10-%02dT00:00:00Z" % (n % 28 + 1))
                 for n in range(1, board.DONE_COL_LIMIT + 4)]
        data = self.build(tasks)
        self.assertEqual(data["cols"]["done"].count("class='bcard"), board.DONE_COL_LIMIT)

    def test_unavailable_scheduler_returns_none(self):
        with patch.object(board, "board_tasks", return_value=None):
            self.assertIsNone(board.build_cards())


class CompletedTests(unittest.TestCase):
    def test_terminal_tasks_feed_completed_views(self):
        tasks = [
            record(1, "Done", updated_at=board.datetime.now(board.CN).isoformat(),
                   pr_number=2, pr_url="https://github.com/o/r/pull/2"),
            record(2, "Duplicate", updated_at="2020-01-01T00:00:00Z"),
            record(3, "Todo"),
        ]
        with patch.object(board, "board_tasks", return_value=tasks):
            data = board.completed_data()
        numbers = [i["number"] for i in data["issues"]]
        self.assertEqual(numbers, [1, 2])
        self.assertEqual(data["issues"][0]["merged_pr"]["number"], 2)
        self.assertEqual(data["issues"][0]["day"], data["today"])
        self.assertEqual(data["issues"][1]["state"], "Duplicate")


class RenderingTests(unittest.TestCase):
    def test_empty_lane_class_and_columns_render(self):
        with patch.object(board, "build_cards", return_value=fixture_cards()):
            document = board.issues_page()
        parser = LaneParser()
        parser.feed(document)
        self.assertEqual(len(parser.lanes), len(board.COLUMNS))
        # Initial HTML defers data loading to JS; every lane starts empty.
        self.assertTrue(all("is-empty" in classes for classes in parser.lanes.values()))
        self.assertIn(".col.is-empty .empty-col{", board.STYLE)
        self.assertIn("classList.toggle('is-empty', empty)", board.BOARD)

    def test_all_empty_lanes(self):
        data = fixture_cards()
        data["cols"]["human_review"] = ""
        data["counts"]["human_review"] = 0
        data["total"] = 0
        with patch.object(board, "build_cards", return_value=data):
            parser = LaneParser()
            parser.feed(board.issues_page())
        self.assertTrue(all(classes == ["col", "is-empty"] for classes in parser.lanes.values()))

    def test_homepage_defers_loading(self):
        with patch.object(board, "build_cards",
                          side_effect=AssertionError("unexpected synchronous load")), patch.object(
            board, "scan_sessions", return_value={}), patch.object(
            board, "board_tasks", return_value=[]):
            self.assertIn("正在加载任务", board.issues_page())

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

    def test_state_sync_failure_and_staleness_are_explicit(self):
        for data in ({"started": True, "at": 0},
                     {"started": True, "at": 1},
                     {"started": True, "at": board.time.time(), "failed": True}):
            with patch.object(board, "_state_cache", data):
                self.assertIn("同步异常", board.state_sync_error())
        with patch.object(board, "_state_cache", {"started": True, "at": board.time.time()}):
            self.assertEqual(board.state_sync_error(), "")

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
            board._message_cache.clear()

    def test_scheduler_request_sends_bearer_when_configured(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b"{}"

        def open_spy(request, timeout=10):
            captured["auth"] = request.headers.get("Authorization")
            return Response()

        opener = type("O", (), {"open": staticmethod(open_spy)})
        with patch.object(board, "API_TOKEN", "secret"), patch.object(
            board.urllib.request, "build_opener", return_value=opener()
        ):
            board.scheduler_request("PATCH", "/api/v1/tasks/1", {"state": "Done"})
        self.assertEqual(captured["auth"], "Bearer secret")

    def test_source_fetch_timeout_covers_proxied_github_calls(self):
        seen = {}

        def fake(method, path, body=None, timeout=10):
            seen["timeout"] = timeout
            return {"issue": {"number": 4}}

        with patch.object(board, "scheduler_request", fake):
            board.fetch_task_source(4)
        # Scheduler-side source proxying chains sequential GitHub calls
        # (issue + comment pages + optional PR); observed real latency ~19s.
        self.assertGreaterEqual(seen["timeout"], 45)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        state = patch.object(board, "symphony_state", return_value={})
        state.start()
        self.addCleanup(state.stop)
        refresh = patch.object(board, "request_scheduler_refresh", return_value=True)
        self.refresh_mock = refresh.start()
        self.addCleanup(refresh.stop)
        self.cards = patch.object(board, "build_cards", side_effect=fixture_cards)
        self.cards.start()
        self.addCleanup(self.cards.stop)
        self.server = board.ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None, drain_lock=True):
        # The threaded server releases _task_lock just after the response;
        # drain it first so sequential requests never see a spurious 409.
        if drain_lock:
            deadline = board.time.time() + 5
            while not board._task_lock.acquire(blocking=False):
                if board.time.time() > deadline:
                    raise AssertionError("task lock never released")
                board.time.sleep(0.01)
            board._task_lock.release()
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def safe_request(self, *args, **kwargs):
        # Windows occasionally aborts a connection while the handler thread
        # is finishing; these calls are always rejected before any mutation.
        try:
            return self.request(*args, **kwargs)
        except (ConnectionAbortedError, ConnectionResetError):
            return self.request(*args, **kwargs)

    def test_index_and_api(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Issue 工作台", body)
        status, body = self.request("GET", "/api/issues")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["counts"]["human_review"], 1)
        self.assertEqual(payload["counts"]["todo"], 0)
        document = self.request("GET", "/")[1]
        self.assertIn("refreshIssues()", document)

    def test_api_source_proxies_through_scheduler(self):
        source = {"issue": {"number": 1, "title": "T"}, "workpad": None, "pull_request": None}
        with patch.object(board, "fetch_task_source", return_value=source) as fetch:
            status, body = self.request("GET", "/api/source/GH-1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["issue"]["title"], "T")
        fetch.assert_called_once_with(1)
        self.assertEqual(self.request("GET", "/api/source/bad")[0], 400)
        with patch.object(board, "fetch_task_source", return_value=None):
            self.assertEqual(self.request("GET", "/api/source/GH-1")[0], 502)

    def test_move_forwards_state_and_revision(self):
        calls = []

        def fake_patch(number, fields, expected_revision):
            calls.append((number, fields, expected_revision))
            return {"task": {"state": "Merging", "revision": 4}}

        with patch.object(board, "request_task_patch", side_effect=fake_patch):
            status, body = self.request(
                "POST", "/api/task",
                json.dumps({"issue": "GH-1", "op": "move",
                            "state": "Merging", "expected_revision": 3}))
        self.assertEqual(status, 200)
        self.assertEqual(calls, [(1, {"state": "Merging"}, 3)])
        payload = json.loads(body)
        self.assertEqual(payload["state"], "Merging")
        self.assertTrue(payload["refresh_queued"])

    def test_comment_is_posted_before_state_change(self):
        order = []

        def comment(number, body):
            order.append(("comment", body))
            return {"comment": {"id": 1}}

        def patch_(number, fields, rev):
            order.append(("patch", fields))
            return {"task": {"state": "Rework", "revision": 2}}

        with patch.object(board, "request_task_comment", side_effect=comment), patch.object(
            board, "request_task_patch", side_effect=patch_
        ):
            status, _ = self.request(
                "POST", "/api/task",
                json.dumps({"issue": "GH-1", "op": "move", "state": "Rework",
                            "comment": "请修复边界", "expected_revision": 1}))
        self.assertEqual(status, 200)
        self.assertEqual(order, [("comment", "请修复边界"), ("patch", {"state": "Rework"})])

    def test_comment_only_op_never_patches(self):
        with patch.object(board, "request_task_comment", return_value={"comment": {"id": 1}}) as c, patch.object(
            board, "request_task_patch",
            side_effect=AssertionError("patch must not run")
        ):
            status, body = self.request(
                "POST", "/api/task",
                json.dumps({"issue": "GH-1", "op": "comment", "comment": "备注"}))
        self.assertEqual(status, 200)
        c.assert_called_once_with(1, "备注")

    def test_revision_conflict_reports_comment_posted(self):
        def comment(number, body):
            return {"comment": {"id": 1}}

        def patch_(number, fields, rev):
            raise board.urllib.error.HTTPError(
                "http://x", 409, "conflict", {},
                __import__("io").BytesIO(b'{"error":{"code":"revision_conflict"}}'))

        with patch.object(board, "request_task_comment", side_effect=comment), patch.object(
            board, "request_task_patch", side_effect=patch_
        ):
            status, body = self.request(
                "POST", "/api/task",
                json.dumps({"issue": "GH-1", "op": "move", "state": "Done",
                            "comment": "先留言", "expected_revision": 1}))
        self.assertEqual(status, 409)
        payload = json.loads(body)
        self.assertEqual(payload["code"], "revision_conflict")
        self.assertTrue(payload["comment_posted"])

    def test_move_validates_target_and_identity(self):
        bad = (
            {"issue": "GH-1", "op": "move", "state": "bogus"},
            {"issue": "GH-1", "op": "move"},
            {"issue": "GH-1", "op": "comment"},
            {"issue": "GH-1", "op": "bogus"},
            {"issue": "jira-1", "op": "move", "state": "Done"},
            {"issue": "GH-x", "op": "move", "state": "Done"},
        )
        for payload in bad:
            status, _ = self.request("POST", "/api/task", json.dumps(payload))
            self.assertEqual(status, 400, payload)

    def test_write_rejects_cross_site_and_non_object(self):
        status, body = self.safe_request(
            "POST", "/api/task", "{}", {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("跨站", json.loads(body)["error"])
        for body in ('"text"', "[]", "123", "null", "true"):
            status, payload = self.safe_request(
                "POST", "/api/task", body, {"Content-Type": "application/json"})
            self.assertEqual(status, 400, body)

    def test_outcomes_distinguish_rejection_and_uncertain(self):
        member_error = board.urllib.error.HTTPError("http://x", 403, "denied", {}, None)
        for error, outcome in [
            (member_error, (403, "not_applied")),
            (board.urllib.error.HTTPError("http://x", 502, "gw", {}, None), (502, "uncertain")),
            (TimeoutError("timeout"), (502, "uncertain")),
        ]:
            with self.subTest(outcome=outcome), patch.object(
                board, "request_task_patch", side_effect=error
            ):
                status, body = self.request(
                    "POST", "/api/task",
                    json.dumps({"issue": "GH-1", "op": "move", "state": "Todo",
                                "expected_revision": 1}))
                self.assertEqual(status, outcome[0])
                self.assertEqual(json.loads(body)["outcome"], outcome[1])

    def test_task_rejects_concurrent_submit(self):
        captured = []
        handler = SimpleNamespace(
            write_guard=lambda: True,
            send_json=lambda status, payload: captured.append((status, payload)),
        )
        with board._task_lock:
            board.Handler.route_post(handler, board.urlparse("/api/task"))
        self.assertEqual(captured[0][0], 409)

    def test_completed_and_history_redirect(self):
        with patch.object(board, "board_tasks", return_value=[]):
            status, body = self.request("GET", "/api/completed")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["issues"], [])
        self.assertEqual(self.request("GET", "/history")[0], 302)
        document = self.request("GET", "/")[1]
        self.assertIn("data-view='today'", document)
        self.assertIn("id='completed-panel'", document)
        self.assertIn("id='cstate'", document)

    def test_invalid_run_reference_and_unknown_route(self):
        self.assertEqual(self.request("GET", "/api/run/GH-1")[0], 400)
        self.assertEqual(self.request("GET", "/not-found")[0], 404)


if __name__ == "__main__":
    unittest.main()
