"""Startup preflight reports fatal errors separately from degraded features."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import config, preflight  # noqa: E402


def _settings(root):
    return config.parse_settings({
        "tracker": {
            "kind": "linear",
            "provider": {
                "project_slug": "project-a",
                "api_key": "token-a",
                "agent_sessions": {"enabled": True, "oauth": {}},
            },
            "active_states": ["Todo"],
            "terminal_states": ["Done"],
        },
        "runtime": {"kind": "pi", "command": "pi --mode rpc"},
        "workspace": {"root": root},
        "server": {"port": 18331},
    })


class PreflightTest(unittest.TestCase):
    def test_missing_optional_capabilities_are_warnings(self):
        with tempfile.TemporaryDirectory() as root:
            settings = _settings(os.path.join(root, "missing"))
            with patch.object(preflight.shutil, "which", return_value=None):
                report = preflight.collect(settings, root)

        self.assertTrue(report["ok"])
        codes = {warning["code"] for warning in report["warnings"]}
        self.assertIn("workspace_root_missing", codes)
        self.assertIn("pi_command_not_found", codes)
        self.assertIn("agent_session_oauth_missing", codes)
        self.assertIn("api_token_not_configured", codes)

    def test_unwritable_workspace_is_fatal(self):
        with tempfile.TemporaryDirectory() as root:
            settings = _settings(root)
            with patch.object(preflight.os, "access", return_value=False):
                report = preflight.collect(settings, root)

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["errors"][0]["code"], "workspace_root_not_writable")

    def test_complete_environment_has_no_errors(self):
        with tempfile.TemporaryDirectory() as root:
            settings = _settings(root)
            settings.server.api_token = "token"
            settings.tracker.provider["agent_sessions"]["oauth"] = {
                "client_id": "$LINEAR_OAUTH_CLIENT_ID",
                "client_secret": "$LINEAR_OAUTH_CLIENT_SECRET",
            }
            with patch.object(
                    preflight.shutil, "which", return_value="pi"), \
                    patch.dict(os.environ, {
                        "LINEAR_OAUTH_CLIENT_ID": "id",
                        "LINEAR_OAUTH_CLIENT_SECRET": "secret",
                    }):
                report = preflight.collect(settings, root)

        self.assertTrue(report["ok"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])


if __name__ == "__main__":
    unittest.main()
