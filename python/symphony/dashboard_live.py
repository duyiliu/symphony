"""Live observability dashboard for Symphony (SSE-based).

The Python port doesn't ship Phoenix LiveView; this module provides an
equivalent experience over standard HTTP using Server-Sent Events:

- ``/`` serves a static HTML dashboard that opens an ``EventSource`` to
  ``/api/v1/stream`` and renders the scheduler snapshot in real time.
- ``/api/v1/stream`` keeps a long-lived connection per client and pushes
  a ``snapshot`` event whenever the orchestrator state changes (or at
  least once every ``TICK_SECONDS`` so clients can detect stalls).

Design notes
------------

* **No WebSocket dependency.** The embedded ``http.server`` handler
  keeps the connection open and flushes SSE frames.  Any HTTP/1.1
  client can consume it.
* **Push on change, tick on idle.**  The orchestrator snapshot is
  polled by the server on a short interval; when the payload hash
  differs we push a new ``snapshot`` event.  If nothing changed for
  ``TICK_SECONDS`` we still emit a ``tick`` event so clients can show
  "last update" freshness.
* **Actions are plain POSTs.**  ``refresh``, ``pause``/``resume``,
  ``release``/``cleanup`` all remain regular POST endpoints — the
  dashboard issues a ``fetch`` and the next SSE frame carries the
  updated state, no special plumbing required.
* **Layout parity.**  The markup, class names, palette and table
  structure mirror ``SymphonyElixirWeb.DashboardLive`` and
  ``priv/static/dashboard.css`` from the Elixir app, so the Python and
  Elixir dashboards stay visually and functionally interchangeable.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("symphony.dashboard_live")

# How often the server re-reads the orchestrator snapshot for SSE fans.
POLL_SECONDS = 1.0

# How often to send a keep-alive ``tick`` frame when nothing changed.
TICK_SECONDS = 15.0


def _snapshot_orchestrator(orchestrator):
    """Return a JSON-serialisable snapshot dict or None."""
    from .http_api import state_payload
    try:
        payload = state_payload(orchestrator)
    except Exception:  # noqa: BLE001 - dashboard must not kill the stream
        log.exception("dashboard_live: failed to build state payload")
        return None
    return payload


def _sse_frame(event, data):
    """Serialise one SSE frame."""
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, default=str))


def stream_dashboard(orchestrator, request, close_event):
    """Generator of SSE frames for one client.

    ``request`` is the active HTTP request object (used only for client
    logging), ``close_event`` is a ``threading.Event`` the server sets
    when the socket should close (e.g. on shutdown).  Yields byte strings.
    """
    client = "%s:%s" % (request.client_address[0], request.client_address[1])
    log.debug("dashboard_live: stream open client=%s", client)
    last_payload_hash = None
    last_send = 0.0
    try:
        # Send a ``ready`` frame immediately so the client knows the
        # channel is alive even before the first snapshot arrives.
        yield _sse_frame("ready", {"ts": time.time()}).encode("utf-8")

        while not close_event.is_set():
            snapshot = _snapshot_orchestrator(orchestrator)
            if snapshot is None:
                # Don't spam the client on transient failures; wait a bit.
                close_event.wait(POLL_SECONDS * 4)
                continue

            payload_hash = hash(json.dumps(snapshot, sort_keys=True, default=str))
            now = time.time()
            if payload_hash != last_payload_hash:
                yield _sse_frame("snapshot", snapshot).encode("utf-8")
                last_payload_hash = payload_hash
                last_send = now
            elif now - last_send >= TICK_SECONDS:
                yield _sse_frame("tick", {"ts": now}).encode("utf-8")
                last_send = now

            close_event.wait(POLL_SECONDS)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        log.debug("dashboard_live: client %s disconnected", client)
    except Exception:  # noqa: BLE001
        log.exception("dashboard_live: stream error client=%s", client)
    finally:
        log.debug("dashboard_live: stream closed client=%s", client)


# ---------------------------------------------------------------------------
# Static dashboard HTML/JS (single file, no build step).
#
# This mirrors the Elixir DashboardLive template plus priv/static/dashboard.css.
# Two additions on top of the upstream layout: a clickable issue-detail card
# and an Agent live-log tail (both fed by extra /api/v1 endpoints).
# ---------------------------------------------------------------------------

DASHBOARD_CSS = r"""
:root {
  color-scheme: light;
  --page: #f7f7f8;
  --page-soft: #fbfbfc;
  --page-deep: #ececf1;
  --card: rgba(255, 255, 255, 0.94);
  --card-muted: #f3f4f6;
  --ink: #202123;
  --muted: #6e6e80;
  --line: #ececf1;
  --line-strong: #d9d9e3;
  --accent: #10a37f;
  --accent-ink: #0f513f;
  --accent-soft: #e8faf4;
  --danger: #b42318;
  --danger-soft: #fef3f2;
  --shadow-sm: 0 1px 2px rgba(16, 24, 40, 0.05);
  --shadow-lg: 0 20px 50px rgba(15, 23, 42, 0.08);
}
* { box-sizing: border-box; }
html { background: var(--page); }
body {
  margin: 0;
  min-height: 100vh;
  background:
    radial-gradient(circle at top, rgba(16, 163, 127, 0.12) 0%, rgba(16, 163, 127, 0) 30%),
    linear-gradient(180deg, var(--page-soft) 0%, var(--page) 24%, #f3f4f6 100%);
  color: var(--ink);
  font-family: "Sohne", "SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif;
  line-height: 1.5;
}
a { color: var(--ink); text-decoration: none; transition: color 140ms ease; }
a:hover { color: var(--accent); }
button {
  appearance: none;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: white;
  border-radius: 999px;
  padding: 0.72rem 1.08rem;
  cursor: pointer;
  font: inherit;
  font-weight: 600;
  letter-spacing: -0.01em;
  box-shadow: 0 8px 20px rgba(16, 163, 127, 0.18);
  transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease, border-color 140ms ease;
}
button:hover { transform: translateY(-1px); box-shadow: 0 12px 24px rgba(16, 163, 127, 0.22); }
button.secondary {
  background: var(--card);
  color: var(--ink);
  border-color: var(--line-strong);
  box-shadow: var(--shadow-sm);
}
button.secondary:hover { box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08); }
.subtle-button {
  appearance: none;
  border: 1px solid var(--line-strong);
  background: rgba(255, 255, 255, 0.72);
  color: var(--muted);
  border-radius: 999px;
  padding: 0.34rem 0.72rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.82rem;
  font-weight: 600;
  letter-spacing: 0.01em;
  box-shadow: none;
  transition: background 140ms ease, border-color 140ms ease, color 140ms ease;
}
.subtle-button:hover {
  transform: none;
  box-shadow: none;
  background: white;
  border-color: var(--muted);
  color: var(--ink);
}
pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
code, pre, .mono {
  font-family: "Sohne Mono", "SFMono-Regular", "SF Mono", Consolas, "Liberation Mono", monospace;
}
.mono, .numeric { font-variant-numeric: tabular-nums slashed-zero; font-feature-settings: "tnum" 1, "zero" 1; }
.app-shell { max-width: 1280px; margin: 0 auto; padding: 2rem 1rem 3.5rem; }
.dashboard-shell { display: grid; gap: 1rem; }
#content { display: grid; gap: 1rem; }
.hero-card, .section-card, .metric-card, .error-card {
  background: var(--card);
  border: 1px solid rgba(217, 217, 227, 0.82);
  box-shadow: var(--shadow-sm);
  backdrop-filter: blur(18px);
}
.hero-card { border-radius: 28px; padding: clamp(1.25rem, 3vw, 2rem); box-shadow: var(--shadow-lg); }
.hero-grid { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 1.25rem; align-items: start; }
.eyebrow {
  margin: 0; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.08em; font-size: 0.76rem; font-weight: 600;
}
.hero-title { margin: 0.35rem 0 0; font-size: clamp(2rem, 4vw, 3.3rem); line-height: 0.98; letter-spacing: -0.04em; }
.hero-copy { margin: 0.75rem 0 0; max-width: 46rem; color: var(--muted); font-size: 1rem; }
.status-stack { display: grid; justify-items: end; align-content: start; min-width: min(100%, 9rem); }
.status-badge {
  display: inline-flex; align-items: center; gap: 0.45rem; min-height: 2rem;
  padding: 0.35rem 0.78rem; border-radius: 999px;
  border: 1px solid var(--line); background: var(--card-muted);
  color: var(--muted); font-size: 0.82rem; font-weight: 700; letter-spacing: 0.01em;
}
.status-badge-dot { width: 0.52rem; height: 0.52rem; border-radius: 999px; background: currentColor; opacity: 0.9; }
.status-badge-live {
  background: var(--accent-soft); border-color: rgba(16, 163, 127, 0.18); color: var(--accent-ink);
}
.status-badge-offline { background: #f5f5f7; border-color: var(--line-strong); color: var(--muted); }
.hero-actions { margin-top: 0.75rem; display: flex; gap: 0.4rem; justify-content: flex-end; flex-wrap: wrap; }
.metric-grid { display: grid; gap: 0.85rem; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.metric-card { border-radius: 22px; padding: 1rem 1.05rem 1.1rem; }
.metric-label { margin: 0; color: var(--muted); font-size: 0.82rem; font-weight: 600; letter-spacing: 0.01em; }
.metric-value { margin: 0.35rem 0 0; font-size: clamp(1.6rem, 2vw, 2.1rem); line-height: 1.05; letter-spacing: -0.03em; }
.metric-detail { margin: 0.45rem 0 0; color: var(--muted); font-size: 0.88rem; }
.section-card { border-radius: 24px; padding: 1.15rem; }
.section-header {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 1rem; flex-wrap: wrap;
}
.section-title { margin: 0; font-size: 1.08rem; line-height: 1.2; letter-spacing: -0.02em; }
.section-copy { margin: 0.35rem 0 0; color: var(--muted); font-size: 0.94rem; }
.table-wrap { overflow-x: auto; margin-top: 1rem; }
.data-table { width: 100%; min-width: 720px; border-collapse: collapse; }
.data-table-running { table-layout: fixed; min-width: 980px; }
.data-table th {
  padding: 0 0.5rem 0.75rem 0; text-align: left; color: var(--muted);
  font-size: 0.78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
}
.data-table td {
  padding: 0.9rem 0.5rem 0.9rem 0; border-top: 1px solid var(--line);
  vertical-align: top; font-size: 0.94rem;
}
.issue-stack, .session-stack, .detail-stack, .token-stack { display: grid; gap: 0.24rem; min-width: 0; }
.event-text {
  font-weight: 500; line-height: 1.45; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.event-meta { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.state-badge {
  display: inline-flex; align-items: center; min-height: 1.85rem;
  padding: 0.3rem 0.68rem; border-radius: 999px;
  border: 1px solid var(--line); background: var(--card-muted);
  color: var(--ink); font-size: 0.8rem; font-weight: 600; line-height: 1;
}
.state-badge-active { background: var(--accent-soft); border-color: rgba(16, 163, 127, 0.18); color: var(--accent-ink); }
.state-badge-warning { background: #fff7e8; border-color: #f1d8a6; color: #8a5a00; }
.state-badge-danger { background: var(--danger-soft); border-color: #f6d3cf; color: var(--danger); }
.issue-id { font-weight: 600; letter-spacing: -0.01em; }
.issue-id-link {
  color: inherit; text-decoration: underline; text-decoration-color: currentColor;
  text-decoration-thickness: 1px; text-underline-offset: 0.18em;
}
.issue-link { color: var(--muted); font-size: 0.86rem; }
.muted { color: var(--muted); }
.code-panel {
  margin-top: 1rem; padding: 1rem; border-radius: 18px;
  background: #f5f5f7; border: 1px solid var(--line);
  color: #353740; font-size: 0.9rem;
}
.empty-state { margin: 1rem 0 0; color: var(--muted); }
.error-card {
  border-radius: 24px; padding: 1.25rem;
  background: linear-gradient(180deg, #fff8f7 0%, var(--danger-soft) 100%);
  border-color: #f6d3cf;
}
.error-title { margin: 0; color: var(--danger); font-size: 1.15rem; letter-spacing: -0.02em; }
.error-copy { margin: 0.45rem 0 0; color: var(--danger); }
.alert-row { margin-top: 0.6rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.kv {
  display: grid; grid-template-columns: 140px 1fr; gap: 0.35rem 1rem;
  margin-top: 1rem; font-size: 0.9rem;
}
.kv dt { color: var(--muted); }
.kv dd { margin: 0; font-family: "Sohne Mono", "SFMono-Regular", "SF Mono", Consolas, monospace; word-break: break-all; }
tr.clickable { cursor: pointer; }
tr.clickable:hover td { background: rgba(16, 163, 127, 0.04); }
select {
  appearance: none; border: 1px solid var(--line-strong); background: white;
  color: var(--ink); border-radius: 999px; padding: 0.34rem 0.72rem;
  font: inherit; font-size: 0.82rem; font-weight: 600;
}
.log-controls { margin-top: 1rem; display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
.log-body {
  margin-top: 0.75rem; padding: 1rem; border-radius: 18px;
  background: #f5f5f7; border: 1px solid var(--line);
  font-family: "Sohne Mono", "SFMono-Regular", "SF Mono", Consolas, monospace;
  font-size: 0.8rem; max-height: 360px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word;
}
.log-line { padding: 2px 0; border-bottom: 1px solid var(--line); }
.log-type { color: var(--muted); }
.log-thinking { color: #8a5a00; }
.log-tool { color: var(--accent-ink); }
.log-error { color: var(--danger); }
@media (max-width: 860px) {
  .app-shell { padding: 1rem 0.85rem 2rem; }
  .hero-grid { grid-template-columns: 1fr; }
  .status-stack { justify-items: start; }
  .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .metric-grid { grid-template-columns: 1fr; }
  .section-card, .hero-card, .error-card { border-radius: 20px; padding: 1rem; }
}
"""


def _load_favicon():
    path = os.path.join(os.path.dirname(__file__), "static", "favicon.png")
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


DASHBOARD_FAVICON = _load_favicon()


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Symphony Observability</title>
<link rel="icon" type="image/png" sizes="128x128" href="/favicon.png">
<link rel="stylesheet" href="/dashboard.css">

</head>
<body>
<main class="app-shell">
<section class="dashboard-shell">

  <header class="hero-card">
    <div class="hero-grid">
      <div>
        <p class="eyebrow">Symphony Observability</p>
        <h1 class="hero-title">Operations Dashboard</h1>
        <p class="hero-copy">Current state, retry pressure, token usage, and orchestration health for the active Symphony runtime.</p>
      </div>
      <div class="status-stack">
        <span class="status-badge status-badge-live" id="badge-live" style="display:none;">
          <span class="status-badge-dot"></span>Live
        </span>
        <span class="status-badge status-badge-offline" id="badge-offline">
          <span class="status-badge-dot"></span>Offline
        </span>
        <div class="hero-actions">
          <button type="button" class="subtle-button" id="btn-refresh">Refresh</button>
          <button type="button" class="subtle-button" id="btn-pause">Pause</button>
          <button type="button" class="subtle-button" id="btn-resume" style="display:none;">Resume</button>
        </div>
      </div>
    </div>
  </header>

  <section class="error-card" id="error-card" style="display:none;">
    <h2 class="error-title">Snapshot unavailable</h2>
    <p class="error-copy" id="error-copy"></p>
  </section>

  <div id="content">

    <section class="metric-grid">
      <article class="metric-card">
        <p class="metric-label">Running</p>
        <p class="metric-value numeric" id="metric-running">0</p>
        <p class="metric-detail">Active issue sessions in the current runtime.</p>
      </article>
      <article class="metric-card">
        <p class="metric-label">Retrying</p>
        <p class="metric-value numeric" id="metric-retrying">0</p>
        <p class="metric-detail">Issues waiting for the next retry window.</p>
      </article>
      <article class="metric-card">
        <p class="metric-label">Blocked</p>
        <p class="metric-value numeric" id="metric-blocked">0</p>
        <p class="metric-detail">Issues paused for operator input or approval.</p>
      </article>
      <article class="metric-card">
        <p class="metric-label">Total tokens</p>
        <p class="metric-value numeric" id="metric-tokens">0</p>
        <p class="metric-detail numeric" id="metric-tokens-detail">In 0 / Out 0</p>
      </article>
      <article class="metric-card">
        <p class="metric-label">Runtime</p>
        <p class="metric-value numeric" id="metric-runtime">0m 0s</p>
        <p class="metric-detail">Total Codex runtime across completed and active sessions.</p>
      </article>
    </section>

    <section class="section-card" id="alerts-card" style="display:none;">
      <div class="section-header">
        <div>
          <h2 class="section-title">Alerts</h2>
          <p class="section-copy">Health signals raised by the scheduler.</p>
        </div>
      </div>
      <div id="alerts-list"></div>
    </section>

    <section class="section-card">
      <div class="section-header">
        <div>
          <h2 class="section-title">Rate limits</h2>
          <p class="section-copy">Latest upstream rate-limit snapshot, when available.</p>
        </div>
      </div>
      <pre class="code-panel" id="rate-limits">n/a</pre>
    </section>

    <section class="section-card">
      <div class="section-header">
        <div>
          <h2 class="section-title">Running sessions</h2>
          <p class="section-copy">Active issues, last known agent activity, and token usage.</p>
        </div>
      </div>
      <p class="empty-state" id="running-empty">No active sessions.</p>
      <div class="table-wrap" id="running-wrap" style="display:none;">
        <table class="data-table data-table-running">
          <colgroup>
            <col style="width: 12rem;">
            <col style="width: 8rem;">
            <col style="width: 7.5rem;">
            <col style="width: 8.5rem;">
            <col>
            <col style="width: 10rem;">
          </colgroup>
          <thead>
            <tr>
              <th>Issue</th>
              <th>State</th>
              <th>Session</th>
              <th>Runtime / turns</th>
              <th>Codex update</th>
              <th>Tokens</th>
            </tr>
          </thead>
          <tbody id="running-body"></tbody>
        </table>
      </div>
    </section>

    <section class="section-card" id="detail-card" style="display:none;">
      <div class="section-header">
        <div>
          <h2 class="section-title" id="detail-title">Issue detail</h2>
          <p class="section-copy">Snapshot from <span class="mono" id="detail-path">/api/v1/{identifier}</span>.</p>
        </div>
      </div>
      <div id="detail-body"></div>
    </section>

    <section class="section-card">
      <div class="section-header">
        <div>
          <h2 class="section-title">Blocked sessions</h2>
          <p class="section-copy">Issues paused because Codex requested operator input or approval.</p>
        </div>
      </div>
      <p class="empty-state" id="blocked-empty">No blocked sessions.</p>
      <div class="table-wrap" id="blocked-wrap" style="display:none;">
        <table class="data-table" style="min-width: 760px;">
          <thead>
            <tr>
              <th>Issue</th>
              <th>State</th>
              <th>Session</th>
              <th>Blocked at</th>
              <th>Last update</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody id="blocked-body"></tbody>
        </table>
      </div>
    </section>

    <section class="section-card">
      <div class="section-header">
        <div>
          <h2 class="section-title">Retry queue</h2>
          <p class="section-copy">Issues waiting for the next retry window.</p>
        </div>
      </div>
      <p class="empty-state" id="retrying-empty">No issues are currently backing off.</p>
      <div class="table-wrap" id="retrying-wrap" style="display:none;">
        <table class="data-table" style="min-width: 680px;">
          <thead>
            <tr>
              <th>Issue</th>
              <th>Attempt</th>
              <th>Due at</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody id="retrying-body"></tbody>
        </table>
      </div>
    </section>

    <section class="section-card">
      <div class="section-header">
        <div>
          <h2 class="section-title">Agent live log</h2>
          <p class="section-copy">Mirrored pi JSONL stream, tailed every 3 seconds.</p>
        </div>
      </div>
      <div class="log-controls">
        <select id="log-session"></select>
        <span class="muted mono" id="log-meta" style="font-size:0.78rem;"></span>
      </div>
      <div class="log-body" id="log-body">no live sessions</div>
    </section>

  </div>
</section>
</main>

<script>
(function () {
  "use strict";

  var state = { payload: null, now: Date.now(), detailOpen: null, selectedSession: null };

  function esc(value) {
    if (value == null) return "";
    return String(value).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function formatInt(value) {
    if (value == null || isNaN(Number(value))) return "n/a";
    return String(Math.trunc(Number(value))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function formatRuntimeSeconds(seconds) {
    var whole = Math.max(0, Math.trunc(Number(seconds) || 0));
    return Math.floor(whole / 60) + "m " + (whole % 60) + "s";
  }

  function runtimeSecondsFrom(startedAt, now) {
    if (!startedAt) return 0;
    var parsed = Date.parse(startedAt);
    if (isNaN(parsed)) return 0;
    return Math.max(0, Math.floor((now - parsed) / 1000));
  }

  function formatRuntimeAndTurns(startedAt, turnCount, now) {
    var base = formatRuntimeSeconds(runtimeSecondsFrom(startedAt, now));
    return (turnCount && Number(turnCount) > 0) ? base + " / " + turnCount : base;
  }

  function stateBadgeClass(value) {
    var normalized = String(value == null ? "" : value).toLowerCase();
    var base = "state-badge";
    if (normalized.indexOf("progress") >= 0 || normalized.indexOf("running") >= 0
        || normalized.indexOf("active") >= 0) return base + " state-badge-active";
    if (normalized.indexOf("blocked") >= 0 || normalized.indexOf("error") >= 0
        || normalized.indexOf("failed") >= 0) return base + " state-badge-danger";
    if (normalized.indexOf("todo") >= 0 || normalized.indexOf("queued") >= 0
        || normalized.indexOf("pending") >= 0 || normalized.indexOf("retry") >= 0) {
      return base + " state-badge-warning";
    }
    return base;
  }

  function externalIssueUrl(url) {
    if (!url) return null;
    try {
      var parsed = new URL(url);
      if ((parsed.protocol === "http:" || parsed.protocol === "https:")
          && parsed.hostname) return url;
    } catch (error) { /* not a valid absolute URL */ }
    return null;
  }

  function issueIdentifierCell(entry) {
    var href = externalIssueUrl(entry.issue_url);
    var identifier = esc(entry.issue_identifier || "unknown");
    var idHtml = href
      ? '<a class="issue-id issue-id-link" href="' + esc(href)
        + '" target="_blank" rel="noopener noreferrer">' + identifier + "</a>"
      : '<span class="issue-id">' + identifier + "</span>";
    var details = entry.issue_identifier
      ? '<a class="issue-link" href="/api/v1/'
        + encodeURIComponent(entry.issue_identifier) + '">JSON details</a>'
      : "";
    return '<div class="issue-stack">' + idHtml + details + "</div>";
  }

  function copyIdCell(sessionId) {
    if (!sessionId) return '<span class="muted">n/a</span>';
    return '<button type="button" class="subtle-button" data-copy="'
      + esc(sessionId) + '">Copy ID</button>';
  }

  function lastUpdateCell(entry) {
    var message = entry.last_message || entry.last_event || "n/a";
    var meta = esc(entry.last_event || "n/a");
    if (entry.last_event_at) {
      meta += ' · <span class="mono numeric">' + esc(entry.last_event_at) + "</span>";
    }
    return '<div class="detail-stack"><span class="event-text" title="'
      + esc(message) + '">' + esc(message) + "</span>"
      + '<span class="muted event-meta">' + meta + "</span></div>";
  }

  function renderMetrics(payload) {
    var counts = payload.counts || {};
    var totals = payload.codex_totals || {};
    document.getElementById("metric-running").textContent = counts.running || 0;
    document.getElementById("metric-retrying").textContent = counts.retrying || 0;
    document.getElementById("metric-blocked").textContent = counts.blocked || 0;
    document.getElementById("metric-tokens").textContent = formatInt(totals.total_tokens);
    document.getElementById("metric-tokens-detail").textContent =
      "In " + formatInt(totals.input_tokens) + " / Out " + formatInt(totals.output_tokens);
    updateRuntimeMetric();
  }

  function updateRuntimeMetric() {
    if (!state.payload) return;
    var totals = state.payload.codex_totals || {};
    var seconds = Number(totals.seconds_running) || 0;
    (state.payload.running || []).forEach(function (entry) {
      seconds += runtimeSecondsFrom(entry.started_at, state.now);
    });
    document.getElementById("metric-runtime").textContent = formatRuntimeSeconds(seconds);
  }

  function renderRateLimits(value) {
    document.getElementById("rate-limits").textContent =
      value == null ? "n/a" : JSON.stringify(value, null, 2);
  }

  function renderAlerts(payload) {
    var alerts = ((payload.health || {}).alerts || []).concat(payload.alerts || []);
    var card = document.getElementById("alerts-card");
    var list = document.getElementById("alerts-list");
    if (!alerts.length) {
      card.style.display = "none";
      list.innerHTML = "";
      return;
    }
    card.style.display = "";
    list.innerHTML = alerts.map(function (alert) {
      var cls = alert.severity === "critical" ? "state-badge-danger"
        : alert.severity === "warning" ? "state-badge-warning" : "state-badge-active";
      return '<div class="alert-row"><span class="state-badge ' + cls + '">'
        + esc(alert.severity || "info") + "</span> " + esc(alert.message) + "</div>";
    }).join("");
  }

  function renderRunning(running) {
    var empty = document.getElementById("running-empty");
    var wrap = document.getElementById("running-wrap");
    var body = document.getElementById("running-body");
    if (!running.length) {
      empty.style.display = "";
      wrap.style.display = "none";
      body.innerHTML = "";
      return;
    }
    empty.style.display = "none";
    wrap.style.display = "";
    body.innerHTML = running.map(function (entry) {
      var tokens = entry.tokens || {};
      return '<tr class="clickable" data-issue="' + esc(entry.issue_identifier) + '">'
        + "<td>" + issueIdentifierCell(entry) + "</td>"
        + '<td><span class="' + stateBadgeClass(entry.state) + '">'
        + esc(entry.state || "unknown") + "</span></td>"
        + "<td>" + copyIdCell(entry.session_id) + "</td>"
        + '<td class="numeric runtime-cell" data-started-at="' + esc(entry.started_at || "")
        + '" data-turns="' + esc(entry.turn_count || 0) + '">'
        + esc(formatRuntimeAndTurns(entry.started_at, entry.turn_count, state.now)) + "</td>"
        + "<td>" + lastUpdateCell(entry) + "</td>"
        + '<td><div class="token-stack numeric"><span>Total: ' + formatInt(tokens.total_tokens)
        + '</span><span class="muted">In ' + formatInt(tokens.input_tokens)
        + " / Out " + formatInt(tokens.output_tokens) + "</span></div></td>"
        + "</tr>";
    }).join("");
  }

  function renderBlocked(blocked) {
    var empty = document.getElementById("blocked-empty");
    var wrap = document.getElementById("blocked-wrap");
    var body = document.getElementById("blocked-body");
    if (!blocked.length) {
      empty.style.display = "";
      wrap.style.display = "none";
      body.innerHTML = "";
      return;
    }
    empty.style.display = "none";
    wrap.style.display = "";
    body.innerHTML = blocked.map(function (entry) {
      return "<tr>"
        + "<td>" + issueIdentifierCell(entry) + "</td>"
        + '<td><span class="' + stateBadgeClass(entry.state || "Blocked") + '">'
        + esc(entry.state || "Blocked") + "</span></td>"
        + "<td>" + copyIdCell(entry.session_id) + "</td>"
        + '<td class="mono">' + esc(entry.blocked_at || "n/a") + "</td>"
        + "<td>" + lastUpdateCell(entry) + "</td>"
        + "<td>" + esc(entry.error || "n/a") + "</td>"
        + "</tr>";
    }).join("");
  }

  function renderRetrying(retrying) {
    var empty = document.getElementById("retrying-empty");
    var wrap = document.getElementById("retrying-wrap");
    var body = document.getElementById("retrying-body");
    if (!retrying.length) {
      empty.style.display = "";
      wrap.style.display = "none";
      body.innerHTML = "";
      return;
    }
    empty.style.display = "none";
    wrap.style.display = "";
    body.innerHTML = retrying.map(function (entry) {
      return "<tr>"
        + "<td>" + issueIdentifierCell(entry) + "</td>"
        + "<td>" + esc(entry.attempt != null ? entry.attempt : "n/a") + "</td>"
        + '<td class="mono">' + esc(entry.due_at || "n/a") + "</td>"
        + "<td>" + esc(entry.error || "n/a") + "</td>"
        + "</tr>";
    }).join("");
  }

  function renderError(payload) {
    var card = document.getElementById("error-card");
    var content = document.getElementById("content");
    if (!payload.error) {
      card.style.display = "none";
      content.style.display = "";
      return;
    }
    card.style.display = "";
    content.style.display = "none";
    document.getElementById("error-copy").innerHTML =
      "<strong>" + esc(payload.error.code || "error") + ":</strong> "
      + esc(payload.error.message || "");
  }

  function renderSnapshot(payload) {
    state.payload = payload;
    state.now = Date.now();
    renderError(payload);
    if (payload.error) return;
    renderMetrics(payload);
    renderAlerts(payload);
    renderRateLimits(payload.rate_limits);
    renderRunning(payload.running || []);
    renderBlocked(payload.blocked || []);
    renderRetrying(payload.retrying || []);
    var paused = payload.paused || (payload.control && payload.control.paused);
    document.getElementById("btn-pause").style.display = paused ? "none" : "";
    document.getElementById("btn-resume").style.display = paused ? "" : "none";
    if (state.detailOpen) refreshIssueDetail(state.detailOpen, true);
  }

  function renderIssueDetail(detail) {
    var run = detail.running || {};
    var attempts = detail.attempts || {};
    var workspace = detail.workspace || {};
    var tokens = run.tokens || {};
    var rows = [
      ["status", detail.status],
      ["state", run.state],
      ["issue id", detail.issue_id],
      ["workspace", workspace.path],
      ["host", workspace.host],
      ["session", run.session_id],
      ["turn", run.turn_count],
      ["tokens", tokens.input_tokens + " in / " + tokens.output_tokens
        + " out / " + tokens.total_tokens + " total"],
      ["started", run.started_at],
      ["last event", run.last_event],
      ["last event at", run.last_event_at],
      ["last message", run.last_message],
      ["retry attempt", attempts.current_retry_attempt],
      ["restarts", attempts.restart_count]
    ];
    var html = '<dl class="kv">';
    rows.forEach(function (pair) {
      if (pair[1] == null || pair[1] === "") return;
      html += "<dt>" + esc(pair[0]) + "</dt><dd>" + esc(pair[1]) + "</dd>";
    });
    html += "</dl>";
    var body = document.getElementById("detail-body");
    body.innerHTML = html;
  }

  function refreshIssueDetail(identifier, silent) {
    var body = document.getElementById("detail-body");
    if (!silent) {
      body.innerHTML = '<p class="empty-state">loading…</p>';
    }
    fetch("/api/v1/" + encodeURIComponent(identifier))
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(renderIssueDetail)
      .catch(function (error) {
        if (!silent) body.innerHTML = '<p class="empty-state">failed: ' + esc(error) + "</p>";
      });
  }

  function toggleIssueDetail(identifier) {
    var card = document.getElementById("detail-card");
    if (state.detailOpen === identifier) {
      state.detailOpen = null;
      card.style.display = "none";
      return;
    }
    state.detailOpen = identifier;
    document.getElementById("detail-title").textContent = "Issue detail — " + identifier;
    document.getElementById("detail-path").textContent = "/api/v1/" + identifier;
    card.style.display = "";
    refreshIssueDetail(identifier, false);
  }

  var logSessions = [];

  function refreshLog() {
    if (!state.selectedSession) return;
    fetch("/api/v1/sessions/" + encodeURIComponent(state.selectedSession) + "/log?lines=80")
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        document.getElementById("log-meta").textContent =
          data.file + " · " + (data.size / 1024).toFixed(1) + " KB · "
          + data.events.length + " events";
        document.getElementById("log-body").innerHTML = data.events.map(function (event) {
          var cls = event.type.indexOf("tool_execution") === 0 ? "log-tool"
            : event.summary.indexOf("thinking") === 0 ? "log-thinking"
            : (event.type === "agent_settled" || event.type === "agent_end") ? "log-error" : "";
          return '<div class="log-line ' + cls + '"><span class="log-type">'
            + esc(event.type) + "</span> " + esc(event.summary) + "</div>";
        }).join("") || '<span class="empty-state">no events</span>';
      })
      .catch(function (error) {
        document.getElementById("log-body").innerHTML =
          '<span class="empty-state">log unavailable: ' + esc(error) + "</span>";
      });
  }

  function refreshSessions() {
    fetch("/api/v1/sessions").then(function (response) { return response.json(); })
      .then(function (data) {
        logSessions = data.sessions || [];
        var select = document.getElementById("log-session");
        var live = logSessions.filter(function (session) { return session.live; });
        var options = (live.length ? live : logSessions).slice(0, 20);
        if (!options.length) {
          select.innerHTML = "";
          state.selectedSession = null;
          document.getElementById("log-body").innerHTML =
            '<span class="empty-state">no live sessions</span>';
          return;
        }
        var known = options.some(function (session) {
          return session.session_id === state.selectedSession;
        });
        if (!known) state.selectedSession = options[0].session_id;
        select.innerHTML = options.map(function (session) {
          var label = (session.identifier ? session.identifier + " · " : "")
            + session.session_id.slice(0, 8) + (session.live ? " ●" : "");
          return '<option value="' + esc(session.session_id) + '"'
            + (session.session_id === state.selectedSession ? " selected" : "")
            + ">" + esc(label) + "</option>";
        }).join("");
        refreshLog();
      })
      .catch(function () { /* SSE loop will retry */ });
  }

  function post(path) {
    return fetch(path, { method: "POST" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  document.getElementById("btn-refresh").addEventListener("click", function () {
    post("/api/v1/refresh").catch(function (error) { alert("refresh failed: " + error); });
  });
  document.getElementById("btn-pause").addEventListener("click", function () {
    post("/api/v1/control/pause").catch(function (error) { alert("pause failed: " + error); });
  });
  document.getElementById("btn-resume").addEventListener("click", function () {
    post("/api/v1/control/resume").catch(function (error) { alert("resume failed: " + error); });
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement)) return;
    var copy = target.getAttribute("data-copy");
    if (copy) {
      navigator.clipboard.writeText(copy).then(function () {
        var label = target.textContent;
        target.textContent = "Copied";
        clearTimeout(target._copyTimer);
        target._copyTimer = setTimeout(function () { target.textContent = label; }, 1200);
      });
      return;
    }
    if (target.closest("a") || target.closest("button")) return;
    var row = target.closest("tr");
    var issueId = row && row.getAttribute("data-issue");
    if (issueId) toggleIssueDetail(issueId);
  });

  document.getElementById("log-session").addEventListener("change", function (event) {
    state.selectedSession = event.target.value;
    refreshLog();
  });

  setInterval(function () {
    state.now = Date.now();
    if (!state.payload || state.payload.error) return;
    updateRuntimeMetric();
    document.querySelectorAll(".runtime-cell").forEach(function (cell) {
      var turns = parseInt(cell.getAttribute("data-turns") || "0", 10);
      cell.textContent = formatRuntimeAndTurns(
        cell.getAttribute("data-started-at"), turns, state.now);
    });
  }, 1000);

  var source = new EventSource("/api/v1/stream");
  source.addEventListener("ready", function () {
    document.getElementById("badge-live").style.display = "";
    document.getElementById("badge-offline").style.display = "none";
  });
  source.addEventListener("snapshot", function (event) {
    try {
      renderSnapshot(JSON.parse(event.data));
    } catch (error) {
      console.error("bad snapshot", error);
    }
  });
  source.onerror = function () {
    document.getElementById("badge-live").style.display = "none";
    document.getElementById("badge-offline").style.display = "";
  };

  fetch("/api/v1/state").then(function (response) { return response.json(); })
    .then(renderSnapshot)
    .catch(function () { /* SSE will fill in */ });

  refreshSessions();
  setInterval(refreshSessions, 3000);
})();
</script>
</body>
</html>
"""
