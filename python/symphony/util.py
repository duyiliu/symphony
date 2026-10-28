"""Shared helpers: time, ISO-8601, shell quoting, state normalisation."""

import re
import time
from datetime import datetime, timezone


def monotonic_ms():
    return int(time.monotonic() * 1000)


def utc_now():
    return datetime.now(timezone.utc)


def iso8601(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        # DateTime.to_iso8601 prints the full microsecond precision.
        text += ".%06d" % dt.microsecond
    return text + "Z"


def iso8601_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (
        dt.microsecond // 1000
    )


def parse_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # DateTime.from_iso8601 requires an explicit offset; naive input is not
    # accepted upstream, so a naive result means malformed input.
    if parsed.tzinfo is None:
        return None
    return parsed


def normalize_issue_state(state_name):
    if not isinstance(state_name, str):
        return ""
    return state_name.strip().lower()


def present_string(value):
    return isinstance(value, str) and value.strip() != ""


def normalize_label(label):
    return label.strip().lower() if isinstance(label, str) else ""


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def sanitize_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))
