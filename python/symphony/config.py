"""Runtime configuration loaded from WORKFLOW.md front matter.

Mirrors SymphonyElixir.Config.Schema: typed sections, defaults, validation and
environment indirection. Only the GitHub tracker kind is supported by this
implementation; other kinds fail validation with ``unsupported_tracker_kind``.
"""

import os
import re
import tempfile
from dataclasses import dataclass, field

from .util import normalize_issue_state, present_string
from .workflow import WorkflowError

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUPPORTED_TRACKER_KINDS = {"github"}

DEFAULT_PROMPT_TEMPLATE = """\
You are working on an issue from the configured tracker.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}

Body:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}
"""


@dataclass
class TrackerSettings:
    kind: str = None
    endpoint: str = None
    api_key: str = None
    project_slug: str = None
    assignee: str = None
    provider: dict = field(default_factory=dict)
    secret_environment_names: list = field(default_factory=list)
    required_labels: list = field(default_factory=list)
    github_workflow: dict = field(default_factory=dict)
    active_states: list = None
    terminal_states: list = None


@dataclass
class PollingSettings:
    interval_ms: int = 30_000


@dataclass
class WorkspaceSettings:
    root: str = field(default_factory=lambda: os.path.join(
        tempfile.gettempdir(), "symphony_workspaces"))


@dataclass
class WorkerSettings:
    ssh_hosts: list = field(default_factory=list)
    max_concurrent_agents_per_host: int = None


@dataclass
class AgentSettings:
    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: dict = field(default_factory=dict)


@dataclass
class RuntimeSettings:
    kind: str = "codex"
    command: str = None
    model: str = None
    thinking: str = None
    turn_timeout_ms: int = 3_600_000
    stall_timeout_ms: int = 300_000
    mcp: dict = field(default_factory=dict)
    live_log_dir: str = None


DEFAULT_APPROVAL_POLICY = {
    "reject": {
        "sandbox_approval": True,
        "rules": True,
        "mcp_elicitations": True,
    }
}


@dataclass
class CodexSettings:
    command: str = "codex app-server"
    approval_policy: object = field(default_factory=lambda: dict(DEFAULT_APPROVAL_POLICY))
    thread_sandbox: str = "workspace-write"
    turn_sandbox_policy: dict = None
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


@dataclass
class HooksSettings:
    after_create: str = None
    before_run: str = None
    after_run: str = None
    before_remove: str = None
    timeout_ms: int = 60_000


@dataclass
class ObservabilitySettings:
    dashboard_enabled: bool = True
    refresh_ms: int = 1_000
    render_interval_ms: int = 16


@dataclass
class ServerSettings:
    port: int = None
    host: str = "127.0.0.1"


@dataclass
class Settings:
    tracker: TrackerSettings = field(default_factory=TrackerSettings)
    polling: PollingSettings = field(default_factory=PollingSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    codex: CodexSettings = field(default_factory=CodexSettings)
    hooks: HooksSettings = field(default_factory=HooksSettings)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)
    server: ServerSettings = field(default_factory=ServerSettings)


def _invalid(message):
    return WorkflowError("invalid_workflow_config", message)


def _section(config, name, errors):
    """Ecto ``cast_embed``: absent → default struct; non-map → invalid."""
    value = config.get(name)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    errors.append("%s is invalid" % name)
    return {}


def _opt_str(value, name, errors):
    """Ecto ``:string`` cast: absent → None; non-binary → validation error."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    errors.append("%s is invalid" % name)
    return None


def _opt_map(value, name, errors):
    """Ecto ``:map`` cast: absent → None; non-map → validation error."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    errors.append("%s is invalid" % name)
    return None


def _opt_bool(value, name, errors):
    """Ecto ``:boolean`` cast: true/false or exact "true"/"false" strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in ("true", "false"):
        return value == "true"
    errors.append("%s is invalid" % name)
    return None


def _opt_int(value, name, minimum, errors):
    """Ecto ``:integer`` cast: integer or full-string integer literal."""
    if value is None:
        return None
    if isinstance(value, str):
        if re.fullmatch(r"[+-]?\d+", value):
            value = int(value)
        else:
            errors.append("%s is invalid" % name)
            return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append("%s is invalid" % name)
        return None
    if minimum is not None and value < minimum:
        errors.append("%s must be >= %s" % (name, minimum))
        return None
    return value


def _opt_str_list(value, name, errors):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append("%s must be a list of strings" % name)
        return None
    return list(value)


def _env_reference_name(value):
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if ENV_NAME_RE.match(name):
            return name
    return None


def env_reference_names(values):
    names = []
    for value in values or []:
        name = _env_reference_name(value)
        if name:
            names.append(name)
    return names


def parse_settings(config):
    """Parse the WORKFLOW.md front matter map into Settings.

    Raises WorkflowError("invalid_workflow_config", message) on violations.
    """
    if not isinstance(config, dict):
        raise _invalid("workflow config must be a map")
    config = _drop_nil(_normalize_keys(config))
    errors = []
    settings = Settings()

    tracker = _section(config, "tracker", errors)
    settings.tracker = TrackerSettings(
        kind=_opt_str(tracker.get("kind"), "tracker.kind", errors),
        endpoint=_opt_str(tracker.get("endpoint"), "tracker.endpoint", errors),
        api_key=_opt_str(tracker.get("api_key"), "tracker.api_key", errors),
        project_slug=_opt_str(tracker.get("project_slug"), "tracker.project_slug", errors),
        assignee=_opt_str(tracker.get("assignee"), "tracker.assignee", errors),
        provider=_opt_map(tracker.get("provider"), "tracker.provider", errors) or {},
        required_labels=_opt_str_list(tracker.get("required_labels"), "tracker.required_labels", errors) or [],
        github_workflow=_opt_map(tracker.get("github_workflow"), "tracker.github_workflow", errors) or {},
        active_states=_opt_str_list(tracker.get("active_states"), "tracker.active_states", errors),
        terminal_states=_opt_str_list(tracker.get("terminal_states"), "tracker.terminal_states", errors),
    )
    settings.tracker.required_labels = list(
        dict.fromkeys(label.strip().lower() for label in settings.tracker.required_labels)
    )

    polling = _section(config, "polling", errors)
    interval = _opt_int(polling.get("interval_ms"), "polling.interval_ms", 1, errors)
    settings.polling = PollingSettings(
        interval_ms=interval if interval is not None else 30_000
    )

    workspace = _section(config, "workspace", errors)
    settings.workspace = WorkspaceSettings(
        root=_resolve_path_value(
            _opt_str(workspace.get("root"), "workspace.root", errors),
            os.path.join(tempfile.gettempdir(), "symphony_workspaces"),
        )
    )

    worker = _section(config, "worker", errors)
    settings.worker = WorkerSettings(
        ssh_hosts=_opt_str_list(worker.get("ssh_hosts"), "worker.ssh_hosts", errors) or [],
        max_concurrent_agents_per_host=_opt_int(
            worker.get("max_concurrent_agents_per_host"),
            "worker.max_concurrent_agents_per_host", 1, errors),
    )

    agent = _section(config, "agent", errors)
    state_limits = agent.get("max_concurrent_agents_by_state")
    if state_limits is not None and not isinstance(state_limits, dict):
        errors.append("agent.max_concurrent_agents_by_state must be a map")
        state_limits = {}
    state_limits = _normalize_state_limits(state_limits or {})
    for state_name, limit in state_limits.items():
        if state_name.strip() == "":
            errors.append("state names must not be blank")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            errors.append("agent.max_concurrent_agents_by_state limits must be positive integers")
    settings.agent = AgentSettings(
        max_concurrent_agents=_opt_int(
            agent.get("max_concurrent_agents"), "agent.max_concurrent_agents", 1, errors) or 10,
        max_turns=_opt_int(agent.get("max_turns"), "agent.max_turns", 1, errors) or 20,
        max_retry_backoff_ms=_opt_int(
            agent.get("max_retry_backoff_ms"), "agent.max_retry_backoff_ms", 1, errors) or 300_000,
        max_concurrent_agents_by_state=state_limits,
    )

    runtime = _section(config, "runtime", errors)
    kind = _opt_str(runtime.get("kind"), "runtime.kind", errors)
    if kind is None:
        kind = "codex"
    kind = kind.strip().lower()
    if kind not in ("codex", "pi"):
        errors.append("runtime.kind must be one of: codex, pi")
    settings.runtime = RuntimeSettings(
        kind=kind,
        command=_opt_str(runtime.get("command"), "runtime.command", errors),
        model=_opt_str(runtime.get("model"), "runtime.model", errors),
        thinking=_opt_str(runtime.get("thinking"), "runtime.thinking", errors),
        turn_timeout_ms=_opt_int(runtime.get("turn_timeout_ms"), "runtime.turn_timeout_ms", 1, errors) or 3_600_000,
        stall_timeout_ms=_opt_int(runtime.get("stall_timeout_ms"), "runtime.stall_timeout_ms", 0, errors) or 0,
        mcp=_opt_map(runtime.get("mcp"), "runtime.mcp", errors) or {},
        live_log_dir=_opt_str(runtime.get("live_log_dir"), "runtime.live_log_dir", errors),
    )
    if runtime.get("stall_timeout_ms") is None:
        settings.runtime.stall_timeout_ms = 300_000

    codex = _section(config, "codex", errors)
    approval_policy = codex.get("approval_policy", dict(DEFAULT_APPROVAL_POLICY))
    if not isinstance(approval_policy, (str, dict)):
        errors.append("codex.approval_policy must be a string or map")
        approval_policy = dict(DEFAULT_APPROVAL_POLICY)
    command = _opt_str(codex.get("command"), "codex.command", errors)
    if command is None:
        command = "codex app-server"
    elif command != "" and command.strip() == "":
        errors.append("codex.command can't be blank")
    thread_sandbox = _opt_str(codex.get("thread_sandbox"), "codex.thread_sandbox", errors)
    turn_sandbox = _opt_map(
        codex.get("turn_sandbox_policy"), "codex.turn_sandbox_policy", errors)
    settings.codex = CodexSettings(
        command=command,
        approval_policy=_normalize_keys(approval_policy) if isinstance(approval_policy, dict) else approval_policy,
        thread_sandbox=thread_sandbox if thread_sandbox is not None else "workspace-write",
        turn_sandbox_policy=_normalize_keys(turn_sandbox) if isinstance(turn_sandbox, dict) else None,
        turn_timeout_ms=_opt_int(codex.get("turn_timeout_ms"), "codex.turn_timeout_ms", 1, errors) or 3_600_000,
        read_timeout_ms=_opt_int(codex.get("read_timeout_ms"), "codex.read_timeout_ms", 1, errors) or 5_000,
        stall_timeout_ms=_opt_int(codex.get("stall_timeout_ms"), "codex.stall_timeout_ms", 0, errors) or 0,
    )
    if codex.get("stall_timeout_ms") is None:
        settings.codex.stall_timeout_ms = 300_000

    hooks = _section(config, "hooks", errors)
    settings.hooks = HooksSettings(
        after_create=_opt_str(hooks.get("after_create"), "hooks.after_create", errors),
        before_run=_opt_str(hooks.get("before_run"), "hooks.before_run", errors),
        after_run=_opt_str(hooks.get("after_run"), "hooks.after_run", errors),
        before_remove=_opt_str(hooks.get("before_remove"), "hooks.before_remove", errors),
        timeout_ms=_opt_int(hooks.get("timeout_ms"), "hooks.timeout_ms", 1, errors) or 60_000,
    )

    observability = _section(config, "observability", errors)
    dashboard_enabled = _opt_bool(
        observability.get("dashboard_enabled"),
        "observability.dashboard_enabled", errors)
    settings.observability = ObservabilitySettings(
        dashboard_enabled=True if dashboard_enabled is None else dashboard_enabled,
        refresh_ms=_opt_int(observability.get("refresh_ms"), "observability.refresh_ms", 1, errors) or 1_000,
        render_interval_ms=_opt_int(
            observability.get("render_interval_ms"), "observability.render_interval_ms", 1, errors) or 16,
    )

    server = _section(config, "server", errors)
    port = _opt_int(server.get("port"), "server.port", 0, errors)
    host = _opt_str(server.get("host"), "server.host", errors)
    if host is not None and host.strip() == "":
        errors.append("server.host can't be blank")
    settings.server = ServerSettings(
        port=port,
        host=host if host is not None else "127.0.0.1",
    )

    if errors:
        raise _invalid("; ".join(errors))

    _finalize(settings)
    return settings


def _finalize(settings):
    tracker = settings.tracker
    provider = _normalize_keys(tracker.provider) or {}
    tracker.provider = provider
    tracker.endpoint = provider.get("endpoint", tracker.endpoint)
    tracker.project_slug = provider.get("project_slug", tracker.project_slug)

    secret_names = []
    if tracker.kind == "github":
        secret_names = [
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_ENTERPRISE_TOKEN",
        ] + env_reference_names([provider.get("token")])
    tracker.secret_environment_names = list(dict.fromkeys(secret_names))

    if settings.runtime.command in (None, ""):
        settings.runtime.command = (
            settings.codex.command if settings.runtime.kind == "codex" else None
        )


def _normalize_key(key):
    """Elixir ``to_string``: bool → "true"/"false", nil → "nil"."""
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "nil"
    return str(key)


def _normalize_keys(value):
    if isinstance(value, dict):
        return {_normalize_key(key): _normalize_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_keys(item) for item in value]
    return value


def _drop_nil(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned = _drop_nil(item)
            if cleaned is not None:
                result[key] = cleaned
        return result
    if isinstance(value, list):
        return [_drop_nil(item) for item in value]
    return value


def _normalize_state_limits(limits):
    return {
        normalize_issue_state(str(name)): limit for name, limit in limits.items()
    }


def _resolve_path_value(value, default):
    if not isinstance(value, str):
        return default
    name = _env_reference_name(value)
    if name:
        resolved = os.environ.get(name)
        return resolved if resolved else default
    return value if value != "" else default


def resolve_env_setting(value, fallback_env=None):
    """Resolve ``"$ENV_NAME"`` indirection or fall back to an env var."""
    if value is None:
        return _normalize_secret(os.environ.get(fallback_env) if fallback_env else None)
    if isinstance(value, str):
        name = _env_reference_name(value)
        if name:
            resolved = os.environ.get(name)
            if resolved is None:
                resolved = os.environ.get(fallback_env) if fallback_env else None
            return _normalize_secret(resolved)
        return _normalize_secret(value)
    return value


def _normalize_secret(value):
    if isinstance(value, str):
        return value if value != "" else None
    return None


def max_concurrent_agents_for_state(settings, state_name):
    return settings.agent.max_concurrent_agents_by_state.get(
        normalize_issue_state(state_name), settings.agent.max_concurrent_agents
    )
