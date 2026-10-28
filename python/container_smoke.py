"""In-container smoke: exercises the POSIX-only paths that Windows cannot
verify — ``sh -lc`` hooks, receipt store permissions (0700/0600), stray
file/dangling-symlink removal. Not part of the unittest suite.
"""
import os
import stat
import tempfile
from types import SimpleNamespace

from symphony import workspace
from symphony.issue import Issue
from symphony.github import workflow as gh_workflow


def main():
    root = tempfile.mkdtemp(prefix="smoke-ws-")
    settings = SimpleNamespace(
        workspace=SimpleNamespace(root=root),
        hooks=SimpleNamespace(
            after_create="echo hook-ok > marker.txt",
            before_run="echo pre >> marker.txt",
            after_run=None,
            before_remove=None,
            timeout_ms=10000),
        worker=SimpleNamespace(ssh_hosts=[]),
    )
    issue = Issue(id="9", identifier="GH-9")

    # hooks run through `sh -lc` and must succeed in this image.
    path = workspace.create_for_issue(settings, issue)
    marker = open(os.path.join(path, "marker.txt")).read()
    assert "hook-ok" in marker, marker
    workspace.run_before_run(settings, issue)
    marker = open(os.path.join(path, "marker.txt")).read()
    assert "pre" in marker, marker

    # stray file + dangling symlink at root are removed like real dirs.
    stray = os.path.join(root, "stray-file")
    open(stray, "w").write("x")
    dangling = os.path.join(root, "dangling-link")
    os.symlink("/nonexistent-target", dangling)
    assert workspace.remove(settings, stray) is True
    assert workspace.remove(settings, dangling) is True
    assert not os.path.lexists(stray) and not os.path.lexists(dangling)

    # receipt store: 0700 dir, 0600 file, atomic replace.
    receipt = os.path.join(root, "receipts", "9.json")
    gh_workflow._store_record(receipt, {"cycle": "develop:1", "attempts": 1})
    assert gh_workflow._read_record(receipt) == {"cycle": "develop:1",
                                               "attempts": 1}
    dir_mode = stat.S_IMODE(os.stat(os.path.dirname(receipt)).st_mode)
    file_mode = stat.S_IMODE(os.stat(receipt).st_mode)
    assert dir_mode == 0o700, oct(dir_mode)
    assert file_mode == 0o600, oct(file_mode)

    print("container smoke ok:", path)


if __name__ == "__main__":
    main()
