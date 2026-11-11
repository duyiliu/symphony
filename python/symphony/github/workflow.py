"""Host-enforced GitHub issue workflow (port of ``workflow.ex``).

Issue labels are the source of truth for the ``develop`` phase:

- ``agent``       : the host may develop this issue
- ``needs-human`` : development is paused until a human relabels
- ``high-risk``   : host approval is required before merge

The host keeps private JSON receipts under ``state_dir`` so admission, risk
evaluation and merge confirmation cannot be forged through agent-visible
tools. ``agent`` is the only execution label: the agent develops, delivers,
and the host performs the deterministic merge admission (real checks, merge
gate, risk rules, review approvals) before squash-merging and closing the
issue. Uncertain merge outcomes fail closed.
"""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from ..workflow import WorkflowError
from . import workflow_policy as policy
from .workflow_api import (
    GateError, add_label, comment, ensure, events, issue as fetch_issue,
    labels as api_labels, linked_prs, merge_gate, pages, pr as fetch_pr,
    pr_files, pr_reviews, remove_label, request, root,
)

log = logging.getLogger("symphony.github.workflow")

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

_FAIL_CLOSED_REASONS = (
    "missing_delivery",
    "delivery_not_published",
    "pr_association_changed",
    "delivery_changed",
    "unexpected_base_branch",
)


def routable(issue, settings):
    if policy.enabled(settings) and policy.phase(issue.labels) == "invalid":
        log.warning(
            "Conflicting GitHub execution labels issue_id=%s "
            "issue_identifier=%s; waiting for human correction",
            issue.id, issue.identifier)
    return policy.routable(issue, settings)


def record_failure(issue, settings, reason):
    if not policy.enabled(settings):
        return

    def body(record, save):
        updated = dict(record)
        updated["last_error"] = _inspect(reason)[:4000]
        save(updated)

    try:
        locked(settings, number_or_fail(issue.id), body)
    except GateError:
        log.warning("Failed to persist workflow failure issue_id=%s", issue.id)


def begin_run(issue, settings):
    if not policy.enabled(settings):
        return "ok"
    number = number_or_fail(issue.id)
    return locked(
        settings, number,
        lambda record, save: _admit(issue, number, settings, record, save))


def _admit(issue, number, settings, record, save):
    if record.get("pending_merge"):
        try:
            resolve_merge(settings, number, record, save)
        except GateError as error:
            if error.reason != "merge_not_confirmed":
                raise
        return "skip"
    return _admit_labeled(issue, number, settings, record, save)


def _admit_labeled(issue, number, settings, record, save):
    event, _labels = signal(settings, number)
    ensure(policy.phase(issue.labels) == "develop", "execution_phase_changed")
    resume = record.get("blocked") is True and record.get("cycle") != cycle(event)
    pause_reason = record.get("error")
    record = new_cycle(record, cycle(event))
    if resume:
        record = publish_resume(
            settings, number, record, save, event, pause_reason)
    if record.get("blocked"):
        return "waiting"
    delivery = record.get("delivery")
    if (isinstance(delivery, dict)
            and delivery.get("published") is True
            and delivery.get("cycle") == record.get("cycle")):
        # A published delivery belongs to the host: evaluate the merge gate
        # inline every admission instead of dispatching an agent that would
        # just poll checks and burn tokens. Repairs still get a worker.
        return _admit_merge(number, settings, record, save)
    if record["attempts"] >= policy.config(settings).get("max_attempts", 3):
        pause(settings, number, record, save,
              "Worker attempt limit reached; human intervention required")
        return "waiting"
    # Fresh develop cycle: the worker develops/delivers; afterwards the host
    # owns the merge poll until merge, repair dispatch or pause.
    updated = dict(record)
    updated["attempts"] = record["attempts"] + 1
    save(updated)
    return "ok"


def _admit_merge(number, settings, record, save):
    """Host-side merge admission for a published delivery.

    Runs inside ``begin_run`` on every poll while a delivery awaits merge:
    ``await`` → ``await_merge`` (no agent, slow re-poll), ``fix`` → ``ok``
    (dispatch a worker to repair), ``human`` → pause. Gate errors that are
    not structural retry on the next poll.
    """
    try:
        labels, pr_data, delivery = delivery_state(settings, number, record)
        decision = auto_ready(settings, delivery, pr_data, labels)
    except GateError as error:
        if error.reason in _FAIL_CLOSED_REASONS:
            pause(settings, number, record, save,
                  "Auto merge declined: %s" % error.reason)
            return "waiting"
        log.warning(
            "Merge admission check failed issue=%s reason=%r; retrying "
            "next poll", number, error.reason)
        return "await_merge"
    if decision[0] == "ready":
        if _label_approved(settings, labels):
            record = save({**record, "approval": "label"})
        try:
            commit_merge(settings, number, record, save)
        except GateError as error:
            if error.reason == "merge_not_confirmed":
                return "waiting"
            log.warning(
                "Host merge attempt failed issue=%s reason=%r; retrying "
                "next poll", number, error.reason)
        return "await_merge"
    if decision[0] == "await":
        log.info("Merge gate waiting issue=%s reason=%s details=%r sha=%s",
                 number, decision[1], decision[2], delivery.get("sha"))
        return "await_merge"
    if decision[0] == "fix":
        log.info(
            "Merge gate needs repair issue=%s reason=%s; dispatching worker",
            number, decision[1])
        return "ok"
    pause(settings, number, record, save,
          "Auto merge declined: %s" % decision[1])
    return "waiting"


def waiting(issue, settings):
    """True when the receipt is blocked for the current authorization cycle.

    Failed refreshes must not release a paused task, so any error fails
    closed to True.
    """
    if not policy.enabled(settings):
        return False
    try:
        number = number_or_fail(issue.id)
    except GateError:
        return True
    try:
        return locked(
            settings, number,
            lambda record, _save: _blocked_cycle(settings, number, record)
        ) is not False
    except Exception as error:  # noqa: BLE001 - fail closed
        log.warning(
            "Workflow check failed, treating issue as waiting issue_id=%s error=%r",
            issue.id, error)
        return True


def _blocked_cycle(settings, number, record):
    event, _labels = signal(settings, number)
    return record.get("blocked") is True and record.get("cycle") == cycle(event)


def merge_pending(issue, settings):
    """True when a published delivery in the current cycle awaits the
    host-side merge admission — the worker run should end rather than
    poll checks itself. Failures return False so normal routing decides."""
    if not policy.enabled(settings):
        return False
    try:
        number = number_or_fail(issue.id)
    except GateError:
        return False
    try:
        return locked(
            settings, number,
            lambda record, _save: _merge_pending(settings, number, record)
        ) is True
    except GateError:
        return False
    except Exception as error:  # noqa: BLE001 - never block the runner loop
        log.debug(
            "merge_pending check failed issue_id=%s error=%r", issue.id, error)
        return False


def _merge_pending(settings, number, record):
    if record.get("blocked") or record.get("pending_merge") \
            or record.get("merged"):
        return False
    delivery = record.get("delivery")
    if not (isinstance(delivery, dict)
            and delivery.get("published") is True
            and delivery.get("cycle") == record.get("cycle")):
        return False
    event, _labels = signal(settings, number)
    return record.get("cycle") == cycle(event)


def same_phase(before, after, settings):
    return policy.same_phase(before, after, settings)


# --------------------------------------------------------------------------
# Agent tool dispatch


def execute(settings, args):
    """Entry point for the ``github_workflow`` agent tool."""
    if not isinstance(args, dict):
        raise GateError("invalid_arguments")
    _validate_policy(settings)
    ensure(policy.enabled(settings), "github_workflow_disabled")
    number = number_or_fail(args.get("issue"))
    return locked(
        settings, number,
        lambda record, save: _dispatch(settings, number, args, record, save))


def _validate_policy(settings):
    try:
        policy.validate(settings)
    except WorkflowError as error:
        raise GateError(error.reason) from error


def _dispatch(settings, number, args, record, save):
    action = args.get("action")
    if action == "status":
        return record
    if action == "deliver":
        return deliver(settings, number, args, record, save)
    if action == "merge":
        return fail_closed(
            settings, number, record, save,
            lambda: merge(settings, number, record, save))
    if action == "pause":
        reason = _inspect(args.get("reason") or "Human intervention required")[:4000]
        pause(settings, number, record, save, reason)
        return {"status": "paused"}
    raise GateError("invalid_github_workflow_action")


# --------------------------------------------------------------------------
# Receipt cycles


def cycle(event):
    return "develop:%s" % event["id"]


def new_cycle(record, cycle_value):
    if record.get("cycle") == cycle_value:
        return record
    return {"cycle": cycle_value, "attempts": 0, "blocked": False}


# --------------------------------------------------------------------------
# deliver / merge


def deliver(settings, number, args, record, save):
    ensure(record.get("pending_merge") is not True, "merge_result_pending")
    event, _labels = signal(settings, number)
    ensure(
        record.get("cycle") == cycle(event) and not record.get("blocked"),
        "no_active_workflow_run")
    tests = args.get("tests")
    ensure(
        isinstance(tests, str) and tests.strip() != ""
        and len(tests.encode("utf-8")) <= 16000,
        "missing_test_evidence")
    pr_number = args.get("pr")
    ensure(
        isinstance(pr_number, int) and not isinstance(pr_number, bool)
        and _linked_pr(settings, number) == pr_number,
        "ambiguous_pr_association")
    pr_data = fetch_pr(settings, pr_number)
    _validate_pr(settings, pr_data)
    sha = pr_data["head"]["sha"]
    previous = record.get("delivery") or {}
    same_delivery = (
        previous.get("cycle") == record.get("cycle")
        and previous.get("sha") == sha)
    ensure(
        _map_get(fetch_pr(settings, pr_number), "head", "sha") == sha,
        "head_changed_during_delivery")
    if same_delivery:
        delivery = previous
    else:
        delivery = {
            "pr": pr_number,
            "sha": sha,
            "base": pr_data["base"]["ref"],
            "cycle": record["cycle"],
            "tests": tests,
            "published": False,
        }
    record = save({**record, "delivery": delivery})
    record = publish_delivery(settings, number, record, save)
    return {"status": "delivered", "delivery": record["delivery"]}


def publish_delivery(settings, number, record, save):
    delivery = record["delivery"]
    if delivery.get("published") is True:
        return record
    comment(
        settings, number,
        "Delivery PR #%s @ `%s` → `%s`\n\n"
        "Agent-reported test evidence:\n%s\n\n"
        "The host will verify checks, review gates and risk rules, then merge "
        "this SHA. High-risk or non-converging changes pause with "
        "`needs-human`."
        % (delivery["pr"], delivery["sha"], delivery["base"],
           delivery["tests"]))
    published = dict(delivery)
    published["published"] = True
    return save({**record, "delivery": published})


def publish_resume(settings, number, record, save, event, pause_reason):
    actor = _map_get(event, "actor", "login") or "unknown"
    comment(
        settings, number,
        "Manual resume (人工恢复执行): `needs-human` removed and `agent` "
        "re-added by @%s.\n\n"
        "Previous pause reason: %s\n"
        "Host resumes as a new develop cycle. This is not merge "
        "authorization: checks, delivery SHA and review approvals are "
        "re-verified before any merge."
        % (actor, pause_reason or "human intervention"))
    return save({**record, "resumed_event": event["id"]})


def merge(settings, number, record, save):
    if record.get("merged"):
        return {"status": "merged"}
    if record.get("pending_merge"):
        return resolve_merge(settings, number, record, save)
    return merge_delivery(settings, number, record, save)


def merge_delivery(settings, number, record, save):
    labels, pr_data, delivery = delivery_state(settings, number, record)
    decision = auto_ready(settings, delivery, pr_data, labels)
    if decision[0] == "ready":
        if _label_approved(settings, labels):
            record = save({**record, "approval": "label"})
        return commit_merge(settings, number, record, save)
    if decision[0] == "await":
        return {"status": "waiting_%s" % decision[1], "pending": decision[2]}
    if decision[0] == "fix":
        return {"status": "needs_repair", "reason": decision[1]}
    pause(settings, number, record, save,
          "Auto merge declined: %s" % decision[1])
    return {"status": "paused"}


def delivery_state(settings, number, record):
    # The four reads are independent; fetching them concurrently keeps a
    # merge call well inside the MCP client timeout on slow networks.
    delivery = record.get("delivery")
    pr_number = delivery.get("pr") if isinstance(delivery, dict) else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_issue = pool.submit(fetch_issue, settings, number)
        f_events = pool.submit(events, settings, number)
        f_linked = pool.submit(linked_prs, settings, number)
        f_pr = (pool.submit(fetch_pr, settings, pr_number)
                if isinstance(pr_number, int)
                and not isinstance(pr_number, bool) else None)
        event, labels = _signal_check(
            settings, f_issue.result(), f_events.result())
        ensure(
            record.get("cycle") == cycle(event) and not record.get("blocked"),
            "not_a_merge_run")
        ensure(
            isinstance(delivery, dict)
            and delivery.get("cycle") == record.get("cycle"),
            "missing_delivery")
        ensure(delivery.get("published") is True, "delivery_not_published")
        open_prs = [
            node for node in f_linked.result()
            if isinstance(node, dict) and node.get("state") == "OPEN"]
        ensure(len(open_prs) == 1, "ambiguous_pr_association")
        ensure(
            open_prs[0]["number"] == delivery["pr"], "pr_association_changed")
        pr_data = f_pr.result()
    _validate_pr(settings, pr_data)
    ensure(
        pr_data["head"]["sha"] == delivery["sha"]
        and pr_data["base"]["ref"] == delivery["base"],
        "delivery_changed")
    return labels, pr_data, delivery


def commit_merge(settings, number, record, save):
    # Re-read labels after CI/risk inspection so mid-flight label edits
    # (needs-human, agent removal, closure) cannot slip through.
    event, labels = signal(settings, number)
    ensure(record.get("cycle") == cycle(event), "authorization_revoked")
    if record.get("approval") == "label":
        ensure(
            _label_approved(settings, labels), "authorization_revoked")
    record = save({**record, "pending_merge": True})
    request(
        settings, "PUT",
        "%s/pulls/%s/merge" % (root(settings), record["delivery"]["pr"]),
        {"sha": record["delivery"]["sha"], "merge_method": "squash"})
    return resolve_merge(settings, number, record, save)


# Deterministic merge admission: real checks, real merge gate, project risk
# rules and real review approvals. Anything unverifiable goes to a human.
def auto_ready(settings, delivery, pr_data, issue_labels):
    sha = delivery["sha"]
    pr_number = delivery["pr"]
    # Independent reads fan out: check-runs + statuses + merge gate +
    # files + reviews were ~9 sequential round-trips before.
    with ThreadPoolExecutor(max_workers=5) as pool:
        f_runs = pool.submit(
            pages, settings,
            "%s/commits/%s/check-runs" % (root(settings), sha), "check_runs")
        f_statuses = pool.submit(
            pages, settings,
            "%s/commits/%s/statuses" % (root(settings), sha), None)
        f_gate = pool.submit(merge_gate, settings, pr_number)
        f_files = pool.submit(pr_files, settings, pr_number)
        f_reviews = pool.submit(pr_reviews, settings, pr_number)
        pending, failing = classify_checks(
            f_runs.result(), f_statuses.result(), sha)
        gate = f_gate.result()
        ensure(
            gate.get("headRefOid") == sha
            and gate.get("baseRefName") == delivery["base"],
            "delivery_changed")
        if failing:
            return ("fix", "checks failing on %s: %s"
                    % (sha, ", ".join(failing)))
        if pending:
            return ("await", "checks", pending)
        if gate.get("reviewDecision") == "CHANGES_REQUESTED":
            return ("fix", "changes requested by a reviewer; address the "
                    "review comments")
        if gate.get("mergeStateStatus") == "UNKNOWN":
            return ("await", "checks", ["mergeability"])
        return _gate_ready(
            settings, delivery, pr_data, gate, issue_labels,
            f_files.result(), f_reviews.result())


def _gate_ready(settings, delivery, pr_data, gate, issue_labels, files=None,
                reviews=None):
    risk = _risk_required(settings, delivery, issue_labels, files)
    if risk[0] == "human":
        return ("human", risk[1])
    approval_missing = (
        (risk[1] or policy.merge_policy(settings) == "manual")
        and not _approved(settings, delivery, pr_data, reviews)
        and not _label_approved(settings, issue_labels))
    return _final_gate(gate, approval_missing)


def _label_approved(settings, issue_labels):
    """Solo-mode approval: repo owner applied the configured approval label.

    Only meaningful when ``approval_label`` is configured; intended for
    single-account repos where a non-author GitHub review is impossible.
    The merge-time SHA freeze still applies, so a post-approval push is
    rejected by ``delivery_changed`` like any other drift.
    """
    label = policy.approval_label(settings)
    return bool(label) and label in (issue_labels or [])


def _final_gate(gate, approval_missing):
    if gate.get("mergeStateStatus") != "CLEAN":
        return ("fix", "merge state %s; resolve conflicts or update the branch"
                % gate.get("mergeStateStatus"))
    if gate.get("reviewDecision") == "REVIEW_REQUIRED":
        return ("await", "review", [])
    if approval_missing:
        return ("await", "review", [])
    return ("ready",)


def classify_checks(runs, statuses, sha):
    seen_contexts = set()
    uniq_statuses = []
    for status in statuses:
        context = status.get("context")
        if context not in seen_contexts:
            seen_contexts.add(context)
            uniq_statuses.append(status)
    names = []
    for name in [run.get("name") for run in runs] + [
            status.get("context") for status in uniq_statuses]:
        if name not in names:
            names.append(name)
    pending = []
    failing = []
    for name in names:
        groups = {}
        for run in runs:
            if run.get("name") == name:
                groups.setdefault(_map_get(run, "app", "id"), []).append(run)
        latest = [
            max(checks, key=lambda run: run.get("id") or 0)
            for checks in groups.values()
        ]
        context_statuses = [
            status for status in uniq_statuses
            if status.get("context") == name
        ]
        if any(run.get("head_sha") != sha for run in latest):
            pending.append(name)
        elif any(run.get("status") != "completed" for run in latest):
            pending.append(name)
        elif any(
                status.get("state") == "pending"
                for status in context_statuses):
            pending.append(name)
        elif (all(
                run.get("conclusion") in ("success", "neutral", "skipped")
                for run in latest)
                and all(
                    status.get("state") == "success"
                    for status in context_statuses)):
            pass
        elif any(
                run.get("conclusion") in _TRANSIENT_CHECK_CONCLUSIONS
                for run in latest):
            # Completed without a definitive verdict (e.g. GitHub briefly
            # reports conclusion null / action_required / startup_failure
            # while a skipped or queued job resolves). Re-poll instead of
            # dispatching a repair worker for a phantom failure.
            pending.append(name)
        else:
            failing.append(name)
    return pending, failing


_TRANSIENT_CHECK_CONCLUSIONS = frozenset(
    {None, "action_required", "startup_failure"})


def _risk_required(settings, delivery, issue_labels, files=None):
    if files is None:
        files = pr_files(settings, delivery["pr"])
    max_files = policy.max_auto_files(settings)
    if len(files) > max_files:
        return ("human", "diff touches %s files, above the auto-merge budget "
                "of %s; human review required" % (len(files), max_files))
    paths = policy.review_required_paths(settings)
    risky = any(
        isinstance(file, dict)
        and policy.path_matches(paths, file.get("filename") or "")
        for file in files)
    return ("ok", risky or policy.high_risk_label() in issue_labels)


def _approved(settings, delivery, pr_data, reviews=None):
    if reviews is None:
        reviews = pr_reviews(settings, delivery["pr"])
    author = _map_get(pr_data, "user", "login")
    latest = {}
    for review in reviews:
        if (isinstance(review, dict)
                and review.get("commit_id") == delivery["sha"]
                and isinstance(review.get("user"), dict)):
            latest[review["user"].get("login")] = review
    return any(
        review.get("state") == "APPROVED"
        and review["user"].get("type") == "User"
        and isinstance(login, str)
        and login not in (author, None)
        for login, review in latest.items())


def resolve_merge(settings, number, record, save):
    delivery = record["delivery"]
    pr_data = fetch_pr(settings, delivery["pr"])
    if (pr_data.get("merged") is True
            and _map_get(pr_data, "head", "sha") == delivery["sha"]
            and _map_get(pr_data, "base", "ref") == delivery["base"]):
        request(
            settings, "PATCH",
            "%s/issues/%s" % (root(settings), number), {"state": "closed"})
        _clear_labels(settings, number)
        save({**record, "pending_merge": False, "blocked": True,
              "merged": True})
        return {"status": "merged"}
    updated = dict(record)
    updated["pending_merge"] = False
    pause(
        settings, number, updated, save,
        "Merge result unconfirmed or different from delivery; inspect "
        "GitHub before re-running")
    raise GateError("merge_not_confirmed")


def fail_closed(settings, number, record, save, fn):
    try:
        return fn()
    except GateError as error:
        if error.reason in _FAIL_CLOSED_REASONS:
            pause(settings, number, record, save, _inspect(error.reason))
        raise


def pause(settings, number, record, save, reason):
    save({**record, "blocked": True, "error": reason})
    comment(
        settings, number,
        "Paused (needs-human): %s\nDelivery: %s\n\n"
        "恢复方式：人工处理后，移除 `needs-human` 标签并重新添加 `agent`。"
        "重贴 `agent` 不是合并授权；合并前宿主会重新核验当前 SHA 的检查与有效 "
        "Review 批准。"
        % (reason, _inspect(record.get("delivery"))))
    add_label(settings, number, policy.pause_label())
    remove_label(settings, number, policy.AGENT_LABEL)


def _clear_labels(settings, number):
    remove_label(settings, number, policy.AGENT_LABEL)
    remove_label(settings, number, policy.pause_label())
    remove_label(settings, number, "merge-approved")
    approval = policy.approval_label(settings)
    if approval and approval != "merge-approved":
        remove_label(settings, number, approval)


def signal(settings, number):
    """Fetch the live signal: issue open, develop phase, required labels
    present, newest ``agent`` label event a ``labeled`` grant."""
    return _signal_check(
        settings,
        fetch_issue(settings, number),
        events(settings, number))


def _signal_check(settings, data, event_list):
    issue_labels = api_labels(data)
    ensure(
        data.get("state") == "open" and policy.phase(issue_labels) == "develop",
        "issue_not_executable")
    required = getattr(settings, "required_labels", None) or []
    ensure(all(label in issue_labels for label in required),
           "required_labels_removed")
    matched = [
        event for event in event_list
        if isinstance(event, dict)
        and _map_get(event, "label", "name") == "agent"
        and event.get("event") in ("labeled", "unlabeled")
    ]
    event = matched[-1] if matched else None
    ensure(
        isinstance(event, dict) and event.get("event") == "labeled"
        and isinstance(event.get("id"), int)
        and not isinstance(event.get("id"), bool),
        "missing_label_event")
    return event, issue_labels


def _linked_pr(settings, number):
    prs = [
        node for node in linked_prs(settings, number)
        if isinstance(node, dict) and node.get("state") == "OPEN"
    ]
    ensure(len(prs) == 1, "ambiguous_pr_association")
    return prs[0]["number"]


def _map_get(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _validate_pr(settings, pr_data):
    ensure(
        pr_data.get("state") == "open" and pr_data.get("draft") is False,
        "pr_not_ready")
    ensure(
        _map_get(pr_data, "base", "ref")
        == policy.config(settings)["base_branch"],
        "unexpected_base_branch")
    repo = policy.repo(settings)
    ensure(
        _map_get(pr_data, "base", "repo", "full_name") == repo
        and _map_get(pr_data, "head", "repo", "full_name") == repo,
        "unexpected_pr_repository")
    sha = _map_get(pr_data, "head", "sha")
    ensure(
        isinstance(sha, str) and re.match(r"^[a-f0-9]{40,64}$", sha),
        "invalid_head_sha")


# --------------------------------------------------------------------------
# Receipt store


def _path(settings, number):
    return os.path.join(
        policy.config(settings)["state_dir"], "%s.json" % number)


def _read_record(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise GateError(("workflow_receipt_read", str(error)))
    except ValueError:
        raise GateError("invalid_workflow_receipt")
    if not isinstance(record, dict):
        raise GateError("invalid_workflow_receipt")
    return record


def _store_record(path, record):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def locked(settings, issue_number, fn):
    """Read the issue receipt, run ``fn(record, save)`` under a per-issue lock.

    Mirrors the Elixir ``locked``: policy validation and number parsing first,
    unexpected exceptions surface as ``("workflow_storage", reason)`` while
    GateError propagates unchanged.
    """
    try:
        _validate_policy(settings)
        number = number_or_fail(issue_number)
        path = _path(settings, number)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(path, threading.Lock())
        with lock:
            record = _read_record(path)
            repo = policy.repo(settings)
            ensure(
                record == {} or record.get("repo") == repo,
                "receipt_repository_mismatch")
            return fn(record, _Save(path, repo))
    except GateError:
        raise
    except Exception as error:  # noqa: BLE001
        raise GateError(("workflow_storage", str(error))) from error


class _Save:
    """Callable: ``save(record)`` persists the receipt for this transaction."""

    def __init__(self, path, repo):
        self.path = path
        self.repo = repo

    def __call__(self, record):
        record = {**record, "repo": self.repo}
        _store_record(self.path, record)
        return record


def number_or_fail(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"[+-]?\d+", value)
        if match and int(value) > 0:
            return int(value)
    raise GateError("invalid_github_workflow_issue")


def _inspect(value):
    """Approximate Elixir ``inspect`` for receipts/comments/error payloads."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nil"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, tuple):
        return "{%s}" % ", ".join(_inspect(item) for item in value)
    if isinstance(value, list):
        return "[%s]" % ", ".join(_inspect(item) for item in value)
    if isinstance(value, dict):
        return "%%{%s}" % ", ".join(
            "%s => %s" % (json.dumps(str(key), ensure_ascii=False),
                          _inspect(item))
            for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0])))
    return repr(value)
