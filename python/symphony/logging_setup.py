"""Structured logging for the scheduler service.

Defaults to journald-friendly single-line text on stderr; ``SYMPHONY_LOG_FORMAT=json``
switches to one JSON object per record for aggregation.
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

_REDACT_KEYS = ("token", "authorization", "api_key", "password", "secret")


class _TextFormat(logging.Formatter):
    def format(self, record):
        base = super().format(record)
        if record.exc_info:
            return base
        return base


class _JsonFormat(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup(level=None, log_file=None):
    fmt = os.environ.get("SYMPHONY_LOG_FORMAT", "text")
    handler = logging.StreamHandler(sys.stderr)
    formatter = (
        _JsonFormat() if fmt == "json"
        else _TextFormat("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.setFormatter(formatter)
    handlers = [handler]
    path = log_file or os.environ.get("SYMPHONY_LOG_FILE")
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    root = logging.getLogger()
    previous = root.handlers[:]
    root.handlers[:] = handlers
    for old in previous:
        old.close()
    root.setLevel((level or os.environ.get("SYMPHONY_LOG_LEVEL", "INFO")).upper())
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
