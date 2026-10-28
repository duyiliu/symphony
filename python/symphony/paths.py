"""Filesystem safety helpers (PathSafety equivalent)."""

import os


def canonicalize(path):
    """Resolve ``path`` to an absolute, symlink-free path.

    Mirrors PathSafety.canonicalize: missing trailing segments are kept,
    symlinked ancestors are resolved. Returns the canonical path.
    """
    if not isinstance(path, str) or path == "":
        raise PathSafetyError("invalid path", path)
    try:
        return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    except (OSError, ValueError) as error:
        raise PathSafetyError(str(error), path) from error


class PathSafetyError(Exception):
    def __init__(self, reason, path=None):
        self.reason = reason
        self.path = path
        super().__init__("path_canonicalize_failed path=%s reason=%s" % (path, reason))


def expand_workspace_root(root, workflow_path=None):
    """Resolve the configured workspace root to a canonical absolute path.

    Relative roots resolve against the directory containing ``workflow_path``
    (falling back to the process cwd), matching the Elixir behaviour of
    treating the workflow file as the anchor for relative config paths.
    """
    if not isinstance(root, str) or root.strip() == "":
        raise PathSafetyError("workspace_root_missing", root)
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        anchor = os.path.dirname(os.path.abspath(workflow_path)) \
            if workflow_path else os.getcwd()
        root = os.path.join(anchor, root)
    return canonicalize(root)


def safe_join(root, *parts):
    """Join ``parts`` under ``root`` and verify the result stays inside it."""
    canonical_root = canonicalize(root)
    candidate = canonicalize(os.path.join(canonical_root, *parts))
    if candidate != canonical_root and not candidate.startswith(
            canonical_root + os.sep):
        raise PathSafetyError("path_escape", candidate)
    return candidate
