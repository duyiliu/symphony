"""Source discovery: import configured-repo open issues as local tasks."""

import logging

from ..github import client as gh_client
from ..github import workflow_policy as policy
from ..github.client import GitHubError
from . import store as store_mod

log = logging.getLogger("symphony.tasks.importer")

MAX_PAGES = 100


def source_fields(issue_data):
    """Cached source projection stored on a task record."""
    return {
        "title": issue_data.get("title") or "",
        "description": issue_data.get("body") or "",
        "labels": [l.get("name") for l in (issue_data.get("labels") or [])
                   if isinstance(l, dict) and l.get("name")],
        "url": issue_data.get("html_url"),
        "native_state": issue_data.get("state"),
    }


def _paginate_open_issues(tracker_settings):
    """Collect the complete open-issue list before committing any record —
    a partial page failure must not partially apply."""
    repo = policy.repo(tracker_settings)
    path = "/repos/%s/issues" % repo
    rows = []
    for page in range(1, MAX_PAGES + 1):
        result = gh_client.request(
            "GET", path, {"state": "open", "per_page": 100, "page": page},
            None, tracker_settings)
        if not (200 <= result["status"] <= 299):
            raise GitHubError(
                "github_status_%s" % result["status"],
                "open issue discovery failed page=%d" % page)
        batch = result["body"]
        if not isinstance(batch, list):
            raise GitHubError("invalid_github_page", path)
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise GitHubError("github_pagination_limit", path)


def import_open_issues(tracker_settings):
    """Upsert every open issue; only missing records become Backlog.

    Returns ``(total, created, refreshed)``. Raises on discovery failure —
    existing records are never erased or marked missing.
    """
    task_store = store_mod.Store(tracker_settings)
    rows = _paginate_open_issues(tracker_settings)
    created = refreshed = 0
    for row in rows:
        if not isinstance(row, dict) or "pull_request" in row:
            continue  # issues API also lists PRs; they are not tasks
        number = row.get("number")
        if not isinstance(number, int):
            continue
        try:
            before = task_store.get(number)
            task_store.upsert_source(number, source_fields(row))
        except store_mod.TaskError as error:
            # Corrupt/old-schema record: report and isolate — never let one
            # bad file abort the whole import or silently overwrite it.
            log.warning("Task record skipped during import issue=%s error=%s",
                        number, error.code)
            continue
        if before is None:
            created += 1
        else:
            refreshed += 1
    return len(rows), created, refreshed


def import_issue_by_number(tracker_settings, number):
    """Explicit single-issue import (closed sources import as ``Closed``)."""
    if not isinstance(number, int) or number <= 0:
        raise store_mod.TaskError("invalid_fields", "issue_number")
    repo = policy.repo(tracker_settings)
    result = gh_client.request(
        "GET", "/repos/%s/issues/%d" % (repo, number), {}, None,
        tracker_settings)
    if result["status"] == 404:
        raise store_mod.TaskError("issue_not_found")
    if not (200 <= result["status"] <= 299):
        raise GitHubError("github_status_%s" % result["status"],
                          "issue %d fetch failed" % number)
    body = result["body"]
    if not isinstance(body, dict) or "pull_request" in body:
        raise store_mod.TaskError("not_an_issue")
    task_store = store_mod.Store(tracker_settings)
    return task_store.upsert_source(number, source_fields(body))
