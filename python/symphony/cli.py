"""Command-line entry point: ``python -m symphony [--workflow PATH]``.

Starts the workflow store, MCP bridge, orchestrator loop and observability
HTTP API, then blocks until SIGINT/SIGTERM. Mirrors the Elixir service shape:
one process, all state in memory, graceful child cleanup on shutdown.
"""

import argparse
import logging
import os
import signal
import sys

from . import http_api, logging_setup, mcp_bridge, workflow_store
from .orchestrator import Orchestrator
from .workflow import WorkflowError

log = logging.getLogger("symphony.cli")

DEFAULT_WORKFLOW_PATHS = (
    "WORKFLOW.md",
    "deploy/WORKFLOW.md",
    "/opt/symphony-src/deploy/WORKFLOW.md",
    "/root/symphony/WORKFLOW.md",
)


def _resolve_workflow_path(arg):
    if arg:
        return os.path.abspath(arg)
    env = os.environ.get("SYMPHONY_WORKFLOW")
    if env:
        return os.path.abspath(env)
    for candidate in DEFAULT_WORKFLOW_PATHS:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="symphony",
        description="Symphony scheduler (Python implementation).")
    parser.add_argument(
        "--workflow", "-w",
        help="Path to WORKFLOW.md (default: SYMPHONY_WORKFLOW or ./WORKFLOW.md)")
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override server.port from WORKFLOW.md")
    parser.add_argument(
        "--host", default=None,
        help="Override server.host from WORKFLOW.md")
    parser.add_argument(
        "--check", action="store_true",
        help="Validate WORKFLOW.md and exit")
    parser.add_argument(
        "--log-level", default=None,
        help="Log level (default: SYMPHONY_LOG_LEVEL or INFO)")
    args = parser.parse_args(argv)

    logging_setup.setup(args.log_level)

    path = _resolve_workflow_path(args.workflow)
    if path is None:
        log.error("No WORKFLOW.md found; pass --workflow or set SYMPHONY_WORKFLOW")
        return 2

    try:
        store = workflow_store.start_store(path)
    except WorkflowError as error:
        log.error("Failed to load workflow: %s", error.describe())
        return 2

    settings = store.settings()
    if args.check:
        log.info("WORKFLOW.md OK path=%s", path)
        workflow_store.stop_store()
        return 0

    host = args.host or settings.server.host or "127.0.0.1"
    port = args.port if args.port is not None else settings.server.port
    if port is None:
        log.error("server.port not configured and no --port override given")
        workflow_store.stop_store()
        return 2

    mcp_bridge.start()
    orchestrator = Orchestrator(store)
    orchestrator.start()
    server = http_api.start(orchestrator, store, path, host, port)

    stop = {"flag": False}

    def _signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    log.info("Symphony scheduler running workflow=%s api=http://%s:%d",
             path, host, port)
    try:
        while not stop["flag"]:
            signal.pause()
    except (KeyboardInterrupt, AttributeError):
        # Windows lacks signal.pause(); fall back to a sleep loop.
        import time
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        log.info("Shutting down")
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        orchestrator.stop()
        mcp_bridge.stop()
        workflow_store.stop_store()
    return 0


if __name__ == "__main__":
    sys.exit(main())
