"""Board tracker: store, importer, migration, tools, adapter, facade."""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import tracker  # noqa: E402
from symphony.github import agent_tool, client as gh_client  # noqa: E402
from symphony.issue import Issue  # noqa: E402
from symphony.tasks import (adapter, importer, migrate, store,  # noqa: E402
                            tools)


def _settings(tmp_path, kind="board", **provider_over):
    provider = {"repo": "o/r", "token": "t", "state_dir": str(tmp_path)}
    provider.update(provider_over)
    return SimpleNamespace(
        kind=kind, provider=provider, required_labels=[],
        active_states=["Todo", "In Progress", "Merging", "Rework"],
        terminal_states=["Closed", "Cancelled", "Canceled", "Duplicate",
                         "Done"])


def _gh_settings(tmp_path):
    settings = _settings(tmp_path, kind="github")
    return settings


def _open_issue(number, **over):
    payload = {
        "number": number, "state": "open", "title": "T%d" % number,
        "body": "b", "html_url": "https://github.com/o/r/issues/%d" % number,
        "labels": [],
    }
    payload.update(over)
    return payload


def _seed(task_store, number, state="Todo", **fields):
    record = {
        "schema_version": store.SCHEMA_VERSION, "repo": "o/r",
        "issue_number": number, "identifier": "GH-%d" % number,
        "state": state, "revision": 1,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }
    record.update(fields)
    task_store.locked(number, lambda _r, save: save(record))
    return record


# --- state model ------------------------------------------------------------

def test_canonical_state_names():
    assert store.canonical_state("todo") == "Todo"
    assert store.canonical_state("in progress") == "In Progress"
    assert store.canonical_state("In_Progress") == "In Progress"
    assert store.canonical_state("Human Review") == "Human Review"
    for alias in ("Closed", "Cancelled", "Canceled", "Duplicate", "Done"):
        assert store.canonical_state(alias) == alias
    assert store.canonical_state("bogus") is None
    assert store.canonical_state(5) is None


def test_state_values_cover_seven_plus_aliases():
    for name in ("Backlog", "Todo", "In Progress", "Human Review",
                 "Merging", "Rework", "Done", "Closed", "Cancelled",
                 "Canceled", "Duplicate"):
        assert name in store.STATE_VALUES


# --- store: settings / $ENV ---------------------------------------------------

def test_validate_settings_resolves_env_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKS_DIR_X", str(tmp_path))
    settings = _settings(tmp_path, state_dir="$TASKS_DIR_X")
    store.validate_settings(settings)  # must not raise


def test_validate_settings_rejects_relative_or_shallow():
    for bad in ("relative/dir", "/root", "", None, "C:"):
        settings = SimpleNamespace(
            provider={"repo": "o/r", "state_dir": bad})
        with pytest.raises(store.TaskError) as ctx:
            store.validate_settings(settings)
        assert ctx.value.code == "invalid_state_dir"


def test_validate_settings_requires_repo(tmp_path):
    settings = _settings(tmp_path, repo="")
    with pytest.raises(store.TaskError) as ctx:
        store.validate_settings(settings)
    assert ctx.value.code == "missing_repo"


# --- store: writes / revision ------------------------------------------------

def test_upsert_source_creates_backlog_revision_1(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    record = task_store.upsert_source(7, importer.source_fields(_open_issue(7)))
    assert record["state"] == "Backlog"
    assert record["revision"] == 1
    assert record["identifier"] == "GH-7"
    assert record["title"] == "T7"
    assert record["native_state"] == "open"
    assert record["history"][0]["to"] == "Backlog"


def test_update_bumps_revision_and_records_history(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1, state="Backlog")
    moved = task_store.update(1, {"state": "Todo"}, expected_revision=1)
    assert moved["state"] == "Todo"
    assert moved["revision"] == 2
    assert moved["history"][-1]["from"] == "Backlog"
    assert moved["history"][-1]["to"] == "Todo"


def test_revision_conflict_rejects_stale_write(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1)
    task_store.update(1, {"state": "In Progress"}, expected_revision=1)
    with pytest.raises(store.TaskError) as ctx:
        task_store.update(1, {"state": "Done"}, expected_revision=1)
    assert ctx.value.code == "revision_conflict"
    assert task_store.get(1)["state"] == "In Progress"


def test_idempotent_repeat_succeeds_despite_stale_revision(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1)
    task_store.update(1, {"state": "In Progress"}, expected_revision=1)
    # Same values + stale revision: idempotent, no write/no bump.
    same = task_store.update(1, {"state": "In Progress"}, expected_revision=1)
    assert same["revision"] == 2


def test_missing_expected_revision_rejected(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1)
    for bad in (None, "1", True):
        with pytest.raises(store.TaskError) as ctx:
            task_store.update(1, {"state": "Done"}, expected_revision=bad)
        assert ctx.value.code == "missing_expected_revision"


def test_update_unknown_or_invalid_fields_rejected(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1)
    with pytest.raises(store.TaskError) as ctx:
        task_store.update(1, {"title": "x"}, expected_revision=1)
    assert ctx.value.code == "invalid_fields"
    with pytest.raises(store.TaskError) as ctx:
        task_store.update(1, {"state": "bogus"}, expected_revision=1)
    assert ctx.value.code == "invalid_state"
    with pytest.raises(store.TaskError) as ctx:
        task_store.update(1, {"pr_number": -2}, expected_revision=1)
    assert ctx.value.code == "invalid_fields"


def test_update_missing_record_is_task_not_found(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    with pytest.raises(store.TaskError) as ctx:
        task_store.update(99, {"state": "Todo"}, expected_revision=1)
    assert ctx.value.code == "task_not_found"


def test_refresh_source_never_touches_business_state(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1, state="In Progress")
    refreshed = task_store.refresh_source(1, {
        "title": "new", "native_state": "closed", "labels": ["x"]})
    assert refreshed["title"] == "new"
    assert refreshed["native_state"] == "closed"
    assert refreshed["state"] == "In Progress"
    assert refreshed["revision"] == 1


def test_repo_mismatch_isolation(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1)
    other = store.Store(_settings(tmp_path, repo="other/repo"))
    records, errors = other.list()
    assert records == []
    assert errors and errors[0]["error"] == "receipt_repository_mismatch"
    with pytest.raises(store.TaskError) as ctx:
        other.locked(1, lambda r, s: r)
    assert ctx.value.code == "receipt_repository_mismatch"


def test_list_skips_bak_tmp_and_reports_corrupt(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1)
    _seed(task_store, 2, state="Done")
    with open(os.path.join(str(tmp_path), "3.json"), "w") as handle:
        handle.write("{not json")
    with open(os.path.join(str(tmp_path), "9.json.bak"), "w") as handle:
        handle.write("{}")
    with open(os.path.join(str(tmp_path), "9.json.tmp"), "w") as handle:
        handle.write("{}")
    records, errors = task_store.list()
    assert sorted(r["issue_number"] for r in records) == [1, 2]
    assert [e["issue_number"] for e in errors] == [3]


# --- importer -----------------------------------------------------------------

def _page_result(rows):
    return {"status": 200, "body": rows}


def test_import_open_issues_paginates_and_skips_prs(tmp_path):
    settings = _settings(tmp_path)
    pages = [
        [_open_issue(i) for i in range(1, 101)],
        [_open_issue(101), _open_issue(
            102, pull_request={"url": "x"}), _open_issue(103)],
    ]
    calls = []

    def fake(method, path, params, body, settings_):
        calls.append(params["page"])
        return _page_result(pages[params["page"] - 1])

    with patch.object(gh_client, "request", fake):
        total, created, refreshed = importer.import_open_issues(settings)
    assert calls == [1, 2]
    assert total == 103
    assert created == 102  # 100 + #101 + #103; PR #102 skipped
    assert refreshed == 0
    task_store = store.Store(settings)
    assert task_store.get(102) is None
    assert task_store.get(103)["state"] == "Backlog"


def test_import_failure_preserves_existing_records(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 5, state="In Progress")

    def boom(method, path, params, body, settings_):
        return {"status": 500, "body": {}}

    with patch.object(gh_client, "request", boom):
        with pytest.raises(gh_client.GitHubError):
            importer.import_open_issues(settings)
    assert task_store.get(5)["state"] == "In Progress"


def test_import_refreshes_source_without_state_change(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1, state="In Progress")

    def fake(method, path, params, body, settings_):
        return _page_result([_open_issue(1, title="renamed")])

    with patch.object(gh_client, "request", fake):
        importer.import_open_issues(settings)
    record = task_store.get(1)
    assert record["title"] == "renamed"
    assert record["state"] == "In Progress"
    assert record["revision"] == 1


def test_import_closed_issue_by_number_imports_closed(tmp_path):
    settings = _settings(tmp_path)

    def fake(method, path, params, body, settings_):
        return {"status": 200, "body": _open_issue(9, state="closed")}

    with patch.object(gh_client, "request", fake):
        record = importer.import_issue_by_number(settings, 9)
    assert record["state"] == "Closed"
    assert record["native_state"] == "closed"


def test_import_by_number_404_and_pr_rejected(tmp_path):
    settings = _settings(tmp_path)

    def fake404(method, path, params, body, settings_):
        return {"status": 404, "body": {}}

    with patch.object(gh_client, "request", fake404):
        with pytest.raises(store.TaskError) as ctx:
            importer.import_issue_by_number(settings, 9)
        assert ctx.value.code == "issue_not_found"

    def fake_pr(method, path, params, body, settings_):
        return {"status": 200,
                "body": _open_issue(9, pull_request={"url": "x"})}

    with patch.object(gh_client, "request", fake_pr):
        with pytest.raises(store.TaskError) as ctx:
            importer.import_issue_by_number(settings, 9)
        assert ctx.value.code == "not_an_issue"


def test_pagination_limit_fails_closed(tmp_path):
    settings = _settings(tmp_path)

    def full(method, path, params, body, settings_):
        return _page_result([_open_issue(i) for i in range(100)])

    with patch.object(gh_client, "request", full), \
            patch.object(importer, "MAX_PAGES", 2):
        with pytest.raises(gh_client.GitHubError) as ctx:
            importer.import_open_issues(settings)
        assert "pagination_limit" in str(ctx.value)


# --- migration ----------------------------------------------------------------

def _v1(number, **fields):
    record = {"repo": "o/r", "state": "todo"}
    record.update(fields)
    return record


def _write_v1(directory, number, record):
    with open(os.path.join(str(directory), "%d.json" % number), "w",
              encoding="utf-8") as handle:
        json.dump(record, handle)


def test_migrate_maps_v1_states_and_preserves_links(tmp_path):
    _write_v1(tmp_path, 1, _v1(
        1, state="review",
        delivery={"published": True, "pr": 22, "sha": "a" * 40,
                  "branch": "gh-1"},
        workpad_comment_id=555))
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert row["action"] == "migrated:Human Review"
    task_store = store.Store(_settings(tmp_path))
    record = task_store.get(1)
    assert record["state"] == "Human Review"
    assert record["pr_number"] == 22
    assert record["pr_head_sha"] == "a" * 40
    assert record["branch_name"] == "gh-1"
    assert record["workpad_comment_id"] == 555
    # Whole-dir backup exists and holds the original record.
    backup = next(r for r in report if r["action"] == "backup")
    assert os.path.isdir(backup["path"])
    with open(os.path.join(backup["path"], "1.json")) as handle:
        assert json.load(handle)["state"] == "review"


@pytest.mark.parametrize("stage,expected", [
    ("handing_off", "Human Review"), ("awaiting", "Human Review"),
    ("waiting_checks", "Human Review"), ("waiting_review", "Human Review"),
    ("waiting_update", "Human Review"), ("checking", "Human Review"),
    ("merging", "Human Review"), ("finalizing", "Human Review"),
    ("merged", "Done"), ("closed", "Closed"),
    ("superseded", "Todo"), ("repair_pending", "Todo"),
    ("requeue_pending", "Todo"),
])
def test_migrate_legacy_stage_table(tmp_path, stage, expected):
    _write_v1(tmp_path, 1, {"repo": "o/r", "host": {"stage": stage}})
    migrate.migrate(str(tmp_path), "o/r", apply=True)
    record = store.Store(_settings(tmp_path)).get(1)
    assert record["state"] == expected


def test_migrate_pending_merge_goes_human_review_unverified(tmp_path):
    _write_v1(tmp_path, 1, _v1(
        1, state="review", delivery={"published": True, "pr": 3},
        pending_merge=True))
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert row["action"].startswith("migrated:Human Review")
    assert "pending_merge_unverified" in row["action"]
    assert row["verify"] is True


def test_migrate_confirmed_merged_stays_done(tmp_path):
    _write_v1(tmp_path, 1, {"repo": "o/r", "merged": True,
                          "pending_merge": True})
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert row["action"] == "migrated:Done"
    assert row["verify"] is False


def test_migrate_blocked_and_approved_sha_are_reported_not_policy(tmp_path):
    _write_v1(tmp_path, 1, _v1(
        1, state="review", blocked=True,
        review={"approved_sha": "a" * 40}))
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert "was_blocked" in row["action"]
    assert "had_approved_sha" in row["action"]
    record = store.Store(_settings(tmp_path)).get(1)
    assert record["state"] == "Human Review"
    assert "approved_sha" not in record  # old approval ≠ new merge auth


def test_migrate_ignores_bak_tmp_and_reports_damage(tmp_path):
    _write_v1(tmp_path, 1, _v1(1))
    _write_v1(tmp_path, 4, "not-a-dict")
    with open(os.path.join(str(tmp_path), "1.json.bak"), "w") as handle:
        handle.write('{"stale": true}')
    with open(os.path.join(str(tmp_path), "2.json.tmp"), "w") as handle:
        handle.write("{broken")
    with open(os.path.join(str(tmp_path), "3.json"), "w") as handle:
        handle.write("[1,2]")
    _write_v1(tmp_path, 5, {"repo": "other/x", "state": "todo"})
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    rows = {r["issue_number"]: r for r in report
            if "issue_number" in r}
    assert rows[1]["action"] == "migrated:Todo"
    assert rows[3]["action"] == "skipped"
    assert "repo_mismatch" in rows[5]["reason"]
    assert rows[4]["action"] == "skipped"
    task_store = store.Store(_settings(tmp_path))
    with pytest.raises(store.TaskError):
        task_store.get(4)  # damaged record preserved verbatim, not migrated
    # .bak/.tmp were never treated as records
    assert task_store.get(2) is None
    assert task_store.get(1)["state"] == "Todo"


def test_migrate_dry_run_writes_nothing(tmp_path):
    _write_v1(tmp_path, 1, _v1(1, state="done"))
    report = migrate.migrate(str(tmp_path), "o/r", apply=False)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert row["action"] == "migrated:Done"
    with open(os.path.join(str(tmp_path), "1.json")) as handle:
        assert json.load(handle)["state"] == "done"
    assert not any(r["action"] == "backup" for r in report)


def test_migrate_already_v2_is_kept(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _seed(task_store, 1, state="Rework")
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert row["action"] == "kept"
    assert task_store.get(1)["state"] == "Rework"


def test_migrate_unknown_state_is_reported(tmp_path):
    _write_v1(tmp_path, 1, {"repo": "o/r", "state": "mystery"})
    report = migrate.migrate(str(tmp_path), "o/r", apply=True)
    row = next(r for r in report if r.get("issue_number") == 1)
    assert row["action"] == "skipped"
    assert "unknown_state" in row["reason"]


def test_migrate_v1_state_view_precedence(tmp_path):
    # delivery.published beats attempts/cycle; attempts beat bare backlog.
    assert migrate._v1_state_view(
        {"delivery": {"published": True}, "attempts": 2}) == "review"
    assert migrate._v1_state_view({"attempts": 1}) == "todo"
    assert migrate._v1_state_view({}) == "backlog"
    assert migrate._v1_state_view({"merged": True}) == "done"


# --- task_api tool ------------------------------------------------------------

def test_task_api_get_update_list(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1)
    got = tools.execute(settings, {"action": "get", "issue": "GH-1"})
    assert got["task"]["state"] == "Todo"
    assert got["task"]["revision"] == 1
    updated = tools.execute(settings, {
        "action": "update", "issue": "1", "expected_revision": 1,
        "state": "In Progress", "branch_name": "gh-1", "pr_number": 4,
        "pr_url": "https://github.com/o/r/pull/4"})
    assert updated["task"]["state"] == "In Progress"
    assert updated["task"]["pr_number"] == 4
    listed = tools.execute(
        settings, {"action": "list", "states": ["In Progress"]})
    assert [t["issue_number"] for t in listed["tasks"]] == [1]


def test_task_api_update_requires_revision_and_valid_issue(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1)
    missing = tools.execute(settings, {
        "action": "update", "issue": "1", "state": "Done"})
    assert missing["error"]["code"] == "missing_expected_revision"
    conflict = tools.execute(settings, {
        "action": "update", "issue": "1", "expected_revision": 9,
        "state": "Done"})
    assert conflict["error"]["code"] == "revision_conflict"
    assert tools.execute(settings, {"action": "get", "issue": "x"})[
        "error"]["code"] == "invalid_issue"
    assert tools.execute(settings, {"action": "get", "issue": "77"})[
        "error"]["code"] == "task_not_found"


def test_task_api_not_exposed_on_native_github(tmp_path):
    settings = _gh_settings(tmp_path)
    result = agent_tool.execute_agent_tool(
        "task_api", {"action": "list"}, settings)
    assert result["error"]["code"] == "unknown_tool"
    names = [s["name"] for s in agent_tool.tool_specs(settings)]
    assert names == ["github_api"]
    board_names = [s["name"] for s in
                   agent_tool.tool_specs(_settings(tmp_path))]
    assert set(board_names) == {"github_api", "task_api"}


# --- github_api tool / policy -------------------------------------------------

def _fake_ok(method, path, params, body, settings_):
    return {"status": 200, "body": {"ok": True}}


def test_github_api_requires_relative_path(tmp_path):
    settings = _settings(tmp_path)
    for bad in ("https://api.github.com/repos/o/r/issues/1",
                "repos/o/r/issues/1", "ftp://x", ""):
        result = agent_tool.execute_agent_tool(
            "github_api", {"method": "GET", "path": bad}, settings)
        assert result["error"]["code"] == "invalid_arguments", bad


def test_github_api_allowlist(tmp_path):
    settings = _settings(tmp_path)
    repo = "/repos/o/r"
    allowed = [
        ("GET", "%s/issues/1/comments" % repo, None),
        ("GET", "%s/commits/abc/check-runs" % repo, None),
        ("GET", "%s/actions/jobs/9/logs" % repo, None),
        ("POST", "%s/issues/1/comments" % repo, {"body": "hi"}),
        ("POST", "%s/pulls/1/comments" % repo,
         {"body": "x", "in_reply_to": 5}),
        ("POST", "%s/issues/1/labels" % repo, {"labels": ["symphony"]}),
        ("POST", "%s/labels" % repo, {"name": "symphony"}),
        ("POST", "%s/pulls" % repo, {"title": "t", "body": "b",
                                     "head": "gh-1", "base": "master"}),
        ("PATCH", "%s/issues/1" % repo, {"state": "closed"}),
        ("PATCH", "%s/pulls/1" % repo, {"state": "closed"}),
        ("PATCH", "%s/issues/comments/9" % repo, {"body": "new"}),
        ("PUT", "%s/pulls/1/merge" % repo,
         {"sha": "a" * 40, "merge_method": "squash",
          "commit_title": "t", "commit_message": "m"}),
        ("DELETE", "%s/issues/comments/9" % repo, None),
    ]
    with patch.object(gh_client, "request") as call:
        call.side_effect = _fake_ok
        for method, path, body in allowed:
            result = agent_tool.execute_agent_tool(
                "github_api",
                {"method": method, "path": path, "body": body}, settings)
            assert result == {"status": 200, "body": {"ok": True}}, \
                (method, path)
    assert call.call_count == len(allowed)


def test_github_api_forbidden_paths_and_bodies(tmp_path):
    settings = _settings(tmp_path)
    forbidden = [
        ("GET", "/repos/other/x/issues/1", None),           # cross-repo
        ("GET", "/repos/o/r/../etc", None),                  # traversal
        ("GET", "/repos/o/r", None),                         # repo root
        ("DELETE", "/repos/o/r/pulls/1", None),              # non-comment
        ("DELETE", "/repos/o/r/git/refs/heads/x", None),
        ("PUT", "/repos/o/r/pulls/1", {"x": 1}),             # non-merge
        ("PUT", "/repos/o/r/pulls/1/merge", {"evil": True}),
        ("POST", "/repos/o/r/issues/1/comments",
         {"body": "x", "admin": True}),                      # bad key
        ("PATCH", "/repos/o/r/pulls/1", {"merged": True}),
        ("POST", "/repos/o/r/issues/1", {"body": "x"}),      # non-collection
        ("DELETE", "/repos/o/r/issues/comments", None),      # missing id
    ]
    with patch.object(gh_client, "request") as call:
        for method, path, body in forbidden:
            result = agent_tool.execute_agent_tool(
                "github_api",
                {"method": method, "path": path, "body": body}, settings)
            assert result["error"]["code"] == "forbidden", (method, path)
    call.assert_not_called()


# --- adapter / facade -----------------------------------------------------------

def test_candidates_only_active_local_states(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1, state="Todo")
    _seed(task_store, 2, state="Human Review")
    _seed(task_store, 3, state="Backlog")
    _seed(task_store, 4, state="Done")
    _seed(task_store, 5, state="Merging")
    ids = sorted(i.id for i in adapter.fetch_candidate_issues(settings))
    assert ids == ["1", "5"]  # Human Review inactive; Backlog/Done excluded


def test_begin_run_rechecks_real_state(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1, state="Todo")
    issue = Issue(id="1", identifier="GH-1", title="t", state="Todo",
                  dispatchable=True)
    assert adapter.begin_run_issue(issue, settings) == "ok"
    task_store.update(1, {"state": "Human Review"}, expected_revision=1)
    assert adapter.begin_run_issue(issue, settings) == "skip"
    assert adapter.begin_run_issue(
        Issue(id="9", identifier="GH-9", title="t", state="Todo",
              dispatchable=True), settings) == "skip"


def test_fetch_by_ids_404_omits_closed_keeps_local(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1, state="In Progress")
    _seed(task_store, 2, state="In Progress")

    def fake(method, path, params, body, settings_):
        if path.endswith("/issues/2"):
            return {"status": 404, "body": {}}
        return {"status": 200,
                "body": _open_issue(1, state="closed", title="renamed")}

    with patch.object(gh_client, "request", fake):
        issues = adapter.fetch_issues_by_ids(settings, ["1", "2"])
    assert [i.id for i in issues] == ["1"]
    issue = issues[0]
    assert issue.state == "In Progress"               # local state kept
    assert issue.native_ref["native_state"] == "closed"  # source-closed flag
    assert task_store.get(1)["title"] == "renamed"


def test_fetch_by_ids_failure_raises_not_omits(tmp_path):
    settings = _settings(tmp_path)
    _seed(store.Store(settings), 1)

    def boom(method, path, params, body, settings_):
        return {"status": 500, "body": {}}

    with patch.object(gh_client, "request", boom):
        with pytest.raises(gh_client.GitHubError):
            adapter.fetch_issues_by_ids(settings, ["1"])


def test_tracker_facade_dispatches_on_kind(tmp_path):
    board = _settings(tmp_path)
    _seed(store.Store(board), 1, state="Todo")
    ids = [i.id for i in tracker.fetch_candidate_issues(board)]
    assert ids == ["1"]
    native = _gh_settings(tmp_path)
    assert tracker.import_sources(native) is None
    issue = Issue(id="1", identifier="GH-1", title="t", state="open",
                  dispatchable=True)
    assert tracker.begin_run_issue(issue, native) == "ok"


def test_record_issue_failure_annotates_without_state_change(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1, state="In Progress")
    issue = Issue(id="1", identifier="GH-1", title="t",
                  state="In Progress", dispatchable=True)
    adapter.record_issue_failure(issue, settings, "boom")
    record = task_store.get(1)
    assert record["last_error"] == "boom"
    assert record["state"] == "In Progress"


def test_labels_carry_no_execution_meaning(tmp_path):
    # Any label set is inert; only required_labels (empty here) gate.
    settings = _settings(tmp_path)
    issue = Issue(id="1", identifier="GH-1", title="t", state="Todo",
                  labels=["needs-human", "merge-approved"],
                  dispatchable=True)
    assert adapter.routable(issue, settings) is True


# --- live-e2e regression: record timestamps reach the prompt template ---------

def test_record_timestamps_normalize_for_template_map(tmp_path):
    """P1 live failure: raw ``str`` reached ``util.iso8601`` → ``.tzinfo``
    AttributeError on the first real Todo dispatch. Regression walks
    store → adapter → Issue.to_template_map end to end."""
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 1, state="Todo",
          created_at="2024-01-01T00:00:00Z")
    issues = adapter.fetch_candidate_issues(settings)
    assert len(issues) == 1
    template_map = issues[0].to_template_map()
    assert template_map["created_at"] == "2024-01-01T00:00:00Z"
    # save() stamps updated_at itself — assert it survives as valid ISO.
    assert template_map["updated_at"].endswith("Z")


def test_record_bad_timestamps_degrade_to_none(tmp_path):
    """Official behaviour: unparseable/nullable timestamps become ``None``
    rather than crashing the prompt render or the created_at sort."""
    settings = _settings(tmp_path)
    record = {
        "schema_version": store.SCHEMA_VERSION, "repo": "o/r",
        "issue_number": 1, "identifier": "GH-1",
        "state": "Todo", "revision": 1,
        "created_at": "not-a-date", "updated_at": "2024-06-01"}
    with open(os.path.join(str(tmp_path), "1.json"), "w",
              encoding="utf-8") as handle:
        json.dump(record, handle)
    issues = adapter.fetch_candidate_issues(settings)
    template_map = issues[0].to_template_map()
    assert template_map["created_at"] is None
    assert template_map["updated_at"] is None  # naive/no-offset → None


# --- R203: structural validation on every read/write --------------------------

def _write_raw(directory, number, record):
    directory = str(directory)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "%d.json" % number),
              "w", encoding="utf-8") as handle:
        json.dump(record, handle)


def _bad_record(number, **over):
    record = {
        "schema_version": store.SCHEMA_VERSION, "repo": "o/r",
        "issue_number": number, "identifier": "GH-%d" % number,
        "state": "Todo", "revision": 1,
    }
    record.update(over)
    return record


def test_get_rejects_foreign_repo(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _write_raw(tmp_path, 4, _bad_record(4, repo="other/repo"))
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(4)
    assert ctx.value.code == "receipt_repository_mismatch"


def test_get_rejects_number_and_identifier_mismatch(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _write_raw(tmp_path, 5, _bad_record(5, issue_number=9))
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(5)
    assert ctx.value.code == "invalid_task_record"
    _write_raw(tmp_path, 6, _bad_record(6, identifier="WRONG-6"))
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(6)
    assert ctx.value.code == "invalid_task_record"


def test_get_requires_schema_v2_and_valid_state_revision(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    # A v1-shaped file (no schema_version) must demand explicit migration,
    # never silently surface as a task.
    _write_raw(tmp_path, 1, _bad_record(1, schema_version=None))
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(1)
    assert ctx.value.code == "unsupported_schema_version"
    _write_raw(tmp_path, 2, _bad_record(2, state="bogus"))
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(2)
    assert ctx.value.code == "invalid_task_record"
    _write_raw(tmp_path, 3, _bad_record(3, revision=0))
    with pytest.raises(store.TaskError) as ctx:
        task_store.get(3)
    assert ctx.value.code == "invalid_task_record"


def test_list_isolates_corrupt_and_keeps_healthy_candidates(tmp_path):
    settings = _settings(tmp_path)
    task_store = store.Store(settings)
    _seed(task_store, 5, state="Todo")
    # Valid JSON but missing issue_number — used to crash the whole list.
    _write_raw(tmp_path, 6, {"repo": "o/r", "state": "Todo"})
    _write_raw(tmp_path, 7, _bad_record(7, schema_version=None))
    records, errors = task_store.list()
    assert [r["issue_number"] for r in records] == [5]
    assert {e["issue_number"] for e in errors} == {6, 7}
    issues = adapter.fetch_candidate_issues(settings)
    assert [i.id for i in issues] == ["5"]


def test_update_rejects_corrupt_record_without_overwrite(tmp_path):
    task_store = store.Store(_settings(tmp_path))
    _write_raw(tmp_path, 8, _bad_record(8, issue_number=99))
    with pytest.raises(store.TaskError) as ctx:
        task_store.update(8, {"state": "Done"}, expected_revision=1)
    assert ctx.value.code == "invalid_task_record"
    with open(task_store.path_for(8), encoding="utf-8") as handle:
        assert json.load(handle)["issue_number"] == 99  # never overwritten


def test_import_isolates_corrupt_record(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_raw(tmp_path, 5, _bad_record(5, state=None))
    calls = []

    def fake_request(method, path, params, body, settings_):
        calls.append(path)
        return {"status": 200, "body": [
            _open_issue(5), _open_issue(6)]}

    monkeypatch.setattr(gh_client, "request", fake_request)
    total, created, refreshed = importer.import_open_issues(settings)
    assert created == 1  # only #6 is created; corrupt #5 is skipped
    task_store = store.Store(settings)
    assert task_store.get(6)["state"] == "Backlog"
    with pytest.raises(store.TaskError):
        task_store.get(5)  # corrupt file survives, not overwritten


def test_corrupt_record_is_fetch_failure_not_phantom_404(tmp_path):
    settings = _settings(tmp_path)
    _write_raw(tmp_path, 9, _bad_record(9, state=None))
    with pytest.raises(store.TaskError):
        adapter.fetch_issues_by_ids(settings, ["GH-9"])


# --- R201: task GET endpoints share the loopback/Bearer boundary --------------

from symphony import http_api, workflow_store  # noqa: E402


class _FakeHandler:
    """Duck-typed handler exposing the real route functions."""

    _handle_tasks_get = http_api._Handler._handle_tasks_get
    _handle_task_source = http_api._Handler._handle_task_source
    _mutation_allowed = http_api._Handler._mutation_allowed
    _tasks_store = staticmethod(http_api._Handler._tasks_store)
    _task_number = staticmethod(http_api._Handler._task_number)
    _task_error_status = staticmethod(http_api._Handler._task_error_status)

    def __init__(self, settings, ip="127.0.0.1", token=None):
        self.api = SimpleNamespace(settings=lambda: settings)
        self.client_address = (ip, 4242)
        self.headers = (
            {"Authorization": "Bearer %s" % token} if token else {})
        self.status = None
        self.payload = None

    def _json(self, status, payload):
        self.status, self.payload = status, payload

    def _error(self, status, code, message):
        self._json(status, {"error": {"code": code, "message": message}})


def _api_settings(tmp_path, api_token="api-secret"):
    return SimpleNamespace(
        server=SimpleNamespace(api_token=api_token),
        tracker=_settings(tmp_path))


def _bind_store(monkeypatch, settings):
    monkeypatch.setattr(
        workflow_store, "get_store",
        lambda: SimpleNamespace(settings=lambda: settings))


def test_task_reads_reject_remote_without_token(tmp_path, monkeypatch):
    settings = _api_settings(tmp_path)
    _bind_store(monkeypatch, settings)
    _seed(store.Store(settings.tracker), 4, state="Todo")
    for tail in ("", "GH-4", "GH-4/source"):
        remote = _FakeHandler(settings, ip="203.0.113.10")
        remote._handle_tasks_get(tail)
        assert remote.status == 403, tail
        wrong = _FakeHandler(settings, ip="203.0.113.10", token="nope")
        wrong._handle_tasks_get(tail)
        assert wrong.status == 403, tail


def test_task_reads_allow_bearer_and_loopback(tmp_path, monkeypatch):
    settings = _api_settings(tmp_path)
    _bind_store(monkeypatch, settings)
    _seed(store.Store(settings.tracker), 4, state="Todo")
    bearer = _FakeHandler(settings, ip="203.0.113.10", token="api-secret")
    bearer._handle_tasks_get("")
    assert bearer.status == 200
    loop = _FakeHandler(settings, ip="127.0.0.1")
    loop._handle_tasks_get("GH-4")
    assert loop.status == 200
    assert loop.payload["task"]["issue_number"] == 4


def test_source_proxies_only_registered_tasks(tmp_path, monkeypatch):
    settings = _api_settings(tmp_path)
    _bind_store(monkeypatch, settings)
    _seed(store.Store(settings.tracker), 4, state="Todo")
    called = []

    def fake_request(method, path, params, body, settings_):
        called.append(path)
        if path.endswith("/issues/4"):
            return {"status": 200, "body": _open_issue(4)}
        return {"status": 404, "body": {}}

    monkeypatch.setattr(gh_client, "request", fake_request)
    ok = _FakeHandler(settings)
    ok._handle_tasks_get("GH-4/source")
    assert ok.status == 200
    assert called and called[0].endswith("/issues/4")
    missing = _FakeHandler(settings)
    missing._handle_tasks_get("GH-77/source")
    assert missing.status == 404
    # Seed has no pr_number: exactly issue + comments-page calls, no PR and
    # no call for the unregistered number.
    assert len(called) == 2
    assert all("/issues/4" in c for c in called)


def test_source_rejects_remote_and_corrupt_records(tmp_path, monkeypatch):
    settings = _api_settings(tmp_path)
    _bind_store(monkeypatch, settings)
    _write_raw(tmp_path, 9, _bad_record(9, state=None))
    remote = _FakeHandler(settings, ip="203.0.113.10")
    remote._handle_tasks_get("GH-9/source")
    assert remote.status == 403
    local = _FakeHandler(settings)
    local._handle_tasks_get("GH-9/source")
    assert local.status == 409  # corrupt record, not a fake 404


# --- live-e2e regression: PATCH must respond before a blocking refresh --------

def test_patch_response_not_blocked_by_refresh(tmp_path, monkeypatch):
    """P3 live failure: request_refresh blocked on a slow GitHub poll for up
    to 15s *before* the 200 was sent → board timed out on a write that had
    already persisted. Real HTTP round-trip proves the client receives the
    200 + bumped revision while refresh is still in flight."""
    import threading
    import time
    import urllib.request

    settings = _api_settings(tmp_path)
    _bind_store(monkeypatch, settings)
    task_store = store.Store(settings.tracker)
    _seed(task_store, 4, state="Backlog")

    gate = threading.Event()
    refresh_called = threading.Event()

    def blocking_refresh():
        refresh_called.set()
        gate.wait(30)
        return None

    orchestrator = SimpleNamespace(request_refresh=blocking_refresh)
    api_store = SimpleNamespace(settings=lambda: settings)
    server = http_api.start(
        orchestrator, api_store, None, "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            "http://127.0.0.1:%d/api/v1/tasks/GH-4" % port,
            data=json.dumps(
                {"state": "Todo", "expected_revision": 1}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH")
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
            status = response.status
        elapsed = time.monotonic() - started
        assert status == 200
        assert payload["task"]["state"] == "Todo"
        assert payload["task"]["revision"] == 2
        # With the old order the handler would still be inside
        # request_refresh and urlopen would hit the 10s timeout instead.
        assert refresh_called.wait(5), "refresh must still be nudged"
        assert not gate.is_set()
        assert elapsed < 10
    finally:
        gate.set()
        server.shutdown()
        server.server_close()
