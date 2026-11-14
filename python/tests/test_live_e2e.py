"""Live e2e smoke tests: real API calls against configured trackers/SSH.

Each test is skipped by default. Set the corresponding environment variable
to ``1`` (and the provider credentials) to run a real request. These mirror
the upstream ``*_live_e2e_test.exs`` shape: one real read/write round-trip per
adapter, no mocks for the HTTP layer.
"""

import os
import unittest
from types import SimpleNamespace

from symphony.asana import client as asana_client
from symphony.gitlab import client as gitlab_client
from symphony.github import client as github_client
from symphony.jira import client as jira_client
from symphony.ssh import parse_target, ssh_args


def _settings(**overrides):
    base = {
        "kind": "github",
        "active_states": ["open"],
        "terminal_states": ["closed"],
        "required_labels": [],
        "provider": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@unittest.skipUnless(
    os.environ.get("SYMPHONY_LIVE_GITHUB") == "1",
    "set SYMPHONY_LIVE_GITHUB=1 to run a real GitHub read",
)
class GitHubLiveTest(unittest.TestCase):
    def test_fetch_open_issues(self):
        tracker = _settings(provider={
            "repo": os.environ["SYMPHONY_LIVE_GITHUB_REPO"],
            "token": "$GITHUB_TOKEN",
        })
        issues = github_client.fetch_issues_by_states(
            ["open"], tracker)
        self.assertIsInstance(issues, list)


@unittest.skipUnless(
    os.environ.get("SYMPHONY_LIVE_JIRA") == "1",
    "set SYMPHONY_LIVE_JIRA=1 to run a real Jira read",
)
class JiraLiveTest(unittest.TestCase):
    def test_fetch_issues_by_states(self):
        tracker = _settings(
            kind="jira",
            provider={
                "base_url": os.environ["JIRA_BASE_URL"],
                "email": os.environ["JIRA_EMAIL"],
                "api_token": "$JIRA_API_TOKEN",
                "project_key": os.environ["SYMPHONY_LIVE_JIRA_PROJECT_KEY"],
            })
        issues = jira_client.fetch_issues_by_states(
            tracker, tracker.active_states)
        self.assertIsInstance(issues, list)


@unittest.skipUnless(
    os.environ.get("SYMPHONY_LIVE_ASANA") == "1",
    "set SYMPHONY_LIVE_ASANA=1 to run a real Asana read",
)
class AsanaLiveTest(unittest.TestCase):
    def test_fetch_tasks(self):
        tracker = _settings(
            kind="asana",
            provider={
                "api_key": "$ASANA_PAT",
                "project_gid": os.environ["SYMPHONY_LIVE_ASANA_PROJECT_GID"],
            })
        issues = asana_client.fetch_candidate_issues(tracker)
        self.assertIsInstance(issues, list)


@unittest.skipUnless(
    os.environ.get("SYMPHONY_LIVE_GITLAB") == "1",
    "set SYMPHONY_LIVE_GITLAB=1 to run a real GitLab read",
)
class GitLabLiveTest(unittest.TestCase):
    def test_fetch_issues(self):
        tracker = _settings(
            kind="gitlab",
            provider={
                "api_key": "$GITLAB_PAT",
                "project_path": os.environ["SYMPHONY_LIVE_GITLAB_PROJECT"],
            })
        issues = gitlab_client.fetch_candidate_issues(tracker)
        self.assertIsInstance(issues, list)


@unittest.skipUnless(
    os.environ.get("SYMPHONY_LIVE_SSH") == "1",
    "set SYMPHONY_LIVE_SSH=1 to test SSH target parsing + args",
)
class SshLiveTest(unittest.TestCase):
    def test_parse_and_build_args(self):
        parse_target(os.environ["SYMPHONY_LIVE_SSH_TARGET"])
        args = ssh_args(
            os.environ["SYMPHONY_LIVE_SSH_TARGET"], "echo ok")
        self.assertIsInstance(args, list)
        self.assertGreater(len(args), 2)


if __name__ == "__main__":
    unittest.main()
