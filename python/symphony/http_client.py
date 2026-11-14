"""Shared HTTP transport with redirect credential isolation.

The tracker clients use ``urllib`` directly.  Its default redirect handler
keeps every request header, which would forward ``Authorization`` (or
``Private-Token``) to a different origin after a 30x response.  The Elixir
implementations drop credentials on cross-origin redirects and retain them
for same-origin redirects; keep that contract here so every adapter behaves
the same way.
"""

import urllib.parse
import urllib.request


SENSITIVE_HEADERS = frozenset({
    "authorization",
    "private-token",
    "proxy-authorization",
    "cookie",
})


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port)


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects without leaking credentials to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(
            req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if _origin(req.full_url) != _origin(redirected.full_url):
            for container in (redirected.headers, redirected.unredirected_hdrs):
                for name in list(container):
                    if name.lower() in SENSITIVE_HEADERS:
                        del container[name]
        return redirected


_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def urlopen(request, timeout=30):
    """Open ``request`` with the safe redirect policy."""

    return _OPENER.open(request, timeout=timeout)
