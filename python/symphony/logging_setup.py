"""Structured logging for the scheduler service.

Defaults to journald-friendly single-line text on stderr; ``SYMPHONY_LOG_FORMAT=json``
switches to one JSON object per record for aggregation.
"""

import json
import logging
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


def setup(level=None):
    fmt = os.environ.get("SYMPHONY_LOG_FORMAT", "text")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormat() if fmt == "json"
        else _TextFormat("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((level or os.environ.get("SYMPHONY_LOG_LEVEL", "INFO")).upper())
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
