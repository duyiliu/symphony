"""Native GitHub tracker adapter (``tracker.kind: github``).

The adapter only validates GitHub-specific state semantics and delegates
reads to :mod:`symphony.github.client`, which owns pagination, ID refresh,
normalization and the ``github_api`` tool boundary.
"""

from ..util import normalize_issue_state
from ..workflow import WorkflowError
from . import client as gh_client

ACTIVE_STATES = ("open",)
TERMINAL_STATES = ("closed",)
ALLOWED_STATES = frozenset(ACTIVE_STATES + TERMINAL_STATES)
LABEL_STATES = frozenset(
    label[len(gh_client.LABEL_PREFIX):]
    for label in gh_client.LABEL_STATE_PRIORITY
    if label.startswith(gh_client.LABEL_PREFIX)
) | frozenset({"review", "rework", "merging", "done", "closed"})


def _validate_states(states, missing_error, tracker_settings):
    if not isinstance(states, list):
        raise WorkflowError(missing_error)
    if gh_client._use_label_states(tracker_settings):
        allowed = LABEL_STATES
    else:
        allowed = ALLOWED_STATES
    if not all(
            normalize_issue_state(state) in allowed
            for state in states):
        raise WorkflowError("invalid_github_states")


def _translate(error):
    raise WorkflowError(error.reason, error.detail) from None


def validate_config(tracker_settings):
    _validate_states(
        getattr(tracker_settings, "active_states", None),
        "missing_github_active_states",
        tracker_settings)
    _validate_states(
        getattr(tracker_settings, "terminal_states", None),
        "missing_github_terminal_states",
        tracker_settings)
    try:
        gh_client.settings(tracker_settings)
    except gh_client.GitHubError as error:
        _translate(error)


def fetch_candidate_issues(tracker_settings):
    """Return issues eligible for orchestrator dispatch.

    In label-state mode ``review``/``done``/``closed`` are delivery-owned
    and excluded from the candidate pool; the delivery layer uses
    ``fetch_issues_by_states`` directly and can see all states.
    In native mode every configured active state is a candidate.
    """
    if gh_client._use_label_states(tracker_settings):
        candidate_states = [
            state for state in tracker_settings.active_states
            if normalize_issue_state(state) in ("todo", "in-progress")
        ]
    else:
        candidate_states = tracker_settings.active_states
    return fetch_issues_by_states(tracker_settings, candidate_states)


def fetch_issues_by_states(tracker_settings, state_names):
    try:
        return gh_client.fetch_issues_by_states(
            state_names, tracker_settings)
    except gh_client.GitHubError as error:
        _translate(error)


def fetch_issues_by_ids(tracker_settings, issue_ids):
    try:
        return gh_client.fetch_issues_by_ids(issue_ids, tracker_settings)
    except gh_client.GitHubError as error:
        _translate(error)


def routable(issue, tracker_settings):
    return issue.routable(tracker_settings.required_labels or [])


def secret_environment_names(tracker_settings):
    return gh_client.secret_environment_names(tracker_settings)
