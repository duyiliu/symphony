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

from . import (
    http_api, logging_setup, mcp_bridge, preflight, state_backup,
    status_dashboard,
    workflow_store,
)
from .delivery import DeliveryController
from .agent_sessions import Reporter
from .orchestrator import Orchestrator
from .workflow import WorkflowError

log = logging.getLogger("symphony.cli")

DEFAULT_WORKFLOW_PATHS = (
    "WORKFLOW.md",
    "deploy/WORKFLOW.md",
    "/opt/symphony-src/deploy/WORKFLOW.md",
    "/root/symphony/WORKFLOW.md",
)

GUARDRAIL_FLAG = (
    "--i-understand-that-this-will-be-running-without-the-usual-guardrails")


def _acknowledgement_banner():
    lines = [
        "This Symphony implementation is a low key engineering preview.",
        "Pi will run without any guardrails.",
        "Symphony is not a supported product and is presented as-is.",
        "To proceed, start with `%s` CLI argument" % GUARDRAIL_FLAG,
    ]
    width = max(len(line) for line in lines)
    border = "─" * (width + 2)
    content = ["╭%s╮" % border, "│ %s │" % (" " * width)]
    content.extend("│ %s │" % line.ljust(width) for line in lines)
    content.extend(["│ %s │" % (" " * width), "╰%s╯" % border])
    return "\x1b[31m\x1b[1m%s\x1b[0m" % "\n".join(content)


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


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="symphony",
        description="Symphony scheduler (Python implementation).")
    parser.add_argument(
        "--workflow", "-w",
        help="Path to WORKFLOW.md (default: SYMPHONY_WORKFLOW or ./WORKFLOW.md)")
    parser.add_argument(
        "workflow_path", nargs="?", default=None,
        help="Positional WORKFLOW.md path (Elixir CLI compatible)")
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
    parser.add_argument(
        "--log-file", default=None,
        help="Write UTF-8 rotating logs directly (or SYMPHONY_LOG_FILE)")
    parser.add_argument(
        "--logs-root", default=None,
        help="Log root; writes <root>/log/symphony.log (default: cwd)")
    parser.add_argument(
        "--restore-state", default=None, metavar="ARCHIVE",
        help="Restore delivery and Agent Session state from a backup zip, "
             "then exit")
    parser.add_argument(
        GUARDRAIL_FLAG, dest="acknowledge_guardrails", action="store_true",
        help="Required to start the scheduler; confirms unsupervised execution")
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.check and not args.restore_state \
            and not args.acknowledge_guardrails:
        sys.stderr.write(_acknowledgement_banner() + "\n")
        return 1

    logging_setup.setup(
        args.log_level, args.log_file, logs_root=args.logs_root)

    path = _resolve_workflow_path(args.workflow or args.workflow_path)
    if path is None:
        log.error("No WORKFLOW.md found; pass --workflow or set SYMPHONY_WORKFLOW")
        return 2
    if not os.path.isfile(path):
        log.error("Workflow file not found: %s", path)
        return 2

    try:
        store = workflow_store.start_store(path)
    except WorkflowError as error:
        log.error("Failed to load workflow: %s", error.describe())
        return 2

    settings = store.settings()
    report = preflight.collect(settings, os.path.dirname(path))
    for warning in report["warnings"]:
        log.warning("Preflight %s: %s",
                    warning["code"], warning["message"])
    if not report["ok"]:
        for error in report["errors"]:
            log.error("Preflight %s: %s",
                      error["code"], error["message"])
        workflow_store.stop_store()
        return 2
    if args.restore_state:
        try:
            restored = state_backup.restore_backup(
                args.restore_state, settings)
        except state_backup.StateBackupError as error:
            log.error("Failed to restore state: %s", error)
            workflow_store.stop_store()
            return 2
        log.info(
            "State restored delivery=%d agent_sessions=%d",
            restored[state_backup.DELIVERY_PREFIX],
            restored[state_backup.SESSION_PREFIX])
        if not args.check:
            workflow_store.stop_store()
            return 0
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
    reporter = Reporter(store).start()
    orchestrator = Orchestrator(store, session_reporter=reporter)
    orchestrator.start()
    delivery = DeliveryController(store, session_reporter=reporter).start()
    orchestrator.delivery_controller = delivery
    backup = state_backup.StateBackup(store.settings()).start()
    orchestrator.state_backup = backup
    server = http_api.start(orchestrator, store, path, host, port)
    dashboard = status_dashboard.StatusDashboard(
        orchestrator, store.settings(),
        dashboard_url_override=status_dashboard.dashboard_url(
            store.settings(), server.server_address[1]),
    ).start()

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
        if dashboard.enabled:
            dashboard.render_offline()
        dashboard.stop()
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if delivery is not None:
            delivery.stop()
        orchestrator.stop()
        reporter.stop()
        backup.stop()
        mcp_bridge.stop()
        workflow_store.stop_store()
    return 0


if __name__ == "__main__":
    sys.exit(main())
