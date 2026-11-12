import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "symphony_board_test", Path(__file__).resolve().parents[2] / "board" / "server.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


def test_agent_without_session_does_not_reactivate_last_session():
    run = {"id": "old-session", "mtime": 100}
    assert not board.session_active(run, {"session_id": None}, {}, 101)


def test_only_matching_session_is_active():
    run = {"id": "session-a", "mtime": 100}
    assert board.session_active(run, {"session_id": "session-a"}, {}, 200)
    assert not board.session_active(run, {"session_id": "session-b"}, {}, 101)
    assert not board.session_active(run, None, {}, 101)


@pytest.mark.parametrize("agent,state,now,expected", [
    ({"session_id": "session-a"}, {}, 200, True),
    ({"session_id": "session-a"}, {}, 101, True),
    ({"session_id": None}, {}, 101, False),
    ({"session_id": "session-b"}, {}, 101, False),
    (None, {}, 101, False),
    (None, None, 114, True),
    (None, None, 115, False),
])
def test_session_status_edges(agent, state, now, expected):
    assert board.session_active({"id": "session-a", "mtime": 100}, agent, state, now) is expected


def test_runs_api_keeps_history_and_marks_only_matching_session(monkeypatch):
    runs = [
        {"id": "old", "short": "old", "started": "first", "mtime": 100, "size": 10, "messages": 1},
        {"id": "new", "short": "new", "started": "second", "mtime": 200, "size": 20, "messages": 2},
    ]
    monkeypatch.setattr(board, "scan_sessions", lambda: {"GH-147": runs})
    monkeypatch.setattr(board, "symphony_state", lambda: {
        "running": [{"issue_identifier": "GH-147", "session_id": "other"}]})
    monkeypatch.setattr(board, "load_runtime", lambda: {})
    captured = []
    handler = SimpleNamespace(send_json=lambda status, payload: captured.append((status, payload)))
    board.Handler.handle_runs(handler, "GH-147")
    status, payload = captured[0]
    assert status == 200
    assert [r["short"] for r in payload["runs"]] == ["new", "old"]
    assert [r["started"] for r in payload["runs"]] == ["second", "first"]
    assert all(not r["active"] for r in payload["runs"])
    assert all(r["ended"] for r in payload["runs"])


def test_runs_api_rejects_invalid_issue():
    captured = []
    handler = SimpleNamespace(send_json=lambda status, payload: captured.append((status, payload)))
    board.Handler.handle_runs(handler, "../invalid")
    assert captured[0][0] == 400


def test_homepage_does_not_wait_for_scheduler(monkeypatch):
    def fail_if_called():
        raise AssertionError("homepage must not fetch cards synchronously")
    monkeypatch.setattr(board, "build_cards", fail_if_called)
    monkeypatch.setattr(board, "board_tasks", lambda: [])
    html = board.issues_page()
    assert "正在加载任务" in html
    assert "id='board'" in html
    assert "refreshIssues()" in html
