"""``task_api`` agent tool: read/update the authoritative task record.

Every write requires ``expected_revision`` so a stale agent turn cannot
overwrite a human board action. Errors return structured codes instead of
raising so the agent can re-read and re-decide (official model: the agent
owns the state machine).
"""

from . import store as store_mod

TASK_API_SPEC = {
    "name": "task_api",
    "description": (
        "Read and update this task's local board record (the workflow "
        "authority). Use `get` before `update`; writes require the exact "
        "`expected_revision` from the last read. On `revision_conflict` "
        "re-read the task and re-decide — do not retry blindly."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "update", "list"],
            },
            "issue": {
                "type": "string",
                "description": "Issue number or identifier, e.g. '123' or "
                               "'GH-123' (required for get/update).",
            },
            "expected_revision": {
                "type": "integer",
                "description": "Revision from the last task read "
                               "(required for update).",
            },
            "state": {
                "type": "string",
                "description": "New workflow state (Backlog, Todo, "
                               "In Progress, Human Review, Merging, "
                               "Rework, Done, Closed, Cancelled, "
                               "Canceled, Duplicate).",
            },
            "branch_name": {"type": "string"},
            "pr_number": {"type": "integer"},
            "pr_url": {"type": "string"},
            "pr_head_sha": {"type": "string"},
            "workpad_comment_id": {"type": "integer"},
            "states": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional state filter for `list`.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _issue_number(value):
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        text = str(value).upper()
        if not text.startswith("GH-"):
            return None
        try:
            number = int(text[3:])
        except ValueError:
            return None
    return number if number > 0 else None


def _err(code, detail=None):
    return {"error": {"code": code, "detail": detail}}


def execute(tracker_settings, arguments, by="agent"):
    """Execute a ``task_api`` call. Returns a dict result."""
    if not isinstance(arguments, dict):
        return _err("invalid_arguments")
    action = arguments.get("action")
    try:
        task_store = store_mod.Store(tracker_settings)
    except store_mod.TaskError as error:
        return _err(error.code, error.detail)

    if action == "list":
        records, errors = task_store.list()
        wanted = arguments.get("states")
        if wanted is not None:
            names = {store_mod.canonical_state(s) for s in wanted
                     if store_mod.canonical_state(s)}
            records = [r for r in records if r.get("state") in names]
        return {"tasks": [store_mod.public_record(r) for r in records],
                "errors": errors}

    number = _issue_number(arguments.get("issue"))
    if number is None:
        return _err("invalid_issue")

    if action == "get":
        try:
            record = task_store.get(number)
        except store_mod.TaskError as error:
            return _err(error.code, error.detail)
        if record is None:
            return _err("task_not_found")
        return {"task": store_mod.public_record(record)}

    if action != "update":
        return _err("invalid_action", action)

    fields = {
        key: arguments[key]
        for key in ("state", "branch_name", "pr_number", "pr_url",
                    "pr_head_sha", "workpad_comment_id")
        if key in arguments}
    if not fields:
        return _err("invalid_fields", "nothing to update")
    try:
        record = task_store.update(
            number, fields, arguments.get("expected_revision"), by=by)
    except store_mod.TaskError as error:
        return _err(error.code, error.detail)
    return {"task": store_mod.public_record(record)}
