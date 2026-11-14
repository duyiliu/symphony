"""AgentRunner: executes one tracker work item in its workspace with pi.

Mirrors ``SymphonyElixir.AgentRunner``: admission through the GitHub workflow,
workspace + hook lifecycle, one pi session serving up to ``max_turns`` turns,
issue re-validation between turns, and structured run results the
orchestrator maps to complete/block/retry decisions.
"""

import logging
import threading

from .. import workspace as workspace_mod
from .. import tracker as tracker_adapter
from ..workflow import WorkflowError
from . import pi_runtime

log = logging.getLogger("symphony.runner")

# Run results pushed back to the orchestrator.
DONE_NORMAL = "normal"
DONE_SKIPPED = "skipped"  # claim-time recheck: task left active states
DONE_CANCELLED = "cancelled"
DONE_DEFERRED = "deferred"
DONE_TERMINATION_UNCONFIRMED = "termination_unconfirmed"

_RETRYABLE_DEFERRAL_REASONS = frozenset({
    "github_api_request",
    "github_unknown_payload",
    "linear_api_request",
    "linear_graphql_errors",
    "linear_rate_limited",
    "linear_unknown_payload",
})

_CONTROL_DEFERRAL_REASONS = frozenset({
    "github_api_status",
    "issue_state_refresh_failed",
    "linear_api_http_error",
}) | _RETRYABLE_DEFERRAL_REASONS


def _issue_context(issue):
    return "issue_id=%s issue_identifier=%s" % (issue.id, issue.identifier)


class RunHandle:
    """Shared cancellation surface between orchestrator and runner thread."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.session = None
        self.lock = threading.Lock()
        self.lease = None
        self.agent_started = False
        self.termination_confirmed = True
        self.deferred_reason = None
        self.deferred_code = None
        self.deferred_retryable = None
        self.deferred_policy = None

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

    def set_lease(self, lease):
        with self.lock:
            self.lease = lease

    def mark_agent_started(self):
        with self.lock:
            self.agent_started = True

    def has_started(self):
        with self.lock:
            return self.agent_started is True

    def current_lease(self):
        with self.lock:
            return self.lease

    def mark_termination_unconfirmed(self):
        with self.lock:
            self.termination_confirmed = False

    def mark_termination_confirmed(self):
        with self.lock:
            self.termination_confirmed = True

    def termination_is_confirmed(self):
        with self.lock:
            return self.termination_confirmed is True

    def defer(self, reason, retryable=True, code=None, wait=False):
        with self.lock:
            self.deferred_reason = str(reason)
            self.deferred_retryable = bool(retryable)
            self.deferred_code = code
            self.deferred_policy = (
                "permanent" if not retryable else
                "wait" if wait else "bounded")

    def force_terminate(self):
        with self.lock:
            session = self.session
            started = self.agent_started
        if session is None:
            return not started
        confirmed = pi_runtime.force_terminate_session(session)
        with self.lock:
            self.termination_confirmed = bool(confirmed)
        return confirmed

    def release_confirmed_lease(self):
        with self.lock:
            lease = self.lease
            confirmed = self.termination_confirmed
            if confirmed:
                self.lease = None
        if lease is not None and confirmed:
            lease.release()


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
    workflow_path = _workflow_path()
    try:
        lease = workspace_mod.acquire_run_lease(
            settings, issue, workflow_path=workflow_path,
            worker_host=worker_host)
    except Exception as error:  # noqa: BLE001 - lease failure blocks execution
        reason = "workspace run lease acquisition failed: %r" % (error,)
        handle.defer(
            reason, retryable=_deferral_is_retryable(error),
            code=_error_reason(error))
        log.error("Deferring agent run for %s: %s", context, reason)
        on_done(issue.id, DONE_DEFERRED)
        return
    if lease is None:
        reason = "workspace run lease is held by another live process"
        handle.defer(
            reason, retryable=True, code="run_lease_held", wait=True)
        log.warning("Deferring agent run for %s: %s", context, reason)
        on_done(issue.id, DONE_DEFERRED)
        return
    handle.set_lease(lease)

    outcome = None
    try:
        admission = tracker_adapter.begin_run_issue(issue, tracker)
        if admission != "ok":
            outcome = DONE_SKIPPED
        else:
            outcome = _run_on_worker_host(
                issue, settings, on_update, handle, worker_host,
                attempt=opts.get("attempt"), workflow_path=workflow_path)
    except _Cancelled:
        outcome = DONE_CANCELLED
    except pi_runtime.TerminationError as error:
        if getattr(error, "session", None) is not None:
            handle.set_session(error.session)
        handle.mark_termination_unconfirmed()
        log.error("Agent run termination unconfirmed for %s: %s", context, error)
        outcome = DONE_TERMINATION_UNCONFIRMED
    except pi_runtime.PiStartupError as error:
        # Popen succeeded, so this belongs to the Agent execution budget even
        # though no usable PiSession was returned to the turn loop.
        handle.mark_agent_started()
        outcome = _fail(issue, tracker, str(error))
    except pi_runtime.TurnError as error:
        if error.reason == "cancelled":
            outcome = DONE_CANCELLED
        elif not handle.has_started():
            handle.defer(
                error.reason,
                retryable=_deferral_is_retryable(error),
                code=_error_reason(error))
            outcome = DONE_DEFERRED
        else:
            outcome = _fail(issue, tracker, error)
    except WorkflowError as error:
        if _is_control_deferral(error) or not handle.has_started():
            handle.defer(
                error.describe(),
                retryable=_deferral_is_retryable(error),
                code=_error_reason(error))
            outcome = DONE_DEFERRED
        else:
            outcome = _fail(issue, tracker, error.describe())
    except Exception as error:  # noqa: BLE001 - runner isolates all failures
        if _is_control_deferral(error) or not handle.has_started():
            handle.defer(
                error,
                retryable=_deferral_is_retryable(error),
                code=_error_reason(error))
            outcome = DONE_DEFERRED
        else:
            outcome = _fail(issue, tracker, error)
    finally:
        handle.release_confirmed_lease()
    on_done(issue.id, outcome or DONE_NORMAL)


def _error_reason(error):
    reason = getattr(error, "reason", None)
    if reason == "issue_state_refresh_failed":
        cause = error.__cause__
        if isinstance(cause, BaseException):
            return _error_reason(cause)
    if isinstance(reason, str):
        return reason
    if isinstance(reason, tuple) and reason:
        return str(reason[0])
    return None


def _deferral_is_retryable(error):
    """Classify failures before Pi starts without hiding local misconfiguration."""
    if getattr(error, "reason", None) == "issue_state_refresh_failed" \
            and isinstance(error.__cause__, BaseException):
        return _deferral_is_retryable(error.__cause__)
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    reason = _error_reason(error)
    if reason in _RETRYABLE_DEFERRAL_REASONS:
        return True
    if reason in {"github_api_status", "linear_api_http_error"}:
        detail = getattr(error, "detail", None)
        status = detail.get("status") if isinstance(detail, dict) else detail
        if isinstance(status, int) and not isinstance(status, bool):
            return status in {408, 425, 429} or status >= 500
        return False
    if isinstance(reason, str) and reason.startswith("github_status_"):
        try:
            status = int(reason.rsplit("_", 1)[1])
        except (TypeError, ValueError):
            return False
        return status in {408, 425, 429} or status >= 500
    return False


def _is_control_deferral(error):
    """Return whether an error came from the tracker/control plane."""
    if getattr(error, "reason", None) == "issue_state_refresh_failed" \
            and isinstance(error.__cause__, BaseException):
        return True
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    reason = _error_reason(error)
    return reason in _CONTROL_DEFERRAL_REASONS \
        or (isinstance(reason, str) and reason.startswith("github_status_"))


def _fail(issue, tracker, reason):
    try:
        tracker_adapter.record_issue_failure(issue, tracker, reason)
    except Exception:  # noqa: BLE001
        pass
    log.error("Agent run failed for %s: %s", _issue_context(issue), reason)
    return reason


def _run_on_worker_host(issue, settings, on_update, handle, worker_host,
                        attempt=None, workflow_path=None):
    context = _issue_context(issue)
    log.info("Starting worker attempt for %s worker_host=%s",
             context, worker_host or "local")
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
        reason = _run_agent_turns(
            workspace, issue, settings, on_update, handle, worker_host, attempt)
    finally:
        workspace_mod.run_after_run(
            settings, issue, workflow_path=workflow_path,
            worker_host=worker_host)
    return reason or DONE_NORMAL


def _run_agent_turns(workspace, issue, settings, on_update, handle, worker_host,
                     attempt):
    if handle.cancel_event.is_set():
        raise _Cancelled()
    if settings.runtime.kind != "pi":
        raise WorkflowError("unsupported_runtime_kind", settings.runtime.kind)
    session = pi_runtime.start_session(
        workspace, settings, worker_host=worker_host,
        run_lease=handle.current_lease(), issue=issue)
    handle.set_session(session)
    handle.mark_agent_started()
    try:
        return _do_run_turns(
            session, workspace, issue, settings, on_update, handle, 1, attempt)
    finally:
        confirmed = pi_runtime.stop_session(session)
        if confirmed:
            handle.set_session(None)
            handle.mark_termination_confirmed()
        else:
            handle.mark_termination_unconfirmed()
            raise pi_runtime.TerminationError(
                "pi process tree is still alive after SIGKILL escalation")


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
    if refreshed is None:
        return DONE_NORMAL
    if turn_number < max_turns:
        log.info(
            "Continuing agent run for %s after normal turn completion turn=%s/%s",
            _issue_context(refreshed), turn_number, max_turns)
        return _do_run_turns(
            session, workspace, refreshed, settings, on_update, handle,
            turn_number + 1, attempt)
    log.info(
        "Reached agent.max_turns for %s with issue still active; "
        "returning control to orchestrator", _issue_context(refreshed))
    return DONE_NORMAL


def _continue_with_issue(issue, settings):
    """Returns a refreshed Issue when it is still active (continue), or
    ``None`` when it left the active workflow (end the run)."""
    try:
        issues = tracker_adapter.fetch_issues_by_ids(
            settings.tracker, [issue.id])
    except Exception as error:  # noqa: BLE001 - preserve control-plane detail
        raise WorkflowError(
            "issue_state_refresh_failed", str(error)) from error
    refreshed = next(
        (candidate for candidate in issues if candidate.id == issue.id), None)
    if refreshed is None:
        return None
    active = {
        state.strip().lower()
        for state in (settings.tracker.active_states or [])
    }
    if (refreshed.state or "").strip().lower() in active \
            and tracker_adapter.routable(refreshed, settings.tracker):
        return refreshed
    return None


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
