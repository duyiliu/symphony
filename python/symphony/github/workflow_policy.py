"""GitHub-only opt-in label routing and least-authority REST tool policy."""

import os
import re

from ..paths import canonicalize, PathSafetyError
from ..util import normalize_label, present_string
from ..workflow import WorkflowError

AGENT_LABEL = "agent"
PAUSE_LABEL = "needs-human"
HIGH_RISK_LABEL = "high-risk"

DEFAULT_REVIEW_REQUIRED_PATHS = [
    "**/*.sql",
    "**/migrations/**",
    ".github/workflows/**",
    "**/.github/workflows/**",
    "deploy/**",
    "**/deploy/**",
    "**/Dockerfile*",
    "**/docker-compose*.yml",
    "**/docker-compose*.yaml",
    "**/*auth*",
    "**/*permission*",
    "**/security/**",
]

DEFAULT_MAX_AUTO_FILES = 100


def config(settings):
    value = getattr(settings, "github_workflow", None)
    return value if isinstance(value, dict) else {}


def enabled(settings):
    return getattr(settings, "kind", None) == "github" and config(settings).get("enabled") is True


def validate(settings):
    cfg = config(settings)
    if "enabled" in cfg and not isinstance(cfg["enabled"], bool):
        raise WorkflowError("invalid_github_workflow_enabled")
    if not enabled(settings):
        return
    required = getattr(settings, "required_labels", []) or []
    if not _absolute_private_path(cfg.get("state_dir")):
        raise WorkflowError("invalid_github_workflow_state_dir")
    if not present_string(cfg.get("base_branch")):
        raise WorkflowError("missing_github_workflow_base_branch")
    max_attempts = cfg.get("max_attempts", 3)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
        raise WorkflowError("invalid_github_workflow_max_attempts")
    if not _risk_config(cfg.get("risk")):
        raise WorkflowError("invalid_github_workflow_risk")
    if any(
        label in (AGENT_LABEL, PAUSE_LABEL, HIGH_RISK_LABEL, "merge-approved")
        for label in required
    ):
        raise WorkflowError("execution_labels_must_not_be_required_labels")


def validate_runtime(settings):
    tracker = settings.tracker
    if not enabled(tracker):
        return
    try:
        state_dir = canonicalize(config(tracker)["state_dir"])
        workspace = canonicalize(settings.workspace.root)
    except PathSafetyError as error:
        raise WorkflowError("invalid_github_workflow_state_dir", str(error))
    if (
        state_dir == workspace
        or state_dir.startswith(workspace + "/")
        or workspace.startswith(state_dir + "/")
    ):
        raise WorkflowError("github_workflow_store_overlaps_workspaces")


def _absolute_private_path(path):
    return isinstance(path, str) and path.startswith("/") and len(
        [segment for segment in path.split("/") if segment]
    ) >= 2


def _risk_config(risk):
    if risk is None:
        return True
    if not isinstance(risk, dict):
        return False
    paths = risk.get("review_required_paths")
    if paths is not None and not (
        isinstance(paths, list) and all(present_string(item) for item in paths)
    ):
        return False
    max_files = risk.get("max_auto_files")
    if max_files is not None and not (
        isinstance(max_files, int) and not isinstance(max_files, bool) and max_files > 0
    ):
        return False
    return all(key in ("review_required_paths", "max_auto_files") for key in risk)


def phase(labels):
    labels = [normalize_label(label) for label in (labels or [])]
    if AGENT_LABEL in labels and PAUSE_LABEL in labels:
        return "invalid"
    if AGENT_LABEL in labels:
        return "develop"
    return "waiting"


def routable(issue, settings):
    return issue.routable(getattr(settings, "required_labels", []) or []) and (
        not enabled(settings) or phase(issue.labels) == "develop"
    )


def same_phase(before, after, settings):
    return not enabled(settings) or phase(before.labels) == phase(after.labels)


def repo(settings):
    """Policy-level repo resolution — unlike client ``settings()`` a ``$VAR``
    reference resolves only against that variable (no fallback chain)."""
    provider = getattr(settings, "provider", None) or {}
    value = provider.get("repo")
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:]) or ""
    if isinstance(value, str):
        return value
    return os.environ.get("GITHUB_REPO") or ""


def pause_label():
    return PAUSE_LABEL


def high_risk_label():
    return HIGH_RISK_LABEL


def review_required_paths(settings):
    paths = config(settings).get("risk", {}).get("review_required_paths") if isinstance(
        config(settings).get("risk"), dict) else None
    if isinstance(paths, list) and paths:
        return paths
    return DEFAULT_REVIEW_REQUIRED_PATHS


def max_auto_files(settings):
    risk = config(settings).get("risk")
    value = risk.get("max_auto_files") if isinstance(risk, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_AUTO_FILES


def max_attempts(settings):
    return config(settings).get("max_attempts", 3)


def allow_request(settings, method, path, body):
    """Least-authority REST allowlist for the ``github_api`` agent tool."""
    if not enabled(settings):
        return
    prefix = "/repos/%s/" % repo(settings)
    in_repo = path.startswith(prefix)
    relative = path[len(prefix):] if in_repo else path
    clean = not any(token in path for token in ("%", "?", "#", "\\", "..", "//"))
    allowed = clean and in_repo and _allowed_endpoint(method, relative, body)
    if not allowed:
        raise WorkflowError("github_workflow_policy_denied")


_GET_RE = re.compile(r"^(issues|pulls|commits|branches|compare|contents|git/trees|actions/runs|actions/jobs)(/|$)")
_COMMENT_RE = re.compile(r"^(issues/\d+/comments|pulls/\d+/comments(?:/\d+/replies)?)$")
_PATCH_RE = re.compile(r"^(?:(?:issues|pulls)/\d+|(?:issues|pulls)/comments/\d+)$")


def _allowed_endpoint(method, relative, body):
    if method == "GET":
        return bool(_GET_RE.match(relative))
    if method == "POST":
        if relative == "pulls":
            return _keys(body, {"title", "body", "head", "base", "draft", "maintainer_can_modify"})
        if _COMMENT_RE.match(relative):
            return _keys(body, {
                "body", "commit_id", "path", "position", "line", "side",
                "start_line", "start_side", "in_reply_to", "subject_type",
            })
        return False
    if method == "PATCH":
        return bool(_PATCH_RE.match(relative)) and _keys(body, {"title", "body"})
    return False


def _keys(body, allowed):
    return isinstance(body, dict) and all(key in allowed for key in body)


def path_matches(patterns, path):
    path = (path or "").lower()
    return any(re.match(glob_regex(pattern), path) for pattern in patterns)


def glob_regex(glob):
    source = "".join(_glob_token(ch) for ch in glob.lower())
    source = source.replace("[^/]*[^/]*", ".*")
    if source.startswith(".*/"):
        source = "(?:.*/)?" + source[3:]
    return "^" + source + "$"


def _glob_token(ch):
    if ch == "*":
        return "[^/]*"
    if ch == "/":
        return "/"
    if ch == "?":
        return "[^/]"
    return re.escape(ch)
