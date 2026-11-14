"""Structured logging for the scheduler service.

Defaults to journald-friendly single-line text on stderr; ``SYMPHONY_LOG_FORMAT=json``
switches to one JSON object per record for aggregation.
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys

_REDACT_KEYS = ("token", "authorization", "api_key", "password", "secret")
_REDACT_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(key) for key in _REDACT_KEYS) + r")\b"
    r"(\s*[:=]\s*)([^\s,;\"'\)\]\}]+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")


def _redact(text):
    if not isinstance(text, str) or not text:
        return text
    text = _BEARER_RE.sub(r"\1 ***", text)
    return _REDACT_RE.sub(r"\1\2***", text)


def default_log_file(logs_root=None):
    """Mirror LogFile.default_log_file/1: ``<root>/log/symphony.log``."""
    return os.path.join(
        os.path.abspath(logs_root or os.getcwd()), "log", "symphony.log")


class _TextFormat(logging.Formatter):
    def format(self, record):
        return _redact(super().format(record))


class _JsonFormat(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = _redact(
                self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup(level=None, log_file=None, logs_root=None):
    fmt = os.environ.get("SYMPHONY_LOG_FORMAT", "text")
    handler = logging.StreamHandler(sys.stderr)
    formatter = (
        _JsonFormat() if fmt == "json"
        else _TextFormat("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.setFormatter(formatter)
    handlers = [handler]
    path = log_file or os.environ.get("SYMPHONY_LOG_FILE") \
        or default_log_file(logs_root)
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
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
