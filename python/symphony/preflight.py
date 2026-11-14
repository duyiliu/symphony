"""Startup capability checks that do not perform network I/O.

Configuration validation rejects structurally invalid workflows. This module
reports environment problems separately: missing optional credentials degrade
one capability, while an unwritable workspace root is fatal.
"""

import os
import shlex
import shutil

from .config import resolve_env_setting


def _workspace_root(settings, base_dir):
    root = settings.workspace.root
    if not root:
        return None
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        root = os.path.join(base_dir or os.getcwd(), root)
    return os.path.abspath(root)


def _agent_session_oauth(settings):
    provider = settings.tracker.provider or {}
    conf = provider.get("agent_sessions") or {}
    if not isinstance(conf, dict) or conf.get("enabled") is not True:
        return None
    oauth = conf.get("oauth") or {}
    return oauth if isinstance(oauth, dict) else {}


def _command_available(command):
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return False
    executable = parts[0] if parts else ""
    if not executable:
        return False
    if os.path.dirname(executable):
        return os.path.isfile(executable)
    return shutil.which(executable) is not None


def collect(settings, base_dir=None):
    errors = []
    warnings = []

    root = _workspace_root(settings, base_dir)
    if root and not os.path.isdir(root):
        warnings.append({
            "code": "workspace_root_missing",
            "message": "workspace.root does not exist yet: %s" % root,
        })
    elif root and not os.access(root, os.W_OK):
        errors.append({
            "code": "workspace_root_not_writable",
            "message": "workspace.root is not writable: %s" % root,
        })

    if settings.runtime.kind == "pi" and not _command_available(
            settings.runtime.command):
        warnings.append({
            "code": "pi_command_not_found",
            "message": "runtime.command is not on PATH: %s"
                       % settings.runtime.command,
        })

    oauth = _agent_session_oauth(settings)
    if oauth is not None:
        client_id = resolve_env_setting(
            oauth.get("client_id"), "LINEAR_OAUTH_CLIENT_ID")
        client_secret = resolve_env_setting(
            oauth.get("client_secret"), "LINEAR_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            warnings.append({
                "code": "agent_session_oauth_missing",
                "message": (
                    "Agent Session is enabled but Linear OAuth client "
                    "credentials are missing"),
            })

    if not (settings.server.api_token or "").strip():
        warnings.append({
            "code": "api_token_not_configured",
            "message": (
                "server.api_token is empty; only loopback clients may use "
                "mutation endpoints"),
        })

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }
