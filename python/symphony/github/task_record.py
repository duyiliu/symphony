"""Single-authority task records for GitHub issues.

The JSON record under ``state_dir`` IS the workflow state (Linear-style
board semantics). GitHub keeps only task identity (issue) and the delivery
artifact (PR); labels carry no authorization meaning.

States: ``backlog`` (never picked up) -> ``todo`` (granted) ->
``in_progress`` (worker running) -> ``review`` (delivery owned by host
until merge) -> ``done``. ``blocked`` is a flag, not a state.
"""

import uuid

STATES = {"backlog", "todo", "in_progress", "review", "done"}

# Review-lane host progress substages; informational only, never
# authorization-bearing.
REVIEW_SUBSTAGES = {
    "waiting_checks", "waiting_review", "waiting_update",
    "checking", "merging", "finalizing",
}

# Legacy host stages -> record state for read-time normalization.
_LEGACY_STAGE_STATE = {
    "handing_off": "review",
    "awaiting": "review",
    "waiting_checks": "review",
    "waiting_review": "review",
    "waiting_update": "review",
    "checking": "review",
    "merging": "review",
    "finalizing": "review",
    "merged": "done",
    "closed": "done",
    "superseded": "todo",
    "repair_pending": "todo",
    "requeue_pending": "todo",
}

_LEGACY_DELIVERY_KEYS = {
    "approval_after", "approval_floor_event", "human_approval",
}


def state_of(record):
    """Effective Linear-style state; a missing record is ``backlog``."""
    if not isinstance(record, dict) or not record:
        return "backlog"
    state = record.get("state")
    if state in STATES:
        return state
    host = record.get("host") or {}
    stage = host.get("stage")
    if stage in _LEGACY_STAGE_STATE:
        return _LEGACY_STAGE_STATE[stage]
    if record.get("merged") is True:
        return "done"
    delivery = record.get("delivery") or {}
    if delivery.get("published") is True:
        return "review"
    if record.get("attempts") or record.get("cycle"):
        # A stale pre-migration execution: redeploying from todo is safe
        # because upgrades happen with the scheduler stopped.
        return "todo"
    return "backlog"


def blocked(record):
    return isinstance(record, dict) and record.get("blocked") is True


def normalize(record, labels=None):
    """Fold a legacy receipt (and surviving GitHub labels) into the
    task-record model. Pure; callers persist via ``save``.

    ``labels`` lets a caller that already fetched issue labels map the
    surviving ``needs-human`` grant to ``blocked`` and a still-valid
    ``merge-approved`` grant to ``review.approved_sha``.
    """
    if not isinstance(record, dict):
        record = {}
    record = dict(record)
    record["state"] = state_of(record)
    if record["state"] == "done":
        record.setdefault("blocked", False)
    if labels is not None:
        lowered = {str(label).lower() for label in labels}
        if "needs-human" in lowered:
            record["blocked"] = True
        delivery = record.get("delivery") or {}
        if ("merge-approved" in lowered and record["state"] == "review"
                and isinstance(delivery.get("sha"), str)):
            review = dict(record.get("review") or {})
            review.setdefault("approved_sha", delivery["sha"])
            review.setdefault("approved_by", "legacy-label")
            record["review"] = review
    delivery = record.get("delivery")
    if isinstance(delivery, dict):
        record["delivery"] = {
            key: value for key, value in delivery.items()
            if key not in _LEGACY_DELIVERY_KEYS}
    record.pop("human_approval", None)
    host = record.get("host")
    if isinstance(host, dict):
        host = {key: value for key, value in host.items()
                if key != "delete_grant"}
        record["host"] = host
    return record


def new_cycle_id():
    return uuid.uuid4().hex


def transition(record, to_state, by, reason=""):
    """Pure state transition; appends a history entry and manages cycle.

    Entering ``todo`` always starts a new execution cycle so any earlier
    delivery/approval for an older cycle can never authorize this one.
    """
    ensure_state(to_state)
    record = dict(record or {})
    from_state = state_of(record)
    record["state"] = to_state
    if to_state == "todo":
        record["cycle"] = new_cycle_id()
        record["attempts"] = 0
        record["pending_merge"] = False
        if record.get("delivery"):
            record["previous_delivery"] = record["delivery"]
        record["delivery"] = {}
        review = dict(record.get("review") or {})
        review["approved_sha"] = None
        review["approved_by"] = None
        review["approved_at"] = None
        record["review"] = review
    if to_state != "review":
        host = dict(record.get("host") or {})
        if host.get("stage") in REVIEW_SUBSTAGES or host.get("stage") == "awaiting":
            host["stage"] = None
        record["host"] = host
    history = list(record.get("history") or [])
    history.append({
        "at": _now(), "from": from_state, "to": to_state,
        "by": by, "reason": reason[:200] if reason else "",
    })
    record["history"] = history[-50:]
    return record


def ensure_state(state):
    if state not in STATES:
        raise ValueError("unknown task state %r" % (state,))


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
