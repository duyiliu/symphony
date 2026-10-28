"""AgentRunner: executes one tracker work item in its workspace with pi.

Mirrors ``SymphonyElixir.AgentRunner``: admission through the GitHub workflow,
workspace + hook lifecycle, one pi session serving up to ``max_turns`` turns,
issue re-validation between turns, and structured run results the
orchestrator maps to complete/block/retry decisions.
"""

import logging
import threading

from .. import workspace as workspace_mod
from ..github import adapter as tracker_adapter
from ..workflow import WorkflowError
from . import pi_runtime

log = logging.getLogger("symphony.runner")

# Run results pushed back to the orchestrator.
DONE_NORMAL = "normal"
DONE_WAITING = "github_workflow_waiting"  # blocked receipt; human must relabel
DONE_CANCELLED = "cancelled"


def _issue_context(issue):
    return "issue_id=%s issue_identifier=%s" % (issue.id, issue.identifier)


class RunHandle:
    """Shared cancellation surface between orchestrator and runner thread."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.session = None
        self.lock = threading.Lock()

    def cancel(self):
        self.cancel_event.set()
        with self.lock:
            session = self.session
        if session is not None:
            try:
                pi_runtime._send(
                    session, {"id": "symphony-abort", "type": "abort"})
            except Exception:  # noqa: BLE001 - best effort wake-up
                pass

    def set_session(self, session):
        with self.lock:
            self.session = session


def selected_worker_host(preferred, configured_hosts):
    hosts = []
    for host in configured_hosts or []:
        host = host.strip()
        if host and host not in hosts:
            hosts.append(host)
    if isinstance(preferred, str) and preferred != "":
        return preferred
    if not hosts:
        return None
    return hosts[0]


def run(issue, settings, on_update, on_done, handle, opts=None):
    """Execute the full run for ``issue`` in the calling thread.

    ``on_update(issue_id, message)`` forwards runtime events;
    ``on_done(issue_id, reason)`` reports the terminal outcome once.
    """
    opts = opts or {}
    worker_host = selected_worker_host(
        opts.get("worker_host"), settings.worker.ssh_hosts)
    tracker = settings.tracker
    context = _issue_context(issue)
    log.info("Starting agent run for %s worker_host=%s",
             context, worker_host or "local")

    try:
        admission = tracker_adapter.begin_run_issue(issue, tracker)
        if admission == "waiting":
            on_done(issue.id, DONE_WAITING)
            return
        if admission == "skip":
            on_done(issue.id, DONE_NORMAL)
            return
        _run_on_worker_host(
            issue, settings, on_update, on_done, handle, worker_host,
            attempt=opts.get("attempt"))
    except _Cancelled:
        on_done(issue.id, DONE_CANCELLED)
    except pi_runtime.TurnError as error:
        if error.reason == "cancelled":
            on_done(issue.id, DONE_CANCELLED)
        else:
            _fail(issue, tracker, on_done, error)
    except WorkflowError as error:
        _fail(issue, tracker, on_done, error.describe())
    except Exception as error:  # noqa: BLE001 - runner isolates all failures
        _fail(issue, tracker, on_done, error)


def _fail(issue, tracker, on_done, reason):
    try:
        tracker_adapter.record_issue_failure(issue, tracker, reason)
    except Exception:  # noqa: BLE001
        pass
    log.error("Agent run failed for %s: %s", _issue_context(issue), reason)
    on_done(issue.id, reason)


def _run_on_worker_host(issue, settings, on_update, on_done, handle, worker_host,
                        attempt=None):
    context = _issue_context(issue)
    log.info("Starting worker attempt for %s worker_host=%s",
             context, worker_host or "local")
    workflow_path = _workflow_path()
    workspace = workspace_mod.create_for_issue(
        settings, issue, workflow_path=workflow_path, worker_host=worker_host)
    on_update(issue.id, {
        "event": "worker_runtime_info",
        "worker_host": worker_host,
        "workspace_path": workspace,
    })
    try:
        workspace_mod.run_before_run(
            settings, issue, workflow_path=workflow_path,
            worker_host=worker_host)
        _run_agent_turns(
            workspace, issue, settings, on_update, handle, worker_host, attempt)
    finally:
        workspace_mod.run_after_run(
            settings, issue, workflow_path=workflow_path,
            worker_host=worker_host)
    on_done(issue.id, DONE_NORMAL)


def _run_agent_turns(workspace, issue, settings, on_update, handle, worker_host,
                     attempt):
    if handle.cancel_event.is_set():
        raise _Cancelled()
    if settings.runtime.kind != "pi":
        raise WorkflowError("unsupported_runtime_kind", settings.runtime.kind)
    session = pi_runtime.start_session(
        workspace, settings, worker_host=worker_host)
    handle.set_session(session)
    try:
        _do_run_turns(
            session, workspace, issue, settings, on_update, handle, 1, attempt)
    finally:
        handle.set_session(None)
        pi_runtime.stop_session(session)


def _do_run_turns(session, workspace, issue, settings, on_update, handle,
                  turn_number, attempt):
    max_turns = settings.agent.max_turns
    context = _issue_context(issue)
    prompt = _build_turn_prompt(issue, settings, turn_number, max_turns, attempt)
    turn = pi_runtime.run_turn(
        session, prompt,
        on_message=lambda message: on_update(issue.id, message),
        cancel_event=handle.cancel_event)
    log.info(
        "Completed agent run for %s session_id=%s workspace=%s turn=%s/%s",
        context, turn["session_id"], workspace, turn_number, max_turns)

    refreshed = _continue_with_issue(issue, settings)
    if refreshed is False:
        return
    if turn_number < max_turns:
        log.info(
            "Continuing agent run for %s after normal turn completion turn=%s/%s",
            _issue_context(refreshed), turn_number, max_turns)
        _do_run_turns(
            session, workspace, refreshed, settings, on_update, handle,
            turn_number + 1, attempt)
    else:
        log.info(
            "Reached agent.max_turns for %s with issue still active; "
            "returning control to orchestrator", _issue_context(refreshed))


def _continue_with_issue(issue, settings):
    """Returns refreshed Issue (continue) or False (done); raises on error."""
    try:
        issues = tracker_adapter.fetch_issues_by_ids(
            settings.tracker, [issue.id])
    except Exception as error:  # noqa: BLE001 - a refresh failure is a run failure
        raise WorkflowError("issue_state_refresh_failed", str(error))
    refreshed = next(
        (candidate for candidate in issues if candidate.id == issue.id), None)
    if refreshed is None:
        return False
    active = {
        state.strip().lower()
        for state in (settings.tracker.active_states or [])
    }
    if (
        (refreshed.state or "").strip().lower() in active
        and tracker_adapter.routable(refreshed, settings.tracker)
        and tracker_adapter.same_workflow_phase(issue, refreshed, settings.tracker)
    ):
        return refreshed
    return False


def _workflow_path():
    from .. import workflow_store

    store = workflow_store.get_store()
    return store.path if store is not None else None


def _build_turn_prompt(issue, settings, turn_number, max_turns, attempt):
    if turn_number == 1:
        from .. import config as config_mod
        from .. import template, workflow_store

        store = workflow_store.get_store()
        if store is None:
            raise WorkflowError("workflow_store_not_started")
        workflow = store.current()
        prompt_template = workflow.prompt_template
        if not prompt_template or not prompt_template.strip():
            prompt_template = config_mod.DEFAULT_PROMPT_TEMPLATE
        return template.render(prompt_template, {
            "issue": issue.to_template_map(),
            "attempt": attempt,
        })
    return (
        "Continuation guidance:\n\n"
        "- The previous Codex turn completed normally, but the tracker work "
        "item is still in an active state.\n"
        "- This is continuation turn #%s of %s for the current agent run.\n"
        "- Resume from the current workspace and workpad state instead of "
        "restarting from scratch.\n"
        "- The original task instructions and prior turn context are already "
        "present in this thread, so do not restate them before acting.\n"
        "- Focus on the remaining ticket work and do not end the turn while "
        "the issue stays active unless you are truly blocked.\n"
        % (turn_number, max_turns)
    )


class _Cancelled(Exception):
    pass
