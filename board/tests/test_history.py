"""Completed issues exclude deliveries, failures, abandoned issues and PRs."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("completed_board", Path(__file__).parents[1] / "server.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


class CompletedTests(unittest.TestCase):
    def setUp(self):
        self.now = board.datetime.now(board.CN).isoformat()
        self.closed = {"number": 1, "title": "Done <script>", "closed_at": self.now,
                       "html_url": "https://github.com/o/r/issues/1", "state": "closed", "state_reason": "completed"}
        board._history_cache.update(repo=None, events=None, at=0)

    def test_only_completed_issues_are_returned_and_cached(self):
        rows = [self.closed, {**self.closed, "number": 2, "pull_request": {}},
                {**self.closed, "number": 3, "state_reason": "not_planned"},
                {**self.closed, "number": 4, "state": "open"}]
        with patch.object(board, "load_tracker", return_value=("o/r", "secret")), patch.object(
            board, "github_request", return_value=rows
        ) as api, patch.object(board, "scan_sessions", side_effect=AssertionError("not sessions")):
            data = board.completed_data()
            self.assertEqual([e["number"] for e in data["issues"]], [1])
            self.assertEqual(data["issues"][0]["title"], "Done <script>")
            self.assertFalse(data["warning"])
            board.completed_data()
            self.assertEqual(api.call_count, 1)
            self.assertIn("state=closed", api.call_args.args[1])
            self.assertNotIn("comments", api.call_args.args[1])

    def test_pagination_and_sort_by_completion_not_update(self):
        first = [{**self.closed, "number": n, "closed_at": "2020-01-01T01:00:00Z"} for n in range(1, 101)]
        second = [{**self.closed, "number": 101}]
        with patch.object(board, "load_tracker", return_value=("o/r", "secret")), patch.object(
            board, "github_request", side_effect=[first, second]
        ) as api:
            data = board.completed_data()
            self.assertEqual(len(data["issues"]), 101)
            self.assertEqual(data["issues"][0]["number"], 101)
            self.assertIn("page=2", api.call_args.args[1])

    def test_api_failure_preserves_cache_and_shows_warning(self):
        board._history_cache.update(repo="o/r", events=[{"number": 1, "at": self.now}], at=0)
        with patch.object(board, "load_tracker", return_value=("o/r", "secret")), patch.object(
            board, "github_request", side_effect=OSError("offline")
        ):
            data = board.completed_data()
            self.assertIn("读取失败", data["warning"])
            self.assertEqual(len(data["issues"]), 1)

    def test_beijing_completion_day(self):
        class Clock(board.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 27, 1, tzinfo=board.CN)
        events = [{**self.closed, "at": "2026-10-26T17:00:00Z"}]
        with patch.object(board, "datetime", Clock), patch.object(board, "github_history", return_value=(events, "")):
            data = board.completed_data()
            self.assertEqual(data["today"], "2026-10-27")
            self.assertEqual(data["issues"][0]["day"], "2026-10-27")


if __name__ == "__main__":
    unittest.main()
