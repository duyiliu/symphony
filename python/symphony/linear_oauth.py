"""Host-only OAuth client-credentials tokens for Linear Agent Sessions.

Tokens stay in memory. Reuse them until shortly before expiry; a 401 can
invalidate the cached token and acquire one replacement. Tracker credentials
and OAuth app credentials deliberately remain separate.
"""

import hashlib
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import resolve_env_setting
from .workflow import WorkflowError


class TokenCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.tokens = {}
        self.retry_at = {}

    @staticmethod
    def _credentials(conf):
        conf = conf or {}
        client_id = resolve_env_setting(
            conf.get("client_id"), "LINEAR_OAUTH_CLIENT_ID")
        secret = resolve_env_setting(
            conf.get("client_secret"), "LINEAR_OAUTH_CLIENT_SECRET")
        endpoint = conf.get("token_url") or "https://api.linear.app/oauth/token"
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise WorkflowError("invalid_linear_oauth_token_url")
        scope = conf.get("scope") or "read,write"
        if not client_id or not secret:
            raise WorkflowError("missing_linear_oauth_credentials")
        key = hashlib.sha256(
            json.dumps([endpoint, client_id, secret, scope]).encode()).hexdigest()
        return key, endpoint, client_id, secret, scope

    def invalidate(self, conf):
        key = self._credentials(conf)[0]
        with self.lock:
            self.tokens.pop(key, None)

    def get(self, conf):
        key, endpoint, client_id, secret, scope = self._credentials(conf)
        with self.lock:
            now = self.clock()
            cached = self.tokens.get(key)
            if cached and cached[1] > now:
                return cached[0]
            if self.retry_at.get(key, 0) > now:
                raise WorkflowError("linear_oauth_backoff")
            request = Request(
                endpoint,
                data=urlencode({
                    "grant_type": "client_credentials",
                    "client_id": client_id, "client_secret": secret,
                    "scope": scope,
                }).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST")
            try:
                with urlopen(request, timeout=10) as response:
                    payload = json.load(response)
                token = payload.get("access_token")
                ttl = payload.get("expires_in")
                if not isinstance(token, str) or not token \
                        or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) \
                        or ttl <= 0 \
                        or str(payload.get("token_type", "Bearer")).lower() != "bearer":
                    raise WorkflowError("invalid_linear_oauth_response")
                self.tokens[key] = (
                    token, self.clock() + ttl - min(300, ttl / 10))
                self.retry_at.pop(key, None)
                return token
            except HTTPError as error:
                try:
                    delay = float(error.headers.get("Retry-After", 60))
                except (TypeError, ValueError):
                    delay = 60
                self.retry_at[key] = self.clock() + max(10, min(delay, 3600))
                raise WorkflowError(
                    "linear_oauth_http_error", {"status": error.code}) from None
            except (URLError, OSError, ValueError, AttributeError):
                self.retry_at[key] = self.clock() + 30
                raise WorkflowError("linear_oauth_request_failed") from None
