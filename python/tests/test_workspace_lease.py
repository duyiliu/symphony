"""Cross-process run ownership and stale Pi recovery tests."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import workspace as workspace_mod  # noqa: E402
from symphony.agent import pi_runtime  # noqa: E402


def _settings(root):
    return SimpleNamespace(
        workspace=SimpleNamespace(root=root),
        hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
    )


def _issue():
    return SimpleNamespace(id="issue-1", identifier="LOC-1")


class WorkspaceRunLeaseTest(unittest.TestCase):
    def test_lease_records_stable_host_process_identity(self):
        with tempfile.TemporaryDirectory(prefix="symphony-lease-identity-") as root:
            lease = workspace_mod.acquire_run_lease(_settings(root), _issue())
            self.assertIsNotNone(lease)
            try:
                self.assertEqual(
                    lease.owner["host_process_identity"],
                    workspace_mod._process_identity(os.getpid()))
                with open(lease.owner_path, "rb") as handle:
                    owner = json.loads(handle.read().decode("utf-8"))
                self.assertEqual(
                    owner["host_process_identity"],
                    lease.owner["host_process_identity"])
            finally:
                lease.release()
            with open(lease.path, "rb") as handle:
                self.assertEqual(handle.read(), b"\0")
            self.assertFalse(os.path.exists(lease.owner_path))

    def test_lease_is_exclusive_and_reusable_after_release(self):
        with tempfile.TemporaryDirectory(prefix="symphony-lease-") as root:
            settings = _settings(root)
            first = workspace_mod.acquire_run_lease(settings, _issue())
            self.assertIsNotNone(first)
            self.assertIsNone(
                workspace_mod.acquire_run_lease(settings, _issue()))
            first.release()
            second = workspace_mod.acquire_run_lease(settings, _issue())
            self.assertIsNotNone(second)
            second.release()

    def test_release_still_removes_sidecar_when_marker_write_fails(self):
        with tempfile.TemporaryDirectory(prefix="symphony-lease-marker-") as root:
            settings = _settings(root)
            first = workspace_mod.acquire_run_lease(settings, _issue())
            self.assertIsNotNone(first)
            owner_path = first.owner_path
            with patch.object(
                    workspace_mod, "_write_run_owner",
                    side_effect=OSError("read-only lease directory")):
                first.release()

            self.assertFalse(os.path.exists(owner_path))
            second = workspace_mod.acquire_run_lease(settings, _issue())
            self.assertIsNotNone(second)
            second.release()

    def test_os_lock_blocks_second_host_process(self):
        with tempfile.TemporaryDirectory(prefix="symphony-lock-host-") as root:
            script = r"""
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, sys.argv[2])
from symphony import workspace

settings = SimpleNamespace(
    workspace=SimpleNamespace(root=sys.argv[1]),
    hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
)
issue = SimpleNamespace(id="issue-1", identifier="LOC-1")
lease = workspace.acquire_run_lease(settings, issue)
if lease is None:
    raise SystemExit(3)
print("ready", flush=True)
time.sleep(60)
"""
            python_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            holder = subprocess.Popen(
                [sys.executable, "-c", script, root, python_root],
                cwd=python_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                self.assertIsNone(
                    workspace_mod.acquire_run_lease(
                        _settings(root), _issue()))
            finally:
                holder.kill()
                holder.communicate(timeout=10)
            lease = workspace_mod.acquire_run_lease(
                _settings(root), _issue())
            self.assertIsNotNone(lease)
            lease.release()

    def test_crashed_half_written_owner_sidecar_is_recovered(self):
        with tempfile.TemporaryDirectory(prefix="symphony-corrupt-owner-") as root:
            script = r"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[2])
from symphony import workspace

settings = SimpleNamespace(
    workspace=SimpleNamespace(root=sys.argv[1]),
    hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
)
issue = SimpleNamespace(id="issue-1", identifier="LOC-1")
lease = workspace.acquire_run_lease(settings, issue)
if lease is None:
    raise SystemExit(3)
with open(lease.owner_path, "wb") as handle:
    handle.write(b'{"host_pid":')
    handle.flush()
    os.fsync(handle.fileno())
with open(lease.owner_path + ".tmp.crashed", "wb") as handle:
    handle.write(b'{"incomplete":')
print(lease.owner_path, flush=True)
os._exit(17)
"""
            python_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            holder = subprocess.Popen(
                [sys.executable, "-c", script, root, python_root],
                cwd=python_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = holder.communicate(timeout=20)
            self.assertEqual(holder.returncode, 17, stderr)
            owner_path = stdout.strip()

            lease = workspace_mod.acquire_run_lease(
                _settings(root), _issue())
            self.assertIsNotNone(lease)
            try:
                with open(lease.owner_path, "rb") as handle:
                    owner = json.loads(handle.read().decode("utf-8"))
                self.assertNotIn("released", owner)
                self.assertEqual(owner["issue_id"], "issue-1")
                self.assertTrue(any(
                    name.startswith(os.path.basename(owner_path) + ".corrupt.")
                    for name in os.listdir(os.path.dirname(owner_path))))
                self.assertFalse(any(
                    name.startswith(os.path.basename(owner_path) + ".tmp.")
                    for name in os.listdir(os.path.dirname(owner_path))))
            finally:
                lease.release()

    def test_live_unverified_process_is_not_reused_or_killed(self):
        owner = {
            "host_pid": None,
            "agent_pid": 200,
            "agent_session_id": "session-1",
        }
        with patch.object(workspace_mod, "_pid_alive", return_value=True), \
                patch.object(workspace_mod, "_process_matches_session",
                             return_value=False), \
                patch.object(workspace_mod,
                             "_kill_verified_process_tree") as kill:
            self.assertFalse(workspace_mod._recover_stale_run_owner(owner))
        kill.assert_not_called()

    def test_reused_host_pid_with_different_identity_is_stale(self):
        owner = {
            "host_pid": 100,
            "host_process_identity": "windows-start-ticks:111",
            "agent_pid": None,
            "agent_session_id": None,
        }
        with patch.object(workspace_mod, "_pid_alive", return_value=True), \
                patch.object(
                    workspace_mod, "_process_identity",
                    return_value="windows-start-ticks:222"):
            self.assertTrue(workspace_mod._recover_stale_run_owner(owner))

    def test_live_host_with_matching_identity_stays_busy(self):
        owner = {
            "host_pid": 100,
            "host_process_identity": "windows-start-ticks:111",
            "agent_pid": None,
            "agent_session_id": None,
        }
        with patch.object(workspace_mod, "_pid_alive", return_value=True), \
                patch.object(
                    workspace_mod, "_process_identity",
                    return_value="windows-start-ticks:111"):
            self.assertFalse(workspace_mod._recover_stale_run_owner(owner))

    def test_reused_agent_pid_is_never_killed(self):
        owner = {
            "host_pid": None,
            "agent_pid": 200,
            "agent_session_id": "session-1",
            "agent_process_identity": "windows-start-ticks:111",
        }
        with patch.object(workspace_mod, "_pid_alive", return_value=True), \
                patch.object(
                    workspace_mod, "_process_identity",
                    return_value="windows-start-ticks:222"), \
                patch.object(
                    workspace_mod, "_find_processes_for_session",
                    return_value=[]), \
                patch.object(
                    workspace_mod, "_kill_verified_process_tree") as kill:
            self.assertTrue(workspace_mod._recover_stale_run_owner(owner))
        kill.assert_not_called()

    def test_unavailable_process_scan_keeps_lease_busy(self):
        owner = {
            "host_pid": 100,
            "agent_pid": None,
            "agent_session_id": "session-1",
        }
        with patch.object(workspace_mod, "_pid_alive", return_value=False), \
                patch.object(workspace_mod, "_find_processes_for_session",
                             return_value=None):
            self.assertFalse(workspace_mod._recover_stale_run_owner(owner))

    def test_crashed_owner_with_matching_live_pi_is_cleaned_before_reacquire(self):
        with tempfile.TemporaryDirectory(prefix="symphony-stale-pi-") as root:
            session_id = "11111111-2222-4333-8444-555555555555"
            script = r"""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[2])
from symphony import workspace

root = sys.argv[1]
session_id = sys.argv[3]
settings = SimpleNamespace(
    workspace=SimpleNamespace(root=root),
    hooks=SimpleNamespace(before_remove=None, timeout_ms=1000),
)
issue = SimpleNamespace(id="issue-1", identifier="LOC-1")
lease = workspace.acquire_run_lease(settings, issue)
if lease is None:
    raise SystemExit(3)
lease.prepare_agent(session_id)
agent = subprocess.Popen([
    sys.executable, "-c", "import time; time.sleep(60)", session_id,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
lease.record_agent(agent.pid, session_id)
print(json.dumps({"agent_pid": agent.pid}), flush=True)
os._exit(17)
"""
            python_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            holder = subprocess.Popen(
                [sys.executable, "-c", script, root, python_root, session_id],
                cwd=python_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = holder.communicate(timeout=20)
            self.assertEqual(holder.returncode, 17, stderr)
            agent_pid = json.loads(stdout.strip())["agent_pid"]
            self.assertTrue(workspace_mod._pid_alive(agent_pid))

            lease = None
            try:
                lease = workspace_mod.acquire_run_lease(
                    _settings(root), _issue())
                self.assertIsNotNone(lease)
                self.assertFalse(workspace_mod._pid_alive(agent_pid))
            finally:
                if lease is not None:
                    lease.release()
                if workspace_mod._pid_alive(agent_pid):
                    workspace_mod._kill_verified_process_tree(agent_pid)


class PiStopTest(unittest.TestCase):
    def test_session_scan_must_be_empty_for_confirmation(self):
        session = pi_runtime.PiSession(
            None, "", SimpleNamespace(), "session-1", None)
        with patch.object(
                workspace_mod, "_find_processes_for_session",
                return_value=[321]):
            self.assertFalse(
                pi_runtime._session_scan_confirmed_gone(session))
        with patch.object(
                workspace_mod, "_find_processes_for_session",
                return_value=[]):
            self.assertTrue(
                pi_runtime._session_scan_confirmed_gone(session))

    def test_stop_session_confirms_real_process_tree_exit(self):
        session_id = str(uuid.uuid4())
        child_script = (
            "import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(60)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            "print(p.pid,flush=True);time.sleep(60)")
        process = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        child_pid = int(process.stdout.readline().strip())
        session = pi_runtime.PiSession(
            process, "", SimpleNamespace(), session_id, None)
        try:
            self.assertTrue(pi_runtime.stop_session(session))
            self.assertIsNotNone(process.poll())
            self.assertFalse(workspace_mod._pid_alive(child_pid))
        finally:
            if process.poll() is None:
                pi_runtime.force_terminate_session(session)
            if workspace_mod._pid_alive(child_pid):
                workspace_mod._kill_verified_process_tree(child_pid)


if __name__ == "__main__":
    unittest.main()
