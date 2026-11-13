"""Tracker facade: dispatch tracker operations on ``tracker.kind``.

``linear`` is the official upstream integration, ``github`` is the plain native tracker; ``board`` is the
local task-record tracker that carries the Linear-style workflow state.
Orchestrator/runner only ever talk to this facade; every function takes
``tracker_settings`` like the old single-kind adapter.
"""

from .github import adapter as github_adapter
from . import linear as linear_adapter


def _is_linear(settings):
    return getattr(settings, "kind", None) == "linear"


def _board():
    from .tasks import adapter as board_adapter
    return board_adapter


def _is_board(tracker_settings):
    return getattr(tracker_settings, "kind", None) == "board"


def validate_config(settings):
    """Full-settings entry used by WorkflowStore."""
    tracker = settings.tracker
    if _is_linear(tracker):
        linear_adapter.validate_config(tracker)
    elif _is_board(tracker):
        _board().validate_config(tracker)
    else:
        github_adapter.validate_config(tracker)


def fetch_candidate_issues(tracker_settings):
    if _is_linear(tracker_settings):
        return linear_adapter.fetch_candidate_issues(tracker_settings)
    if _is_board(tracker_settings):
        return _board().fetch_candidate_issues(tracker_settings)
    return github_adapter.fetch_candidate_issues(tracker_settings)


def fetch_issues_by_states(tracker_settings, state_names):
    if _is_linear(tracker_settings):
        return linear_adapter.fetch_issues_by_states(tracker_settings, state_names)
    if _is_board(tracker_settings):
        return _board().fetch_issues_by_states(
            tracker_settings, state_names)
    return github_adapter.fetch_issues_by_states(
        tracker_settings, state_names)


def fetch_issues_by_ids(tracker_settings, issue_ids):
    if _is_linear(tracker_settings):
        return linear_adapter.fetch_issues_by_ids(tracker_settings, issue_ids)
    if _is_board(tracker_settings):
        return _board().fetch_issues_by_ids(tracker_settings, issue_ids)
    return github_adapter.fetch_issues_by_ids(tracker_settings, issue_ids)


def import_sources(tracker_settings):
    """Poll-tick source discovery (board tracker only; native is a no-op)."""
    if _is_board(tracker_settings):
        return _board().import_sources(tracker_settings)
    return None


def import_issue_by_number(tracker_settings, number):
    if _is_board(tracker_settings):
        return _board().import_issue_by_number(tracker_settings, number)
    from .tasks import store as store_mod
    raise store_mod.TaskError("unsupported_tracker_kind")


def begin_run_issue(issue, tracker_settings):
    if _is_board(tracker_settings):
        return _board().begin_run_issue(issue, tracker_settings)
    return "ok"


def routable(issue, tracker_settings):
    if _is_linear(tracker_settings):
        return linear_adapter.routable(issue, tracker_settings)
    if _is_board(tracker_settings):
        return _board().routable(issue, tracker_settings)
    return github_adapter.routable(issue, tracker_settings)


def record_issue_failure(issue, tracker_settings, reason):
    if _is_board(tracker_settings):
        _board().record_issue_failure(issue, tracker_settings, reason)


def tool_specs(tracker_settings=None):
    from .github import agent_tool
    if tracker_settings is None:
        from .workflow_store import get_store
        store = get_store()
        tracker_settings = store.settings().tracker if store else None
    if tracker_settings and _is_linear(tracker_settings):
        specs = linear_adapter.tool_specs()
        delivery = _delivery_settings()
        if delivery is not None and delivery.enabled:
            specs = specs + agent_tool.delivery_tool_specs()
        return specs
    return agent_tool.tool_specs(tracker_settings)


def execute_agent_tool(tool_name, arguments, tracker_settings=None):
    from .github import agent_tool
    if tracker_settings is None:
        from .workflow_store import get_store
        store = get_store()
        tracker_settings = store.settings().tracker if store else None
    if tracker_settings and _is_linear(tracker_settings):
        if tool_name == "github_api":
            delivery = _delivery_settings()
            if delivery is None or not delivery.enabled:
                return {"error": {"code": "unknown_tool", "detail": tool_name}}
            return agent_tool.execute_delivery_tool(arguments, delivery)
        return linear_adapter.execute_agent_tool(tracker_settings, tool_name, arguments)
    return agent_tool.execute_agent_tool(tool_name, arguments,
                                         tracker_settings)


def _delivery_settings():
    from .workflow_store import get_store

    store = get_store()
    if store is None:
        return None
    return store.settings().delivery


def secret_environment_names(tracker_settings):
    return list(getattr(tracker_settings, "secret_environment_names", None)
                or [])
