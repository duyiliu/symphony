"""GitHub tracker adapter: issue fetching plus host workflow/tool glue."""

from ..workflow import WorkflowError
from . import agent_tool, client, workflow, workflow_policy


def validate_config(tracker_settings):
    if tracker_settings.kind != "github":
        raise WorkflowError("unsupported_tracker_kind", tracker_settings.kind)
    _validate_states(
        tracker_settings.active_states, ("open",),
        "missing_github_active_states")
    _validate_states(
        tracker_settings.terminal_states, ("closed",),
        "missing_github_terminal_states")
    try:
        client.settings(tracker_settings)
    except client.GitHubError as error:
        if isinstance(error.reason, str):
            raise WorkflowError(error.reason)
        raise WorkflowError("invalid_workflow_config", error.reason)
    workflow_policy.validate(tracker_settings)


def _validate_states(states, allowed, missing_error):
    if not isinstance(states, list):
        raise WorkflowError(missing_error)
    normalized = [
        state.strip().lower() if isinstance(state, str) else ""
        for state in states
    ]
    if not all(state in allowed for state in normalized):
        raise WorkflowError("invalid_github_states")


def fetch_candidate_issues(tracker_settings):
    return client.fetch_issues_by_states(
        tracker_settings.active_states, tracker_settings)


def fetch_issues_by_states(tracker_settings, state_names):
    return client.fetch_issues_by_states(state_names, tracker_settings)


def fetch_issues_by_ids(tracker_settings, issue_ids):
    return client.fetch_issues_by_ids(issue_ids, tracker_settings)


def fetch_issue_states_by_ids(tracker_settings, issue_ids):
    return client.fetch_issues_by_ids(issue_ids, tracker_settings)


def begin_run_issue(issue, tracker_settings):
    return workflow.begin_run(issue, tracker_settings)


def record_issue_failure(issue, tracker_settings, reason):
    return workflow.record_failure(issue, tracker_settings, reason)


def issue_blocked(issue, tracker_settings):
    return workflow.waiting(issue, tracker_settings)


def same_workflow_phase(before, after, tracker_settings):
    return workflow_policy.same_phase(before, after, tracker_settings)


def routable(issue, tracker_settings):
    return workflow.routable(issue, tracker_settings)


def tool_specs():
    return agent_tool.tool_specs()


def execute_agent_tool(tool, arguments):
    return agent_tool.execute(tool, arguments)


def secret_environment_names(tracker_settings):
    return tracker_settings.secret_environment_names
