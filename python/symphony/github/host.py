"""GitHub-only delivery checker. Reads durable task records; never starts AI."""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from . import task_record, workflow, workflow_policy as policy

log = logging.getLogger("symphony.github.host")


class HostController:
    def __init__(self, store, active_ids):
        self.store = store
        self.active_ids = active_ids
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="github-deliveries", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def refresh(self):
        self.wake_event.set()

    def records(self, settings):
        directory = policy.config(settings).get("state_dir")
        if not directory or not os.path.isdir(directory):
            return []
        rows = []
        for name in os.listdir(directory):
            if not name.endswith(".json") or not name[:-5].isdigit():
                continue
            number = int(name[:-5])
            try:
                record = workflow._read_record(os.path.join(directory, name))
                if record.get("repo") != policy.repo(settings):
                    raise ValueError("receipt_repository_mismatch")
                rows.append((number, record))
            except Exception:
                log.exception("Invalid delivery receipt issue=%s", number)
                rows.append((number, {"host": {"stage": "storage_error", "reason": "交付记录损坏或仓库不匹配，需人工核对", "last_error": "receipt unavailable"}}))
        return rows

    def snapshot(self):
        settings = self.store.settings().tracker
        if not policy.enabled(settings):
            return {}
        result = {}
        for number, record in self.records(settings):
            delivery = record.get("delivery") or {}
            host = record.get("host") or {}
            stage = "merged" if record.get("merged") else "paused" if record.get("blocked") else host.get("stage")
            if not stage and delivery.get("published"):
                stage = "awaiting"
            state = task_record.state_of(record)
            review = record.get("review") or {}
            result["GH-%s" % number] = {
                "stage": stage, "state": state,
                "blocked": task_record.blocked(record),
                "reason": host.get("reason") or str(record.get("error") or ""),
                "pr": delivery.get("pr"), "sha": delivery.get("sha"),
                "observed_at": host.get("observed_at"), "error": host.get("last_error"),
                "approved": bool(review.get("approved_sha") and review["approved_sha"] == delivery.get("sha")),
                "can_approve": state == "review" and delivery.get("published") is True
                    and not record.get("blocked") and not record.get("pending_merge"),
            }
        return result

    def check(self, settings, number, active):
        try:
            workflow.reconcile_delivery(settings, number, active=active)
        except Exception as error:
            log.warning("Delivery check deferred issue=%s error=%r", number, error)
            try:
                workflow.locked(settings, number, lambda record, save: save({
                    **record, "host": {**record.get("host", {}), "last_error": str(error)}}))
            except Exception:
                log.exception("Cannot record delivery check error issue=%s", number)

    def poll_once(self):
        settings = self.store.settings().tracker
        if not policy.enabled(settings):
            return
        active = {str(value) for value in self.active_ids()}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.check, settings, number, str(number) in active)
                       for number, record in self.records(settings)
                       if task_record.state_of(record) == "review"
                       and not record.get("blocked") and not record.get("merged")]
            for future in futures:
                future.result()

    def _loop(self):
        while not self.stop_event.is_set():
            self.wake_event.clear()
            try:
                self.poll_once()
            except Exception:
                log.exception("Delivery checker failed; retrying")
            self.wake_event.wait(30)
