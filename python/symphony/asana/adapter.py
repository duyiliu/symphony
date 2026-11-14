"""Asana-backed tracker adapter."""

from ..util import present_string
from ..workflow import WorkflowError
from . import agent_tool
from . import client


def _validate_states(states, missing_error):
    if not isinstance(states, list):
        raise WorkflowError(missing_error)
    if any(not present_string(state) for state in states):
        raise WorkflowError("invalid_asana_states")


def validate_config(tracker_settings):
    _validate_states(
        getattr(tracker_settings, "active_states", None),
        "missing_asana_active_states")
    _validate_states(
        getattr(tracker_settings, "terminal_states", None),
        "missing_asana_terminal_states")
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
