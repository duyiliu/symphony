"""Close open GitHub PRs for the current branch before workspace removal.

Port of upstream ``mix workspace.before_remove``. Intended for use from the
``before_remove`` workspace hook so stale review PRs don't linger after a
workspace is cleaned up.

Usage:
    python -m symphony.workspace_before_remove
    python -m symphony.workspace_before_remove --branch feature/x --repo o/r
"""

import argparse
import json
import logging
import shutil
import subprocess

log = logging.getLogger("symphony.workspace_before_remove")

DEFAULT_REPO = "openai/symphony"


def _run(cmd, timeout=30):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gh_available():
    return shutil.which("gh") is not None


def _gh_authenticated():
    result = _run(["gh", "auth", "status"], timeout=15)
    return result is not None and result.returncode == 0


def _current_branch(cwd=None):
    result = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    if result is None or result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch and branch != "HEAD" else None


def _open_pr_numbers(repo, branch):
    result = _run([
        "gh", "pr", "list", "--repo", repo, "--head", branch,
        "--state", "open", "--json", "number", "--limit", "50",
    ])
    if result is None or result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    numbers = []
    for row in payload:
        if isinstance(row, dict) and isinstance(
                row.get("number"), int):
            numbers.append(row["number"])
    return numbers


def _close_pr(repo, branch, number):
    result = _run([
        "gh", "pr", "close", str(number), "--repo", repo,
        "--comment",
        "Closing automatically: branch workspace is being removed.",
    ])
    if result is None or result.returncode != 0:
        log.warning(
            "Failed to close PR #%s repo=%s branch=%s", number, repo, branch)


def maybe_close_open_pull_requests(repo, branch):
    if not branch:
        return
    if not (_gh_available() and _gh_authenticated()):
        return
    for number in _open_pr_numbers(repo, branch):
        _close_pr(repo, branch, number)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m symphony.workspace_before_remove",
        description=(
            "Close open GitHub PRs for the current branch before workspace "
            "removal."))
    parser.add_argument("--branch", default=None,
                        help="Branch name (default: current git branch)")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help="GitHub repo owner/name")
    args = parser.parse_args(argv)

    branch = args.branch or _current_branch()
    maybe_close_open_pull_requests(args.repo, branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
