"""Host-owned delivery gate for Linear issues with GitHub pull requests.

The Pi worker may create and update a PR, but it never receives a credential
that can merge. This module is the only merge entry point: it re-reads the
real PR head, checks the checks associated with that SHA, enforces the
configured risk/review/branch-protection policy, merges through the host
credential, verifies the merged state on GitHub, and only then moves the
Linear issue to Done.

All mutations are idempotent or re-checked before retrying. A persisted
per-issue record provides restart recovery and comment de-duplication; the
record is not a second task tracker. Linear remains the task authority.
"""

import datetime
import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
from types import SimpleNamespace

from . import linear
from . import tracker as tracker_adapter
from .github import client as gh_client

log = logging.getLogger("symphony.delivery")

_PR_URL_RE = re.compile(
    r"^https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)(?:/)?$")


class DeliveryError(Exception):
    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        message = str(reason)
        if detail is not None:
            message = "%s: %s" % (message, detail)
        super().__init__(message)


class _NotFoundType:
    pass


_NOT_FOUND = _NotFoundType()


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _iso(value):
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value if isinstance(value, str) else None


def _record_name(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or "issue"))


def _digest(*values):
    return hashlib.sha256(
        "\0".join(str(value) for value in values).encode("utf-8")
    ).hexdigest()[:20]


def _comment_exists(settings, issue_id, notice_key):
    """Return whether a deterministic delivery comment is already visible."""
    marker = "[symphony-delivery:%s]" % notice_key
    after = None
    for _ in range(10):
        payload = linear.graphql(
            settings.tracker,
            "query SymphonyDeliveryCommentExists("
            "$id: String!, $after: String) {"
            " issue(id: $id) { comments(first: 100, after: $after) {"
            " nodes { id body }"
            " pageInfo { hasNextPage endCursor } } } }",
            {"id": issue_id, "after": after},
        )
        issue = (payload.get("data") or {}).get("issue") or {}
        comments = issue.get("comments") or {}
        if any(
                isinstance(item, dict)
                and marker in str(item.get("body") or "")
                for item in comments.get("nodes") or []):
            return True
        page = comments.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return False
        cursor = page.get("endCursor")
        if not cursor or cursor == after:
            raise DeliveryError("invalid_linear_comment_pagination")
        after = cursor
    raise DeliveryError("linear_comment_pagination_limit")


class GitHub:
    """Small REST facade using the host-only delivery credential."""

    def __init__(self, settings):
        self._settings = gh_client.GitHubSettings(
            settings.api_url, settings.repo, settings.token)
        self.repo = settings.repo

    def request(self, method, path, params=None, body=None,
                allow_missing=False):
        result = gh_client.perform_request(
            method, path, params or {}, body, self._settings)
        status = result["status"]
        if 200 <= status <= 299:
            return result["body"]
        if allow_missing and status == 404:
            return _NOT_FOUND
        payload = result.get("body")
        message = payload.get("message") if isinstance(payload, dict) else ""
        headers = result.get("headers") or {}
        rate_limited = (
            status == 429
            or "rate limit" in str(message).lower()
            or str(headers.get("X-RateLimit-Remaining", "")) == "0"
        )
        if rate_limited:
            raise DeliveryError("github_rate_limited", {
                "status": status,
                "method": method,
                "path": path,
                "retry_after": gh_client.retry_after_seconds(headers),
            })
        if allow_missing and status == 403:
            return _NOT_FOUND
        raise DeliveryError(
            "github_status", {
                "status": status, "method": method, "path": path,
                "retry_after": gh_client.retry_after_seconds(headers),
            })

    def pages(self, path, field=None):
        rows = []
        page = 1
        while True:
            params = {"per_page": 100, "page": page}
            payload = self.request("GET", path, params=params)
            current = payload.get(field) if field else payload
            if not isinstance(current, list):
                raise DeliveryError("github_unknown_payload", path)
            rows.extend(current)
            if len(current) < 100:
                return rows
            page += 1
            if page > 100:
                raise DeliveryError("github_pagination_limit", path)


def _repo_path(repo, suffix):
    return "/repos/%s%s" % (gh_client.encoded_repo(repo), suffix)


def _check_run_successful(run):
    return (
        isinstance(run, dict)
        and run.get("status") == "completed"
        and run.get("conclusion") in ("success", "neutral", "skipped")
    )


def classify_checks(runs, statuses, sha):
    """Return (pending, failing, successful, present) for one exact SHA.

    Only the newest run per (check name, app) counts. A check for another SHA
    is treated as pending because it cannot prove this commit.
    """
    runs = [run for run in (runs or []) if isinstance(run, dict)]
    statuses = [row for row in (statuses or []) if isinstance(row, dict)]
    by_context = {}
    for status in statuses:
        context = status.get("context")
        if context not in by_context:
            by_context[context] = status
    statuses = list(by_context.values())

    names = []
    for value in [run.get("name") for run in runs] + [
            status.get("context") for status in statuses]:
        if value not in names:
            names.append(value)

    pending = []
    failing = []
    successful = []
    for name in names:
        groups = {}
        for run in runs:
            if run.get("name") == name:
                key = (run.get("app") or {}).get("id")
                groups.setdefault(key, []).append(run)
        latest = [
            max(checks, key=lambda item: item.get("id") or 0)
            for checks in groups.values()
        ]
        context_statuses = [
            status for status in statuses if status.get("context") == name
        ]
        if any(run.get("head_sha") != sha for run in latest):
            pending.append(name)
        elif any(run.get("status") != "completed" for run in latest):
            pending.append(name)
        elif any(
                status.get("state") == "pending"
                for status in context_statuses):
            pending.append(name)
        elif latest and all(_check_run_successful(run) for run in latest) \
                and all(
                    status.get("state") == "success"
                    for status in context_statuses):
            successful.append(name)
        elif not latest and context_statuses and all(
                status.get("state") == "success"
                for status in context_statuses):
            successful.append(name)
        elif any(
                run.get("conclusion") in (None, "action_required",
                                          "startup_failure")
                for run in latest):
            pending.append(name)
        else:
            failing.append(name)
    return pending, failing, successful, bool(runs or statuses)


def latest_reviews(reviews):
    latest = {}
    for review in reviews or []:
        if not isinstance(review, dict) or not isinstance(
                review.get("user"), dict):
            continue
        login = review["user"].get("login")
        if not isinstance(login, str):
            continue
        current = latest.get(login)
        if current is None or (
                review.get("submitted_at") or ""
        ) >= (current.get("submitted_at") or ""):
            latest[login] = review
    return latest


def review_state(reviews, sha, author):
    """Return (changes_requested, approvals) for the current PR head."""
    current = latest_reviews(reviews)
    changes = False
    approvals = 0
    for login, review in current.items():
        if review.get("commit_id") != sha:
            continue
        if review.get("state") == "CHANGES_REQUESTED":
            changes = True
        if (review.get("state") == "APPROVED"
                and review["user"].get("type") == "User"
                and login != author):
            approvals += 1
    return changes, approvals


def risk_match(paths, filename):
    normalized = (filename or "").replace("\\", "/")
    if not normalized:
        return True
    return any(
        fnmatch.fnmatchcase(normalized, pattern)
        or fnmatch.fnmatchcase(normalized.lstrip("/"), pattern)
        for pattern in (paths or [])
    )


def risk_files(delivery, files, labels):
    if not isinstance(files, list) or not files:
        return "pull request has no file list"
    if len(files) > delivery.max_auto_files:
        return "diff touches %s files, above the auto-merge budget of %s" % (
            len(files), delivery.max_auto_files)
    for file in files:
        if not isinstance(file, dict):
            return "pull request file list is malformed"
        filename = file.get("filename") or ""
        if risk_match(delivery.risk_paths, filename):
            return "high-risk path requires human review: %s" % filename
    issue_labels = {str(label).strip().lower() for label in labels or []}
    for label in delivery.risk_labels or []:
        if label.strip().lower() in issue_labels:
            return "high-risk label requires human review: %s" % label
    return None


def _protection_contexts(protection):
    required = (protection or {}).get("required_status_checks") or {}
    contexts = set(required.get("contexts") or [])
    for check in required.get("checks") or []:
        if isinstance(check, dict) and check.get("context"):
            contexts.add(check["context"])
    return contexts


def protection_gate(delivery, protection, successful, approvals):
    """Return (ok, wait_review, reason)."""
    if protection is _NOT_FOUND or protection is None:
        if delivery.require_branch_protection \
                and not delivery.allow_without_branch_protection:
            return False, False, "branch protection is not available"
        return True, False, None
    if not isinstance(protection, dict):
        return False, False, "branch protection response is malformed"
    if delivery.require_branch_protection:
        if not (protection.get("enforce_admins") or {}).get("enabled"):
            return False, False, "branch protection allows admin bypass"
        if (protection.get("allow_force_pushes") or {}).get("enabled"):
            return False, False, "branch protection allows force pushes"
        if (protection.get("allow_deletions") or {}).get("enabled"):
            return False, False, "branch protection allows branch deletion"
    missing = _protection_contexts(protection) - set(successful)
    if missing:
        return False, False, "required checks are not green: %s" % ", ".join(
            sorted(missing))
    required_reviews = (
        (protection.get("required_pull_request_reviews") or {})
        .get("required_approving_review_count") or 0)
    if approvals < required_reviews:
        return False, True, "%s required approval(s), found %s" % (
            required_reviews, approvals)
    return True, False, None


def discover_pr(issue, repo):
    """Return the single PR number attached to this Linear issue."""
    found = []
    for attachment in issue.attachments or []:
        if not isinstance(attachment, dict):
            continue
        url = attachment.get("url")
        match = _PR_URL_RE.match(url) if isinstance(url, str) else None
        if not match or match.group(1).lower() != repo.lower():
            continue
        number = int(match.group(2))
        if number not in found:
            found.append(number)
    if not found:
        return None
    if len(found) > 1:
        open_numbers = []
        for attachment in issue.attachments or []:
            match = _PR_URL_RE.match(attachment.get("url") or "") \
                if isinstance(attachment, dict) else None
            if not match or match.group(1).lower() != repo.lower():
                continue
            metadata = attachment.get("metadata") or {}
            if str(metadata.get("status") or "").lower() == "open":
                open_numbers.append(int(match.group(2)))
        if len(set(open_numbers)) == 1:
            return open_numbers[0]
        raise DeliveryError(
            "ambiguous_pr_association", "multiple PRs: %s" % found)
    return found[0]


def _pr_repo_matches(pr, repo):
    base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    return base_repo == repo and head_repo == repo


def _pr_branch_matches_issue(pr, issue):
    identifier = getattr(issue, "identifier", None)
    head_ref = (pr.get("head") or {}).get("ref")
    if not isinstance(identifier, str) or not identifier.strip() \
            or not isinstance(head_ref, str) or not head_ref.strip():
        return False
    pattern = r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(
        identifier.strip())
    return re.search(pattern, head_ref, flags=re.IGNORECASE) is not None


def _valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40,64}", value)


def evaluate(settings, issue, pr, checks, files, reviews, protection,
             record):
    """Pure admission decision. Returns (action, reason, details)."""
    delivery = settings.delivery
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    sha = head.get("sha")
    if not _valid_sha(sha):
        return "pause", "pull request head SHA is invalid", {}
    if not _pr_repo_matches(pr, delivery.repo):
        return "pause", "pull request is not in the configured repository", {}
    if base.get("ref") != delivery.base_branch:
        return "pause", "pull request targets %r, expected %r" % (
            base.get("ref"), delivery.base_branch), {}
    if not _pr_branch_matches_issue(pr, issue):
        return "pause", (
            "pull request head branch is not bound to %s"
            % getattr(issue, "identifier", "the current issue")), {}
    if pr.get("merged") is True:
        return "confirm", "pull request is already merged", {
            "sha": sha, "pr": pr.get("number")}
    if pr.get("state") != "open":
        return "pause", "pull request is closed without merge", {}
    if pr.get("draft") is True:
        return "wait", "pull request is still a draft", {"sha": sha}

    pending, failing, successful, present = checks
    if pending:
        return "wait", "checks are pending: %s" % ", ".join(pending), {
            "sha": sha, "checks": pending}
    if failing:
        return "rework", "checks failed: %s" % ", ".join(failing), {
            "sha": sha, "checks": failing}
    if not present:
        return "pause", "no CI checks are attached to the head SHA", {"sha": sha}

    changes, approvals = review_state(
        reviews, sha, (pr.get("user") or {}).get("login"))
    if changes:
        return "rework", "a reviewer requested changes on the current SHA", {
            "sha": sha}

    protection_ok, wait_review, protection_reason = protection_gate(
        delivery, protection, successful, approvals)
    if not protection_ok:
        return ("wait" if wait_review else "pause"), protection_reason, {
            "sha": sha}

    required = delivery.required_approvals
    if approvals < required:
        return "wait", "%s required approval(s), found %s" % (
            required, approvals), {"sha": sha}

    risk_reason = risk_files(delivery, files, issue.labels)
    if risk_reason:
        return "pause", risk_reason, {"sha": sha}

    if not delivery.allow_auto_merge:
        return "pause", "automatic merge is disabled by policy", {"sha": sha}

    mergeable = pr.get("mergeable")
    state = pr.get("mergeable_state")
    if mergeable is None or state in (None, "unknown"):
        return "wait", "GitHub is still computing mergeability", {"sha": sha}
    if mergeable is False:
        if state == "dirty":
            return "rework", "pull request has merge conflicts", {"sha": sha}
        return "pause", "pull request is not mergeable (%s)" % state, {
            "sha": sha}
    if state in ("dirty",):
        return "rework", "pull request has merge conflicts", {"sha": sha}
    if state == "behind":
        return "rework", "pull request branch is behind the base branch", {
            "sha": sha}
    if state in ("blocked",):
        return "pause", "GitHub reports the merge is blocked", {"sha": sha}
    if state in ("unstable",):
        return "wait", "GitHub reports unstable checks", {"sha": sha}
    if state not in ("clean", "has_hooks"):
        return "pause", "unexpected merge state %r" % state, {"sha": sha}
    return "merge", "all delivery gates passed", {"sha": sha}


class DeliveryRecordStore:
    def __init__(self, directory):
        self.directory = directory

    def _path(self, issue):
        return os.path.join(self.directory, _record_name(issue.id) + ".json")

    def load(self, issue):
        path = self._path(issue)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            log.warning("Ignoring invalid delivery record path=%s error=%s",
                        path, error)
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, issue, record):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        path = self._path(issue)
        temp = "%s.tmp.%d.%s" % (path, os.getpid(), uuid.uuid4().hex)
        payload = dict(record)
        payload["updated_at"] = _utc_now()
        try:
            with open(temp, "x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, path)
        finally:
            try:
                os.remove(temp)
            except FileNotFoundError:
                pass
            except OSError as error:
                log.warning(
                    "Unable to remove temporary delivery record path=%s "
                    "error=%s", temp, error)
        return payload

    def load_pending_sync(self):
        """Return merges or delivery comments whose sync is still pending.

        These records must stay discoverable after an issue reaches a terminal
        state because the active-state poll will no longer return it.
        """
        try:
            names = sorted(os.listdir(self.directory))
        except OSError:
            return []
        pending = []
        for name in names:
            if not name.endswith(".json"):
                continue
            issue_id = name[:-5]
            record = self.load(SimpleNamespace(id=issue_id))
            if record.get("phase") != "merged_pending_sync" \
                    and not record.get("pending_notice_key"):
                continue
            stored_id = record.get("issue_id")
            if isinstance(stored_id, str) and stored_id:
                issue_id = stored_id
            record = dict(record)
            record.setdefault("issue_id", issue_id)
            pending.append(record)
        return pending


class DeliveryController:
    """Background host checker. It never starts an Agent."""

    def __init__(self, store, session_reporter=None, clock=None):
        self.store = store
        self.session_reporter = session_reporter
        self._clock = clock or time.monotonic
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._backoff_lock = threading.RLock()
        self._deferred = {}
        self._global_backoff_until = 0
        self._global_backoff_reason = None
        self._last_poll_started_at = None
        self._last_poll_completed_at = None
        self._last_success_at = None
        self._last_error = None
        self._poll_count = 0

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="symphony-delivery", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def refresh(self):
        self._wake.set()

    def snapshot(self):
        now = self._clock()
        try:
            enabled = self.store.settings().delivery.enabled
        except Exception:  # noqa: BLE001 - observability is best effort
            enabled = None
        with self._backoff_lock:
            deferred = []
            for issue_id, item in self._deferred.items():
                remaining_ms = max(0, int((item["until"] - now) * 1000))
                if remaining_ms <= 0:
                    continue
                deferred.append({
                    "issue_id": issue_id,
                    "identifier": item.get("identifier"),
                    "reason": item.get("reason"),
                    "attempts": item.get("attempts", 1),
                    "retry_in_ms": remaining_ms,
                })
            deferred.sort(key=lambda item: (
                item.get("identifier") or "", item.get("issue_id") or ""))
            global_remaining_ms = max(
                0, int((self._global_backoff_until - now) * 1000))
            return {
                "enabled": enabled,
                "thread_alive": bool(
                    self._thread and self._thread.is_alive()),
                "poll_count": self._poll_count,
                "last_poll_started_at": self._last_poll_started_at,
                "last_poll_completed_at": self._last_poll_completed_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "deferred_count": len(deferred),
                "deferred": deferred,
                "global_backoff_reason": self._global_backoff_reason,
                "global_backoff_remaining_ms": global_remaining_ms,
            }

    def _remaining_backoff(self):
        with self._backoff_lock:
            remaining = self._global_backoff_until - self._clock()
            if remaining <= 0:
                self._global_backoff_until = 0
                self._global_backoff_reason = None
                return 0
            return remaining

    def _issue_is_deferred(self, issue_id):
        with self._backoff_lock:
            item = self._deferred.get(issue_id)
            if item is None:
                return False
            if item["until"] > self._clock():
                return True
            self._deferred.pop(issue_id, None)
            return False

    def _clear_issue_backoff(self, issue_id):
        with self._backoff_lock:
            self._deferred.pop(issue_id, None)

    @staticmethod
    def _backoff_delay(error, previous_attempts, poll_interval_ms):
        detail = getattr(error, "detail", None)
        detail = detail if isinstance(detail, dict) else {}
        reason = getattr(error, "reason", type(error).__name__)
        status = detail.get("status")
        if reason == "github_rate_limited" or status == 429:
            try:
                return max(1.0, min(float(detail.get("retry_after")), 3600.0))
            except (TypeError, ValueError):
                return 60.0
        base = max(30.0, float(poll_interval_ms or 30_000) / 1000.0)
        if status in (401, 403):
            base = max(base, 300.0)
        return min(base * (2 ** min(previous_attempts, 6)), 1800.0)

    def _defer_issue(self, issue, error):
        settings = self.store.settings()
        reason = getattr(error, "reason", type(error).__name__)
        detail = getattr(error, "detail", None)
        detail = detail if isinstance(detail, dict) else {}
        with self._backoff_lock:
            previous = self._deferred.get(issue.id, {})
            attempts = int(previous.get("attempts") or 0) + 1
            delay = self._backoff_delay(
                error, max(0, attempts - 1),
                settings.delivery.poll_interval_ms)
            until = self._clock() + delay
            self._deferred[issue.id] = {
                "identifier": issue.identifier,
                "reason": reason,
                "attempts": attempts,
                "until": until,
            }
            if reason == "github_rate_limited" or detail.get("status") == 429:
                self._global_backoff_until = max(
                    self._global_backoff_until, until)
                self._global_backoff_reason = reason
            self._last_error = reason
        log.warning(
            "delivery check deferred identifier=%s reason=%s retry_after_s=%s",
            issue.identifier, reason, round(delay, 1))
        return delay

    def _lock_for(self, issue_id):
        with self._locks_guard:
            return self._locks.setdefault(issue_id, threading.Lock())

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - supervisor must keep running
                log.exception("delivery poll failed")
            try:
                interval = self.store.settings().delivery.poll_interval_ms
            except Exception:  # noqa: BLE001
                interval = 30_000
            self._wake.wait(max(interval, 1000) / 1000.0)

    def poll_once(self):
        settings = self.store.settings()
        delivery = settings.delivery
        if not delivery.enabled:
            return
        if self._remaining_backoff() > 0:
            return
        with self._backoff_lock:
            self._poll_count += 1
            self._last_poll_started_at = _utc_now()
        try:
            issues = tracker_adapter.fetch_issues_by_states(
                settings.tracker,
                [delivery.review_state, delivery.merge_state],
            )
            records = DeliveryRecordStore(delivery.state_dir)
            pending_by_id = {
                record["issue_id"]: record
                for record in records.load_pending_sync()
                if isinstance(record.get("issue_id"), str) and record["issue_id"]
            }
            found_ids = {issue.id for issue in issues}
            missing_ids = [
                issue_id for issue_id in pending_by_id
                if issue_id not in found_ids
            ]
            if missing_ids:
                try:
                    issues.extend(tracker_adapter.fetch_issues_by_ids(
                        settings.tracker, missing_ids))
                except Exception as error:  # noqa: BLE001
                    log.warning(
                        "delivery pending-sync issue refresh failed ids=%s error=%s",
                        missing_ids, error)

            seen = set()
            for issue in issues:
                if issue.id in seen:
                    continue
                seen.add(issue.id)
                if self._remaining_backoff() > 0:
                    break
                if self._issue_is_deferred(issue.id):
                    continue
                pending = pending_by_id.get(issue.id)
                if pending is not None:
                    if pending.get("phase") == "merged_pending_sync":
                        self._resume_pending_sync(settings, issue, pending)
                    else:
                        self._retry_pending_notice(settings, issue)
                else:
                    self.check(settings, issue)
        except Exception as error:
            with self._backoff_lock:
                self._last_error = getattr(
                    error, "reason", type(error).__name__)
                self._last_poll_completed_at = _utc_now()
            raise
        else:
            with self._backoff_lock:
                self._last_poll_completed_at = _utc_now()
                self._last_success_at = self._last_poll_completed_at
                if not self._deferred and not self._global_backoff_until:
                    self._last_error = None

    def _resume_pending_sync(self, settings, issue, record):
        """Finish a merge whose GitHub result was already durably confirmed."""
        lock = self._lock_for(issue.id)
        if not lock.acquire(blocking=False):
            return
        try:
            records = DeliveryRecordStore(settings.delivery.state_dir)
            current = records.load(issue)
            if current.get("phase") != "merged_pending_sync":
                return
            if not self._pending_sync_belongs_to_current_lifecycle(
                    settings, issue, current):
                self._supersede_pending_sync(settings, issue, records, current)
                return
            pr_number = current.get("pr")
            expected_sha = current.get("head_sha") or current.get("gated_sha")
            if not isinstance(pr_number, int) or not _valid_sha(expected_sha):
                raise DeliveryError(
                    "invalid_pending_merge_record",
                    (pr_number, expected_sha))
            github = GitHub(settings.delivery)
            pr = github.request(
                "GET",
                _repo_path(settings.delivery.repo, "/pulls/%s" % pr_number))
            if not isinstance(pr, dict):
                raise DeliveryError("github_unknown_payload", "pull request")
            actual_sha = (pr.get("head") or {}).get("sha")
            if pr.get("merged") is not True or actual_sha != expected_sha:
                raise DeliveryError(
                    "pending_merge_still_unconfirmed",
                    (pr_number, expected_sha, actual_sha))
            self._confirm_merge(
                settings, issue, records, current, pr, github)
            self._clear_issue_backoff(issue.id)
        except (DeliveryError, gh_client.GitHubError) as error:
            self._defer_issue(issue, error)
        except Exception:  # noqa: BLE001 - one issue must not stop recovery
            log.exception(
                "delivery pending-sync failed identifier=%s", issue.identifier)
            self._defer_issue(issue, DeliveryError("delivery_internal_error"))
        finally:
            lock.release()

    def _pending_sync_belongs_to_current_lifecycle(
            self, settings, issue, record):
        """Fail closed if a terminal merge was followed by a new task run.

        A pending-sync record may be left after Linear reached Done and the
        final local save was lost. It may also be left while Linear still sits
        in the delivery states before that transition. In both cases Done is a
        valid recovery target. An issue back in the ordinary active states is a
        new lifecycle and must not be completed from the old PR.
        """
        from .util import normalize_issue_state

        delivery = settings.delivery
        previous_lifecycle = self._record_lifecycle_marker(record)
        current_lifecycle = self._current_lifecycle_marker(
            issue, strict=bool(previous_lifecycle))
        if isinstance(previous_lifecycle, str) and previous_lifecycle \
                and isinstance(current_lifecycle, str) and current_lifecycle \
                and previous_lifecycle != current_lifecycle:
            return False
        state = normalize_issue_state(issue.state or "")
        done = normalize_issue_state(delivery.done_state)
        terminal = {
            normalize_issue_state(value)
            for value in (settings.tracker.terminal_states or [])
        }
        if state == done or state in terminal:
            return True
        allowed = {
            normalize_issue_state(delivery.review_state),
            normalize_issue_state(delivery.merge_state),
        }
        if state not in allowed:
            return False
        expected_state = normalize_issue_state(
            record.get("pre_sync_state") or "")
        expected_updated = record.get("pre_sync_updated_at")
        return (
            state == expected_state
            and bool(expected_updated)
            and expected_updated == _iso(issue.updated_at)
        )

    @staticmethod
    def _record_lifecycle_marker(record):
        marker = record.get("lifecycle_marker")
        if isinstance(marker, str) and marker:
            return marker
        envelope = record.get("agent_session_report")
        if isinstance(envelope, dict):
            marker = envelope.get("lifecycle")
            if isinstance(marker, str) and marker:
                return marker
        return None

    def _current_lifecycle_marker(self, issue, strict=False):
        if self.session_reporter is None:
            return None
        records = getattr(self.session_reporter, "records", None)
        if isinstance(records, dict):
            current = records.get(issue.id)
            if isinstance(current, dict):
                marker = current.get("marker")
                if isinstance(marker, str) and marker:
                    return marker
        try:
            envelope = self.session_reporter.delivery_event(
                issue, "waiting", "delivery lifecycle check", None, None)
        except Exception as error:  # noqa: BLE001 - reporting stays optional
            if strict:
                raise DeliveryError(
                    "delivery_lifecycle_unavailable") from error
            return None
        if not isinstance(envelope, dict):
            if strict:
                raise DeliveryError("delivery_lifecycle_unavailable")
            return None
        marker = envelope.get("lifecycle")
        return marker if isinstance(marker, str) and marker else None

    def _delivery_scope_changed(self, record, pr_number, lifecycle_marker):
        previous_pr = record.get("pr")
        if isinstance(previous_pr, int) and previous_pr != pr_number:
            return True
        previous_lifecycle = self._record_lifecycle_marker(record)
        if not isinstance(previous_lifecycle, str) or not previous_lifecycle:
            previous_lifecycle = None
        return (
            isinstance(previous_lifecycle, str)
            and bool(previous_lifecycle)
            and isinstance(lifecycle_marker, str)
            and bool(lifecycle_marker)
            and previous_lifecycle != lifecycle_marker
        )

    def _notice_belongs_to_current_scope(self, settings, issue, record):
        if record.get("phase") == "superseded_pending_sync":
            return False
        try:
            current_pr = discover_pr(issue, settings.delivery.repo)
        except DeliveryError:
            return False
        previous_pr = record.get("pr")
        if isinstance(previous_pr, int) and current_pr is not None \
                and previous_pr != current_pr:
            return False
        previous_lifecycle = self._record_lifecycle_marker(record)
        current_lifecycle = self._current_lifecycle_marker(
            issue, strict=bool(previous_lifecycle))
        return not (
            isinstance(previous_lifecycle, str)
            and bool(previous_lifecycle)
            and isinstance(current_lifecycle, str)
            and bool(current_lifecycle)
            and previous_lifecycle != current_lifecycle
        )

    def _superseded_delivery_matches(self, record, pr_number):
        superseded_pr = record.get("pr")
        return (
            superseded_pr is None
            or pr_number is None
            or pr_number == superseded_pr
        )

    def _supersede_pending_sync(self, settings, issue, records, record):
        self._save(records, issue, record, {
            "phase": "superseded_pending_sync",
            "session_phase": "superseded_pending_sync",
            "reason": (
                "任务在合并同步完成前已进入新的生命周期；旧交付记录不再"
                "推进当前状态。"
            ),
            "superseded_at": _utc_now(),
            "pending_notice_key": None,
            "pending_notice_body": None,
            "pending_notice_creation_attempted": None,
        })
        log.warning(
            "delivery pending-sync superseded identifier=%s state=%s",
            issue.identifier, issue.state)

    def _retry_pending_notice(self, settings, issue):
        lock = self._lock_for(issue.id)
        if not lock.acquire(blocking=False):
            return
        try:
            records = DeliveryRecordStore(settings.delivery.state_dir)
            current = records.load(issue)
            notice_key = current.get("pending_notice_key")
            body = current.get("pending_notice_body")
            if not isinstance(notice_key, str) or not notice_key:
                return
            if not isinstance(body, str) or not body:
                log.warning(
                    "delivery pending notice malformed identifier=%s",
                    issue.identifier)
                return
            if not self._notice_belongs_to_current_scope(
                    settings, issue, current):
                self._save(records, issue, current, {
                    "pending_notice_key": None,
                    "pending_notice_body": None,
                    "pending_notice_creation_attempted": None,
                })
                log.warning(
                    "delivery pending notice discarded as stale "
                    "identifier=%s pr=%s", issue.identifier,
                    current.get("pr"))
                return
            delivered = self._notice(
                settings, issue, records, current, notice_key, body,
                scoped=True)
            if delivered.get("last_notice_key") == notice_key:
                self._clear_issue_backoff(issue.id)
        except DeliveryError as error:
            self._defer_issue(issue, error)
        except Exception:  # noqa: BLE001 - one notice must not stop the poll
            log.exception(
                "delivery notice recovery failed identifier=%s",
                issue.identifier)
            self._defer_issue(issue, DeliveryError("delivery_internal_error"))
        finally:
            lock.release()

    def check(self, settings, issue):
        delivery = settings.delivery
        lock = self._lock_for(issue.id)
        if not lock.acquire(blocking=False):
            return
        try:
            self._check_locked(settings, issue, delivery)
            self._clear_issue_backoff(issue.id)
        except (DeliveryError, gh_client.GitHubError) as error:
            self._defer_issue(issue, error)
            if error.reason == "ambiguous_pr_association":
                records = DeliveryRecordStore(delivery.state_dir)
                record = records.load(issue)
                self._pause(
                    settings, issue, records, record, 0, None,
                    "ambiguous PR association; human intervention required",
                    {
                        "issue_updated_at": _iso(issue.updated_at) or "",
                        "head_sha": None,
                        "pr_updated_at": None,
                    })
        except Exception:  # noqa: BLE001 - one issue must not stop the poll
            log.exception("delivery check failed identifier=%s", issue.identifier)
            self._defer_issue(issue, DeliveryError("delivery_internal_error"))
        finally:
            lock.release()

    def _check_locked(self, settings, issue, delivery):
        pr_number = discover_pr(issue, delivery.repo)
        if pr_number is None:
            return
        records = DeliveryRecordStore(delivery.state_dir)
        record = records.load(issue)
        lifecycle_marker = self._current_lifecycle_marker(
            issue, strict=bool(self._record_lifecycle_marker(record)))
        if record.get("phase") == "superseded_pending_sync" \
                and self._superseded_delivery_matches(record, pr_number):
            log.info(
                "delivery ignored superseded PR identifier=%s pr=%s",
                issue.identifier, pr_number)
            return
        if record.get("phase") == "superseded_pending_sync" \
                or self._delivery_scope_changed(
                    record, pr_number, lifecycle_marker):
            # A different PR or Agent Session run is a new delivery object.
            # Drop old counters, gates, and notice fingerprints before using
            # any result from the new object.
            log.info(
                "delivery reset for new scope identifier=%s old_pr=%s "
                "new_pr=%s old_lifecycle=%s new_lifecycle=%s",
                issue.identifier, record.get("pr"), pr_number,
                record.get("lifecycle_marker"), lifecycle_marker)
            record = {}
        github = GitHub(delivery)

        pr = github.request(
            "GET", _repo_path(delivery.repo, "/pulls/%s" % pr_number))
        if not isinstance(pr, dict):
            raise DeliveryError("github_unknown_payload", "pull request")
        head_sha = ((pr.get("head") or {}).get("sha"))
        issue_updated = _iso(issue.updated_at) or ""
        pr_updated = pr.get("updated_at") or ""
        fingerprint = {
            "issue_updated_at": issue_updated,
            "head_sha": head_sha,
            "pr_updated_at": pr_updated,
        }
        if record.get("phase") == "paused" \
                and record.get("paused_fingerprint") == fingerprint:
            return

        sha = head_sha
        runs = []
        statuses = []
        if _valid_sha(sha):
            runs = github.pages(_repo_path(
                delivery.repo,
                "/commits/%s/check-runs" % sha), "check_runs")
            statuses = github.pages(_repo_path(
                delivery.repo,
                "/commits/%s/statuses" % sha))
        checks = classify_checks(runs, statuses, sha)
        files = github.pages(_repo_path(
            delivery.repo, "/pulls/%s/files" % pr_number))
        reviews = github.pages(_repo_path(
            delivery.repo, "/pulls/%s/reviews" % pr_number))
        protection = github.request(
            "GET",
            _repo_path(
                delivery.repo,
                "/branches/%s/protection" %
                urllib.parse.quote(delivery.base_branch, safe="")),
            allow_missing=True,
        )

        action, reason, details = evaluate(
            settings, issue, pr, checks, files, reviews, protection, record)
        sha = details.get("sha") or head_sha
        if record.get("head_sha") != sha:
            record = dict(record)
            record["head_sha"] = sha
            record["paused_fingerprint"] = None
            record["last_notice_key"] = None

        if action == "wait":
            self._save(records, issue, record, {
                "phase": "waiting",
                "reason": reason,
                "pr": pr_number,
                "head_sha": sha,
            })
            self._report_session(issue, "waiting", reason, pr_number, sha)
            return
        if action == "pause":
            self._pause(settings, issue, records, record, pr_number, sha,
                        reason, fingerprint)
            return
        if action == "rework":
            self._rework(settings, issue, records, record, pr_number, sha,
                         reason)
            return
        if action == "confirm":
            self._confirm_merge(settings, issue, records, record, pr, github)
            return
        if action == "merge":
            self._merge(settings, issue, records, record, pr, github)
            return
        self._pause(settings, issue, records, record, pr_number, sha,
                    "unknown delivery decision: %s" % action, fingerprint)

    def _save(self, records, issue, record, changes):
        payload = {**record, **changes}
        payload["issue_id"] = issue.id
        payload["identifier"] = issue.identifier
        if issue.url:
            payload["issue_url"] = issue.url
        phase = changes.get("phase")
        if phase is not None:
            payload.pop("agent_session_report", None)
            lifecycle_marker = self._current_lifecycle_marker(issue)
            if isinstance(lifecycle_marker, str) and lifecycle_marker:
                payload["lifecycle_marker"] = lifecycle_marker
            if self.session_reporter and phase in (
                    "merged", "merged_pending_sync", "paused"):
                report_phase = (
                    changes.get("session_phase", "merged_pending_sync")
                    if phase == "merged" else phase)
                sha = payload.get("head_sha")
                reason = (
                    "宿主已核验 GitHub 合并结果；HEAD %s。" % sha
                    if phase in ("merged", "merged_pending_sync")
                    else payload.get("reason", "paused"))
                try:
                    envelope = self.session_reporter.delivery_event(
                        issue, report_phase, reason, payload.get("pr"), sha)
                    if isinstance(envelope, dict):
                        payload["agent_session_report"] = envelope
                        envelope_lifecycle = envelope.get("lifecycle")
                        if isinstance(envelope_lifecycle, str) \
                                and envelope_lifecycle:
                            payload["lifecycle_marker"] = envelope_lifecycle
                except Exception as error:  # noqa: BLE001 - reporting stays optional
                    log.warning("Agent delivery envelope unavailable reason=%s",
                                getattr(error, "reason", type(error).__name__))
        return records.save(issue, payload)

    def _report_session(self, issue, phase, reason, pr_number, sha):
        if not self.session_reporter:
            return
        try:
            self.session_reporter.delivery(issue, phase, reason, pr_number, sha)
        except Exception as error:  # noqa: BLE001 - reporting is asynchronous
            log.warning(
                "Agent delivery report unavailable identifier=%s reason=%s",
                issue.identifier, getattr(error, "reason", type(error).__name__))

    def _notice(self, settings, issue, records, record, key, body,
                scoped=False):
        lifecycle_marker = self._current_lifecycle_marker(issue)
        if not lifecycle_marker:
            lifecycle_marker = record.get("lifecycle_marker")
        notice_key = (
            key if scoped or not lifecycle_marker
            else _digest("notice", key, lifecycle_marker))
        if record.get("last_notice_key") == notice_key:
            return record
        if record.get("pending_notice_key") == notice_key \
                and record.get("pending_notice_creation_attempted") is True:
            try:
                if _comment_exists(settings, issue.id, notice_key):
                    return self._save(records, issue, record, {
                        "last_notice_key": notice_key,
                        "pending_notice_key": None,
                        "pending_notice_body": None,
                        "pending_notice_creation_attempted": None,
                    })
            except Exception as error:  # noqa: BLE001 - retry the send
                log.warning(
                    "Linear comment confirmation failed identifier=%s "
                    "error=%s", issue.identifier, error)
                raise DeliveryError(
                    "linear_comment_confirmation_failed", repr(error)
                ) from error
        changes = {
            "pending_notice_key": notice_key,
            "pending_notice_body": body,
            "pending_notice_creation_attempted": True,
        }
        if isinstance(lifecycle_marker, str) and lifecycle_marker:
            changes["lifecycle_marker"] = lifecycle_marker
        record = self._save(records, issue, record, changes)
        try:
            linear.create_comment(
                settings.tracker, issue.id,
                "[symphony-delivery:%s]\n%s" % (notice_key, body))
        except Exception as error:  # noqa: BLE001 - keep a retry marker
            log.warning("Linear comment failed identifier=%s error=%s",
                        issue.identifier, error)
            return record
        return self._save(records, issue, record, {
            "last_notice_key": notice_key,
            "pending_notice_key": None,
            "pending_notice_body": None,
            "pending_notice_creation_attempted": None,
        })

    def _transition(self, settings, issue, state_name):
        from .util import normalize_issue_state

        if normalize_issue_state(issue.state) == normalize_issue_state(state_name):
            return
        previous = issue.state
        linear.update_issue_state(
            settings.tracker, issue.id, issue.team_id, state_name)
        issue.state = state_name
        log.info(
            "delivery moved issue identifier=%s state=%s -> %s",
            issue.identifier, previous, state_name)

    def _refresh_issue(self, settings, issue):
        """Re-read Linear immediately before a host mutation.

        The polled issue can be stale; a cancelled or re-routed issue must not
        be merged, paused back into review, or sent to rework.
        """
        try:
            found = tracker_adapter.fetch_issues_by_ids(
                settings.tracker, [issue.id])
        except Exception as error:  # noqa: BLE001 - fail closed
            log.warning("delivery issue refresh failed identifier=%s error=%s",
                        issue.identifier, error)
            return None
        current = next(
            (candidate for candidate in found if candidate.id == issue.id),
            None)
        if current is None:
            return None
        issue.state = current.state
        issue.updated_at = current.updated_at
        return current

    def _allowed_delivery_state(self, settings, issue, current):
        from .util import normalize_issue_state

        if current is None:
            return False
        state = normalize_issue_state(current.state or "")
        allowed = {
            normalize_issue_state(settings.delivery.review_state),
            normalize_issue_state(settings.delivery.merge_state),
        }
        return state in allowed

    def _pause(self, settings, issue, records, record, pr_number, sha,
               reason, fingerprint):
        current = self._refresh_issue(settings, issue)
        if current is None:
            return
        if not self._allowed_delivery_state(settings, issue, current):
            log.info(
                "delivery pause skipped; issue left review/merge state "
                "identifier=%s state=%s", issue.identifier, current.state)
            return
        try:
            self._transition(settings, issue, settings.delivery.review_state)
        except Exception as error:  # noqa: BLE001 - still record the pause
            log.warning("delivery pause transition failed identifier=%s error=%s",
                        issue.identifier, error)
        key = _digest("pause", settings.delivery.repo, pr_number, sha, reason)
        record = self._notice(
            settings, issue, records, record, key,
            "自动交付已暂停，需要人工介入。\n\n"
            "PR: `%s#%s`\nSHA: `%s`\n原因: %s\n\n"
            "处理后将 Issue 移回 `%s` 或 `Rework`，宿主会重新核验。"
            % (settings.delivery.repo, pr_number, sha or "unknown", reason,
               settings.delivery.review_state))
        self._save(records, issue, record, {
            "phase": "paused",
            "reason": reason,
            "pr": pr_number,
            "head_sha": sha,
            "paused_fingerprint": fingerprint,
        })
        self._report_session(issue, "paused", reason, pr_number, sha)

    def _rework(self, settings, issue, records, record, pr_number, sha,
                reason):
        current = self._refresh_issue(settings, issue)
        if current is None:
            return
        if not self._allowed_delivery_state(settings, issue, current):
            log.info(
                "delivery rework skipped; issue left review/merge state "
                "identifier=%s state=%s", issue.identifier, current.state)
            return
        attempts = int(record.get("rework_attempts") or 0)
        last = record.get("last_rework") or {}
        if attempts >= settings.delivery.max_rework_attempts:
            self._pause(
                settings, issue, records, record, pr_number, sha,
                "automatic rework limit reached: %s" % reason,
                {"head_sha": sha, "issue_updated_at": _iso(issue.updated_at)})
            return
        if last.get("head_sha") == sha and last.get("reason") == reason:
            self._pause(
                settings, issue, records, record, pr_number, sha,
                "rework produced no new head SHA: %s" % reason,
                {"head_sha": sha, "issue_updated_at": _iso(issue.updated_at)})
            return
        key = _digest("rework", settings.delivery.repo, pr_number, sha, reason)
        record = self._notice(
            settings, issue, records, record, key,
            "自动返工已排入队列。\n\n"
            "PR: `%s#%s`\nSHA: `%s`\n原因: %s\n"
            "返工次数: %s/%s。"
            % (settings.delivery.repo, pr_number, sha or "unknown", reason,
               attempts + 1, settings.delivery.max_rework_attempts))
        record = self._save(records, issue, record, {
            "phase": "rework",
            "reason": reason,
            "pr": pr_number,
            "head_sha": sha,
            "rework_attempts": attempts + 1,
            "last_rework": {"head_sha": sha, "reason": reason},
        })
        try:
            self._transition(settings, issue, settings.delivery.rework_state)
            self._report_session(issue, "rework", reason, pr_number, sha)
        except Exception as error:  # noqa: BLE001
            log.warning("delivery rework transition failed identifier=%s error=%s",
                        issue.identifier, error)

    def _merge(self, settings, issue, records, record, pr, github):
        delivery = settings.delivery
        sha = (pr.get("head") or {}).get("sha")
        pr_number = pr.get("number")
        current = self._refresh_issue(settings, issue)
        if current is None:
            return
        if not self._allowed_delivery_state(settings, issue, current):
            log.info(
                "delivery merge skipped; issue left review/merge state "
                "identifier=%s state=%s", issue.identifier, current.state)
            return
        try:
            self._transition(settings, issue, delivery.merge_state)
        except Exception as error:  # noqa: BLE001 - do not merge without the
            # visible host-owned state transition
            self._pause(
                settings, issue, records, record, pr_number, sha,
                "cannot move issue to merge state: %s" % error,
                {"head_sha": sha, "issue_updated_at": _iso(issue.updated_at)})
            return
        record = self._save(records, issue, record, {
            "phase": "merging",
            "pr": pr_number,
            "head_sha": sha,
            "gated_sha": sha,
            "merge_attempts": int(record.get("merge_attempts") or 0) + 1,
        })
        try:
            result = github.request(
                "PUT",
                _repo_path(delivery.repo, "/pulls/%s/merge" % pr_number),
                body={
                    "sha": sha,
                    "merge_method": delivery.merge_method,
                },
            )
        except DeliveryError as error:
            self._handle_merge_error(
                settings, issue, records, record, pr_number, sha, error)
            return
        if isinstance(result, dict) and result.get("merged") is True:
            try:
                confirmed = github.request(
                    "GET",
                    _repo_path(delivery.repo, "/pulls/%s" % pr_number))
            except DeliveryError as error:
                self._handle_merge_error(
                    settings, issue, records, record, pr_number, sha, error)
                return
            self._confirm_merge(
                settings, issue, records, record, confirmed, github)
            return
        self._handle_merge_error(
            settings, issue, records, record, pr_number, sha,
            DeliveryError("merge_not_confirmed", result))

    def _handle_merge_error(self, settings, issue, records, record,
                            pr_number, sha, error):
        # The request may have succeeded even when its response was lost.
        try:
            pr = GitHub(settings.delivery).request(
                "GET",
                _repo_path(
                    settings.delivery.repo, "/pulls/%s" % pr_number))
        except Exception:  # noqa: BLE001
            pr = {}
        if isinstance(pr, dict) and pr.get("merged") is True \
                and (pr.get("head") or {}).get("sha") == sha:
            self._confirm_merge(settings, issue, records, record, pr,
                                GitHub(settings.delivery))
            return
        self._pause(
            settings, issue, records, record, pr_number, sha,
            "merge call failed or was not confirmed: %s" % error,
            {"head_sha": sha, "issue_updated_at": _iso(issue.updated_at)})

    def _confirm_merge(self, settings, issue, records, record, pr, github):
        delivery = settings.delivery
        was_pending_sync = record.get("phase") == "merged_pending_sync"
        sha = (pr.get("head") or {}).get("sha")
        gated = record.get("gated_sha")
        pr_number = pr.get("number")
        if gated and sha != gated:
            self._pause(
                settings, issue, records, record, pr_number, sha,
                "merged SHA %s differs from gated SHA %s" % (sha, gated),
                {"head_sha": sha, "issue_updated_at": _iso(issue.updated_at)})
            return
        if pr.get("merged") is not True:
            self._pause(
                settings, issue, records, record, pr_number, sha,
                "merge was not confirmed by GitHub",
                {"head_sha": sha, "issue_updated_at": _iso(issue.updated_at)})
            return

        # GitHub is now the durable source of truth. Persist that confirmation
        # before mutating Linear so a crash cannot strand a Done issue without
        # a recoverable terminal delivery record.
        record = self._save(records, issue, record, {
            "phase": "merged_pending_sync",
            "session_phase": "merged_pending_sync",
            "reason": "GitHub PR 已确认合并，正在同步 Linear 终态。",
            "pr": pr_number,
            "head_sha": sha,
            "gated_sha": sha,
            "merged_at": _utc_now(),
            "issue_updated_at": _iso(issue.updated_at),
            "pre_sync_state": issue.state,
            "pre_sync_updated_at": _iso(issue.updated_at),
        })
        from .util import normalize_issue_state

        current = self._refresh_issue(settings, issue)
        terminal = {
            normalize_issue_state(state)
            for state in (settings.tracker.terminal_states or [])
        }
        done = normalize_issue_state(delivery.done_state)
        current_state = normalize_issue_state(
            current.state if current is not None else issue.state or "")
        linear_done = current is not None and current_state == done
        terminal_without_done = (
            current is not None
            and current_state in terminal
            and current_state != done
        )
        if current is not None and not linear_done and not terminal_without_done:
            allowed = {
                normalize_issue_state(delivery.review_state),
                normalize_issue_state(delivery.merge_state),
            }
            if current_state not in allowed or (
                    was_pending_sync
                    and not self._pending_sync_belongs_to_current_lifecycle(
                        settings, issue, record)):
                self._supersede_pending_sync(
                    settings, issue, records, record)
                return
            refreshed_state = current.state
            refreshed_updated = _iso(current.updated_at)
            if record.get("pre_sync_state") != refreshed_state \
                    or record.get("pre_sync_updated_at") != refreshed_updated:
                record = self._save(records, issue, record, {
                    "phase": "merged_pending_sync",
                    "session_phase": "merged_pending_sync",
                    "reason": (
                        "GitHub PR 已确认合并，正在确认 Linear 生命周期后"
                        "同步终态。"
                    ),
                    "pr": pr_number,
                    "head_sha": sha,
                    "gated_sha": sha,
                    "pre_sync_state": refreshed_state,
                    "pre_sync_updated_at": refreshed_updated,
                })
        if current is None:
            log.error(
                "merged PR but Linear state refresh failed identifier=%s",
                issue.identifier)
        elif terminal_without_done:
            log.warning(
                "merged PR but issue is already terminal identifier=%s state=%s",
                issue.identifier, current.state)
        else:
            try:
                self._transition(settings, issue, delivery.done_state)
                linear_done = True
            except Exception as error:  # noqa: BLE001 - GitHub is already merged
                log.error(
                    "merged PR but Linear transition failed identifier=%s "
                    "error=%s", issue.identifier, error)

        if not linear_done and not terminal_without_done:
            # Keep the intermediate record discoverable. A later poll can
            # query this issue by ID even if a lost response actually moved it
            # to Done before the controller observed the result.
            self._save(records, issue, record, {
                "phase": "merged_pending_sync",
                "session_phase": "merged_pending_sync",
                "reason": "GitHub PR 已确认合并，Linear 终态同步尚未确认。",
                "pr": pr_number,
                "head_sha": sha,
                "gated_sha": sha,
            })
            self._report_session(
                issue, "merged_pending_sync",
                "宿主已核验 GitHub 合并结果；Linear 终态同步尚未确认。",
                pr_number, sha)
            log.warning(
                "delivery merge confirmed; Linear sync pending "
                "identifier=%s pr=%s sha=%s",
                issue.identifier, pr_number, sha)
            return

        key = _digest("merged", delivery.repo, pr_number, sha)
        if linear_done:
            result_text = (
                "PR 已由宿主安全门禁合并。\n\n"
                "PR: `%s#%s`\nSHA: `%s`\n合并方式: `%s`\n"
                "Linear 已更新为 `%s`。"
                % (delivery.repo, pr_number, sha, delivery.merge_method,
                   delivery.done_state))
        else:
            result_text = (
                "PR 已由宿主安全门禁合并，但 Linear 状态未能自动更新。\n\n"
                "PR: `%s#%s`\nSHA: `%s`\n合并方式: `%s`\n"
                "请人工确认 Linear 当前状态；GitHub 合并结果已经核验。"
                % (delivery.repo, pr_number, sha, delivery.merge_method))
        record = self._notice(
            settings, issue, records, record, key, result_text)
        self._save(records, issue, record, {
            "phase": "merged",
            "session_phase": "merged" if linear_done else "merged_pending_sync",
            "reason": "merged",
            "pr": pr_number,
            "head_sha": sha,
            "gated_sha": sha,
        })
        self._report_session(
            issue, "merged" if linear_done else "merged_pending_sync",
            "宿主已核验 GitHub 合并结果；HEAD %s。" % sha, pr_number, sha)
        log.info(
            "delivery merged identifier=%s pr=%s sha=%s",
            issue.identifier, pr_number, sha)
