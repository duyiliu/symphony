"""Regression: stopping a pi session must close the pipe handles it opened.

``Popen.wait`` reaps the child but leaves our ``stdin``/``stdout``/``stderr``
handles open, which leaks file descriptors across scheduler runs.
"""

import os
import queue
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.agent import pi_runtime  # noqa: E402


@unittest.skipUnless(hasattr(os, "killpg"), "pi session stop uses process groups")
class SessionCleanupTest(unittest.TestCase):
    def _session(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        session = SimpleNamespace(
            process=process, records=queue.Queue(), reader=None)

        def drain():
            try:
                for _line in iter(process.stdout.readline, b""):
                    pass
            except (OSError, ValueError):
                pass

        session.reader = threading.Thread(target=drain, daemon=True)
        session.reader.start()
        return session

    def test_stop_closes_every_pipe(self):
        session = self._session()
        try:
            pi_runtime.stop_session(session)
            self.assertIsNotNone(session.process.returncode)
            self.assertTrue(session.process.stdin.closed)
            self.assertTrue(session.process.stdout.closed)
            self.assertTrue(session.process.stderr.closed)
            self.assertFalse(session.reader.is_alive())
        finally:
            if session.process.poll() is None:
                session.process.kill()
                session.process.wait(timeout=5)

    def test_stop_twice_is_safe(self):
        session = self._session()
        try:
            pi_runtime.stop_session(session)
            pi_runtime.stop_session(session)
            self.assertTrue(session.process.stdin.closed)
            self.assertTrue(session.process.stdout.closed)
        finally:
            if session.process.poll() is None:
                session.process.kill()
                session.process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
