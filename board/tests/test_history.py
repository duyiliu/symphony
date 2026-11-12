"""Completed views are fed by local terminal task records, not GitHub reads."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("completed_board", Path(__file__).parents[1] / "server.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


def record(number, state, **extra):
    base = {
        "issue_number": number,
        "identifier": "GH-%d" % number,
        "state": state,
        "revision": 1,
        "repo": "o/r",
        "title": "Task %d" % number,
        "url": "https://github.com/o/r/issues/%d" % number,
        "native_state": "open",
        "updated_at": "2026-10-27T01:00:00Z",
    }
    base.update(extra)
    return base


class CompletedTests(unittest.TestCase):
    def test_only_terminal_states_are_returned(self):
        now = board.datetime.now(board.CN).isoformat()
        tasks = [
            record(1, "Done", updated_at=now),
            record(2, "Closed", updated_at=now),
            record(3, "Cancelled", updated_at=now),
            record(4, "Canceled", updated_at=now),
            record(5, "Duplicate", updated_at=now),
            record(6, "Todo"),
            record(7, "Human Review"),
            record(8, "Merging"),
        ]
        with patch.object(board, "board_tasks", return_value=tasks):
            data = board.completed_data()
        self.assertEqual(sorted(i["number"] for i in data["issues"]), [1, 2, 3, 4, 5])
        self.assertFalse(data["warning"])

    def test_completion_is_sorted_newest_first_and_day_bucketed(self):
        tasks = [
            record(1, "Done", updated_at="2026-10-25T01:00:00Z"),
            record(2, "Done", updated_at="2026-10-27T01:00:00Z"),
        ]
        with patch.object(board, "board_tasks", return_value=tasks):
            data = board.completed_data()
        self.assertEqual([i["number"] for i in data["issues"]], [2, 1])
        self.assertEqual(data["issues"][0]["day"], "2026-10-27")
        self.assertEqual(data["issues"][1]["day"], "2026-10-25")

    def test_merged_pr_comes_from_the_record_not_a_rescan(self):
        tasks = [
            record(1, "Done", pr_number=153, pr_url="https://github.com/o/r/pull/153"),
            record(2, "Done"),
        ]
        with patch.object(board, "board_tasks", return_value=tasks):
            data = board.completed_data()
        self.assertEqual(data["issues"][0]["merged_pr"],
                         {"number": 153, "url": "https://github.com/o/r/pull/153"})
        self.assertIsNone(data["issues"][1]["merged_pr"])

    def test_missing_or_bad_timestamps_are_skipped(self):
        tasks = [record(1, "Done", updated_at=""), record(2, "Done", updated_at="garbage")]
        with patch.object(board, "board_tasks", return_value=tasks):
            self.assertEqual(board.completed_data()["issues"], [])


if __name__ == "__main__":
    unittest.main()
