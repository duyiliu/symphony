"""Task record store edge cases: backups, field preservation, seeding."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.tasks import store  # noqa: E402


@pytest.fixture
def task_store(tmp_path):
    return store.Store(SimpleNamespace(
        kind="board", required_labels=[],
        provider={"repo": "o/r", "token": "t",
                  "state_dir": str(tmp_path)},
        active_states=[], terminal_states=[]))


def _seed(task_store, number, **fields):
    record = {
        "schema_version": store.SCHEMA_VERSION, "repo": "o/r",
        "issue_number": number, "identifier": "GH-%d" % number,
        "state": "Backlog", "revision": 1,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    record.update(fields)
    task_store.locked(number, lambda _r, save: save(record))
    return record


def test_write_keeps_dot_bak_of_previous_record(task_store):
    _seed(task_store, 1, state="Todo")
    task_store.update(1, {"state": "In Progress"}, expected_revision=1)
    backup = task_store.path_for(1) + ".bak"
    assert os.path.exists(backup)
    with open(backup, "rb") as handle:
        assert b'"state": "Todo"' in handle.read()


def test_update_preserves_untouched_pointer_fields(task_store):
    _seed(task_store, 1, state="In Progress", pr_number=9,
          pr_url="https://github.com/o/r/pull/9", branch_name="gh-1",
          workpad_comment_id=7, title="cached")
    moved = task_store.update(1, {"state": "Human Review"},
                              expected_revision=1)
    assert moved["pr_number"] == 9
    assert moved["branch_name"] == "gh-1"
    assert moved["workpad_comment_id"] == 7
    assert moved["title"] == "cached"


def test_history_appends_and_tracks_actor(task_store):
    _seed(task_store, 1, state="Backlog")
    task_store.update(1, {"state": "Todo"}, expected_revision=1, by="board")
    moved = task_store.update(1, {"state": "In Progress"},
                              expected_revision=2, by="agent")
    hops = [(h["from"], h["to"], h["by"]) for h in moved["history"]]
    assert hops == [("Backlog", "Todo", "board"),
                    ("Todo", "In Progress", "agent")]


def test_non_state_updates_do_not_extend_history(task_store):
    _seed(task_store, 1, state="In Progress")
    moved = task_store.update(1, {"pr_number": 3}, expected_revision=1)
    assert moved["revision"] == 2
    assert len(moved.get("history") or []) == 0


def test_corrupt_record_raises_not_silent_default(task_store):
    path = task_store.path_for(9)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(9)
    assert ctx.value.code == "invalid_task_record"


def test_non_dict_record_raises(task_store):
    path = task_store.path_for(9)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("[1, 2]")
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(9)
    assert ctx.value.code == "invalid_task_record"


def test_locked_save_stamps_repo_and_updated_at(task_store):
    record = task_store.locked(
        3, lambda _r, save: save({"state": "Backlog", "revision": 1}))
    assert record["repo"] == "o/r"
    assert record["updated_at"]
    with open(task_store.path_for(3), encoding="utf-8") as handle:
        assert json.load(handle)["repo"] == "o/r"
