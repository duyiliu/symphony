"""One-shot v1 → v2 record migration (offline, explicit).

Stop the service, back up the whole task directory, then run:

    python -m symphony.tasks.migrate --state-dir DIR --repo owner/repo --apply

Only ``<number>.json`` files are processed (``*.bak``/``*.tmp`` ignored).
Ambiguous or corrupt records are reported, never silently overwritten or
auto-executed. Legacy fields stay readable in the backup/report but are not
carried into the new schema as strategy inputs.
"""

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import sys

from . import store as store_mod

log = logging.getLogger("symphony.tasks.migrate")

_RECORD_NAME = re.compile(r"^(\d+)\.json$")

_V1_STATE_MAP = {
    "backlog": "Backlog",
    "todo": "Todo",
    "in_progress": "In Progress",
    "review": "Human Review",
    "done": "Done",
}

# Legacy ``host.stage`` → v1 state view (mirrors v1 ``state_of``), except
# ``closed`` which maps to Closed — a closed-but-unmerged PR must not be
# reported Done.
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
    "closed": "closed",
    "superseded": "todo",
    "repair_pending": "todo",
    "requeue_pending": "todo",
}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _backup(state_dir):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = state_dir.rstrip("/\\") + ".v1-backup-" + stamp
    shutil.copytree(state_dir, target)
    return target


def _v1_state_view(record):
    """Effective v1 state replicating ``task_record.state_of`` precedence,
    with host.stage read from ``record['host']['stage']``. A present but
    unrecognised explicit ``state`` is ambiguous — reported, not guessed."""
    state = record.get("state")
    if state in _V1_STATE_MAP:
        return state
    if state is not None:
        return ("unknown_state", state)
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
        return "todo"
    return "backlog"


def _v1_target(record):
    """Map a v1 record to the new state + report flags."""
    flags = []
    view = _v1_state_view(record)
    if isinstance(view, tuple):
        return None, ["%s:%r" % view]
    target = _V1_STATE_MAP.get(view)
    if target is None:
        if view == "closed":
            target = "Closed"
        else:
            return None, ["unknown_state:%r" % view]
    if record.get("blocked") is True:
        target = "Human Review"
        flags.append("was_blocked")
    pending = (record.get("merge") or {}).get("pending_merge") \
        or record.get("pending_merge")
    if pending and record.get("merged") is not True:
        # A merge PUT may already be in flight upstream: never auto-execute,
        # surface for a human to verify the real PR state.
        target = "Human Review"
        flags.append("pending_merge_unverified")
    if target == "Human Review" and (record.get("review") or {}).get(
            "approved_sha"):
        flags.append("had_approved_sha")  # old approval ≠ new merge path
    return target, flags


def _new_record(old, number, target_state):
    delivery = old.get("delivery") or {}
    pr_number = old.get("pr_number") or delivery.get("pr")
    pr_url = old.get("pr_url") or delivery.get("pr_url")
    if pr_url is None and pr_number and old.get("repo"):
        pr_url = "https://github.com/%s/pull/%s" % (old["repo"], pr_number)
    pr_head_sha = old.get("pr_head_sha") or delivery.get("sha")
    history = list(old.get("history") or [])
    history.append({"at": _now(), "from": old.get("state"),
                    "to": target_state, "by": "migrate-v1"})
    record = {
        "schema_version": store_mod.SCHEMA_VERSION,
        "repo": old.get("repo"),
        "issue_number": number,
        "identifier": "GH-%d" % number,
        "state": target_state,
        "revision": 1,
        "branch_name": old.get("branch_name") or delivery.get("branch"),
        "pr_number": pr_number,
        "pr_url": pr_url,
        "pr_head_sha": pr_head_sha,
        "workpad_comment_id": old.get("workpad_comment_id"),
        "title": old.get("title"),
        "url": old.get("url"),
        "native_state": old.get("native_state"),
        "labels": old.get("labels") or [],
        "created_at": old.get("created_at") or _now(),
        "updated_at": _now(),
        "history": history[-store_mod.HISTORY_LIMIT:],
    }
    if old.get("blocked") is True:
        record["blocked_reason"] = old.get("blocked_reason") \
            or (old.get("host") or {}).get("reason") \
            or old.get("error") or "v1 blocked"
    return record


def migrate(state_dir, repo, apply=False):
    """Migrate ``state_dir`` to schema v2. Returns the per-record report."""
    report = []
    if not os.path.isdir(state_dir):
        raise SystemExit("state dir missing: %s" % state_dir)
    names = sorted(os.listdir(state_dir))
    parsed = []
    for name in names:
        match = _RECORD_NAME.match(name)
        if not match:
            continue  # *.bak / *.tmp / non-numeric ignored on purpose
        number = int(match.group(1))
        path = os.path.join(state_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
            if not isinstance(record, dict):
                raise ValueError("record is not an object")
        except Exception as error:  # noqa: BLE001
            report.append({"issue_number": number, "action": "skipped",
                           "reason": "unreadable:%s" % error})
            continue
        if record.get("schema_version") == store_mod.SCHEMA_VERSION \
                and record.get("repo") == repo:
            report.append({"issue_number": number, "action": "kept",
                           "reason": "already_v2"})
            continue
        parsed.append((number, path, record))

    if apply:
        # Back up the ORIGINAL directory before any record is rewritten.
        backup = _backup(state_dir)
        report.append({"action": "backup", "path": backup})
    for number, path, record in parsed:
        if record.get("repo") and record.get("repo") != repo:
            report.append({"issue_number": number, "action": "skipped",
                           "reason": "repo_mismatch:%s" % record.get("repo")})
            continue
        target, flags = _v1_target(record)
        if target is None:
            report.append({"issue_number": number, "action": "skipped",
                           "reason": ";".join(flags)})
            continue
        new = _new_record({**record, "repo": repo}, number, target)
        action = "migrated:%s" % target
        if flags:
            action += " (%s)" % ",".join(flags)
        report.append({"issue_number": number, "action": action,
                       "verify": "pending_merge_unverified" in flags})
        if apply:
            store_mod._write_file(path, new)  # same atomic path as runtime
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="symphony.tasks.migrate",
        description="One-shot v1 → v2 task record migration (offline).")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="write records + back up the directory "
                             "(default: dry-run report only)")
    args = parser.parse_args(argv)
    report = migrate(args.state_dir, args.repo, apply=args.apply)
    for row in report:
        detail = row.get("path") or row.get("reason") or ""
        verify = " [MANUAL VERIFY]" if row.get("verify") else ""
        print("#%s %s %s%s" % (
            row.get("issue_number", "-"), row["action"], detail, verify))
    return 0


if __name__ == "__main__":
    sys.exit(main())
