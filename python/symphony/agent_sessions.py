"""Best-effort, asynchronous Linear Agent Session reporting.

The scheduler and delivery controller enqueue semantic events. A host-only
reporter owns OAuth, session state and a durable outbox; network failures never
block the execution loop. Worker completion is not delivery completion.
"""

import datetime
import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from types import SimpleNamespace

from . import linear
from .config import resolve_env_setting
from .linear_oauth import TokenCache
from .workflow import WorkflowError

log = logging.getLogger("symphony.agent_sessions")
_tokens = TokenCache()
_lock = threading.RLock()
_FINAL_PHASES = {"merged", "completed", "stopped"}
_LIFECYCLE_BOUNDARY_PHASES = _FINAL_PHASES | {
    "merged_pending_sync",
    "superseded_pending_sync",
}


def _conf(settings):
    value = (getattr(settings.tracker, "provider", None) or {}).get(
        "agent_sessions")
    return value if isinstance(value, dict) else {}


def enabled(settings):
    return settings.tracker.kind == "linear" and _conf(settings).get("enabled") is True


def _scope_key(settings):
    oauth = _conf(settings).get("oauth") or {}
    identity = [
        settings.tracker.endpoint or "https://api.linear.app/graphql",
        settings.tracker.project_slug,
        oauth.get("token_url") or "https://api.linear.app/oauth/token",
        resolve_env_setting(oauth.get("client_id"), "LINEAR_OAUTH_CLIENT_ID"),
    ]
    # Client secrets and access tokens are intentionally excluded: rotating a
    # secret does not change the app actor. A new app/endpoint/project does.
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def _state_root(settings):
    root = _conf(settings).get("state_dir") or os.path.join(
        settings.workspace.root, "_agent_sessions")
    return os.path.abspath(os.path.join(root, _scope_key(settings)))


def _state_path(settings, issue_id):
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in issue_id)
    return os.path.join(_state_root(settings), "%s.json" % safe)


def _load_record(settings, issue_id):
    try:
        with open(_state_path(settings, issue_id), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_record(settings, record):
    path = _state_path(settings, record["issue_id"])
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def _external_urls(issue, extra=None):
    result = {}
    if getattr(issue, "url", None):
        result[issue.url] = {"url": issue.url, "label": "Issue"}
    for item in extra or []:
        if isinstance(item, dict) and item.get("url"):
            result[item["url"]] = {
                "url": item["url"], "label": item.get("label") or "Link"}
    return list(result.values())[:10]


def _decode_external_urls(value):
    # Linear's JSON scalar is returned as a JSON-encoded string; accept a
    # decoded list too. A malformed response must not cause duplicate creation.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise WorkflowError("invalid_agent_session_external_urls") from None
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise WorkflowError("invalid_agent_session_external_urls")
    return value


def _graphql(settings, query, variables=None):
    oauth = _conf(settings).get("oauth") or {}
    for attempt in range(2):
        token = _tokens.get(oauth)
        try:
            return linear.graphql(
                settings.tracker, query, variables,
                authorization="Bearer " + token, timeout=10)
        except WorkflowError as error:
            status = error.detail.get("status") if isinstance(error.detail, dict) else None
            if attempt == 0 and (status == 401 or error.reason == "linear_unauthenticated"):
                _tokens.invalidate(oauth)
                continue
            raise


def _result(payload, mutation):
    value = (payload.get("data") or {}).get(mutation) or {}
    if value.get("success") is not True:
        raise WorkflowError("linear_agent_mutation_failed", mutation)
    return value


def _ensure_session(settings, issue, record):
    if record.get("session_id"):
        return record["session_id"]
    # Persist a unique marker before the non-idempotent create call. If its
    # response is lost, recover the session by that marker before recreating.
    if not record.get("marker"):
        base = _conf(settings).get("status_url")
        marker = (
            "%s/%s?run=%s" % (base.rstrip("/"), issue.identifier, uuid.uuid4())
            if base else "%s#symphony-%s" % (issue.url, uuid.uuid4()))
        record["marker"] = marker
    if record.get("creation_attempted"):
        after = None
        while True:
            payload = _graphql(
                settings,
                "query SymphonyRecoverAgentSession($after: String) {"
                " agentSessions(first: 50, after: $after) { nodes {"
                " id externalUrls issue { id } }"
                " pageInfo { hasNextPage endCursor } } }",
                {"after": after})
            rows = (payload.get("data") or {}).get("agentSessions") or {}
            for row in rows.get("nodes") or []:
                if (row.get("issue") or {}).get("id") == issue.id and any(
                        item.get("url") == record["marker"]
                        for item in _decode_external_urls(row.get("externalUrls"))):
                    record["session_id"] = row["id"]
                    _save_record(settings, record)
                    return row["id"]
            page = rows.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor or cursor == after:
                raise WorkflowError("invalid_agent_session_pagination")
            after = cursor
    record["creation_attempted"] = True
    _save_record(settings, record)
    payload = _graphql(
        settings,
        "mutation SymphonyAgentSessionCreate($input: AgentSessionCreateOnIssue!) {"
        " agentSessionCreateOnIssue(input: $input) { success agentSession { id } } }",
        {"input": {
            "issueId": issue.id,
            "externalUrls": _external_urls(issue, [
                {"url": record["marker"], "label": "本机运行状态"}]),
        }})
    value = _result(payload, "agentSessionCreateOnIssue")
    session_id = (value.get("agentSession") or {}).get("id")
    if not session_id:
        raise WorkflowError("linear_agent_session_id_missing")
    record["session_id"] = session_id
    _save_record(settings, record)
    log.info("Linear agent session created identifier=%s session=%s",
             issue.identifier, session_id)
    return session_id


def ensure_session(settings, issue):
    """Synchronous primitive; production callers use Reporter."""
    if not enabled(settings):
        return None
    try:
        with _lock:
            record = _load_record(settings, issue.id)
            record.update(issue_id=issue.id, identifier=issue.identifier, url=issue.url)
            return _ensure_session(settings, issue, record)
    except Exception as error:  # noqa: BLE001 - reporting is optional
        log.warning("Agent session unavailable identifier=%s reason=%s",
                    issue.identifier, getattr(error, "reason", type(error).__name__))
        return None


def _post_activity(settings, session_id, content, event_id, signal=None):
    fields = {
        "id": event_id, "agentSessionId": session_id, "content": content}
    if signal:
        fields["signal"] = signal
    payload = _graphql(
        settings,
        "mutation SymphonyAgentActivity($input: AgentActivityCreateInput!) {"
        " agentActivityCreate(input: $input) { success agentActivity { id } } }",
        {"input": fields})
    _result(payload, "agentActivityCreate")


def _update_session(settings, issue, session_id, summary, external_urls=None):
    # Linear summary is a one-line display title, not a long result body.
    update = {"summary": " ".join(summary.replace("\0", "").split())[:255] or "Symphony"}
    if external_urls:
        update["addedExternalUrls"] = _external_urls(issue, external_urls)
    payload = _graphql(
        settings,
        "mutation SymphonyAgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {"
        " agentSessionUpdate(id: $id, input: $input) { success agentSession { id status } } }",
        {"id": session_id, "input": update})
    _result(payload, "agentSessionUpdate")


def post_activity(settings, issue, session_id, content_type, body, signal=None):
    if not enabled(settings) or not session_id:
        return
    try:
        _post_activity(
            settings, session_id, {"type": content_type, "body": body},
            str(uuid.uuid4()), signal)
    except Exception as error:  # noqa: BLE001
        log.warning("Agent activity unavailable identifier=%s reason=%s",
                    issue.identifier, getattr(error, "reason", type(error).__name__))


def finish_session(settings, issue, session_id, summary, external_urls=None):
    if not enabled(settings) or not session_id:
        return
    try:
        _update_session(settings, issue, session_id, summary, external_urls)
    except Exception as error:  # noqa: BLE001
        log.warning("Agent summary unavailable identifier=%s reason=%s",
                    issue.identifier, getattr(error, "reason", type(error).__name__))


class Reporter:
    def __init__(self, store, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.queue = queue.Queue(maxsize=256)
        self.records = {}
        self.retry_at = {}
        self._stop = threading.Event()
        self._thread = None
        self._progress_lock = threading.Lock()
        self._overflow_lock = threading.Lock()
        self._overflow = {}
        self._progress = {}
        self._pending_count = 0
        self.last_success_at = None
        self.last_error = None
        self.api_retry_at = 0
        self._active_root = None
        self._delivery_scan_at = 0

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="linear-agent-sessions", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=12)

    def snapshot(self):
        return {
            "enabled": enabled(self.store.settings()),
            "queue_length": self.queue.qsize() + len(self._overflow),
            "pending_events": self._pending_count,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
        }

    def _event(self, issue, phase, body, key=None, urls=None, content=None):
        settings = self.store.settings()
        event = {
            "id": str(uuid.uuid4()), "phase": phase, "body": body,
            "key": key or hashlib.sha256(
                json.dumps([phase, body], ensure_ascii=False).encode()).hexdigest(),
            "urls": urls or [], "content": content,
        }
        identity = {
            "id": issue.id, "identifier": issue.identifier, "url": issue.url,
            "state_root": _state_root(settings),
        }
        return identity, event

    def _enqueue(self, issue, phase, body, key=None, urls=None, content=None):
        if not enabled(self.store.settings()) or issue is None:
            return
        identity, event = self._event(issue, phase, body, key, urls, content)
        try:
            self.queue.put_nowait((identity, event))
        except queue.Full:
            if phase != "progress":
                # Coalesce repeated control events, never discard a final
                # result merely because progress filled the bounded queue.
                with self._overflow_lock:
                    self._overflow[
                        (identity["state_root"], issue.id, event["key"])
                    ] = identity, event
            else:
                log.warning("Agent progress queue full identifier=%s", issue.identifier)

    def started(self, issue, attempt=None, model=None):
        self._enqueue(
            issue, "running",
            "已领取 %s，正在使用 %s 执行（尝试 %s）。"
            % (issue.identifier, model or "Pi", attempt or 1),
            key="start:%s:%s" % (attempt, uuid.uuid4()))

    def progress(self, issue, update):
        if issue is None or update.get("event") not in (
                "tool_call_completed", "tool_call_failed"):
            return
        details = update.get("details") or {}
        tool = details.get("tool")
        if tool not in ("bash", "edit", "write"):
            return
        settings = self.store.settings()
        conf = _conf(settings)
        now = self.clock()
        interval = max(2, float(conf.get("progress_interval_seconds", 30)))
        limit = max(0, int(conf.get("max_progress_activities", 20)))
        with self._progress_lock:
            previous, count = self._progress.get(issue.id, (-float("inf"), 0))
            if count >= limit or now - previous < interval:
                return
            self._progress[issue.id] = now, count + 1
        # Never send raw tool input/output, shell arguments, or model thoughts.
        failed = update["event"] == "tool_call_failed"
        action = {"bash": "执行命令或验证", "edit": "修改文件", "write": "写入文件"}[tool]
        self._enqueue(
            issue, "progress", action, key="progress:%s" % uuid.uuid4(),
            content={"type": "action", "action": action,
                     "parameter": issue.identifier,
                     "result": "该步骤失败，执行器将继续处理。" if failed else "该步骤已完成。"})

    def worker_finished(self, issue, normal):
        self._enqueue(
            issue, "waiting" if normal else "retrying",
            "本轮执行结束，正在核对任务状态、PR 与交付门禁。"
            if normal else "本轮执行失败，调度器将按重试预算恢复；详情见 Workpad。")

    def blocked(self, issue):
        self._enqueue(
            issue, "blocked", "执行已阻塞，需要人工处理。请查看 Workpad 和宿主阻塞原因。")

    def stopped(self, issue):
        self._enqueue(
            issue, "stopped", "任务已离开执行范围，执行器已停止；这不表示代码已经合并。")

    def delivery_event(self, issue, phase, reason, pr_number=None, sha=None):
        """Pure envelope, also saved atomically with the host delivery result."""
        settings = self.store.settings()
        if not enabled(settings):
            return None
        url = (
            "https://github.com/%s/pull/%s" % (settings.delivery.repo, pr_number)
            if pr_number else None)
        body = {
            "waiting": "PR 已提交，正在等待 CI、Review 或其他交付门禁。",
            "rework": "交付检查需要返工，已交回执行器。",
            "paused": "自动交付已暂停，需要人工处理。",
            "merged": "GitHub PR 已确认合并，Linear 已更新为 Done。",
            "merged_pending_sync": "GitHub PR 已确认合并，Linear 状态仍需核对。",
        }.get(phase, "正在核对交付状态。")
        body += "\n\n原因：%s" % reason
        if url:
            body += "\nPR：%s\nHEAD：%s" % (url, sha or "unknown")
        identity, event = self._event(
            issue, phase, body,
            key="delivery:%s:%s:%s:%s" % (phase, pr_number, sha, reason),
            urls=[{"url": url, "label": "Pull request"}] if url else [])
        record = _load_record(settings, issue.id)
        return {
            "identity": identity, "event": event,
            "lifecycle": record.get("marker"),
        }

    def delivery(self, issue, phase, reason, pr_number=None, sha=None):
        if not enabled(self.store.settings()):
            return
        envelope = self.delivery_event(issue, phase, reason, pr_number, sha)
        if envelope is None:
            return
        identity, event = envelope["identity"], envelope["event"]
        try:
            self.queue.put_nowait((identity, event))
        except queue.Full:
            with self._overflow_lock:
                self._overflow[
                    (identity["state_root"], issue.id, event["key"])
                ] = identity, event

    def _recover(self, settings=None):
        settings = settings or self.store.settings()
        if not enabled(settings):
            return
        try:
            names = os.listdir(_state_root(settings))
        except OSError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            record = _load_record(settings, name[:-5])
            if record.get("issue_id") and (
                    record.get("pending") or record.get("session_id")):
                self.records[record["issue_id"]] = record

    def _loop(self):
        while not self._stop.is_set() or not self.queue.empty() or self._overflow:
            self.process_pending(wait=0.5)

    def _activate(self, settings):
        root = _state_root(settings) if enabled(settings) else None
        if root != self._active_root:
            self.records.clear()
            self.retry_at.clear()
            self.api_retry_at = 0
            self._delivery_scan_at = 0
            self._active_root = root
            with self._progress_lock:
                self._progress.clear()
            if root:
                self._recover(settings)

    def _recover_delivery(self, settings):
        if not settings.delivery.enabled or self.clock() < self._delivery_scan_at:
            return
        self._delivery_scan_at = self.clock() + 5
        from .delivery import DeliveryRecordStore

        source = DeliveryRecordStore(settings.delivery.state_dir)
        for record in list(self.records.values()):
            if not record.get("marker") or any(
                    item["phase"] == "running" for item in record.get("pending", [])):
                continue
            issue = SimpleNamespace(id=record["issue_id"])
            envelope = source.load(issue).get("agent_session_report") or {}
            identity, event = envelope.get("identity"), envelope.get("event")
            if not isinstance(identity, dict) or not isinstance(event, dict):
                continue
            if identity.get("state_root") != self._active_root \
                    or envelope.get("lifecycle") != record["marker"]:
                continue
            self._stage(settings, identity, event)

    def _delivery_lifecycle_ended(self, settings, issue_id, record):
        """Return true when a new run follows an already-ended delivery."""
        if record.get("phase") in _LIFECYCLE_BOUNDARY_PHASES or any(
                item.get("phase") in _LIFECYCLE_BOUNDARY_PHASES
                for item in record.get("pending", [])):
            return True
        if not getattr(settings, "delivery", None) \
                or not settings.delivery.enabled:
            return False
        try:
            from .delivery import DeliveryRecordStore
            delivery_record = DeliveryRecordStore(
                settings.delivery.state_dir).load(
                    SimpleNamespace(id=issue_id))
        except Exception as error:  # noqa: BLE001 - reporting stays best effort
            log.warning(
                "Agent lifecycle delivery check failed reason=%s",
                getattr(error, "reason", type(error).__name__))
            return False
        return delivery_record.get("phase") in _LIFECYCLE_BOUNDARY_PHASES

    def process_pending(self, wait=0):
        """One deterministic pump, also used by local integration tests."""
        settings = self.store.settings()
        self._activate(settings)
        # Stage a batch before doing any network work. Delivery's atomic
        # envelope also covers a crash before a terminal event reaches here.
        for index in range(512):
            try:
                identity, event = self.queue.get(timeout=wait if index == 0 else 0)
                queued = True
            except queue.Empty:
                with self._overflow_lock:
                    if not self._overflow:
                        break
                    key = next(iter(self._overflow))
                    identity, event = self._overflow.pop(key)
                queued = False
            try:
                if identity.get("state_root", self._active_root) != self._active_root:
                    continue
                if enabled(settings):
                    self._stage(settings, identity, event)
            except Exception as error:  # noqa: BLE001
                self.last_error = getattr(error, "reason", type(error).__name__)
                log.warning("Agent reporting state unavailable reason=%s", self.last_error)
            finally:
                if queued:
                    self.queue.task_done()
        if not enabled(settings):
            self._pending_count = 0
            return
        try:
            self._recover_delivery(settings)
        except Exception as error:  # noqa: BLE001 - local reporting cannot stop execution
            self.last_error = getattr(error, "reason", type(error).__name__)
            log.warning("Agent delivery recovery deferred reason=%s", self.last_error)
        for issue_id, record in list(self.records.items()):
            if not record.get("pending") or self.clock() < max(
                    self.retry_at.get(issue_id, 0), self.api_retry_at):
                continue
            try:
                self._drain(settings, record)
                self.retry_at.pop(issue_id, None)
                self.last_error = None
                self.last_success_at = datetime.datetime.now(
                    datetime.timezone.utc).isoformat()
            except Exception as error:  # noqa: BLE001 - execution must continue
                reason = getattr(error, "reason", type(error).__name__)
                self.last_error = reason
                self.retry_at[issue_id] = self.clock() + 30
                detail = getattr(error, "detail", None)
                if reason == "linear_rate_limited" or (
                        isinstance(detail, dict) and detail.get("status") == 429):
                    try:
                        delay = float(detail.get("retry_after", 60))
                    except (AttributeError, TypeError, ValueError):
                        delay = 60
                    self.api_retry_at = self.clock() + max(10, min(delay, 3600))
                log.warning("Agent reporting deferred identifier=%s reason=%s",
                            record.get("identifier"), reason)
        self._pending_count = sum(len(r.get("pending") or []) for r in self.records.values())

    def _stage(self, settings, identity, event):
        issue_id = identity["id"]
        record = self.records.get(issue_id)
        if record is None:
            record = _load_record(settings, issue_id)
        if event["phase"] == "running" \
                and self._delivery_lifecycle_ended(
                    settings, issue_id, record):
            # A running event after a merge/pending-sync belongs to a new task
            # lifecycle. Discard the old unsent controls before they can be
            # attached to the new Linear Agent Session.
            record["pending"] = []
            record["sent_keys"] = []
            record["phase"] = "running"
            for key in ("session_id", "marker", "creation_attempted"):
                record.pop(key, None)
            with self._progress_lock:
                self._progress.pop(issue_id, None)
        record.update(
            issue_id=issue_id, identifier=identity["identifier"], url=identity["url"])
        pending = record.setdefault("pending", [])
        if event["key"] in record.get("sent_keys", []) or any(
                item["key"] == event["key"] for item in pending):
            return
        final = record.get("phase") in _FINAL_PHASES or any(
            item["phase"] in _FINAL_PHASES for item in pending)
        if final and event["phase"] in (
                "progress", "waiting", "retrying", "stopped", "blocked", "paused", "rework"):
            return
        paused = record.get("phase") in ("paused", "blocked", "merged_pending_sync") or any(
            item["phase"] in ("paused", "blocked", "merged_pending_sync") for item in pending)
        if paused and event["phase"] in ("progress", "waiting", "retrying"):
            return
        # Old delivery issues without a reported execution are not retrofitted.
        if not record.get("session_id") and not pending and event["phase"] != "running":
            return
        pending.append(event)
        self.records[issue_id] = record
        _save_record(settings, record)

    def _drain(self, settings, record):
        issue = SimpleNamespace(
            id=record["issue_id"], identifier=record["identifier"], url=record["url"])
        while record.get("pending"):
            event = record["pending"][0]
            phase = event["phase"]
            if phase == "running" and record.get("phase") in _FINAL_PHASES:
                for key in ("session_id", "marker", "creation_attempted"):
                    record.pop(key, None)
                record["phase"] = "running"
                _save_record(settings, record)
                with self._progress_lock:
                    self._progress.pop(issue.id, None)
            session_id = _ensure_session(settings, issue, record)
            acknowledged = False
            if event.get("activity_attempted"):
                payload = _graphql(
                    settings,
                    "query SymphonyRecoverAgentActivity($id: ID!, $session: String!) {"
                    " agentActivities(first: 1, filter: {id: {eq: $id},"
                    " agentSessionId: {eq: $session}}) { nodes { id } } }",
                    {"id": event["id"], "session": session_id})
                acknowledged = any(
                    item.get("id") == event["id"]
                    for item in ((payload.get("data") or {}).get(
                        "agentActivities") or {}).get("nodes") or [])
            content_type = (
                "response" if phase in _FINAL_PHASES else
                "elicitation" if phase in ("blocked", "paused", "merged_pending_sync")
                else "thought")
            if not acknowledged:
                _update_session(
                    settings, issue, session_id,
                    "%s · %s" % (issue.identifier, event["body"].splitlines()[0]),
                    event.get("urls"))
                event["activity_attempted"] = True
                _save_record(settings, record)
                _post_activity(
                    settings, session_id,
                    event.get("content") or {"type": content_type, "body": event["body"]},
                    event["id"])
            record["phase"] = phase if phase != "progress" else record.get("phase", "running")
            record["sent_keys"] = (record.get("sent_keys", []) + [event["key"]])[-128:]
            record["pending"].pop(0)
            _save_record(settings, record)
