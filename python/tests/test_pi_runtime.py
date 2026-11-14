"""Pi RPC parity tests: timeout, blocking input, usage and framing."""

import json
import os
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony.agent import pi_runtime  # noqa: E402


class _FakeStdin:
    def __init__(self, records):
        self.records = records
        self.writes = []

    def write(self, payload):
        command = json.loads(payload.decode("utf-8"))
        self.writes.append(command)
        if command.get("type") == "abort":
            self.records.put(pi_runtime._EOF)

    def flush(self):
        pass


class _FakeProcess:
    def __init__(self, records, returncode=None):
        self.stdin = _FakeStdin(records)
        self._returncode = returncode

    def poll(self):
        return self._returncode


def _session(records, timeout_ms=1000, returncode=None):
    return SimpleNamespace(
        process=_FakeProcess(records, returncode=returncode),
        config=SimpleNamespace(turn_timeout_ms=timeout_ms),
        session_id="session-1",
        records=records,
        mcp_token=None,
        workspace="/tmp/workspace",
        live_path=None,
    )


def _record(**fields):
    return (json.dumps(fields) + "\n").encode("utf-8")


class TurnTimeoutTest(unittest.TestCase):
    def test_turn_timeout_is_reset_by_stream_activity(self):
        records = queue.Queue()
        session = _session(records, timeout_ms=150)
        events = []
        started = time.monotonic()

        def feed_activity():
            time.sleep(0.10)
            records.put(_record(
                type="message_update",
                assistantMessageEvent={"type": "text_delta", "delta": "x"}))

        threading.Thread(target=feed_activity, daemon=True).start()

        with self.assertRaises(pi_runtime.TurnError) as caught:
            pi_runtime.run_turn(session, "work", events.append)

        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.reason, "turn_timeout")
        # Without the reset this would fire at 0.15s; activity at 0.10s must
        # extend the silence deadline to roughly 0.25s.
        self.assertGreater(elapsed, 0.20)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(session.process.stdin.writes[0]["type"], "prompt")
        self.assertEqual(
            session.process.stdin.writes[-1]["type"], "abort")

    def test_cancellation_aborts_the_turn(self):
        records = queue.Queue()
        session = _session(records)
        cancel = threading.Event()
        cancel.set()

        with self.assertRaises(pi_runtime.TurnError) as caught:
            pi_runtime.run_turn(
                session, "work", lambda event: None, cancel_event=cancel)

        self.assertEqual(caught.exception.reason, "cancelled")
        self.assertEqual(
            [item["type"] for item in session.process.stdin.writes],
            ["prompt", "abort"])


class TurnEventTest(unittest.TestCase):
    def test_malformed_and_side_output_lines_are_ignored(self):
        records = queue.Queue()
        records.put(b"not json\n")
        records.put(b"[1, 2, 3]\n")
        records.put(_record(type="agent_settled", aborted=False))
        session = _session(records)

        result = pi_runtime.run_turn(
            session, "work", lambda event: None)

        self.assertEqual(result["session_id"], "session-1")

    def test_dialog_ui_request_blocks_the_turn(self):
        records = queue.Queue()
        records.put(_record(
            type="extension_ui_request", id="req-1", method="select",
            title="Allow dangerous command?", options=["Allow", "Block"]))
        records.put(_record(type="agent_settled", aborted=False))
        session = _session(records)
        events = []

        with self.assertRaises(pi_runtime.TurnError) as caught:
            pi_runtime.run_turn(session, "work", events.append)

        self.assertEqual(caught.exception.reason, "turn_input_required")
        self.assertEqual(events[-1]["event"], "turn_input_required")
        payload = events[-1]["payload"]
        self.assertEqual(payload["method"], "extension_ui_request")
        self.assertEqual(payload["params"]["request_id"], "req-1")
        self.assertEqual(payload["params"]["method"], "select")

    def test_fire_and_forget_ui_request_does_not_block(self):
        records = queue.Queue()
        records.put(_record(
            type="extension_ui_request", id="note-1", method="notify",
            message="working"))
        records.put(_record(type="agent_settled", aborted=False))
        session = _session(records)

        result = pi_runtime.run_turn(
            session, "work", lambda event: None)

        self.assertEqual(result["usage"]["total_tokens"], 0)

    def test_assistant_usage_is_accumulated_across_messages(self):
        records = queue.Queue()
        records.put(_record(
            type="message_end",
            message={"role": "assistant",
                     "usage": {"input": 100, "output": 10}}))
        records.put(_record(
            type="message_end",
            message={"role": "assistant",
                     "usage": {"input": 120, "output": 15}}))
        records.put(_record(type="agent_settled", aborted=False))
        session = _session(records)
        events = []

        result = pi_runtime.run_turn(session, "work", events.append)

        self.assertEqual(result["usage"], {
            "input_tokens": 120,
            "output_tokens": 25,
            "total_tokens": 145,
        })
        completed = [e for e in events if e["event"] == "turn_completed"]
        self.assertEqual(completed[-1]["usage"]["total_tokens"], 145)

    def test_tool_execution_failure_is_reported(self):
        records = queue.Queue()
        records.put(_record(
            type="tool_execution_end", toolCallId="call-1",
            toolName="bash", isError=True, durationMs=12))
        records.put(_record(type="agent_settled", aborted=False))
        session = _session(records)
        events = []

        pi_runtime.run_turn(session, "work", events.append)

        failed = [e for e in events if e["event"] == "tool_call_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["details"]["tool"], "bash")
        self.assertTrue(failed[0]["details"]["error"])

    def test_unexpected_exit_reports_process_status(self):
        records = queue.Queue()
        records.put(pi_runtime._EOF)
        session = _session(records, returncode=7)

        with self.assertRaises(pi_runtime.TurnError) as caught:
            pi_runtime.run_turn(session, "work", lambda event: None)

        self.assertEqual(caught.exception.reason, ("pi_exited", 7))


class ReaderLoopTest(unittest.TestCase):
    def test_partial_final_line_is_delivered_without_newline(self):
        class Stream:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            def readline(self):
                return self._chunks.pop(0) if self._chunks else b""

        session = SimpleNamespace(
            process=SimpleNamespace(stdout=Stream([
                b'{"type":"agent_start"}\n',
                b'{"type":"agent_settled","aborted":false}',
            ])),
            live_path=None,
            records=queue.Queue(),
        )

        pi_runtime._reader_loop(session)

        first = session.records.get_nowait()
        second = session.records.get_nowait()
        self.assertEqual(
            pi_runtime._parse(first), {"type": "agent_start"})
        self.assertEqual(
            pi_runtime._parse(second),
            {"type": "agent_settled", "aborted": False})
        self.assertIs(session.records.get_nowait(), pi_runtime._EOF)


class ScriptIsolationTest(unittest.TestCase):
    def test_secret_environment_names_are_unset_before_pi_starts(self):
        config = SimpleNamespace(
            command="pi --mode rpc", model=None, thinking=None, mcp={})

        script = pi_runtime._build_script(
            config, ["LINEAR_API_KEY", "GITHUB_TOKEN"], "session-1")

        self.assertIn(
            "unset LINEAR_API_KEY;unset GITHUB_TOKEN;", script)
        self.assertIn("--session-id 'session-1'", script)
        self.assertTrue(script.startswith("unset "))


if __name__ == "__main__":
    unittest.main()
