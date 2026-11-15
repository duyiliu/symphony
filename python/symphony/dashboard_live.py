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
"""

import json
import logging
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
# Static dashboard HTML/JS (single file, no build step)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Symphony Observability</title>
<style>
  :root {
    --bg: #0b0e14;
    --panel: #131822;
    --border: #1f2937;
    --text: #e5e7eb;
    --muted: #9ca3af;
    --ok: #34d399;
    --warn: #fbbf24;
    --err: #f87171;
    --accent: #60a5fa;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg); color: var(--text); font-size: 14px;
  }
  header {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    background: var(--panel); position: sticky; top: 0; z-index: 10;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; font-family: var(--mono); }
  .container { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px; overflow: hidden;
  }
  .card h2 {
    margin: 0 0 10px; font-size: 13px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
  }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; font-family: var(--mono);
  }
  .badge-ok { background: rgba(52, 211, 153, .15); color: var(--ok); }
  .badge-warn { background: rgba(251, 191, 36, .15); color: var(--warn); }
  .badge-err { background: rgba(248, 113, 113, .15); color: var(--err); }
  .badge-info { background: rgba(96, 165, 250, .15); color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; }
  tr:last-child td { border-bottom: 0; }
  .empty { color: var(--muted); font-style: italic; padding: 8px 0; }
  .actions { margin-top: 8px; }
  button {
    background: var(--accent); color: #fff; border: 0; padding: 6px 12px;
    border-radius: 4px; font-size: 12px; cursor: pointer; margin-right: 6px;
  }
  button:hover { opacity: .9; }
  button.secondary { background: var(--border); color: var(--text); }
  button.danger { background: var(--err); }
  #status-line {
    padding: 8px 20px; background: var(--panel); border-bottom: 1px solid var(--border);
    font-size: 12px; color: var(--muted); font-family: var(--mono);
  }
  #status-line.live { color: var(--ok); }
  #status-line.stale { color: var(--warn); }
  .issue-link { color: var(--accent); text-decoration: none; }
  .issue-link:hover { text-decoration: underline; }
  .kv { display: grid; grid-template-columns: 140px 1fr; gap: 4px 12px; font-size: 12px; }
  .kv dt { color: var(--muted); } .kv dd { margin: 0; font-family: var(--mono); }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: var(--muted); font-size: 12px; }
  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 8px; font-size: 11px; overflow-x: auto; max-height: 200px; overflow-y: auto;
  }
</style>
</head>
<body>

<header>
  <h1>Symphony Observability</h1>
  <div class="meta" id="header-meta">connecting…</div>
</header>

<div id="status-line">connecting…</div>

<div class="container">
  <div class="grid">

    <div class="card">
      <h2>Scheduler</h2>
      <dl class="kv" id="scheduler-kv">
        <dt>status</dt><dd id="health-status">—</dd>
        <dt>polling</dt><dd id="poll-count">—</dd>
        <dt>last poll</dt><dd id="poll-last">—</dd>
        <dt>running</dt><dd id="running-count">—</dd>
        <dt>blocked</dt><dd id="blocked-count">—</dd>
        <dt>retrying</dt><dd id="retrying-count">—</dd>
      </dl>
      <div class="actions">
        <button id="btn-refresh">Refresh</button>
        <button id="btn-pause" class="secondary">Pause</button>
        <button id="btn-resume" class="secondary" style="display:none">Resume</button>
      </div>
    </div>

    <div class="card">
      <h2>Alerts</h2>
      <div id="alerts-list" class="empty">none</div>
    </div>

    <div class="card">
      <h2>Agent Sessions</h2>
      <dl class="kv">
        <dt>enabled</dt><dd id="sess-enabled">—</dd>
        <dt>queue</dt><dd id="sess-queue">—</dd>
        <dt>pending</dt><dd id="sess-pending">—</dd>
      </dl>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <h2>Running Issues</h2>
      <table id="running-table">
        <thead><tr>
          <th>Issue</th><th>State</th><th>Turn</th><th>Age</th><th>Session</th><th>Workspace</th>
        </tr></thead>
        <tbody><tr><td colspan="6" class="empty">no running issues</td></tr></tbody>
      </table>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <h2>Retrying</h2>
      <table id="retrying-table">
        <thead><tr>
          <th>Issue</th><th>Attempt</th><th>Due</th><th>Error</th>
        </tr></thead>
        <tbody><tr><td colspan="4" class="empty">none</td></tr></tbody>
      </table>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <h2>Blocked</h2>
      <table id="blocked-table">
        <thead><tr>
          <th>Issue</th><th>State</th><th>Error</th><th>Blocked at</th><th>Actions</th>
        </tr></thead>
        <tbody><tr><td colspan="5" class="empty">none</td></tr></tbody>
      </table>
    </div>

  </div>
</div>

<script>
(function () {
  "use strict";

  var statusLine = document.getElementById("status-line");
  var headerMeta = document.getElementById("header-meta");
  var lastSnapshotAt = 0;
  var lastTickAt = 0;

  function fmtTs(ts) {
    if (!ts) return "—";
    var d = new Date(ts);
    return d.toLocaleTimeString() + "." + String(d.getMilliseconds()).padStart(3, "0");
  }

  function fmtAgo(ts) {
    if (!ts) return "—";
    var s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
    if (s < 60) return s.toFixed(0) + "s";
    if (s < 3600) return (s / 60).toFixed(1) + "m";
    return (s / 3600).toFixed(1) + "h";
  }

  function esc(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>\"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"}[c];
    });
  }

  function issueCell(identifier, url) {
    if (!identifier) return "<td>—</td>";
    if (url) {
      return '<td><a class="issue-link" href="' + esc(url) + '" target="_blank">'
        + esc(identifier) + "</a></td>";
    }
    return "<td>" + esc(identifier) + "</td>";
  }

  function renderSnapshot(snap) {
    lastSnapshotAt = Date.now();
    var health = snap.health || {};
    var status = health.status || "unknown";
    document.getElementById("health-status").innerHTML =
      '<span class="badge badge-' + (status === "healthy" ? "ok" : status === "degraded" ? "warn" : "err") + '">'
      + esc(status) + "</span>";
    document.getElementById("poll-count").textContent =
      (snap.polling && snap.polling.count) || "0";
    document.getElementById("poll-last").textContent =
      snap.polling && snap.polling.last_poll_at ? fmtAgo(snap.polling.last_poll_at) + " ago" : "—";
    document.getElementById("running-count").textContent =
      (snap.running || []).length;
    document.getElementById("blocked-count").textContent =
      (snap.blocked || []).length;
    document.getElementById("retrying-count").textContent =
      (snap.retrying || []).length;

    var paused = snap.control && snap.control.paused;
    document.getElementById("btn-pause").style.display = paused ? "none" : "";
    document.getElementById("btn-resume").style.display = paused ? "" : "none";

    var alerts = (health.alerts || []).concat(snap.alerts || []);
    var alertsEl = document.getElementById("alerts-list");
    if (!alerts.length) {
      alertsEl.innerHTML = '<span class="empty">none</span>';
    } else {
      alertsEl.innerHTML = alerts.map(function (a) {
        return '<div><span class="badge badge-' + (a.severity === "critical" ? "err" : a.severity === "warning" ? "warn" : "info") + '">'
          + esc(a.severity) + "</span> " + esc(a.message) + "</div>";
      }).join("");
    }

    var sess = snap.agent_sessions || {};
    document.getElementById("sess-enabled").textContent =
      sess.enabled ? "yes" : "no";
    document.getElementById("sess-queue").textContent =
      sess.queue_length || "0";
    document.getElementById("sess-pending").textContent =
      sess.pending_events || "0";

    var runningBody = document.querySelector("#running-table tbody");
    var running = snap.running || [];
    if (!running.length) {
      runningBody.innerHTML = '<tr><td colspan="6" class="empty">no running issues</td></tr>';
    } else {
      runningBody.innerHTML = running.map(function (r) {
        return "<tr>" +
          issueCell(r.identifier, r.issue_url) +
          "<td>" + esc(r.state || "—") + "</td>" +
          "<td>" + esc(r.turn || "—") + "</td>" +
          "<td>" + esc(fmtAgo(r.started_at)) + "</td>" +
          "<td>" + esc((r.session_id || "").slice(0, 8) || "—") + "</td>" +
          "<td>" + esc(r.workspace_path || r.worker_host || "—") + "</td>" +
          "</tr>";
      }).join("");
    }

    var retryingBody = document.querySelector("#retrying-table tbody");
    var retrying = snap.retrying || [];
    if (!retrying.length) {
      retryingBody.innerHTML = '<tr><td colspan="4" class="empty">none</td></tr>';
    } else {
      retryingBody.innerHTML = retrying.map(function (r) {
        return "<tr>" +
          issueCell(r.identifier, r.issue_url) +
          "<td>" + esc(r.attempt || "—") + "</td>" +
          "<td>" + esc(r.due_at ? fmtAgo(r.due_at) : "—") + "</td>" +
          "<td>" + esc((r.error || "").slice(0, 80)) + "</td>" +
          "</tr>";
      }).join("");
    }

    var blockedBody = document.querySelector("#blocked-table tbody");
    var blocked = snap.blocked || [];
    if (!blocked.length) {
      blockedBody.innerHTML = '<tr><td colspan="5" class="empty">none</td></tr>';
    } else {
      blockedBody.innerHTML = blocked.map(function (r) {
        return "<tr>" +
          issueCell(r.identifier, r.issue_url) +
          "<td>" + esc(r.state || "—") + "</td>" +
          "<td>" + esc((r.error || "").slice(0, 60)) + "</td>" +
          "<td>" + esc(fmtAgo(r.blocked_at)) + "</td>" +
          '<td><button class="secondary" data-release="' + esc(r.identifier) + '">release</button>'
          + '<button class="secondary" data-cleanup="' + esc(r.identifier) + '">cleanup</button></td>' +
          "</tr>";
      }).join("");
    }
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  document.getElementById("btn-refresh").addEventListener("click", function () {
    post("/api/v1/refresh").catch(function (e) { alert("refresh failed: " + e); });
  });
  document.getElementById("btn-pause").addEventListener("click", function () {
    post("/api/v1/control/pause").catch(function (e) { alert("pause failed: " + e); });
  });
  document.getElementById("btn-resume").addEventListener("click", function () {
    post("/api/v1/control/resume").catch(function (e) { alert("resume failed: " + e); });
  });

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    var release = t.getAttribute("data-release");
    var cleanup = t.getAttribute("data-cleanup");
    if (release) {
      post("/api/v1/control/release/" + encodeURIComponent(release))
        .catch(function (e) { alert("release failed: " + e); });
    } else if (cleanup) {
      post("/api/v1/control/cleanup/" + encodeURIComponent(cleanup))
        .catch(function (e) { alert("cleanup failed: " + e); });
    }
  });

  var source = new EventSource("/api/v1/stream");
  source.addEventListener("ready", function () {
    statusLine.textContent = "live (connected)";
    statusLine.className = "live";
  });
  source.addEventListener("snapshot", function (ev) {
    try {
      var snap = JSON.parse(ev.data);
      renderSnapshot(snap);
      headerMeta.textContent = "updated " + fmtTs(Date.now());
    } catch (e) {
      console.error("bad snapshot", e);
    }
  });
  source.addEventListener("tick", function (ev) {
    lastTickAt = Date.now();
    if (lastSnapshotAt) {
      statusLine.textContent = "live (idle " + fmtAgo(new Date(lastSnapshotAt).toISOString()) + ")";
      statusLine.className = "live";
    }
  });
  source.onerror = function () {
    statusLine.textContent = "disconnected — retrying";
    statusLine.className = "stale";
  };

  // Initial fetch so we render something even before the first SSE snapshot.
  fetch("/api/v1/state").then(function (r) { return r.json(); })
    .then(renderSnapshot)
    .catch(function () { /* SSE will fill in */ });
})();
</script>

</body>
</html>
"""
