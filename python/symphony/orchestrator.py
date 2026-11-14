"""Orchestrator: polls the tracker and dispatches issues to agent runners.

Single-threaded mailbox loop equivalent to the Elixir GenServer:

- ``queue.Queue`` plays the role of the process mailbox; runner threads and
  timers only ever push messages, never touch state directly.
- ``threading.Thread`` per issue plays the role of the supervised Task;
  a worker-done message replaces ``:DOWN`` monitoring.
- ``threading.Timer`` plays the role of ``Process.send_after`` for poll ticks
  and retry backoff.
"""

import datetime
import itertools
import logging
import queue
import re
import threading
import time

from . import config as config_mod
from . import workspace as workspace_mod
from .agent import runner as agent_runner
from . import tracker as tracker_adapter
from .util import normalize_issue_state, present_string
from .workflow import WorkflowError

log = logging.getLogger("symphony.orchestrator")

CONTINUATION_RETRY_DELAY_MS = 1_000
CONTROL_RETRY_DELAY_MS = 1_000
CONTROL_RETRY_MAX_MS = 30_000
CONTROL_DEFERRAL_LIMIT = 30
FAILURE_RETRY_BASE_MS = 10_000
STOP_GRACE_S = 2.0
STOP_FORCE_WAIT_S = 3.0
TERMINATION_PROBE_INTERVAL_MS = 30_000

_EMPTY_TOTALS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "seconds_running": 0,
}


class _WorkspaceIssue:
    def __init__(self, identifier):
        self.identifier = identifier


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _monotonic_ms():
    return int(time.monotonic() * 1000)


def _issue_context(issue):
    return "issue_id=%s issue_identifier=%s" % (issue.id, issue.identifier)


class _Call:
    """Synchronous call into the orchestrator loop (snapshot/request_refresh)."""

    def __init__(self, fn):
        self.fn = fn
        self.event = threading.Event()
        self.result = None
        self.error = None

    def complete(self, state):
        try:
            self.result = self.fn(state)
        except Exception as error:  # noqa: BLE001
            self.error = error
        self.event.set()


class Orchestrator:
    def __init__(self, store, session_reporter=None):
        self._store = store
        self.session_reporter = session_reporter
        self._mailbox = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self._ref_counter = itertools.count(1)
        self._started = False

        settings = store.settings()
        self.poll_interval_ms = settings.polling.interval_ms
        self.max_concurrent_agents = settings.agent.max_concurrent_agents
        self.next_poll_due_at_ms = _monotonic_ms()
        self.poll_check_in_progress = False
        self.tick_timer = None
        self.tick_token = None

        self.running = {}
        self.claimed = set()
        self.blocked = {}
        self.retry_attempts = {}
        self.codex_totals = dict(_EMPTY_TOTALS)
        self.codex_rate_limits = None
        self._published = None
        self.last_poll_started_at = None
        self.last_poll_completed_at = None
        self.last_poll_success_at = None
        self.last_poll_error = None
        self.poll_count = 0
        self.metrics = {
            "dispatch_count": 0,
            "retry_count": 0,
            "control_retry_count": 0,
            "blocked_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "dispatch_latency_ms_total": 0,
            "dispatch_latency_ms_last": None,
            "dispatch_latency_samples": 0,
            "run_seconds_total": 0,
            "run_samples": 0,
        }
        self._first_seen_at = {}
        self.paused = False

    # -- lifecycle ------------------------------------------------------

    def start(self):
        self._run_terminal_workspace_cleanup()
        self._thread = threading.Thread(
            target=self._loop, name="orchestrator", daemon=True)
        self._started = True
        self.tick_token = self._new_token()
        self._thread.start()
        self._mailbox.put(("tick", self.tick_token))
        return self

    def stop(self):
        self._stop.set()
        self._mailbox.put(("stop",))
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        if self.tick_timer:
            self.tick_timer.cancel()
        for retry in self.retry_attempts.values():
            timer = retry.get("timer")
            if timer:
                timer.cancel()
        for entry in list(self.running.values()):
            handle = entry.get("handle")
            if handle:
                handle.cancel()
        for entry in list(self.running.values()):
            thread = entry.get("thread")
            if thread:
                thread.join(timeout=STOP_GRACE_S)
                if thread.is_alive():
                    handle = entry.get("handle")
                    if handle:
                        handle.force_terminate()
                    thread.join(timeout=STOP_FORCE_WAIT_S)
                if thread.is_alive():
                    log.error(
                        "Agent runner still alive during orchestrator stop: "
                        "issue_id=%s", entry.get("identifier"))

    # -- public API (thread-safe) -----------------------------------------

    def snapshot(self, timeout_s=15):
        # Fast path: the loop publishes an immutable snapshot after every
        # handled message, so readers never queue behind a long poll cycle.
        # Before the first publish (or if the loop is wedged entirely) fall
        # back to the mailbox call.
        published = self._published
        if published is not None:
            # A cached snapshot is useful even when a poll is slow, but it
            # must not hide a dead scheduler thread or an old publication.
            result = dict(published)
            result["scheduler_alive"] = bool(
                self._thread and self._thread.is_alive())
            return result

        def handler(state):
            self._refresh_runtime_config()
            return self._snapshot(state)
        return self._call(handler, timeout_s)

    def request_refresh(self):
        return self._call(self._request_refresh, 15)

    def pause(self):
        def handler(_state):
            self.paused = True
            log.info("Scheduler paused by operator")
            return {"paused": True, "running": len(self.running)}
        return self._call(handler, 15)

    def resume(self):
        def handler(_state):
            self.paused = False
            self._schedule_tick(0)
            log.info("Scheduler resumed by operator")
            return {"paused": False, "running": len(self.running)}
        return self._call(handler, 15)

    def control_state(self):
        def handler(_state):
            return {
                "paused": self.paused,
                "counts": {
                    "running": len(self.running),
                    "retrying": len(self.retry_attempts),
                    "blocked": len(self.blocked),
                },
                "termination_unconfirmed": sum(
                    1 for entry in self.running.values()
                    if entry.get("termination_unconfirmed") is True),
            }
        return self._call(handler, 15)

    def release_unconfirmed(self, identifier):
        def handler(_state):
            for issue_id, entry in list(self.running.items()):
                if entry.get("identifier") != identifier:
                    continue
                if entry.get("termination_unconfirmed") is not True:
                    return {
                        "released": False,
                        "identifier": identifier,
                        "reason": "not_unconfirmed",
                    }
                confirmed = self._request_running_issue_stop(
                    issue_id, entry,
                    entry.get("stop_requested")
                    or {"kind": "release", "cleanup_workspace": False},
                    self._store.settings())
                return {
                    "released": bool(confirmed),
                    "identifier": identifier,
                    "reason": None if confirmed else "termination_unconfirmed",
                }
            return {
                "released": False,
                "identifier": identifier,
                "reason": "not_running",
            }
        return self._call(handler, 60)

    def cleanup_workspace(self, identifier):
        def handler(_state):
            if self._identifier_active(identifier):
                return {
                    "cleaned": False,
                    "identifier": identifier,
                    "reason": "issue_busy",
                }
            cleaned = self._cleanup_issue_workspace(
                identifier, {}, self._store.settings())
            return {
                "cleaned": bool(cleaned),
                "identifier": identifier,
                "reason": None if cleaned else "cleanup_failed",
            }
        return self._call(handler, 30)

    def _identifier_active(self, identifier):
        for collection in (self.running, self.retry_attempts, self.blocked):
            for entry in collection.values():
                if entry.get("identifier") == identifier:
                    return True
        return False

    def _call(self, fn, timeout_s):
        if not self._started:
            return "unavailable"
        call = _Call(fn)
        self._mailbox.put(("call", call))
        if not call.event.wait(timeout=timeout_s):
            return "timeout"
        if call.error is not None:
            raise call.error
        return call.result

    # -- mailbox loop -------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            try:
                message = self._mailbox.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle(message)
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("Orchestrator failed handling message=%r", message)
            self._publish_snapshot()

    def _publish_snapshot(self):
        try:
            import copy

            self._published = copy.deepcopy(self._snapshot(self))
        except Exception:  # noqa: BLE001 - publishing must never kill the loop
            log.exception("snapshot publish failed")

    def _handle(self, message):
        kind = message[0]
        if kind == "tick":
            if len(message) > 1 and message[1] != self.tick_token:
                return
            self._on_tick()
        elif kind == "call":
            message[1].complete(self)
        elif kind == "worker_done":
            _tag, issue_id, ref, reason = message
            self._on_worker_done(issue_id, ref, reason)
        elif kind == "worker_update":
            _tag, issue_id, update = message
            self._on_worker_update(issue_id, update)
        elif kind == "worker_runtime_info":
            _tag, issue_id, info = message
            self._on_worker_runtime_info(issue_id, info)
        elif kind == "run_poll_cycle":
            self._on_run_poll_cycle()
        elif kind == "retry_issue":
            if len(message) < 3:
                return
            _tag, issue_id, token = message
            self._on_retry_issue(issue_id, token)
        elif kind == "stop":
            pass
        else:
            log.debug("Orchestrator ignored message: %r", message)

    def _new_token(self):
        return next(self._ref_counter)

    # -- polling ---------------------------------------------------------

    def _on_tick(self):
        """Tick: mark the poll as checking and enqueue the actual cycle.

        Like the Elixir ``schedule_poll_cycle_start``, the heavyweight poll
        runs as the next mailbox message so anything already queued
        (worker downs, refresh calls) is handled first.
        """
        self._refresh_runtime_config()
        self.poll_check_in_progress = True
        self.next_poll_due_at_ms = None
        self.tick_timer = None
        self.tick_token = None
        self._mailbox.put(("run_poll_cycle",))

    def _on_run_poll_cycle(self):
        self.last_poll_started_at = _now()
        try:
            self._refresh_runtime_config()
            self._maybe_dispatch()
        except Exception as error:  # noqa: BLE001 - the poll loop must keep ticking
            self.last_poll_error = getattr(error, "reason", type(error).__name__)
            log.exception("poll cycle failed")
        finally:
            self.last_poll_completed_at = _now()
            self.poll_count += 1
            self._schedule_tick(self.poll_interval_ms)
            self.poll_check_in_progress = False

    def _schedule_tick(self, delay_ms):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None
        self.tick_token = self._new_token()
        token = self.tick_token
        timer = threading.Timer(
            max(delay_ms, 0) / 1000.0,
            lambda: self._mailbox.put(("tick", token)))
        timer.daemon = True
        timer.start()
        self.tick_timer = timer
        self.next_poll_due_at_ms = _monotonic_ms() + delay_ms

    def _request_refresh(self, _state):
        now_ms = _monotonic_ms()
        already_due = (
            isinstance(self.next_poll_due_at_ms, int)
            and self.next_poll_due_at_ms <= now_ms
        )
        coalesced = self.poll_check_in_progress or already_due
        if not coalesced:
            self._schedule_tick(0)
        return {
            "queued": True,
            "coalesced": coalesced,
            "requested_at": _now(),
            "operations": ["poll", "reconcile"],
        }

    def _refresh_runtime_config(self):
        settings = self._store.settings()
        self.poll_interval_ms = settings.polling.interval_ms
        self.max_concurrent_agents = settings.agent.max_concurrent_agents

    def _maybe_dispatch(self):
        self._reconcile_running_issues()
        self._reconcile_blocked_issues()
        if self.paused:
            self.last_poll_success_at = _now()
            self.last_poll_error = None
            return

        try:
            self._store.force_reload()
        except WorkflowError as error:
            self.last_poll_error = error.reason
            self._log_config_error(error)
            return
        settings = self._store.settings()
        try:
            # Source discovery shares the same in-process locks as task
            # writes; a failed discovery must never hide existing records.
            tracker_adapter.import_sources(settings.tracker)
        except Exception as error:  # noqa: BLE001
            log.warning("Source discovery failed: %r", error)
        try:
            issues = tracker_adapter.fetch_candidate_issues(settings.tracker)
        except Exception as error:  # noqa: BLE001
            self.last_poll_error = getattr(error, "reason", type(error).__name__)
            log.error("Failed to fetch from issue tracker: %r", error)
            return
        if self.last_poll_error:
            log.info("Issue tracker polling recovered")
        self.last_poll_success_at = _now()
        self.last_poll_error = None
        self._record_seen_issues(issues)
        if self._available_slots() > 0:
            self._choose_issues(issues, settings)

    def _record_seen_issues(self, issues):
        now = _now()
        seen = set()
        for issue in issues:
            issue_id = getattr(issue, "id", None)
            if not isinstance(issue_id, str) or not issue_id:
                continue
            seen.add(issue_id)
            self._first_seen_at.setdefault(issue_id, now)
        for issue_id in list(self._first_seen_at):
            if issue_id not in seen:
                self._first_seen_at.pop(issue_id, None)

    def _log_config_error(self, error):
        reason = error.reason
        if reason == "invalid_workflow_config":
            log.error("Invalid WORKFLOW.md config: %s", error.detail)
        elif reason == "missing_workflow_file":
            log.error("Missing WORKFLOW.md at %s: %s", error.detail[0], error.detail[1])
        elif reason == "workflow_front_matter_not_a_map":
            log.error("Failed to parse WORKFLOW.md: workflow front matter must decode to a map")
        elif reason == "workflow_parse_error":
            log.error("Failed to parse WORKFLOW.md: %s", error.detail)
        else:
            log.error("WORKFLOW.md validation failed: %s", error.describe())

    # -- dispatch ----------------------------------------------------------

    def _available_slots(self):
        return max(self.max_concurrent_agents - len(self.running), 0)

    def _active_state_set(self, settings):
        return {
            normalize_issue_state(state)
            for state in (settings.tracker.active_states or [])
            if normalize_issue_state(state) != ""
        }

    def _terminal_state_set(self, settings):
        return {
            normalize_issue_state(state)
            for state in (settings.tracker.terminal_states or [])
            if normalize_issue_state(state) != ""
        }

    def _choose_issues(self, issues, settings):
        active = self._active_state_set(settings)
        terminal = self._terminal_state_set(settings)
        for issue in self._sort_issues(issues):
            if self._should_dispatch(issue, active, terminal, settings):
                self._dispatch_issue(issue, settings)

    @staticmethod
    def _sort_issues(issues):
        def key(issue):
            priority = issue.priority if isinstance(issue.priority, int) \
                and not isinstance(issue.priority, bool) and 1 <= issue.priority <= 4 else 5
            created = issue.created_at
            created_key = (
                int(created.timestamp() * 1_000_000)
                if isinstance(created, datetime.datetime)
                else 9_223_372_036_854_775_807
            )
            return (priority, created_key, issue.identifier or issue.id or "")
        return sorted(issues, key=key)

    def _candidate_issue(self, issue, active, terminal, settings):
        if not all(present_string(v) for v in (
                issue.id, issue.identifier, issue.title, issue.state)):
            return False
        if not tracker_adapter.routable(issue, settings.tracker):
            return False
        normalized = normalize_issue_state(issue.state)
        return normalized in active and normalized not in terminal

    def _should_dispatch(self, issue, active, terminal, settings):
        return (
            self._candidate_issue(issue, active, terminal, settings)
            and issue.id not in self.claimed
            and issue.id not in self.running
            and issue.id not in self.blocked
            and issue.id not in self.retry_attempts
            and self._available_slots() > 0
            and self._state_slots_available(issue, settings)
            and self._worker_slots_available(settings)
        )

    def _state_slots_available(self, issue, settings):
        limit = config_mod.max_concurrent_agents_for_state(settings, issue.state)
        normalized = normalize_issue_state(issue.state)
        used = sum(
            1 for entry in self.running.values()
            if entry.get("issue") is not None
            and normalize_issue_state(entry["issue"].state or "") == normalized
        )
        return limit > used

    def _worker_slots_available(self, settings, preferred=None):
        return self._select_worker_host(settings, preferred) != "no_worker_capacity"

    def _select_worker_host(self, settings, preferred):
        hosts = [host for host in (settings.worker.ssh_hosts or []) if host]
        if not hosts:
            return None
        limit = settings.worker.max_concurrent_agents_per_host
        available = [
            host for host in hosts
            if not (isinstance(limit, int) and limit > 0)
            or self._running_on_host(host) < limit
        ]
        if not available:
            return "no_worker_capacity"
        if preferred and preferred in available:
            return preferred
        return min(
            enumerate(available),
            key=lambda pair: (self._running_on_host(pair[1]), pair[0]),
        )[1]

    def _running_on_host(self, host):
        return sum(
            1 for entry in self.running.values()
            if entry.get("worker_host") == host)

    def _dispatch_issue(self, issue, settings, attempt=None, preferred_host=None):
        refreshed = self._refresh_issue_for_dispatch(issue, settings)
        if not hasattr(refreshed, "id"):
            return
        worker_host = self._select_worker_host(settings, preferred_host)
        if worker_host == "no_worker_capacity":
            log.debug(
                "No SSH worker slots available for %s preferred_worker_host=%r",
                _issue_context(refreshed), preferred_host)
            return
        self._spawn_issue(refreshed, settings, attempt, worker_host)

    def _refresh_issue_for_dispatch(self, issue, settings):
        """Returns the refreshed Issue, ``"skip"`` (gone/stale) or ``"error"``."""
        try:
            issues = tracker_adapter.fetch_issues_by_ids(settings.tracker, [issue.id])
        except Exception as error:  # noqa: BLE001
            log.warning(
                "Skipping dispatch; issue refresh failed for %s: %r",
                _issue_context(issue), error)
            return ("error", error)
        refreshed = next(
            (candidate for candidate in issues if candidate.id == issue.id), None)
        if refreshed is None:
            log.info(
                "Skipping dispatch; issue no longer active or visible: %s",
                _issue_context(issue))
            return ("skip", None)
        terminal = self._terminal_state_set(settings)
        active = self._active_state_set(settings)
        if not self._candidate_issue(refreshed, active, terminal, settings):
            log.info(
                "Skipping stale dispatch after issue refresh: %s state=%r "
                "blocked_by=%d",
                _issue_context(refreshed), refreshed.state,
                len(refreshed.blocked_by or []))
            return ("skip", refreshed)
        return refreshed

    def _spawn_issue(self, issue, settings, attempt, worker_host,
                     control_state=None):
        control_state = control_state or {}
        ref = self._new_token()
        handle = agent_runner.RunHandle()

        def on_update(issue_id, update):
            if isinstance(update, dict) and update.get("event") == "worker_runtime_info":
                self._mailbox.put(("worker_runtime_info", issue_id, {
                    "worker_host": update.get("worker_host"),
                    "workspace_path": update.get("workspace_path"),
                }))
            else:
                self._mailbox.put(("worker_update", issue_id, update))

        def on_done(issue_id, reason):
            self._mailbox.put(("worker_done", issue_id, ref, reason))

        def target():
            try:
                agent_runner.run(
                    issue, settings, on_update, on_done, handle,
                    opts={"worker_host": worker_host, "attempt": attempt})
            except Exception as error:  # noqa: BLE001 - last-resort isolation
                log.exception("Agent runner crashed for %s", _issue_context(issue))
                on_done(issue.id, error)

        thread = threading.Thread(
            target=target, name="runner-%s" % issue.identifier, daemon=True)
        entry = {
            "thread": thread,
            "ref": ref,
            "handle": handle,
            "identifier": issue.identifier,
            "issue": issue,
            "worker_host": worker_host,
            "workspace_path": None,
            "session_id": None,
            "last_codex_message": None,
            "last_codex_timestamp": None,
            "last_codex_event": None,
            "codex_app_server_pid": None,
            "codex_input_tokens": 0,
            "codex_output_tokens": 0,
            "codex_total_tokens": 0,
            "codex_last_reported_input_tokens": 0,
            "codex_last_reported_output_tokens": 0,
            "codex_last_reported_total_tokens": 0,
            "turn_count": 0,
            "retry_attempt": attempt if isinstance(attempt, int) and attempt > 0 else 0,
            "control_attempt": control_state.get("control_attempt", 0),
            "bounded_attempt": control_state.get("bounded_attempt", 0),
            "wait_attempt": control_state.get("wait_attempt", 0),
            "control_policy": control_state.get("control_policy"),
            "deferred_code": control_state.get("deferred_code"),
            "stop_requested": None,
            "started_at": _now(),
        }
        # Register ownership before the worker can enqueue worker_done. The
        # mailbox event may arrive immediately, but it is not handled until
        # this method returns; the running entry must already exist by then.
        self.running[issue.id] = entry
        self.claimed.add(issue.id)
        try:
            thread.start()
        except Exception as error:  # noqa: BLE001
            log.error("Unable to spawn agent for %s: %r", _issue_context(issue), error)
            self.running.pop(issue.id, None)
            execution_attempt = (
                attempt if isinstance(attempt, int) and attempt > 0 else 0)
            self._schedule_control_retry(issue.id, execution_attempt, {
                "identifier": issue.identifier,
                "issue_url": issue.url,
                "error": "failed to spawn agent: %r" % error,
                "issue": issue,
                "control_attempt": control_state.get("control_attempt", 0),
                "bounded_attempt": control_state.get("bounded_attempt", 0),
                "wait_attempt": control_state.get("wait_attempt", 0),
                "control_policy": "bounded",
                "deferred_code": control_state.get("deferred_code"),
                "worker_host": worker_host,
            }, settings, policy="bounded")
            return

        log.info(
            "Agent runner spawned for issue: %s attempt=%r worker_host=%s",
            _issue_context(issue), attempt, worker_host or "local")
        self.metrics["dispatch_count"] += 1
        self._record_dispatch_latency(issue.id)
        self.retry_attempts.pop(issue.id, None)
        if self.session_reporter:
            self.session_reporter.started(
                issue, attempt=attempt, model=settings.runtime.model)

    def _record_dispatch_latency(self, issue_id):
        started = self._first_seen_at.pop(issue_id, None)
        if not isinstance(started, datetime.datetime):
            return
        latency_ms = max(0, int((_now() - started).total_seconds() * 1000))
        self.metrics["dispatch_latency_ms_total"] += latency_ms
        self.metrics["dispatch_latency_ms_last"] = latency_ms
        self.metrics["dispatch_latency_samples"] += 1

    # -- worker lifecycle -------------------------------------------------

    def _on_worker_done(self, issue_id, ref, reason):
        entry = self.running.get(issue_id)
        if entry is None or entry.get("ref") != ref:
            return
        if entry.get("stop_requested"):
            entry["thread_exited"] = True
            self._record_session_completion_totals(entry)
            self._finalize_stopped_issue(issue_id, entry)
            return
        entry = self.running.pop(issue_id)
        self._record_session_completion_totals(entry)
        session_id = entry.get("session_id") or "n/a"
        self._finish_agent_session(entry, reason)
        if reason == agent_runner.DONE_SKIPPED:
            # Claim-time recheck found the task out of active states;
            # release so a later poll can pick it up if it returns.
            log.info(
                "Agent run skipped for issue_id=%s: task not in an active "
                "state; releasing claim", issue_id)
            self._release_issue_claim(issue_id)
        elif reason == agent_runner.DONE_DEFERRED:
            handle = entry.get("handle")
            deferred_reason = getattr(handle, "deferred_reason", None) \
                or "agent execution deferred by control plane"
            retryable = getattr(handle, "deferred_retryable", True)
            deferred_code = getattr(handle, "deferred_code", None)
            deferral_policy = getattr(
                handle, "deferred_policy", "bounded" if retryable else "permanent")
            if retryable is False:
                error = (
                    "agent execution blocked before start: %s"
                    % deferred_reason)
                log.error(
                    "Agent execution has a local permanent error for "
                    "issue_id=%s: %s", issue_id, deferred_reason)
                self._block_issue_from_entry(
                    issue_id, entry, error, deferred_code=deferred_code)
                return
            log.warning(
                "Agent execution deferred for issue_id=%s: %s",
                issue_id, deferred_reason)
            self.claimed.add(issue_id)
            self._schedule_control_retry(
                issue_id,
                entry.get("retry_attempt")
                if isinstance(entry.get("retry_attempt"), int) else 0,
                {
                    "identifier": entry.get("identifier"),
                    "issue_url": entry["issue"].url if entry.get("issue") else None,
                    "error": deferred_reason,
                    "deferred_code": deferred_code,
                    "issue": entry.get("issue"),
                    "control_attempt": entry.get("control_attempt", 0),
                    "bounded_attempt": entry.get("bounded_attempt", 0),
                    "wait_attempt": entry.get("wait_attempt", 0),
                    "control_policy": deferral_policy,
                    "worker_host": entry.get("worker_host"),
                    "workspace_path": entry.get("workspace_path"),
                },
                policy=deferral_policy)
        elif reason == agent_runner.DONE_CANCELLED:
            # Host cancellation does not spend the Agent execution retry budget.
            self.claimed.add(issue_id)
            self._schedule_control_retry(
                issue_id,
                entry.get("retry_attempt")
                if isinstance(entry.get("retry_attempt"), int) else 0,
                {
                    "identifier": entry.get("identifier"),
                    "issue_url": entry["issue"].url if entry.get("issue") else None,
                    "error": "agent execution cancelled",
                    "issue": entry.get("issue"),
                    "worker_host": entry.get("worker_host"),
                    "workspace_path": entry.get("workspace_path"),
                },
                policy="wait")
        elif reason == agent_runner.DONE_TERMINATION_UNCONFIRMED:
            self._record_termination_unconfirmed(issue_id, entry)
        elif reason == agent_runner.DONE_NORMAL:
            if self._input_required_blocker(entry):
                self._block_input_required(issue_id, entry, session_id, "normal")
            else:
                self.metrics["completed_count"] += 1
                log.info(
                    "Agent task completed for issue_id=%s session_id=%s; "
                    "scheduling active-state continuation check",
                    issue_id, session_id)
                self.retry_attempts.pop(issue_id, None)
                self._schedule_issue_retry(issue_id, 1, {
                    "identifier": entry.get("identifier"),
                    "issue_url": entry["issue"].url if entry.get("issue") else None,
                    "delay_type": "continuation",
                    "worker_host": entry.get("worker_host"),
                    "workspace_path": entry.get("workspace_path"),
                })
        else:
            if self._input_required_blocker(entry):
                self._block_input_required(issue_id, entry, session_id, reason)
            else:
                self.metrics["failed_count"] += 1
                self._retry_agent_down(issue_id, entry, session_id, reason)
        log.info(
            "Agent task finished for issue_id=%s session_id=%s reason=%r",
            issue_id, session_id, reason)

    def _finish_agent_session(self, entry, reason):
        issue = entry.get("issue")
        if issue is None or not self.session_reporter:
            return
        if reason in {
                agent_runner.DONE_SKIPPED,
                agent_runner.DONE_DEFERRED,
                agent_runner.DONE_CANCELLED,
                agent_runner.DONE_TERMINATION_UNCONFIRMED,
        }:
            return
        normal = reason == agent_runner.DONE_NORMAL
        self.session_reporter.worker_finished(issue, normal)

    def _block_input_required(self, issue_id, entry, session_id, reason):
        error = self._blocker_error(entry, "agent exited: %r" % (reason,))
        log.warning(
            "Agent task blocked for issue_id=%s issue_identifier=%s "
            "session_id=%s: %s",
            issue_id, entry.get("identifier"), session_id, error)
        self._block_issue_from_entry(issue_id, entry, error)

    def _retry_agent_down(self, issue_id, entry, session_id, reason):
        settings = self._store.settings()
        next_attempt = self._next_retry_attempt_from_running(entry) or 1
        if self._retry_limit_exceeded(next_attempt, settings):
            error = self._retry_limit_error(
                next_attempt, settings, "agent exited: %r" % (reason,))
            log.error(
                "Agent retry limit exceeded for issue_id=%s issue_identifier=%s "
                "session_id=%s; blocking: %s",
                issue_id, entry.get("identifier"), session_id, error)
            self._stop_and_block_issue(issue_id, entry, error)
            return
        log.warning(
            "Agent task exited for issue_id=%s session_id=%s reason=%r; "
            "scheduling retry", issue_id, session_id, reason)
        self._schedule_issue_retry(issue_id, next_attempt, {
            "identifier": entry.get("identifier"),
            "issue_url": entry["issue"].url if entry.get("issue") else None,
            "error": "agent exited: %r" % (reason,),
            "worker_host": entry.get("worker_host"),
            "workspace_path": entry.get("workspace_path"),
        })

    def _on_worker_runtime_info(self, issue_id, info):
        entry = self.running.get(issue_id)
        if entry is None:
            return
        if info.get("worker_host") is not None:
            entry["worker_host"] = info["worker_host"]
        if info.get("workspace_path") is not None:
            entry["workspace_path"] = info["workspace_path"]

    def _on_worker_update(self, issue_id, update):
        entry = self.running.get(issue_id)
        if entry is None or not isinstance(update, dict):
            return
        if "event" not in update or "timestamp" not in update:
            return
        if self.session_reporter:
            self.session_reporter.progress(entry.get("issue"), update)
        token_delta = self._extract_token_delta(entry, update)
        entry["codex_input_tokens"] = entry.get("codex_input_tokens", 0) + token_delta["input_tokens"]
        entry["codex_output_tokens"] = entry.get("codex_output_tokens", 0) + token_delta["output_tokens"]
        entry["codex_total_tokens"] = entry.get("codex_total_tokens", 0) + token_delta["total_tokens"]
        entry["codex_last_reported_input_tokens"] = max(
            entry.get("codex_last_reported_input_tokens", 0), token_delta["input_reported"])
        entry["codex_last_reported_output_tokens"] = max(
            entry.get("codex_last_reported_output_tokens", 0), token_delta["output_reported"])
        entry["codex_last_reported_total_tokens"] = max(
            entry.get("codex_last_reported_total_tokens", 0), token_delta["total_reported"])

        entry["last_codex_timestamp"] = update.get("timestamp")
        entry["last_codex_message"] = {
            "event": update.get("event"),
            "message": update.get("payload") or update.get("raw"),
            "timestamp": update.get("timestamp"),
        }
        entry["last_codex_event"] = update.get("event")
        session_id = update.get("session_id")
        if isinstance(session_id, str):
            if update.get("event") == "session_started" \
                    and session_id != entry.get("session_id"):
                entry["turn_count"] = entry.get("turn_count", 0) + 1
            entry["session_id"] = session_id
        pid = update.get("codex_app_server_pid")
        if isinstance(pid, (str, int)) and not isinstance(pid, bool):
            entry["codex_app_server_pid"] = str(pid)
        self.codex_totals["input_tokens"] = max(
            0, self.codex_totals.get("input_tokens", 0) + token_delta["input_tokens"])
        self.codex_totals["output_tokens"] = max(
            0, self.codex_totals.get("output_tokens", 0) + token_delta["output_tokens"])
        self.codex_totals["total_tokens"] = max(
            0, self.codex_totals.get("total_tokens", 0) + token_delta["total_tokens"])
        rate_limits = self._extract_rate_limits(update)
        if rate_limits is not None:
            self.codex_rate_limits = rate_limits

    # -- retry machinery ------------------------------------------------------

    def _schedule_issue_retry(self, issue_id, attempt, metadata, settings=None):
        self.metrics["retry_count"] += 1
        previous = self.retry_attempts.get(issue_id, {"attempt": 0})
        next_attempt = attempt if isinstance(attempt, int) else previous.get("attempt", 0) + 1
        settings = settings or self._store.settings()
        delay_ms = self._retry_delay(next_attempt, metadata, settings)
        old_timer = previous.get("timer")
        if old_timer:
            old_timer.cancel()
        token = self._new_token()
        due_at_ms = _monotonic_ms() + delay_ms
        entry = {
            "attempt": next_attempt,
            "timer": None,
            "retry_token": token,
            "due_at_ms": due_at_ms,
            "delay_type": metadata.get("delay_type") or previous.get("delay_type"),
            "identifier": metadata.get("identifier")
                or previous.get("identifier") or issue_id,
            "issue_url": metadata.get("issue_url") or previous.get("issue_url"),
            "error": metadata.get("error") or previous.get("error"),
            "control_attempt": metadata.get(
                "control_attempt", previous.get("control_attempt", 0)),
            "bounded_attempt": metadata.get(
                "bounded_attempt", previous.get("bounded_attempt", 0)),
            "wait_attempt": metadata.get(
                "wait_attempt", previous.get("wait_attempt", 0)),
            "control_policy": metadata.get("control_policy")
                or previous.get("control_policy") or "bounded",
            "deferred_code": metadata.get("deferred_code")
                or previous.get("deferred_code"),
            "issue": metadata.get("issue") or previous.get("issue"),
            "worker_host": metadata.get("worker_host") or previous.get("worker_host"),
            "workspace_path": metadata.get("workspace_path") or previous.get("workspace_path"),
        }
        timer = threading.Timer(
            delay_ms / 1000.0,
            lambda: self._mailbox.put(("retry_issue", issue_id, token)))
        timer.daemon = True
        timer.start()
        entry["timer"] = timer
        self.retry_attempts[issue_id] = entry
        error_suffix = " error=%s" % entry["error"] if entry["error"] else ""
        log.warning(
            "Retrying issue_id=%s issue_identifier=%s in %dms (attempt %d)%s",
            issue_id, entry["identifier"], delay_ms, next_attempt, error_suffix)

    def _schedule_control_retry(self, issue_id, attempt, metadata, settings=None,
                                policy="bounded"):
        """Defer without spending execution attempts, but with a separate bound."""
        metadata = dict(metadata or {})
        policy = policy if policy in {"bounded", "wait"} else "bounded"
        control_attempt = metadata.get("control_attempt")
        if not isinstance(control_attempt, int) or control_attempt < 0:
            previous = self.retry_attempts.get(issue_id, {})
            control_attempt = previous.get("control_attempt")
        if not isinstance(control_attempt, int) or control_attempt < 0:
            control_attempt = 0
        control_attempt += 1
        self.metrics["control_retry_count"] += 1
        metadata["control_attempt"] = control_attempt
        metadata["control_policy"] = policy
        count_key = "bounded_attempt" if policy == "bounded" else "wait_attempt"
        policy_attempt = metadata.get(count_key)
        if not isinstance(policy_attempt, int) or policy_attempt < 0:
            previous = self.retry_attempts.get(issue_id, {})
            policy_attempt = previous.get(count_key)
        if not isinstance(policy_attempt, int) or policy_attempt < 0:
            policy_attempt = 0
        policy_attempt += 1
        metadata[count_key] = policy_attempt
        if policy == "bounded" and policy_attempt > CONTROL_DEFERRAL_LIMIT:
            error = (
                "control-plane deferral limit exceeded after %d deferrals: %s; "
                "manual intervention required"
                % (policy_attempt - 1, metadata.get("error")
                   or "unknown control-plane failure"))
            log.error(
                "Control-plane deferral limit exceeded for issue_id=%s: %s",
                issue_id, error)
            self._block_issue_from_entry(issue_id, metadata, error)
            return False
        metadata["delay_type"] = "control"
        self._schedule_issue_retry(issue_id, attempt, metadata, settings)
        return True

    def _retry_delay(self, attempt, metadata, settings):
        delay_type = metadata.get("delay_type")
        if delay_type == "control":
            base_ms = max(
                CONTROL_RETRY_DELAY_MS,
                int(getattr(settings.polling, "interval_ms", 0) or 0))
            if metadata.get("control_policy") == "wait":
                return base_ms
            bounded_attempt = metadata.get("bounded_attempt", 1)
            if not isinstance(bounded_attempt, int) or bounded_attempt < 1:
                bounded_attempt = 1
            multiplier = 1 << min(bounded_attempt - 1, 5)
            return min(
                base_ms * multiplier,
                max(base_ms, CONTROL_RETRY_MAX_MS))
        if delay_type == "continuation" and attempt == 1:
            return CONTINUATION_RETRY_DELAY_MS
        return self._failure_retry_delay(attempt, settings)

    @staticmethod
    def _failure_retry_delay(attempt, settings):
        power = min(attempt - 1, 10)
        return min(
            FAILURE_RETRY_BASE_MS * (1 << power),
            settings.agent.max_retry_backoff_ms)

    @staticmethod
    def _retry_limit_exceeded(attempt, settings):
        limit = getattr(settings.agent, "max_retry_attempts", 0)
        return (
            isinstance(limit, int) and limit > 0
            and isinstance(attempt, int) and attempt > limit
        )

    @staticmethod
    def _retry_limit_error(attempt, settings, reason):
        limit = getattr(settings.agent, "max_retry_attempts", 0)
        return (
            "retry limit exceeded after %d failed attempt(s) "
            "(agent.max_retry_attempts=%d retries allowed): %s; "
            "manual intervention required"
            % (attempt, limit, reason)
        )

    def _on_retry_issue(self, issue_id, token):
        entry = self.retry_attempts.get(issue_id)
        if entry is None or entry.get("retry_token") != token:
            return
        attempt = entry["attempt"]
        metadata = {
            "identifier": entry.get("identifier"),
            "issue_url": entry.get("issue_url"),
            "error": entry.get("error"),
            "control_attempt": entry.get("control_attempt", 0),
            "bounded_attempt": entry.get("bounded_attempt", 0),
            "wait_attempt": entry.get("wait_attempt", 0),
            "control_policy": entry.get("control_policy") or "bounded",
            "deferred_code": entry.get("deferred_code"),
            "issue": entry.get("issue"),
            "worker_host": entry.get("worker_host"),
            "workspace_path": entry.get("workspace_path"),
            "delay_type": entry.get("delay_type"),
        }
        self.retry_attempts.pop(issue_id, None)
        settings = self._store.settings()
        try:
            issues = tracker_adapter.fetch_issues_by_ids(settings.tracker, [issue_id])
        except Exception as error:  # noqa: BLE001
            log.warning(
                "Retry poll failed for issue_id=%s issue_identifier=%s: %r",
                issue_id, metadata.get("identifier") or issue_id, error)
            metadata["error"] = "retry poll failed: %r" % error
            self._schedule_control_retry(issue_id, attempt, metadata, settings)
            return
        issue = next(
            (candidate for candidate in issues if candidate.id == issue_id), None)
        self._handle_retry_issue_lookup(issue, issue_id, attempt, metadata, settings)

    def _handle_retry_issue_lookup(self, issue, issue_id, attempt, metadata, settings):
        if issue is None:
            log.debug("Issue no longer visible, removing claim issue_id=%s", issue_id)
            self._release_issue_claim(issue_id)
            return
        terminal = self._terminal_state_set(settings)
        if normalize_issue_state(issue.state or "") in terminal:
            log.info(
                "Issue state is terminal: %s state=%s; removing associated workspace",
                _issue_context(issue), issue.state)
            self._cleanup_issue_workspace(issue, metadata, settings)
            self._release_issue_claim(issue_id)
            return
        active = self._active_state_set(settings)
        if self._candidate_issue(issue, active, terminal, settings):
            self._handle_active_retry(issue, attempt, metadata, settings)
            return
        log.debug(
            "Issue left active states, removing claim issue_id=%s "
            "issue_identifier=%s", issue_id, issue.identifier)
        self._release_issue_claim(issue_id)

    def _handle_active_retry(self, issue, attempt, metadata, settings):
        metadata["issue"] = issue
        terminal = self._terminal_state_set(settings)
        active = self._active_state_set(settings)
        slots = self._candidate_issue(issue, active, terminal, settings) and (
            self._available_slots() > 0
            and self._state_slots_available(issue, settings)
            and self._worker_slots_available(settings, metadata.get("worker_host")))
        if slots:
            if metadata.get("delay_type") not in {"continuation", "control"} \
                    and self._retry_limit_exceeded(attempt, settings):
                error = self._retry_limit_error(
                    attempt, settings,
                    metadata.get("error")
                    or "retry limit reached before dispatch")
                log.error(
                    "Retry limit exceeded before dispatch for %s; blocking: %s",
                    _issue_context(issue), error)
                self._block_issue_from_entry(issue.id, {
                    "identifier": issue.identifier,
                    "issue": issue,
                    "worker_host": metadata.get("worker_host"),
                    "workspace_path": metadata.get("workspace_path"),
                }, error)
                return
            refreshed = self._refresh_issue_for_dispatch(issue, settings)
            if isinstance(refreshed, tuple) and refreshed[0] == "error":
                metadata["identifier"] = issue.identifier
                metadata["error"] = \
                    "retry dispatch refresh failed: %r" % (refreshed[1],)
                self._schedule_control_retry(
                    issue.id, attempt, metadata, settings)
                return
            if not hasattr(refreshed, "id"):
                stale = refreshed[1] if isinstance(refreshed, tuple) else None
                self._handle_retry_issue_lookup(
                    stale, issue.id, attempt, metadata, settings)
                return
            self._dispatch_refreshed(refreshed, settings, attempt, metadata)
            return
        log.debug("No available slots for retrying %s; retrying again",
                  _issue_context(issue))
        metadata["identifier"] = issue.identifier
        metadata["error"] = "no available orchestrator slots"
        self._schedule_control_retry(
            issue.id, attempt, metadata, settings, policy="wait")

    def _dispatch_refreshed(self, issue, settings, attempt, metadata):
        worker_host = self._select_worker_host(settings, metadata.get("worker_host"))
        if worker_host == "no_worker_capacity":
            metadata["identifier"] = issue.identifier
            metadata["error"] = "no available orchestrator slots"
            self._schedule_control_retry(
                issue.id, attempt, metadata, settings, policy="wait")
            return
        self._spawn_issue(
            issue, settings, attempt, worker_host,
            control_state={
                "control_attempt": metadata.get("control_attempt", 0),
                "bounded_attempt": metadata.get("bounded_attempt", 0),
                "wait_attempt": metadata.get("wait_attempt", 0),
                "control_policy": metadata.get("control_policy"),
                "deferred_code": metadata.get("deferred_code"),
            })

    def _release_issue_claim(self, issue_id):
        self.claimed.discard(issue_id)
        self.blocked.pop(issue_id, None)
        retry = self.retry_attempts.pop(issue_id, None)
        if retry and retry.get("timer"):
            retry["timer"].cancel()

    def _block_issue_from_entry(self, issue_id, entry, error, **extra):
        self.metrics["blocked_count"] += 1
        blocked_entry = {
            "issue_id": issue_id,
            "identifier": entry.get("identifier", issue_id),
            "issue": entry.get("issue"),
            "worker_host": entry.get("worker_host"),
            "workspace_path": entry.get("workspace_path"),
            "session_id": entry.get("session_id") or "n/a",
            "error": error,
            "blocked_at": _now(),
            "last_codex_message": entry.get("last_codex_message"),
            "last_codex_event": entry.get("last_codex_event"),
            "last_codex_timestamp": entry.get("last_codex_timestamp"),
        }
        blocked_entry.update(extra)
        self.running.pop(issue_id, None)
        retry = self.retry_attempts.pop(issue_id, None)
        if retry and retry.get("timer"):
            retry["timer"].cancel()
        self.claimed.add(issue_id)
        self.blocked[issue_id] = blocked_entry
        if self.session_reporter:
            self.session_reporter.blocked(entry.get("issue"))

    # -- reconciliation -------------------------------------------------------

    def _reconcile_running_issues(self):
        self._recheck_unconfirmed_terminations()
        settings = self._store.settings()
        self._reconcile_stalled_running_issues(settings)
        running_ids = list(self.running.keys())
        if not running_ids:
            return
        try:
            issues = tracker_adapter.fetch_issues_by_ids(settings.tracker, running_ids)
        except Exception as error:  # noqa: BLE001
            log.debug(
                "Failed to refresh running issue states: %r; keeping active workers",
                error)
            return
        active = self._active_state_set(settings)
        terminal = self._terminal_state_set(settings)
        visible = set()
        for issue in issues:
            if issue.id in self.running:
                visible.add(issue.id)
                self._reconcile_issue_state(issue, active, terminal, settings)
        for issue_id in running_ids:
            if issue_id not in visible and issue_id in self.running:
                self._log_missing_running_issue(issue_id)
                self._terminate_running_issue(issue_id, False, settings)

    def _recheck_unconfirmed_terminations(self):
        now_ms = _monotonic_ms()
        for issue_id, entry in list(self.running.items()):
            if entry.get("termination_unconfirmed") is not True:
                continue
            last_probe = entry.get("termination_probe_at_ms")
            if isinstance(last_probe, int) \
                    and now_ms - last_probe < TERMINATION_PROBE_INTERVAL_MS:
                continue
            entry["termination_probe_at_ms"] = now_ms
            handle = entry.get("handle")
            confirmed = handle.force_terminate() \
                if handle is not None and hasattr(handle, "force_terminate") \
                else False
            if not confirmed:
                log.warning(
                    "Termination still unconfirmed issue_id=%s identifier=%s",
                    issue_id, entry.get("identifier"))
                continue
            entry["termination_unconfirmed"] = False
            log.info(
                "Termination confirmed on recheck; releasing claim and slot "
                "issue_id=%s identifier=%s",
                issue_id, entry.get("identifier"))
            self._finalize_stopped_issue(issue_id, entry)

    def _reconcile_issue_state(self, issue, active, terminal, settings):
        state_name = normalize_issue_state(issue.state or "")
        if state_name in terminal:
            log.info(
                "Issue moved to terminal state: %s state=%s; stopping active agent",
                _issue_context(issue), issue.state)
            if self.session_reporter:
                self.session_reporter.stopped(issue)
            self._terminate_running_issue(issue.id, True, settings)
        elif not tracker_adapter.routable(issue, settings.tracker):
            log.info(
                "Issue no longer routed to this worker: %s assignee=%r; "
                "stopping active agent", _issue_context(issue), issue.assignee_id)
            if self.session_reporter:
                self.session_reporter.stopped(issue)
            self._terminate_running_issue(issue.id, False, settings)
        elif state_name in active:
            entry = self.running.get(issue.id)
            if entry is not None and entry.get("issue") is not None:
                entry["issue"] = issue
        else:
            log.info(
                "Issue moved to non-active state: %s state=%s; stopping active agent",
                _issue_context(issue), issue.state)
            if self.session_reporter:
                if settings.delivery.enabled and state_name in {
                        normalize_issue_state(settings.delivery.review_state),
                        normalize_issue_state(settings.delivery.merge_state)}:
                    self.session_reporter.worker_finished(issue, True)
                else:
                    self.session_reporter.stopped(issue)
            self._terminate_running_issue(issue.id, False, settings)

    def _reconcile_blocked_issues(self):
        settings = self._store.settings()
        blocked_ids = list(self.blocked.keys())
        if not blocked_ids:
            return
        try:
            issues = tracker_adapter.fetch_issues_by_ids(settings.tracker, blocked_ids)
        except Exception as error:  # noqa: BLE001
            log.debug(
                "Failed to refresh blocked issue states: %r; keeping blocked issues",
                error)
            return
        active = self._active_state_set(settings)
        terminal = self._terminal_state_set(settings)
        visible = {issue.id for issue in issues if issue.id in self.blocked}
        for issue in issues:
            if issue.id in self.blocked:
                self._reconcile_blocked_issue_state(issue, active, terminal, settings)
        for issue_id in blocked_ids:
            if issue_id not in visible and issue_id in self.blocked:
                log.info(
                    "Blocked issue no longer visible during state refresh: "
                    "issue_id=%s; releasing block", issue_id)
                self._release_issue_claim(issue_id)

    def _reconcile_blocked_issue_state(self, issue, active, terminal, settings):
        state_name = normalize_issue_state(issue.state or "")
        if state_name in terminal:
            log.info(
                "Blocked issue moved to terminal state: %s state=%s; releasing block",
                _issue_context(issue), issue.state)
            self._cleanup_issue_workspace(
                issue, self.blocked.get(issue.id, {}), settings)
            self._release_issue_claim(issue.id)
        elif not tracker_adapter.routable(issue, settings.tracker):
            log.info(
                "Blocked issue no longer routed to this worker: %s assignee=%r; "
                "releasing block", _issue_context(issue), issue.assignee_id)
            self._release_issue_claim(issue.id)
        elif state_name in active:
            entry = self.blocked.get(issue.id)
            if entry is not None and entry.get("issue") is not None:
                entry["issue"] = issue
        else:
            log.info(
                "Blocked issue moved to non-active state: %s state=%s; "
                "releasing block", _issue_context(issue), issue.state)
            self._release_issue_claim(issue.id)

    def _reconcile_stalled_running_issues(self, settings):
        timeout_ms = _stall_timeout_ms(settings)
        if timeout_ms <= 0 or not self.running:
            return
        now = _now()
        for issue_id, entry in list(self.running.items()):
            if issue_id in self.blocked:
                continue
            handle = entry.get("handle")
            if handle is not None and hasattr(handle, "has_started") \
                    and not handle.has_started():
                continue
            last = entry.get("last_codex_timestamp") or entry.get("started_at")
            if not isinstance(last, datetime.datetime):
                continue
            elapsed_ms = max(0, int((now - last).total_seconds() * 1000))
            if elapsed_ms <= timeout_ms:
                continue
            identifier = entry.get("identifier", issue_id)
            session_id = entry.get("session_id") or "n/a"
            if self._input_required_blocker(entry):
                error = self._blocker_error(
                    entry,
                    "stalled for %dms after Codex requested operator input" % elapsed_ms)
                log.warning(
                    "Issue blocked: issue_id=%s issue_identifier=%s session_id=%s "
                    "elapsed_ms=%s; %s", issue_id, identifier, session_id,
                    elapsed_ms, error)
                self._stop_and_block_issue(issue_id, entry, error)
            else:
                log.warning(
                    "Issue stalled: issue_id=%s issue_identifier=%s session_id=%s "
                    "elapsed_ms=%s; restarting with backoff",
                    issue_id, identifier, session_id, elapsed_ms)
                next_attempt = self._next_retry_attempt_from_running(entry) or 1
                if self._retry_limit_exceeded(next_attempt, settings):
                    error = self._retry_limit_error(
                        next_attempt, settings,
                        "stalled for %dms without codex activity" % elapsed_ms)
                    log.error(
                        "Issue stalled and retry limit exceeded: issue_id=%s "
                        "issue_identifier=%s session_id=%s; blocking: %s",
                        issue_id, identifier, session_id, error)
                    self._stop_and_block_issue(issue_id, entry, error)
                else:
                    self._request_running_issue_stop(
                        issue_id, entry,
                        {
                            "kind": "retry",
                            "attempt": next_attempt,
                            "metadata": {
                                "identifier": identifier,
                                "issue_url": entry["issue"].url
                                if entry.get("issue") else None,
                                "error": (
                                    "stalled for %dms without codex activity"
                                    % elapsed_ms),
                                "worker_host": entry.get("worker_host"),
                                "workspace_path": entry.get("workspace_path"),
                            },
                        },
                        settings)

    def _log_missing_running_issue(self, issue_id):
        entry = self.running.get(issue_id)
        if entry and entry.get("identifier"):
            log.info(
                "Issue no longer visible during running-state refresh: issue_id=%s "
                "issue_identifier=%s; stopping active agent",
                issue_id, entry["identifier"])
        else:
            log.info(
                "Issue no longer visible during running-state refresh: "
                "issue_id=%s; stopping active agent", issue_id)

    def _terminate_running_issue(self, issue_id, cleanup_workspace, settings):
        entry = self.running.get(issue_id)
        if entry is None:
            self._release_issue_claim(issue_id)
            return True
        return self._request_running_issue_stop(
            issue_id, entry,
            {"kind": "release", "cleanup_workspace": bool(cleanup_workspace)},
            settings)

    def _stop_and_block_issue(self, issue_id, entry, error):
        self._request_running_issue_stop(
            issue_id, entry, {"kind": "block", "error": error},
            self._store.settings())

    def _request_running_issue_stop(self, issue_id, entry, action, settings):
        """Request stop and defer state release until the runner has exited."""
        current = entry.get("stop_requested")
        if current is None:
            entry["stop_requested"] = action
        elif action.get("kind") == "release" \
                and action.get("cleanup_workspace") is True:
            current["cleanup_workspace"] = True

        handle = entry.get("handle")
        if handle:
            handle.cancel()

        thread = entry.get("thread")
        is_current = self.running.get(issue_id) is entry
        if thread is not None:
            thread.join(timeout=STOP_GRACE_S)
            if thread.is_alive():
                log.warning(
                    "Agent did not stop within %.1fs; force-terminating Pi "
                    "process tree issue_id=%s identifier=%s",
                    STOP_GRACE_S, issue_id, entry.get("identifier"))
                confirmed = handle.force_terminate() \
                    if handle is not None and hasattr(handle, "force_terminate") \
                    else False
                if not confirmed:
                    if is_current:
                        self.running[issue_id] = entry
                    entry["termination_unconfirmed"] = True
                    log.error(
                        "Forced Pi termination was not confirmed; retaining "
                        "claim and slot issue_id=%s identifier=%s",
                        issue_id, entry.get("identifier"))
                    return False
                thread.join(timeout=STOP_FORCE_WAIT_S)
            if thread.is_alive():
                if is_current:
                    self.running[issue_id] = entry
                entry["termination_unconfirmed"] = True
                log.error(
                    "Agent runner did not exit after forced termination; "
                    "retaining claim and slot issue_id=%s identifier=%s",
                    issue_id, entry.get("identifier"))
                return False
        if handle is not None \
                and hasattr(handle, "termination_is_confirmed") \
                and not handle.termination_is_confirmed():
            # A worker can exit after reporting unconfirmed termination. Keep
            # probing the retained session so capacity is not leaked forever
            # once the orphan eventually dies.
            confirmed = handle.force_terminate() \
                if hasattr(handle, "force_terminate") else False
            if not confirmed:
                if is_current:
                    self.running[issue_id] = entry
                entry["termination_unconfirmed"] = True
                log.error(
                    "Pi termination remains unconfirmed; retaining claim and "
                    "slot issue_id=%s identifier=%s",
                    issue_id, entry.get("identifier"))
                return False

        entry["thread_exited"] = True
        if is_current:
            self._record_session_completion_totals(entry)
            self._finalize_stopped_issue(issue_id, entry, settings)
        elif action.get("kind") == "block":
            self._block_issue_from_entry(
                issue_id, entry, action.get("error") or "agent stopped")
        elif action.get("kind") == "retry":
            self.claimed.add(issue_id)
            self._schedule_issue_retry(
                issue_id, action.get("attempt"),
                action.get("metadata") or {}, settings)
        return True

    def _finalize_stopped_issue(self, issue_id, entry, settings=None):
        if self.running.get(issue_id) is not entry:
            return
        settings = settings or self._store.settings()
        handle = entry.get("handle")
        if handle is not None and hasattr(handle, "termination_is_confirmed") \
                and not handle.termination_is_confirmed():
            entry["termination_unconfirmed"] = True
            log.error(
                "Pi termination is still unconfirmed; retaining claim and slot "
                "issue_id=%s identifier=%s",
                issue_id, entry.get("identifier"))
            return
        if handle is not None and hasattr(handle, "release_confirmed_lease"):
            handle.release_confirmed_lease()

        action = entry.get("stop_requested") or {
            "kind": "release", "cleanup_workspace": False}
        kind = action.get("kind")
        if kind == "block":
            self._block_issue_from_entry(
                issue_id, entry, action.get("error") or "agent stopped")
            return
        if kind == "retry":
            self.running.pop(issue_id, None)
            self.claimed.add(issue_id)
            self._schedule_issue_retry(
                issue_id,
                action.get("attempt"),
                action.get("metadata") or {},
                settings)
            return

        if action.get("cleanup_workspace"):
            self._cleanup_issue_workspace(
                entry.get("issue") or entry.get("identifier"), entry, settings)
        self.running.pop(issue_id, None)
        self._release_issue_claim(issue_id)

    def _record_termination_unconfirmed(self, issue_id, entry):
        """Retain capacity and ownership when an orphan may still be alive."""
        entry["termination_unconfirmed"] = True
        entry.setdefault("stop_requested", {
            "kind": "release", "cleanup_workspace": False})
        self.running[issue_id] = entry
        self.claimed.add(issue_id)
        log.error(
            "Keeping issue claim and slot because Pi termination is "
            "unconfirmed issue_id=%s identifier=%s",
            issue_id, entry.get("identifier"))

    def _cleanup_issue_workspace(self, issue_or_identifier, metadata, settings):
        workspace_path = (metadata or {}).get("workspace_path") \
            if isinstance(metadata, dict) else None
        worker_host = (metadata or {}).get("worker_host") \
            if isinstance(metadata, dict) else None
        identifier = getattr(issue_or_identifier, "identifier", None)
        if identifier is None and isinstance(issue_or_identifier, str):
            identifier = issue_or_identifier
        try:
            if present_string(workspace_path):
                lease_path = workspace_path
            elif hasattr(issue_or_identifier, "identifier"):
                lease_path = workspace_mod.workspace_path(
                    settings, issue_or_identifier, self._store.path,
                    worker_host=worker_host)
            else:
                lease_path = workspace_mod.workspace_path(
                    settings, _WorkspaceIssue(identifier), self._store.path,
                    worker_host=worker_host)
            lease = workspace_mod.acquire_path_run_lease(
                settings, lease_path,
                issue_id=getattr(issue_or_identifier, "id", None),
                identifier=identifier, workflow_path=self._store.path,
                canonical=worker_host is None,
                lease_key=("remote\0%s\0%s" % (worker_host, lease_path))
                if worker_host is not None else None)
        except Exception as error:  # noqa: BLE001 - cleanup is best effort
            log.warning(
                "Unable to acquire workspace cleanup lease identifier=%s "
                "path=%s error=%r", identifier, workspace_path, error)
            return False
        if lease is None:
            log.warning(
                "Skipping workspace cleanup because its run lease is active "
                "identifier=%s path=%s",
                identifier, workspace_path)
            return False
        try:
            if present_string(workspace_path):
                try:
                    return workspace_mod.remove_recorded(
                        settings, workspace_path, worker_host=worker_host,
                        workflow_path=self._store.path)
                except WorkflowError:
                    return False
            if identifier:
                return workspace_mod.remove_issue_workspaces(
                    settings, [identifier], workflow_path=self._store.path,
                    worker_host=worker_host)
            return False
        finally:
            lease.release()

    def _run_terminal_workspace_cleanup(self):
        try:
            settings = self._store.settings()
            issues = tracker_adapter.fetch_issues_by_states(
                settings.tracker, settings.tracker.terminal_states or [])
        except Exception as error:  # noqa: BLE001
            log.warning(
                "Skipping startup terminal workspace cleanup; failed to fetch "
                "terminal issues: %r", error)
            return
        for issue in issues:
            self._cleanup_issue_workspace(issue, None, settings)

    # -- input-required / blocker helpers -------------------------------------

    @staticmethod
    def _input_required_blocker(entry):
        if not isinstance(entry, dict):
            return False
        if entry.get("last_codex_event") in ("turn_input_required", "approval_required"):
            return True
        completion = entry.get("completion")
        if isinstance(completion, dict):
            outcome = completion.get("outcome")
            if outcome in ("input_required", "needs_input", "approval_required"):
                return True
        return _codex_message_method(entry.get("last_codex_message")) == \
            "mcpServer/elicitation/request"

    @staticmethod
    def _blocker_error(entry, fallback):
        event = entry.get("last_codex_event")
        if event == "turn_input_required":
            return "codex turn requires operator input"
        if event == "approval_required":
            return "codex turn requires approval"
        completion = entry.get("completion")
        if isinstance(completion, dict):
            outcome = completion.get("outcome")
            if outcome in ("input_required", "needs_input"):
                return "codex turn requires operator input"
            if outcome == "approval_required":
                return "codex turn requires approval"
        if _codex_message_method(entry.get("last_codex_message")) == \
                "mcpServer/elicitation/request":
            return "codex MCP elicitation requires operator input"
        return fallback

    @staticmethod
    def _next_retry_attempt_from_running(entry):
        attempt = entry.get("retry_attempt")
        if isinstance(attempt, int) and attempt > 0:
            return attempt + 1
        return None

    def _record_session_completion_totals(self, entry):
        if not isinstance(entry, dict):
            return
        if entry.get("session_completion_totals_recorded") is True:
            return
        started = entry.get("started_at")
        if isinstance(started, datetime.datetime):
            entry["session_completion_totals_recorded"] = True
            seconds = max(0, int((_now() - started).total_seconds()))
            self.codex_totals["seconds_running"] = max(
                0, self.codex_totals.get("seconds_running", 0) + seconds)
            self.metrics["run_seconds_total"] += seconds
            self.metrics["run_samples"] += 1

    # -- token accounting -------------------------------------------------------

    def _extract_token_delta(self, entry, update):
        usage = _extract_token_usage(update)

        def compute(key, reported_key):
            next_total = _token_usage_value(usage, key)
            prev = entry.get(reported_key, 0)
            if isinstance(next_total, int) and next_total >= prev:
                delta = next_total - prev
            else:
                delta = 0
            return {
                "delta": max(delta, 0),
                "reported": next_total if isinstance(next_total, int) else prev,
            }

        input_result = compute("input", "codex_last_reported_input_tokens")
        output_result = compute("output", "codex_last_reported_output_tokens")
        total_result = compute("total", "codex_last_reported_total_tokens")
        return {
            "input_tokens": input_result["delta"],
            "output_tokens": output_result["delta"],
            "total_tokens": total_result["delta"],
            "input_reported": input_result["reported"],
            "output_reported": output_result["reported"],
            "total_reported": total_result["reported"],
        }

    @staticmethod
    def _extract_rate_limits(update):
        for candidate in (
            update.get("rate_limits"),
            update.get("payload"),
            update,
        ):
            result = _rate_limits_from_payload(candidate)
            if result is not None:
                return result
        return None

    # -- snapshot ----------------------------------------------------------------

    def _snapshot(self, _state):
        now = _now()
        now_ms = _monotonic_ms()
        running = []
        for issue_id, entry in self.running.items():
            running.append({
                "issue_id": issue_id,
                "identifier": entry.get("identifier"),
                "issue_url": entry["issue"].url if entry.get("issue") else None,
                "state": entry["issue"].state if entry.get("issue") else None,
                "worker_host": entry.get("worker_host"),
                "workspace_path": entry.get("workspace_path"),
                "session_id": entry.get("session_id"),
                "codex_app_server_pid": entry.get("codex_app_server_pid"),
                "codex_input_tokens": entry.get("codex_input_tokens", 0),
                "codex_output_tokens": entry.get("codex_output_tokens", 0),
                "codex_total_tokens": entry.get("codex_total_tokens", 0),
                "turn_count": entry.get("turn_count", 0),
                "started_at": entry.get("started_at"),
                "last_codex_timestamp": entry.get("last_codex_timestamp"),
                "last_codex_message": entry.get("last_codex_message"),
                "last_codex_event": entry.get("last_codex_event"),
                "runtime_seconds": _running_seconds(entry.get("started_at"), now),
            })
        retrying = []
        for issue_id, retry in self.retry_attempts.items():
            retrying.append({
                "issue_id": issue_id,
                "attempt": retry.get("attempt"),
                "due_in_ms": max(0, retry.get("due_at_ms", 0) - now_ms),
                "delay_type": retry.get("delay_type"),
                "identifier": retry.get("identifier"),
                "issue_url": retry.get("issue_url"),
                "error": retry.get("error"),
                "worker_host": retry.get("worker_host"),
                "workspace_path": retry.get("workspace_path"),
            })
        blocked = []
        for issue_id, entry in self.blocked.items():
            issue = entry.get("issue")
            blocked.append({
                "issue_id": issue_id,
                "identifier": entry.get("identifier"),
                "issue_url": issue.url if issue else None,
                "state": issue.state if issue else None,
                "worker_host": entry.get("worker_host"),
                "workspace_path": entry.get("workspace_path"),
                "session_id": entry.get("session_id"),
                "error": entry.get("error"),
                "blocked_at": entry.get("blocked_at"),
                "last_codex_timestamp": entry.get("last_codex_timestamp"),
                "last_codex_message": entry.get("last_codex_message"),
                "last_codex_event": entry.get("last_codex_event"),
            })
        return {
            "published_at": now,
            "scheduler_alive": bool(self._thread and self._thread.is_alive()),
            "paused": self.paused,
            "running": running,
            "retrying": retrying,
            "blocked": blocked,
            "codex_totals": self.codex_totals,
            "rate_limits": self.codex_rate_limits,
            "polling": {
                "last_started_at": self.last_poll_started_at,
                "last_completed_at": self.last_poll_completed_at,
                "last_success_at": self.last_poll_success_at,
                "last_error": self.last_poll_error,
                "count": self.poll_count,
                "checking": self.poll_check_in_progress is True,
                "next_poll_in_ms": (
                    max(0, self.next_poll_due_at_ms - now_ms)
                    if isinstance(self.next_poll_due_at_ms, int) else None),
                "poll_interval_ms": self.poll_interval_ms,
            },
            "metrics": self._metrics_snapshot(),
        }

    def _metrics_snapshot(self):
        metrics = self.metrics
        latency_samples = metrics["dispatch_latency_samples"]
        run_samples = metrics["run_samples"]
        dispatches = metrics["dispatch_count"]
        return {
            "dispatch": {
                "count": dispatches,
                "latency_ms_avg": (
                    round(metrics["dispatch_latency_ms_total"] / latency_samples, 1)
                    if latency_samples else None),
                "latency_ms_last": metrics["dispatch_latency_ms_last"],
            },
            "runs": {
                "completed": metrics["completed_count"],
                "failed": metrics["failed_count"],
                "active": len(self.running),
                "avg_seconds": (
                    round(metrics["run_seconds_total"] / run_samples, 1)
                    if run_samples else None),
            },
            "retries": {
                "execution": metrics["retry_count"],
                "control": metrics["control_retry_count"],
            },
            "blocked": {
                "count": metrics["blocked_count"],
                "current": len(self.blocked),
            },
            "rates": {
                "retry_per_dispatch": (
                    round(metrics["retry_count"] / dispatches, 3)
                    if dispatches else 0.0),
                "blocked_per_dispatch": (
                    round(metrics["blocked_count"] / dispatches, 3)
                    if dispatches else 0.0),
            },
            "termination_unconfirmed": sum(
                1 for entry in self.running.values()
                if entry.get("termination_unconfirmed") is True),
        }


def _running_seconds(started_at, now):
    if isinstance(started_at, datetime.datetime):
        return max(0, int((now - started_at).total_seconds()))
    return 0


def _stall_timeout_ms(settings):
    """Use the active runtime's stall timeout.

    The Python port originally only supported Codex and read
    ``codex.stall_timeout_ms``. The Pi workflow configures
    ``runtime.stall_timeout_ms``; reading the Codex value silently reduces
    Pi's configured timeout to the Codex default.
    """
    runtime = getattr(settings, "runtime", None)
    if getattr(runtime, "kind", None) == "pi":
        value = getattr(runtime, "stall_timeout_ms", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    codex = getattr(settings, "codex", None)
    value = getattr(codex, "stall_timeout_ms", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _codex_message_method(message):
    if not isinstance(message, dict):
        return None
    inner = message.get("message")
    if isinstance(inner, dict) and isinstance(inner.get("method"), str):
        return inner["method"]
    method = message.get("method")
    return method if isinstance(method, str) else None


# --------------------------------------------------------------------------
# Token usage extraction (ported from the Elixir token-delta machinery)


_TOKEN_FIELDS = {
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens",
    "completion_tokens", "inputTokens", "outputTokens", "totalTokens",
    "promptTokens", "completionTokens",
}

_ABSOLUTE_USAGE_PATHS = [
    ["params", "msg", "payload", "info", "total_token_usage"],
    ["params", "msg", "info", "total_token_usage"],
    ["params", "tokenUsage", "total"],
    ["tokenUsage", "total"],
]


def _extract_token_usage(update):
    payloads = [
        update.get("usage"),
        update.get("payload"),
        update,
    ]
    for payload in payloads:
        usage = _absolute_token_usage_from_payload(payload)
        if usage is not None:
            return usage
    for payload in payloads:
        usage = _turn_completed_usage_from_payload(payload)
        if usage is not None:
            return usage
    return {}


def _absolute_token_usage_from_payload(payload):
    if not isinstance(payload, dict):
        return None
    for path in _ABSOLUTE_USAGE_PATHS:
        value = _map_at_path(payload, path)
        if isinstance(value, dict) and _integer_token_map(value):
            return value
    return None


def _turn_completed_usage_from_payload(payload):
    if not isinstance(payload, dict):
        return None
    if payload.get("method") not in ("turn/completed", "turn_completed"):
        return None
    direct = payload.get("usage")
    if direct is None or direct is False:
        direct = _map_at_path(payload, ["params", "usage"])
    if isinstance(direct, dict) and _integer_token_map(direct):
        return direct
    return None


def _map_at_path(payload, path):
    value = payload
    for key in path:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


def _integer_token_map(payload):
    return any(
        _integer_like(payload.get(field)) is not None for field in _TOKEN_FIELDS)


def _integer_like(value):
    """Integer.parse semantics: int ≥0, or a string with an integer prefix."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        match = re.match(r"\s*[+-]?\d+", value.strip())
        if match:
            try:
                parsed = int(match.group(0))
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
    return None


def _token_usage_value(usage, key):
    if not isinstance(usage, dict):
        return None
    fields = {
        "input": [
            "input_tokens", "prompt_tokens", "input",
            "promptTokens", "inputTokens",
        ],
        "output": [
            "output_tokens", "completion_tokens", "output",
            "completion", "outputTokens", "completionTokens",
        ],
        "total": ["total_tokens", "total", "totalTokens"],
    }[key]
    for field in fields:
        value = _integer_like(usage.get(field))
        if value is not None:
            return value
    return None


def _rate_limits_map(payload):
    if not isinstance(payload, dict):
        return False
    limit_id = payload.get("limit_id")
    if limit_id is None or limit_id is False:
        limit_id = payload.get("limit_name")
    has_buckets = any(
        key in payload for key in ("primary", "secondary", "credits"))
    return limit_id is not None and limit_id is not False and has_buckets


def _rate_limits_from_payload(payload):
    if isinstance(payload, dict):
        direct = payload.get("rate_limits")
        if _rate_limits_map(direct):
            return direct
        if _rate_limits_map(payload):
            return payload
        for value in payload.values():
            result = _rate_limits_from_payload(value)
            if result is not None:
                return result
    elif isinstance(payload, list):
        for value in payload:
            result = _rate_limits_from_payload(value)
            if result is not None:
                return result
    return None
