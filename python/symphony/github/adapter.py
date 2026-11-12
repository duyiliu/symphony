"""Native GitHub tracker adapter (``tracker.kind: github``).

Plain polling tracker: candidates are repo issues whose native
``open``/``closed`` state matches the configured active/terminal lists.
It knows nothing about local task records — the Linear-style workflow
lives in ``tracker.kind: board`` (``symphony.tasks``).
"""

import logging

from ..issue import Issue
from ..util import normalize_issue_state
from ..workflow import WorkflowError
from . import client as gh_client
from . import workflow_policy as policy

log = logging.getLogger("symphony.github.adapter")

_API_ROOT = "https://api.github.com"


def validate_config(tracker_settings):
    if not tracker_settings.active_states:
        raise WorkflowError("missing_tracker_active_states")
    if not tracker_settings.terminal_states:
        raise WorkflowError("missing_tracker_terminal_states")
    allowed = {"open", "closed"}
    for name in tracker_settings.active_states + \
            tracker_settings.terminal_states:
        if normalize_issue_state(name) not in allowed:
            raise WorkflowError("unsupported_tracker_state", name)
    if not policy.repo(tracker_settings):
        raise WorkflowError("missing_tracker_repo")


def fetch_candidate_issues(tracker_settings):
    return fetch_issues_by_states(
        tracker_settings, tracker_settings.active_states)


def fetch_issues_by_states(tracker_settings, state_names):
    wanted = {normalize_issue_state(state) for state in state_names}
    repo = policy.repo(tracker_settings)
    result = gh_client.request(
        "GET", "/repos/%s/issues" % repo,
        {"per_page": 100, "state": "all"}, None, tracker_settings)
    if result["status"] != 200 or not isinstance(result["body"], list):
        return []
    return [issue for issue in
            (_normalize(row) for row in result["body"])
            if issue is not None
            and normalize_issue_state(issue.state) in wanted]


def fetch_issues_by_ids(tracker_settings, issue_ids):
    repo = policy.repo(tracker_settings)
    issues = []
    for issue_id in issue_ids:
        result = gh_client.request(
            "GET", "/repos/%s/issues/%s" % (repo, issue_id), {}, None,
            tracker_settings)
        if result["status"] != 200 or not isinstance(result["body"], dict):
            continue
        issue = _normalize(result["body"])
        if issue is not None:
            issues.append(issue)
    return issues


def _normalize(row):
    if not isinstance(row, dict) or "pull_request" in row:
        return None  # the issues endpoint also lists PRs; they aren't tasks
    number = row.get("number")
    if number is None:
        return None
    labels = []
    for label in row.get("labels") or []:
        if isinstance(label, dict) and label.get("name"):
            labels.append(label["name"])
    issue = Issue(
        id=str(number),
        identifier="GH-%s" % number,
        title=row.get("title") or "",
        description=row.get("body") or "",
        state=row.get("state") or "",
        url=row.get("html_url"),
        labels=labels,
        blocked_by=[],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
    issue.dispatchable = True
    return issue


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])
