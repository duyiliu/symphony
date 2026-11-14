"""Terminal status dashboard compatible with the upstream Elixir renderer.

The renderer is deliberately pure: it formats one orchestrator snapshot into
the same box layout used by upstream Symphony. A small background thread polls
the orchestrator and writes frames to a terminal.
"""

import os
import re
import shutil
import sys
import threading
import time

from . import http_api

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
BLUE = "\x1b[34m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
GRAY = "\x1b[90m"

ID_WIDTH = 8
STAGE_WIDTH = 14
PID_WIDTH = 8
AGE_WIDTH = 12
TOKENS_WIDTH = 10
SESSION_WIDTH = 14
EVENT_DEFAULT_WIDTH = 44
EVENT_MIN_WIDTH = 12
ROW_CHROME_WIDTH = 10
DEFAULT_COLUMNS = 115
MIN_IDLE_RERENDER_MS = 1_000
THROUGHPUT_WINDOW_MS = 5_000
GRAPH_WINDOW_MS = 10 * 60 * 1000


def _color(value, code):
    return "%s%s%s" % (code, value, RESET)


def _map_value(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _format_count(value):
    if value is None:
        return "0"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return "{:,}".format(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return "{:,}".format(int(stripped))
        except ValueError:
            return value
    return str(value)


def _format_tps(value):
    try:
        return _format_count(int(value))
    except (TypeError, ValueError):
        return "0"


def _format_runtime_seconds(seconds):
    if isinstance(seconds, bool):
        return "0m 0s"
    if isinstance(seconds, int):
        return "%dm %ds" % (seconds // 60, seconds % 60)
    if isinstance(seconds, str):
        return seconds
    return "0m 0s"


def _format_runtime_and_turns(seconds, turn_count):
    if isinstance(turn_count, int) and not isinstance(turn_count, bool) \
            and turn_count > 0:
        return "%s / %d" % (_format_runtime_seconds(seconds), turn_count)
    return _format_runtime_seconds(seconds)


def _format_cell(value, width, align="left"):
    text = str(value if value is not None else "n/a")
    text = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if len(text) > width:
        text = text[:max(0, width - 3)] + "..."
    return text.rjust(width) if align == "right" else text.ljust(width)


def _compact_session_id(session_id):
    if not isinstance(session_id, str):
        return "n/a"
    if len(session_id) > 10:
        return session_id[:4] + "..." + session_id[-6:]
    return session_id


def _format_retry_error(error):
    if not isinstance(error, str):
        return ""
    sanitized = error
    for token in ("\\r\\n", "\\r", "\\n", "\r\n", "\r", "\n"):
        sanitized = sanitized.replace(token, " ")
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if sanitized == "":
        return ""
    if len(sanitized.encode("utf-8")) > 96:
        sanitized = sanitized[:96] + "..."
    return " " + _color("error=" + sanitized, DIM)


def _next_in_words(due_in_ms):
    if not isinstance(due_in_ms, int) or isinstance(due_in_ms, bool):
        return "n/a"
    seconds = due_in_ms // 1000
    millis = due_in_ms % 1000
    return "%d.%03ds" % (seconds, millis)


def _format_retry_rows(retrying):
    if not retrying:
        return ["│  " + _color("No queued retries", GRAY)]
    rows = []
    for entry in sorted(
            retrying,
            key=lambda item: item.get("due_in_ms")
            if isinstance(item.get("due_in_ms"), int) else 0):
        issue_id = entry.get("issue_id") or "unknown"
        identifier = entry.get("identifier") or issue_id
        attempt = entry.get("attempt") or 0
        due_in_ms = entry.get("due_in_ms") or 0
        rows.append(
            "│  " + _color("↻", YELLOW) + " "
            + _color(str(identifier), RED) + " "
            + _color("attempt=%s" % attempt, YELLOW)
            + _color(" in ", DIM)
            + _color(_next_in_words(due_in_ms), CYAN)
            + _format_retry_error(entry.get("error")))
    return rows


def _format_rate_limit_bucket(bucket):
    if bucket is None:
        return "n/a"
    if not isinstance(bucket, dict):
        return str(bucket)
    remaining = _map_value(bucket, ["remaining"])
    limit = _map_value(bucket, ["limit"])
    reset_value = _map_value(bucket, [
        "reset_in_seconds", "resetInSeconds", "reset_at", "resetAt",
        "resets_at", "resetsAt",
    ])
    if isinstance(remaining, int) and not isinstance(remaining, bool) \
            and isinstance(limit, int) and not isinstance(limit, bool):
        base = "%s/%s" % (_format_count(remaining), _format_count(limit))
    elif isinstance(remaining, int) and not isinstance(remaining, bool):
        base = "remaining %s" % _format_count(remaining)
    elif isinstance(limit, int) and not isinstance(limit, bool):
        base = "limit %s" % _format_count(limit)
    elif not bucket:
        base = "n/a"
    else:
        base = str(bucket)
    if reset_value is None:
        return base
    if isinstance(reset_value, int) and not isinstance(reset_value, bool):
        reset_text = "%ss" % _format_count(reset_value)
    else:
        reset_text = str(reset_value)
    return "%s reset %s" % (base, reset_text)


def _format_rate_limit_credits(credits):
    if credits is None:
        return "credits n/a"
    if not isinstance(credits, dict):
        return "credits %s" % credits
    unlimited = credits.get("unlimited") is True
    has_credits = credits.get("has_credits") is True
    balance = credits.get("balance")
    if unlimited:
        return "credits unlimited"
    if has_credits and isinstance(balance, (int, float)) \
            and not isinstance(balance, bool):
        if isinstance(balance, float):
            return "credits %.2f" % balance
        return "credits %s" % _format_count(balance)
    if has_credits:
        return "credits available"
    return "credits none"


def _format_rate_limits(rate_limits):
    if not isinstance(rate_limits, dict):
        return _color("unavailable", GRAY)
    limit_id = _map_value(rate_limits, ["limit_id", "limit_name"]) or "unknown"
    primary = _format_rate_limit_bucket(_map_value(rate_limits, ["primary"]))
    secondary = _format_rate_limit_bucket(
        _map_value(rate_limits, ["secondary"]))
    credits = _format_rate_limit_credits(
        _map_value(rate_limits, ["credits"]))
    return (
        _color(str(limit_id), YELLOW) + _color(" | ", GRAY)
        + _color("primary %s" % primary, CYAN) + _color(" | ", GRAY)
        + _color("secondary %s" % secondary, CYAN) + _color(" | ", GRAY)
        + _color(credits, GREEN))


def _status_color(event):
    if event in (None, "none"):
        return RED
    if event == "codex/event/token_count":
        return YELLOW
    if event == "codex/event/task_started":
        return GREEN
    if event == "turn_completed":
        return MAGENTA
    return BLUE


def _format_running_summary(entry, event_width):
    event = entry.get("last_codex_event")
    color = _status_color(event)
    issue = _format_cell(entry.get("identifier") or "unknown", ID_WIDTH)
    state = _format_cell(entry.get("state") or "unknown", STAGE_WIDTH)
    session = _format_cell(
        _compact_session_id(entry.get("session_id")), SESSION_WIDTH)
    pid = _format_cell(entry.get("codex_app_server_pid") or "n/a", PID_WIDTH)
    age = _format_cell(
        _format_runtime_and_turns(
            entry.get("runtime_seconds") or 0,
            entry.get("turn_count") or 0),
        AGE_WIDTH)
    tokens = _format_cell(
        _format_count(entry.get("codex_total_tokens") or 0),
        TOKENS_WIDTH, align="right")
    message = http_api._summarize_message(entry.get("last_codex_message"))
    event_label = _format_cell(message or "none", event_width)
    return "".join([
        "│ ", _color("●", color), " ",
        _color(issue, CYAN), " ",
        _color(state, color), " ",
        _color(pid, YELLOW), " ",
        _color(age, MAGENTA), " ",
        _color(tokens, YELLOW), " ",
        _color(session, CYAN), " ",
        _color(event_label, color),
    ])


def _fixed_running_width():
    return ID_WIDTH + STAGE_WIDTH + PID_WIDTH + AGE_WIDTH + TOKENS_WIDTH \
        + SESSION_WIDTH


def _terminal_columns(columns=None):
    if isinstance(columns, int) and not isinstance(columns, bool) and columns > 0:
        return columns
    env = os.environ.get("COLUMNS")
    if env:
        try:
            parsed = int(env.strip())
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    try:
        return shutil.get_terminal_size((DEFAULT_COLUMNS, 24)).columns
    except OSError:
        return DEFAULT_COLUMNS


def _running_event_width(columns=None):
    return max(
        EVENT_MIN_WIDTH,
        _terminal_columns(columns) - _fixed_running_width() - ROW_CHROME_WIDTH)


def _running_table_header(event_width):
    header = " ".join([
        _format_cell("ID", ID_WIDTH),
        _format_cell("STAGE", STAGE_WIDTH),
        _format_cell("PID", PID_WIDTH),
        _format_cell("AGE / TURN", AGE_WIDTH),
        _format_cell("TOKENS", TOKENS_WIDTH),
        _format_cell("SESSION", SESSION_WIDTH),
        _format_cell("EVENT", event_width),
    ])
    return "│   " + _color(header, GRAY)


def _running_table_separator(event_width):
    width = _fixed_running_width() + event_width + 6
    return "│   " + _color("─" * width, GRAY)


def _project_lines(settings, dashboard_url):
    tracker = getattr(settings, "tracker", None)
    kind = getattr(tracker, "kind", None)
    slug = getattr(tracker, "project_slug", None)
    if kind == "linear" and isinstance(slug, str) and slug:
        project = _color(
            "https://linear.app/project/%s/issues" % slug, CYAN)
    else:
        project = _color("n/a", GRAY)
    lines = [_color("│ Project: ", BOLD) + project]
    if dashboard_url:
        lines.append(
            _color("│ Dashboard: ", BOLD) + _color(dashboard_url, CYAN))
    return lines


def _refresh_line(polling):
    prefix = _color("│ Next refresh: ", BOLD)
    if isinstance(polling, dict) and polling.get("checking") is True:
        return prefix + _color("checking now…", CYAN)
    if isinstance(polling, dict) and isinstance(
            polling.get("next_poll_in_ms"), int):
        due = max(0, polling["next_poll_in_ms"])
        seconds = (due + 999) // 1000
        return prefix + _color("%ds" % seconds, CYAN)
    return prefix + _color("n/a", GRAY)


def render_snapshot(snapshot, settings=None, tps=0.0, columns=None,
                    dashboard_url=None):
    """Render one snapshot dict; ``None`` renders the unavailable frame."""
    if not isinstance(snapshot, dict):
        return "\n".join([
            _color("╭─ SYMPHONY STATUS", BOLD),
            _color("│ Orchestrator snapshot unavailable", RED),
            _color("│ Throughput: ", BOLD) + _color(
                "%s tps" % _format_tps(tps), CYAN),
            *_project_lines(settings, dashboard_url),
            _refresh_line(None),
            "╰─",
        ])
    running = snapshot.get("running") or []
    retrying = snapshot.get("retrying") or []
    totals = snapshot.get("codex_totals") or {}
    event_width = _running_event_width(columns)
    rows = [
        _format_running_summary(entry, event_width)
        for entry in sorted(
            running, key=lambda item: str(item.get("identifier") or ""))
    ]
    if not rows:
        rows = ["│  " + _color("No active agents", GRAY), "│"]
    spacer = ["│"] if running else []
    max_agents = getattr(
        getattr(settings, "agent", None), "max_concurrent_agents", 10)
    lines = [
        _color("╭─ SYMPHONY STATUS", BOLD),
        _color("│ Agents: ", BOLD)
        + _color(str(len(running)), GREEN) + _color("/", GRAY)
        + _color(str(max_agents), GRAY),
        _color("│ Throughput: ", BOLD) + _color(
            "%s tps" % _format_tps(tps), CYAN),
        _color("│ Runtime: ", BOLD) + _color(
            _format_runtime_seconds(totals.get("seconds_running", 0)),
            MAGENTA),
        _color("│ Tokens: ", BOLD)
        + _color("in %s" % _format_count(totals.get("input_tokens", 0)), YELLOW)
        + _color(" | ", GRAY)
        + _color("out %s" % _format_count(totals.get("output_tokens", 0)), YELLOW)
        + _color(" | ", GRAY)
        + _color("total %s" % _format_count(totals.get("total_tokens", 0)), YELLOW),
        _color("│ Rate Limits: ", BOLD)
        + _format_rate_limits(snapshot.get("rate_limits")),
        *_project_lines(settings, dashboard_url),
        _refresh_line(snapshot.get("polling")),
        _color("├─ Running", BOLD),
        "│",
        _running_table_header(event_width),
        _running_table_separator(event_width),
        *rows,
        *spacer,
        _color("├─ Backoff queue", BOLD),
        "│",
        *_format_retry_rows(retrying),
        "╰─",
    ]
    return "\n".join(lines)


def rolling_tps(samples, now_ms, current_tokens):
    points = [(now_ms, current_tokens)] + list(samples or [])
    minimum = now_ms - THROUGHPUT_WINDOW_MS
    points = [
        (timestamp, tokens) for timestamp, tokens in points
        if isinstance(timestamp, int) and timestamp >= minimum
    ]
    if len(points) < 2:
        return 0.0
    start_ms, start_tokens = min(points, key=lambda item: item[0])
    elapsed_ms = now_ms - start_ms
    if elapsed_ms <= 0:
        return 0.0
    return max(0, current_tokens - start_tokens) / (elapsed_ms / 1000.0)


def throttled_tps(last_second, last_value, now_ms, token_samples,
                  current_tokens):
    second = now_ms // 1000
    if last_second == second and isinstance(last_value, (int, float)):
        return second, last_value
    return second, rolling_tps(token_samples, now_ms, current_tokens)


def _prune_samples(samples, now_ms):
    minimum = now_ms - GRAPH_WINDOW_MS
    return [
        (timestamp, tokens) for timestamp, tokens in samples
        if timestamp >= minimum
    ]


def _dashboard_url(host, configured_port, bound_port=None):
    host = host or "127.0.0.1"
    port = bound_port or configured_port
    if not isinstance(port, int) or port <= 0:
        return None
    host = host.strip()
    if host in ("0.0.0.0", "::", "[::]", ""):
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = "[%s]" % host
    return "http://%s:%d/" % (host, port)


def dashboard_url(settings, bound_port=None):
    server = getattr(settings, "server", None)
    return _dashboard_url(
        getattr(server, "host", None),
        getattr(server, "port", None),
        bound_port)


def dashboard_url_for_test(host, configured_port, bound_port):
    return _dashboard_url(host, configured_port, bound_port)


def tps_graph(samples, now_ms, current_tokens):
    bucket_ms = GRAPH_WINDOW_MS // 24
    active_bucket_start = (now_ms // bucket_ms) * bucket_ms
    graph_window_start = active_bucket_start - 23 * bucket_ms
    points = [(now_ms, current_tokens)] + list(samples or [])
    minimum = now_ms - GRAPH_WINDOW_MS
    points = sorted(
        (item for item in points if item[0] >= minimum),
        key=lambda item: item[0])
    rates = []
    for (start_ms, start_tokens), (end_ms, end_tokens) in zip(
            points, points[1:]):
        elapsed_ms = end_ms - start_ms
        delta = max(0, end_tokens - start_tokens)
        rates.append(
            (end_ms, 0.0 if elapsed_ms <= 0 else delta / (elapsed_ms / 1000.0)))
    buckets = []
    blocks = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    for index in range(24):
        start = graph_window_start + index * bucket_ms
        end = start + bucket_ms
        last = index == 23
        values = [
            value for timestamp, value in rates
            if timestamp >= start and (timestamp <= end if last else timestamp < end)
        ]
        buckets.append(sum(values) / len(values) if values else 0.0)
    maximum = max(buckets) if buckets else 0.0
    characters = []
    for value in buckets:
        if maximum <= 0:
            index = 0
        else:
            index = round(value / maximum * (len(blocks) - 1))
        characters.append(blocks[index])
    return "".join(characters)


def format_running_summary_for_test(entry, terminal_columns=None):
    return _format_running_summary(entry, _running_event_width(terminal_columns))


def humanize_codex_message(message):
    return http_api._humanize_codex_message(message)


class StatusDashboard:
    def __init__(self, orchestrator, settings, stream=None, enabled=None,
                 dashboard_url_override=None):
        self._orchestrator = orchestrator
        self._settings = settings
        self._stream = stream or sys.stdout
        observability = getattr(settings, "observability", None)
        self._refresh_s = max(
            0.1, (getattr(observability, "refresh_ms", 1000) or 1000) / 1000)
        self._render_interval_s = max(
            0.016,
            (getattr(observability, "render_interval_ms", 16) or 16) / 1000)
        if enabled is None:
            configured = getattr(
                observability, "dashboard_enabled", True)
            interactive = bool(
                getattr(self._stream, "isatty", lambda: False)())
            forced = os.environ.get("SYMPHONY_DASHBOARD", "").lower() \
                in ("1", "true", "yes")
            enabled = bool(configured and (interactive or forced))
        self.enabled = enabled
        self._dashboard_url = (
            dashboard_url_override
            if dashboard_url_override is not None
            else dashboard_url(settings))
        self._stop = threading.Event()
        self._thread = None
        self._samples = []
        self._last_tps_second = None
        self._last_tps = 0.0
        self._last_content = None
        self._last_rendered_at = None
        self._lock = threading.Lock()

    def start(self):
        if not self.enabled:
            return self
        self._thread = threading.Thread(
            target=self._loop, name="status-dashboard", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self):
        while not self._stop.wait(self._refresh_s):
            self.render_once()

    def render_once(self):
        snapshot = self._orchestrator.snapshot(15)
        if not isinstance(snapshot, dict):
            content = render_snapshot(
                None, self._settings, 0.0, dashboard_url=self._dashboard_url)
        else:
            now_ms = int(time.monotonic() * 1000)
            totals = snapshot.get("codex_totals") or {}
            tokens = totals.get("total_tokens", 0)
            if not isinstance(tokens, int):
                tokens = 0
            self._samples = _prune_samples(
                [(now_ms, tokens)] + self._samples, now_ms)
            second = now_ms // 1000
            if second == self._last_tps_second:
                tps = self._last_tps
            else:
                tps = rolling_tps(self._samples, now_ms, tokens)
                self._last_tps_second = second
                self._last_tps = tps
            content = render_snapshot(
                snapshot, self._settings, tps,
                dashboard_url=self._dashboard_url)
        self._write(content)

    def _write(self, content):
        now = time.monotonic()
        with self._lock:
            if content == self._last_content \
                    and self._last_rendered_at is not None \
                    and (now - self._last_rendered_at) * 1000 \
                    < MIN_IDLE_RERENDER_MS:
                return
            try:
                self._stream.write("\x1b[H\x1b[2J" + content + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                return
            self._last_content = content
            self._last_rendered_at = now

    def render_offline(self):
        content = "\n".join([
            _color("╭─ SYMPHONY STATUS", BOLD),
            _color("│ app_status=offline", RED),
            "╰─",
        ])
        self._write(content)


def format_snapshot_content_for_test(snapshot_data, tps=0.0,
                                     terminal_columns=None, settings=None,
                                     dashboard_url=None):
    if isinstance(snapshot_data, tuple) and len(snapshot_data) == 2 \
            and snapshot_data[0] == "ok":
        snapshot_data = snapshot_data[1]
    return render_snapshot(
        snapshot_data, settings, tps, terminal_columns, dashboard_url)
