"""GitLab-backed tracker adapter."""

from ..util import normalize_issue_state
from ..workflow import WorkflowError
from . import agent_tool
from . import client

ACTIVE_STATES = {"opened"}
TERMINAL_STATES = {"closed"}


def _validate_states(states, allowed, missing_error):
    if not isinstance(states, list):
        raise WorkflowError(missing_error)
    if any(normalize_issue_state(state) not in allowed for state in states):
        raise WorkflowError("invalid_gitlab_states")


def validate_config(tracker_settings):
    _validate_states(
        getattr(tracker_settings, "active_states", None),
        ACTIVE_STATES, "missing_gitlab_active_states")
    _validate_states(
        getattr(tracker_settings, "terminal_states", None),
        TERMINAL_STATES, "missing_gitlab_terminal_states")
    client.validate_settings(tracker_settings)


def fetch_candidate_issues(tracker_settings):
    return client.fetch_candidate_issues(tracker_settings)


def fetch_issues_by_states(tracker_settings, state_names):
    return client.fetch_issues_by_states(tracker_settings, state_names)


def fetch_issues_by_ids(tracker_settings, issue_ids):
    return client.fetch_issues_by_ids(tracker_settings, issue_ids)


def routable(issue, tracker_settings):
    return client.routable(issue, tracker_settings)


def tool_specs():
    return agent_tool.tool_specs()


def execute_agent_tool(tool_name, arguments, tracker_settings=None,
                       request_fun=None):
    return agent_tool.execute_agent_tool(
        tool_name, arguments, tracker_settings=tracker_settings,
        request_fun=request_fun)


def secret_environment_names(tracker_settings):
    return client.secret_environment_names(tracker_settings)
