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


def _validate_states(states, missing_error):
    if not isinstance(states, list):
        raise WorkflowError(missing_error)
    if not all(
            normalize_issue_state(state) in ALLOWED_STATES
            for state in states):
        raise WorkflowError("invalid_github_states")


def _translate(error):
    raise WorkflowError(error.reason, error.detail) from None


def validate_config(tracker_settings):
    _validate_states(
        getattr(tracker_settings, "active_states", None),
        "missing_github_active_states")
    _validate_states(
        getattr(tracker_settings, "terminal_states", None),
        "missing_github_terminal_states")
    try:
        gh_client.settings(tracker_settings)
    except gh_client.GitHubError as error:
        _translate(error)


def fetch_candidate_issues(tracker_settings):
    return fetch_issues_by_states(
        tracker_settings, tracker_settings.active_states)


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
