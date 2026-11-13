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
import urllib.parse
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
        if allow_missing and status == 403:
            payload = result.get("body")
            message = payload.get("message") if isinstance(payload, dict) else ""
            if "rate limit" in str(message).lower():
                raise DeliveryError("github_rate_limited", message)
            return _NOT_FOUND
        raise DeliveryError(
            "github_status", (status, method, path))

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
        temp = path + ".tmp"
        payload = dict(record)
        payload["updated_at"] = _utc_now()
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
        return payload

    def load_pending_sync(self):
        """Return confirmed merges whose Linear state still needs syncing.

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
            if record.get("phase") != "merged_pending_sync":
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

    def __init__(self, store, session_reporter=None):
        self.store = store
        self.session_reporter = session_reporter
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._locks = {}
        self._locks_guard = threading.Lock()

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
            pending = pending_by_id.get(issue.id)
            if pending is not None:
                self._resume_pending_sync(settings, issue, pending)
            else:
                self.check(settings, issue)

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
        except DeliveryError as error:
            log.warning(
                "delivery pending-sync deferred identifier=%s reason=%s detail=%s",
                issue.identifier, error.reason, error.detail)
        except Exception:  # noqa: BLE001 - one issue must not stop recovery
            log.exception(
                "delivery pending-sync failed identifier=%s", issue.identifier)
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
        })
        log.warning(
            "delivery pending-sync superseded identifier=%s state=%s",
            issue.identifier, issue.state)

    def check(self, settings, issue):
        delivery = settings.delivery
        lock = self._lock_for(issue.id)
        if not lock.acquire(blocking=False):
            return
        try:
            self._check_locked(settings, issue, delivery)
        except DeliveryError as error:
            log.warning(
                "delivery check deferred identifier=%s reason=%s detail=%s",
                issue.identifier, error.reason, error.detail)
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
        finally:
            lock.release()

    def _check_locked(self, settings, issue, delivery):
        pr_number = discover_pr(issue, delivery.repo)
        if pr_number is None:
            return
        records = DeliveryRecordStore(delivery.state_dir)
        record = records.load(issue)
        if record.get("phase") == "superseded_pending_sync" \
                and self._superseded_delivery_matches(record, pr_number):
            log.info(
                "delivery ignored superseded PR identifier=%s pr=%s",
                issue.identifier, pr_number)
            return
        if record.get("phase") == "superseded_pending_sync":
            # A different PR is a new delivery object; do not carry rework
            # counters or gate fingerprints from the superseded lifecycle.
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
                except Exception as error:  # noqa: BLE001 - reporting stays optional
                    log.warning("Agent delivery envelope unavailable reason=%s",
                                getattr(error, "reason", type(error).__name__))
        return records.save(issue, payload)

    def _report_session(self, issue, phase, reason, pr_number, sha):
        if self.session_reporter:
            self.session_reporter.delivery(issue, phase, reason, pr_number, sha)

    def _notice(self, settings, issue, records, record, key, body):
        if record.get("last_notice_key") == key:
            return record
        try:
            linear.create_comment(
                settings.tracker, issue.id,
                "[symphony-delivery] %s" % body)
        except Exception as error:  # noqa: BLE001 - state still advances
            log.warning("Linear comment failed identifier=%s error=%s",
                        issue.identifier, error)
        return self._save(records, issue, record, {"last_notice_key": key})

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
