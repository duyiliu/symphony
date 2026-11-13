"""Linear Agent Session integration (host-side, opt-in).

When ``tracker.provider.agent_sessions.enabled`` is true the orchestrator
mirrors each claimed issue into a Linear Agent Session so that Linear stays
the single progress surface. All calls run in the trusted host process,
never inside the Pi worker. Failures degrade to log warnings only: the
local scheduler remains the authority for execution, and Linear comments
(workpad / delivery notices) keep working as the fallback channel.

Lifecycle mapping:

- claim  -> ``agentSessionCreateOnIssue`` + started ``agentActivityCreate``
- finish -> terminal ``agentActivityCreate`` + ``agentSessionUpdate``
  (summary + external links for the workspace/PR)

Session IDs are persisted per-issue in a small state dir so restarts do
not create duplicate sessions for the same issue.
"""

import json
import logging
import os
import threading

from . import linear

log = logging.getLogger("symphony.agent_sessions")

_lock = threading.Lock()


def enabled(settings):
    provider = (getattr(settings.tracker, "provider", None) or {})
    conf = provider.get("agent_sessions")
    return isinstance(conf, dict) and conf.get("enabled") is True


def _state_path(settings, issue_id):
    provider = (settings.tracker.provider or {})
    conf = provider.get("agent_sessions") or {}
    root = conf.get("state_dir")
    if not root:
        return None
    os.makedirs(root, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in issue_id)
    return os.path.join(root, "%s.json" % safe)


def _load_session_id(settings, issue_id):
    path = _state_path(settings, issue_id)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("session_id")
    except Exception:  # noqa: BLE001 - corrupt state must not break runs
        return None


def _store_session_id(settings, issue_id, session_id):
    path = _state_path(settings, issue_id)
    if not path:
        return
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"issue_id": issue_id, "session_id": session_id}, handle)
    os.replace(tmp, path)


def _external_urls(issue, extra=None):
    urls = []
    if getattr(issue, "url", None):
        urls.append({"url": issue.url, "label": "Issue"})
    for item in extra or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append({"url": item["url"], "label": item.get("label") or "Link"})
    return urls[:10]


def ensure_session(settings, issue):
    """Create (or reuse) the Linear Agent Session for a claimed issue."""
    if not enabled(settings):
        return None
    with _lock:
        existing = _load_session_id(settings, issue.id)
        if existing:
            return existing
        try:
            payload = linear.graphql(
                settings.tracker,
                "mutation SymphonyAgentSessionCreate("
                "$input: AgentSessionCreateOnIssue!) {"
                " agentSessionCreateOnIssue(input: $input) {"
                "   success agentSession { id } } }",
                {"input": {
                    "issueId": issue.id,
                    "externalUrls": _external_urls(issue),
                }},
            )
            result = (payload.get("data") or {}).get(
                "agentSessionCreateOnIssue")
            session = (result or {}).get("agentSession") or {}
            session_id = session.get("id")
            if not session_id:
                log.warning(
                    "agent session create returned no id for %s: %s",
                    issue.identifier, payload)
                return None
            _store_session_id(settings, issue.id, session_id)
            log.info(
                "Linear agent session %s created for %s",
                session_id, issue.identifier)
            return session_id
        except Exception as error:  # noqa: BLE001 - best-effort only
            log.warning(
                "agent session create failed for %s: %r",
                issue.identifier, error)
            return None


def post_activity(settings, issue, session_id, content_type, body,
                  signal=None):
    """Append an activity entry; ``content_type`` maps to Linear's
    ``AgentActivityContent`` union (thought/action/response/error)."""
    if not enabled(settings) or not session_id:
        return
    content = {"type": content_type, "body": body}
    input_fields = {"agentSessionId": session_id, "content": content}
    if signal:
        input_fields["signal"] = signal
    try:
        linear.graphql(
            settings.tracker,
            "mutation SymphonyAgentActivity("
            "$input: AgentActivityCreateInput!) {"
            " agentActivityCreate(input: $input) {"
            "   success agentActivity { id } } }",
            {"input": input_fields},
        )
    except Exception as error:  # noqa: BLE001 - never break the run
        log.warning(
            "agent activity post failed for %s session=%s: %r",
            issue.identifier, session_id, error)


def finish_session(settings, issue, session_id, summary, external_urls=None):
    """Record the terminal summary and links for the session."""
    if not enabled(settings) or not session_id:
        return
    update = {"summary": summary[:2000]}
    urls = _external_urls(issue, external_urls)
    if urls:
        update["externalUrls"] = urls
    try:
        linear.graphql(
            settings.tracker,
            "mutation SymphonyAgentSessionUpdate("
            "$id: String!, $input: AgentSessionUpdateInput!) {"
            " agentSessionUpdate(id: $id, input: $input) {"
            "   success agentSession { id status } } }",
            {"id": session_id, "input": update},
        )
    except Exception as error:  # noqa: BLE001
        log.warning(
            "agent session update failed for %s session=%s: %r",
            issue.identifier, session_id, error)
