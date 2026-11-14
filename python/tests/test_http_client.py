"""Safe redirect tests shared by all HTTP tracker clients."""

import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from symphony import http_client


class _RecordingServer:
    def __init__(self):
        outer = self
        self.location = "/sink"
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", outer.location)
                    self.end_headers()
                    return
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SafeRedirectTest(unittest.TestCase):
    def setUp(self):
        self.server = _RecordingServer()
        self.addCleanup(self.server.close)

    def _request(self, path="/start"):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.server.port, path),
            headers={
                "Authorization": "Bearer secret",
                "Private-Token": "private",
                "Cookie": "session=secret",
            })
        with http_client.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_direct_request_keeps_credentials(self):
        self._request("/sink")
        _, headers = self.server.requests[-1]
        self.assertEqual(headers.get("Authorization"), "Bearer secret")
        self.assertEqual(headers.get("Private-Token"), "private")

    def test_same_origin_redirect_keeps_credentials(self):
        self.server.location = "/sink"
        self._request()
        _, headers = self.server.requests[-1]
        self.assertEqual(headers.get("Authorization"), "Bearer secret")
        self.assertEqual(headers.get("Private-Token"), "private")
        self.assertEqual(headers.get("Cookie"), "session=secret")

    def test_cross_origin_redirect_drops_credentials(self):
        target = _RecordingServer()
        self.addCleanup(target.close)
        self.server.location = "http://127.0.0.1:%d/sink" % target.port

        self._request()

        _, origin_headers = self.server.requests[0]
        self.assertEqual(
            origin_headers.get("Authorization"), "Bearer secret")
        self.assertTrue(target.requests)
        _, redirected_headers = target.requests[-1]
        self.assertIsNone(redirected_headers.get("Authorization"))
        self.assertIsNone(redirected_headers.get("Private-Token"))
        self.assertIsNone(redirected_headers.get("Cookie"))


if __name__ == "__main__":
    unittest.main()
