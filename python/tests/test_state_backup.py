"""Durable state backup and restore tests."""

import datetime
import json
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import agent_sessions, state_backup  # noqa: E402


def _settings(root, keep=24, interval_ms=3_600_000):
    workspace_root = os.path.join(root, "workspace")
    sessions_conf = {
        "enabled": True,
        "state_dir": os.path.join(root, "sessions"),
    }
    return SimpleNamespace(
        workspace=SimpleNamespace(root=workspace_root),
        delivery=SimpleNamespace(
            state_dir=os.path.join(root, "delivery")),
        server=SimpleNamespace(
            state_backup_dir=os.path.join(root, "backups"),
            state_backup_interval_ms=interval_ms,
            state_backup_keep=keep),
        tracker=SimpleNamespace(
            kind="linear",
            endpoint="https://api.linear.app/graphql",
            project_slug="project",
            provider={"agent_sessions": sessions_conf},
            secret_environment_names=[]),
    )


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _state_files(settings):
    delivery = os.path.join(settings.delivery.state_dir, "issue-1.json")
    sessions_root = agent_sessions.state_root(settings)
    session = os.path.join(sessions_root, "issue-1.json")
    return delivery, session


class BackupTest(unittest.TestCase):
    def _populate(self, settings):
        delivery, session = _state_files(settings)
        _write(delivery, {"issue_id": "issue-1", "phase": "merged"})
        _write(session, {
            "issue_id": "issue-1", "phase": "running", "pending": []})
        temp = delivery + ".tmp.1.abc"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write("partial")
        owner = os.path.join(
            os.path.dirname(delivery), "issue-1.owner.json")
        _write(owner, {"pid": 1})

    def test_backup_writes_only_durable_json_state(self):
        with tempfile.TemporaryDirectory(prefix="symphony-backup-") as root:
            settings = _settings(root)
            self._populate(settings)

            result = state_backup.backup_once(
                settings,
                now=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))

            self.assertIsNotNone(result)
            self.assertEqual(result["files"], 2)
            with zipfile.ZipFile(result["path"]) as archive:
                names = sorted(archive.namelist())
            self.assertEqual(names, [
                "agent_sessions/issue-1.json",
                "delivery/issue-1.json",
            ])

    def test_backup_returns_none_without_state(self):
        with tempfile.TemporaryDirectory(prefix="symphony-backup-") as root:
            settings = _settings(root)
            self.assertIsNone(state_backup.backup_once(settings))

    def test_backup_prunes_old_archives(self):
        with tempfile.TemporaryDirectory(prefix="symphony-backup-") as root:
            settings = _settings(root, keep=2)
            self._populate(settings)
            for day in range(1, 4):
                state_backup.backup_once(
                    settings,
                    now=datetime.datetime(
                        2026, 1, day, tzinfo=datetime.timezone.utc))

            names = [
                name for name in os.listdir(settings.server.state_backup_dir)
                if name.endswith(".zip")
            ]
            self.assertEqual(len(names), 2)
            self.assertIn("20260102", "".join(names))
            self.assertIn("20260103", "".join(names))

    def test_restore_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="symphony-backup-") as root:
            settings = _settings(root)
            self._populate(settings)
            result = state_backup.backup_once(settings)
            delivery, session = _state_files(settings)
            os.remove(delivery)
            os.remove(session)

            restored = state_backup.restore_backup(
                result["path"], settings)

            self.assertEqual(restored, {
                state_backup.DELIVERY_PREFIX: 1,
                state_backup.SESSION_PREFIX: 1,
            })
            with open(delivery, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["phase"], "merged")
            with open(session, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["phase"], "running")

    def test_restore_rejects_path_traversal_and_bad_json(self):
        with tempfile.TemporaryDirectory(prefix="symphony-backup-") as root:
            settings = _settings(root)
            archive_path = os.path.join(root, "bad.zip")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("delivery/../escape.json", "{}")
            with self.assertRaises(state_backup.StateBackupError):
                state_backup.restore_backup(archive_path, settings)

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("delivery/broken.json", "not-json")
            with self.assertRaises(state_backup.StateBackupError):
                state_backup.restore_backup(archive_path, settings)

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("other/file.json", "{}")
            with self.assertRaises(state_backup.StateBackupError):
                state_backup.restore_backup(archive_path, settings)


class BackupServiceTest(unittest.TestCase):
    def test_service_records_success_and_disabled_state(self):
        with tempfile.TemporaryDirectory(prefix="symphony-backup-") as root:
            settings = _settings(root)
            delivery, session = _state_files(settings)
            _write(delivery, {"issue_id": "issue-1"})
            _write(session, {"issue_id": "issue-1"})

            service = state_backup.StateBackup(settings)
            self.assertTrue(service.backup())
            self.assertTrue(service.snapshot()["enabled"])
            self.assertIsNotNone(service.snapshot()["last_success_at"])
            service.stop()

            disabled = _settings(root, interval_ms=0)
            service = state_backup.StateBackup(disabled)
            self.assertFalse(service.snapshot()["enabled"])
            service.start()
            self.assertFalse(service.snapshot()["thread_alive"])
            service.stop()


if __name__ == "__main__":
    unittest.main()
