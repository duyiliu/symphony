import io
import json
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import agent_sessions, config, linear, linear_oauth
from symphony.workflow import WorkflowError
from tests.test_agent_sessions import make_settings


def response(token="oauth-token", ttl=3600):
    return io.BytesIO(json.dumps({
        "access_token": token, "expires_in": ttl, "token_type": "Bearer",
    }).encode())


class OAuthTest(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.cache = linear_oauth.TokenCache(clock=lambda: self.now)
        self.conf = {"client_id": "client", "client_secret": "private-secret"}

    def test_token_cached_and_refreshed_before_expiry(self):
        with patch.object(linear_oauth, "urlopen", side_effect=[
                response("one"), response("two")]) as call:
            self.assertEqual(self.cache.get(self.conf), "one")
            self.assertEqual(self.cache.get(self.conf), "one")
            self.now += 3301
            self.assertEqual(self.cache.get(self.conf), "two")
        self.assertEqual(call.call_count, 2)
        self.assertIn(b"grant_type=client_credentials", call.call_args[0][0].data)

    def test_concurrent_requests_share_one_token(self):
        with patch.object(linear_oauth, "urlopen",
                          side_effect=lambda *_a, **_k: response()) as call:
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _: self.cache.get(self.conf), range(16)))
        self.assertEqual(set(values), {"oauth-token"})
        self.assertEqual(call.call_count, 1)

    def test_http_failure_is_redacted_and_throttled(self):
        error = HTTPError("https://example.test", 429,
                          "private-secret", {"Retry-After": "90"},
                          io.BytesIO(b"private-secret"))
        with patch.object(linear_oauth, "urlopen", side_effect=error) as call:
            with self.assertRaises(WorkflowError) as first:
                self.cache.get(self.conf)
            self.assertNotIn("private-secret", str(first.exception))
            with self.assertRaises(WorkflowError) as second:
                self.cache.get(self.conf)
            self.assertEqual(second.exception.reason, "linear_oauth_backoff")
        self.assertEqual(call.call_count, 1)

    def test_plaintext_token_endpoint_is_rejected(self):
        with self.assertRaises(WorkflowError):
            self.cache.get({**self.conf, "token_url": "http://example.test/token"})

    def test_401_refresh_does_not_change_tracker_token(self):
        cfg = make_settings("unused")
        with patch.object(agent_sessions._tokens, "get", side_effect=["old", "new"]), \
                patch.object(agent_sessions._tokens, "invalidate") as invalidate, \
                patch.object(linear, "graphql", side_effect=[
                    WorkflowError("linear_api_http_error", {"status": 401}),
                    {"data": {"ok": True}},
                ]) as graphql:
            got = agent_sessions._graphql(cfg, "query { viewer { id } }")
        self.assertEqual(got, {"data": {"ok": True}})
        self.assertEqual(cfg.tracker.api_key, "k")
        self.assertEqual(invalidate.call_count, 1)
        self.assertEqual(graphql.call_args.kwargs["authorization"], "Bearer new")

    def test_oauth_environment_names_are_removed_from_workers(self):
        cfg = make_settings("unused")
        conf = cfg.tracker.provider["agent_sessions"]
        conf["oauth"] = {"client_secret": "$CUSTOM_LINEAR_SECRET"}
        raw = {
            "tracker": {"kind": "linear", "provider": cfg.tracker.provider,
                        "active_states": ["Todo"], "terminal_states": ["Done"]}}
        names = config.parse_settings(raw).tracker.secret_environment_names
        for expected in ("CUSTOM_LINEAR_SECRET", "LINEAR_OAUTH_CLIENT_SECRET",
                         "LINEAR_WEBHOOK_SIGNING_SECRET", "LINEAR_OAUTH_ACCESS_TOKEN"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
