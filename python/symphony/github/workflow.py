"""Host-enforced GitHub issue workflow.

The task record under ``state_dir`` is the only workflow authority
(Linear-style board semantics): ``state`` decides whether a worker may run,
``blocked`` freezes it, and ``review.approved_sha`` is the human merge
approval for the current delivery. GitHub issues provide task identity;
GitHub PRs provide the delivery artifact, checks and review evidence.
Execution labels are retired: ``high-risk`` remains a read-only business
flag, while ``agent``/``needs-human``/``merge-approved`` carry no meaning.

The host performs deterministic merge admission (real checks, merge gate,
risk rules, approvals) before squash-merging and closing the issue.
Uncertain outcomes fail closed.
"""

import json
import logging
import os
import re
import threading
import datetime
import uuid
from concurrent.futures import ThreadPoolExecutor

from ..workflow import WorkflowError
from . import workflow_policy as policy
from . import rework
from . import task_record
from .workflow_api import (
    GateError, comment, ensure, issue as fetch_issue,
    labels as api_labels, linked_prs, merge_gate, pages, pr as fetch_pr,
    pr_files, pr_reviews, request, root,
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
    """A candidate worker may run: routable issue whose task record is todo."""
    if not issue.routable(getattr(settings, "required_labels", []) or []):
        return False
    if not policy.enabled(settings):
        return True
    try:
        state = locked(
            settings, number_or_fail(issue.id),
            lambda record, _save: (
                task_record.state_of(record), task_record.blocked(record)))
    except Exception as error:  # noqa: BLE001 - fail closed
        log.warning(
            "Task record check failed, not routable issue_id=%s error=%r",
            issue.id, error)
        return False
    return state == ("todo", False)


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


def begin_run(issue, settings, admission_only=False):
    if not policy.enabled(settings):
        return "ok"
    number = number_or_fail(issue.id)
    return locked(
        settings, number,
        lambda record, save: _admit(
            issue, number, settings, record, save, admission_only=admission_only))


def _admit(issue, number, settings, record, save, admission_only=False):
    if record.get("pending_merge"):
        try:
            resolve_merge(settings, number, record, save)
        except GateError as error:
            if error.reason != "merge_not_confirmed":
                raise
        return "skip"
    state = task_record.state_of(record)
    if task_record.blocked(record) or state in {"backlog", "done"}:
        return "waiting"
    if state in {"review", "in_progress"}:
        # A published delivery or a live worker owns this cycle; another
        # runner must not join.
        return "skip"
    ensure(state == "todo", "execution_phase_changed")
    if admission_only:
        # Persist nothing: merge_eval shells only re-check ownership.
        return "ok"
    if record.get("attempts", 0) >= policy.max_attempts(settings):
        pause(settings, number, record, save,
              "Worker attempt limit reached; human intervention required")
        return "waiting"
    # Grant the cycle atomically: state + attempts under the same lock.
    updated = dict(record)
    updated["attempts"] = record.get("attempts", 0) + 1
    updated["state"] = "in_progress"
    save(updated)
    return "ok"


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
    return task_record.blocked(record)


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
    if task_record.state_of(record) != "review":
        return False
    delivery = record.get("delivery")
    return (isinstance(delivery, dict)
            and delivery.get("published") is True
            and delivery.get("cycle") == record.get("cycle"))


def prepare_rework(issue, settings):
    """Freeze feedback visible to this turn; never change the grant."""
    if not policy.enabled(settings):
        return ""
    number = number_or_fail(issue.id)

    def body(record, save):
        if (task_record.state_of(record) != "in_progress"
                or task_record.blocked(record)):
            # The run left in_progress (pause/rework landed mid-turn): no
            # feedback claim for a worker that no longer owns the cycle.
            return ""
        pr_number = (record.get("delivery") or {}).get("pr")
        if not pr_number:
            linked = [p for p in linked_prs(settings, number)
                      if p.get("state") == "OPEN"]
            ensure(len(linked) <= 1, "ambiguous_pr_association")
            if not linked:
                return ""
            pr_number = linked[0]["number"]
        feedback = rework.snapshot(settings, number, pr_number)
        save({**record, "feedback_claim": {
            "pr": pr_number, "cycle": record["cycle"],
            "digest": feedback["digest"]}})
        # The WORKFLOW defines how to read/respond to feedback. This snapshot
        # is only an optimistic concurrency guard for delivery.
        return ""
    return locked(settings, number, body)


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
        raise GateError("merge_host_only")
    if action == "pause":
        reason = _inspect(args.get("reason") or "Human intervention required")[:4000]
        pause(settings, number, record, save, reason)
        return {"status": "paused"}
    raise GateError("invalid_github_workflow_action")


# --------------------------------------------------------------------------
# Board task operations: state transitions on the single authority record.
# The board calls these through the scheduler HTTP API (its process shares
# neither memory nor the file locks).


def task_operate(settings, number, op, payload=None, by="board"):
    """A board action serialized with worker admission and host merges.

    ``payload``: ``{"sha": ...}`` for approve, ``{"body": ...}`` for
    rework comments. Returns ``{"ok": True, "state": ...}``.
    """
    payload = payload or {}

    def body(record, save):
        record = dict(record)
        state = task_record.state_of(record)
        if op == "start":
            ensure(state != "done", "task_done")
            ensure(not task_record.blocked(record), "task_blocked")
            record = task_record.transition(record, "todo", by, "开始执行")
        elif op == "pause":
            ensure(state != "done", "task_done")
            record["blocked"] = True
            reason = str(payload.get("comment") or "").strip()
            _record_history(record, "pause", by, reason)
            if reason:
                try:
                    comment(settings, number,
                            "[board] 人工暂停：%s" % reason[:500])
                except Exception:
                    pass  # 评论失败不回滚暂停状态
        elif op == "resume":
            ensure(task_record.blocked(record), "task_not_blocked")
            record["blocked"] = False
            _record_history(record, "resume", by)
        elif op == "rework":
            ensure(state == "review", "task_not_in_review")
            delivery = record.get("delivery") or {}
            body_text = str(payload.get("body") or "").strip()
            if body_text:
                comment(settings, number,
                        "[board] 继续优化（关联 GH-%s）\n\n%s" % (number, body_text))
                if isinstance(delivery.get("pr"), int):
                    feedback = rework.snapshot(settings, number, delivery["pr"])
                    record["rework_feedback"] = {
                        "pr": delivery["pr"], "digest": feedback["digest"]}
            record = task_record.transition(record, "todo", by, "继续优化")
        elif op == "approve":
            ensure(state == "review", "task_not_in_review")
            delivery = record.get("delivery") or {}
            sha = payload.get("sha")
            ensure(
                delivery.get("published") is True and delivery.get("sha")
                and delivery.get("sha") == sha, "delivery_changed")
            review = dict(record.get("review") or {})
            review["approved_sha"] = sha
            review["approved_by"] = by
            review["approved_at"] = _utc_now()
            record["review"] = review
            _record_history(record, "approve", by)
        elif op in {"stop", "close"}:
            ensure(state != "done", "task_done")
            record = task_record.transition(record, "done", by, "人工关闭")
        else:
            raise GateError(("unknown_task_operation", str(op)))
        save(record)
        return {"ok": True, "state": task_record.state_of(record),
                "blocked": task_record.blocked(record)}

    return locked(settings, number, body)


def _record_history(record, action, by, detail=""):
    history = list(record.get("history") or [])
    entry = {"at": _utc_now(), "action": action, "by": by}
    if detail:
        entry["detail"] = detail[:200]
    history.append(entry)
    record["history"] = history[-50:]


# --------------------------------------------------------------------------
# deliver / merge


def deliver(settings, number, args, record, save):
    ensure(record.get("pending_merge") is not True, "merge_result_pending")
    ensure(
        task_record.state_of(record) == "in_progress"
        and not task_record.blocked(record), "no_active_workflow_run")
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
    feedback = rework.snapshot(settings, number, pr_number)
    claim = record.get("feedback_claim") or {}
    if (feedback["digest"] != claim.get("digest", "")
            or (feedback["digest"] and (claim.get("pr") != pr_number or claim.get("cycle") != record["cycle"]))):
        return {"status": "needs_repair", "reason": "new_rework_feedback",
                "message": "New PR feedback arrived. End this turn; the next turn will load it."}
    previous = record.get("delivery") or {}
    same_delivery = (
        previous.get("cycle") == record.get("cycle")
        and previous.get("sha") == sha
        and previous.get("feedback_digest", "") == feedback["digest"])
    ensure(
        _map_get(fetch_pr(settings, pr_number), "head", "sha") == sha,
        "head_changed_during_delivery")
    if same_delivery:
        delivery = previous
    else:
        delivery = {
            "id": str(uuid.uuid4()),
            "pr": pr_number,
            "sha": sha,
            "base": pr_data["base"]["ref"],
            "cycle": record["cycle"],
            "tests": tests,
            "published": False,
            "feedback_digest": feedback["digest"],
            "feedback_after": max([previous.get("feedback_after", "")] + [c["updated_at"] for c in feedback["comments"]]),
            "issue": number,
        }
        # A new delivery invalidates any earlier board approval for this PR.
        review = dict(record.get("review") or {})
        if review.get("approved_sha"):
            review["approved_sha"] = None
            review["approved_by"] = None
            review["approved_at"] = None
            record = save({**record, "review": review})
    record = save({**record, "delivery": delivery})
    record = publish_delivery(settings, number, record, save)
    record = task_record.transition(record, "review", "worker", "交付")
    record = _set_host(record, save, "awaiting",
                       "交付已记录，宿主接管门禁与合并")
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
        "this SHA. High-risk or non-converging changes pause the task record."
        % (delivery["pr"], delivery["sha"], delivery["base"],
           delivery["tests"]))
    published = dict(delivery)
    published["published"] = True
    return save({**record, "delivery": published})


def merge(settings, number, record, save):
    if record.get("merged"):
        return {"status": "merged"}
    if record.get("pending_merge"):
        return resolve_merge(settings, number, record, save)
    # The background host checker owns the merge; workers only observe.
    return {"status": "waiting_host",
            "stage": (record.get("host") or {}).get("stage") or "awaiting"}


def delivery_state(settings, number, record):
    # The reads are independent; fetching them concurrently keeps a merge
    # call well inside the MCP client timeout on slow networks.
    delivery = record.get("delivery")
    pr_number = delivery.get("pr") if isinstance(delivery, dict) else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_issue = pool.submit(fetch_issue, settings, number)
        f_linked = pool.submit(linked_prs, settings, number)
        f_pr = (pool.submit(fetch_pr, settings, pr_number)
                if isinstance(pr_number, int)
                and not isinstance(pr_number, bool) else None)
        issue_data = f_issue.result()
        labels = api_labels(issue_data)
        ensure(issue_data.get("state") == "open",
               "host_authorization_revoked")
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
    issue = fetch_issue(settings, number)
    ensure(issue.get("state") == "open", "host_authorization_revoked")
    ensure(not rework.pending(record["delivery"], rework.snapshot(
        settings, number, record["delivery"]["pr"])), "rework_requested")
    record = save({**record, "pending_merge": True, "host": {**record.get("host", {}), "stage": "merging", "reason": "正在确认合并结果", "observed_at": _utc_now()}})
    request(
        settings, "PUT",
        "%s/pulls/%s/merge" % (root(settings), record["delivery"]["pr"]),
        {"sha": record["delivery"]["sha"], "merge_method": "squash"})
    return resolve_merge(settings, number, record, save)


# Deterministic merge admission: real checks, real merge gate, project risk
# rules and real review approvals. Anything unverifiable goes to a human.
def auto_ready(settings, delivery, pr_data, issue_labels, record=None):
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
            f_files.result(), f_reviews.result(), record)


def _gate_ready(settings, delivery, pr_data, gate, issue_labels, files=None,
                reviews=None, record=None):
    risk = _risk_required(settings, delivery, issue_labels, files)
    if risk[0] == "human":
        return ("human", risk[1])
    board_approved = (
        isinstance(record, dict)
        and (record.get("review") or {}).get("approved_sha") == delivery["sha"])
    approval_missing = (
        (risk[1] or policy.merge_policy(settings) == "manual")
        and not board_approved
        and not _approved(settings, delivery, pr_data, reviews))
    return _final_gate(gate, approval_missing)


def _final_gate(gate, approval_missing):
    # Review-first ordering: REVIEW_REQUIRED blocks mergeStateStatus, so
    # awaiting review must be decided before reporting a fix is needed.
    if gate.get("reviewDecision") == "REVIEW_REQUIRED" or approval_missing:
        return ("await", "review", [])
    if gate.get("mergeStateStatus") != "CLEAN":
        return ("fix", "merge state %s; resolve conflicts or update the branch"
                % gate.get("mergeStateStatus"))
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
        and (not delivery.get("feedback_after")
             or review.get("submitted_at", "") > delivery["feedback_after"])
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
        record = _set_host(record, save, "finalizing", "PR 已合并，正在关闭 Issue")
        issue = fetch_issue(settings, number)
        if issue.get("state") != "closed":
            request(
                settings, "PATCH",
                "%s/issues/%s" % (root(settings), number), {"state": "closed"})
        record = task_record.transition(
            {**record, "pending_merge": False, "blocked": False,
             "merged": True}, "done", "host", "PR 已合并")
        _set_host(record, save, "merged", "PR 已合并，任务已完成")
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
    record = dict(record)
    record["blocked"] = True
    record["error"] = reason
    record["host"] = {**record.get("host", {}), "stage": "paused",
                      "reason": str(reason), "observed_at": _utc_now()}
    save(record)
    comment(
        settings, number,
        "Paused: %s\nDelivery: %s\n\n"
        "恢复方式：处理完成后在看板点“恢复执行”（解除 blocked）。"
        "恢复不是合并授权；合并前宿主会重新核验当前 SHA 的检查与批准。"
        % (reason, _inspect(record.get("delivery"))))


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


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_host(record, save, stage, reason, **values):
    return save({**record, "host": {**record.get("host", {}), "stage": stage,
                 "reason": str(reason), "observed_at": _utc_now(), "last_error": None, **values}})


def _queue_host_repair(settings, number, record, save, reason):
    used = record.get("repair_budget", 0)
    if used >= policy.max_attempts(settings):
        pause(settings, number, record, save, "自动修复次数已达上限：%s" % reason)
        return
    record = save({**record, "repair_budget": used + 1})
    record = task_record.transition(record, "todo", "host", "退回修复：%s" % reason)
    save(record)


def reconcile_delivery(settings, number, active=False):
    """One background host step. No Agent process or orchestrator slot.

    Only ``review`` records are owned here. External convergence (issue
    closed, PR merged/closed) is checked first so no waiting substate can
    hang."""
    def body(record, save):
        if record.get("merged") or task_record.blocked(record):
            return
        if task_record.state_of(record) != "review":
            return
        delivery = record.get("delivery") or {}
        if record.get("pending_merge"):
            resolve_merge(settings, number, record, save)
            return
        if active:
            return
        # Convergence first: the issue is the task identity.
        data = fetch_issue(settings, number)
        labels = api_labels(data)
        if data.get("state") != "open":
            record = task_record.transition(record, "done", "host",
                                            "Issue 已关闭")
            _set_host(record, save, "closed", "Issue 已关闭，停止自动合并")
            return
        if not delivery.get("published"):
            return
        pr = fetch_pr(settings, delivery["pr"])
        if pr.get("merged") is True:
            record = save({**record, "pending_merge": True})
            return resolve_merge(settings, number, record, save)
        if pr.get("state") == "closed":
            record = task_record.transition(record, "done", "host",
                                            "PR 已关闭但未合并")
            _set_host(record, save, "closed", "PR 已关闭但未合并")
            return
        if rework.pending(delivery, rework.snapshot(settings, number, delivery["pr"])):
            return _set_host(record, save, "waiting_update", "修改意见已记录，等待人工退回执行")
        record = _set_host(record, save, "checking",
                           "正在检查交付 SHA、CI 与批准")
        try:
            labels, pr, delivery_checked = delivery_state(settings, number, record)
        except GateError as error:
            if error.reason == "delivery_changed":
                return _queue_host_repair(settings, number, record, save, "PR 提交已变化，需要重新验证交付")
            raise
        decision = auto_ready(settings, delivery_checked, pr, labels,
                              record)
        if decision[0] == "ready":
            return commit_merge(settings, number, record, save)
        if decision[0] == "await":
            stage = "waiting_review" if decision[1] == "review" else "waiting_checks"
            return _set_host(record, save, stage,
                             "等待批准" if stage == "waiting_review"
                             else "等待 CI 检查：%s" % decision[2],
                             review_required=stage == "waiting_review")
        if decision[0] == "fix":
            return _queue_host_repair(settings, number, record, save, decision[1])
        pause(settings, number, record, save, decision[1])
    return locked(settings, number, body)


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
    return task_record.normalize(record)


def _store_record(path, record):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    # Backup before overwrite: the record is the only workflow authority.
    if os.path.exists(path):
        try:
            with open(path, "rb") as src, open(path + ".bak", "wb") as dst:
                dst.write(src.read())
        except OSError:
            log.warning("Unable to back up task record %s", path)
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
