"""Logging: one JSON object per line in production (for Docker / a log collector), plain text in development.

Never log secrets: tokens, passwords and message text stay out of logs. The audit trail of who did what
lives in the `events` table; logs are for operations (errors, latency, restarts).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from . import config

_RESERVED = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        out.update({k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(service: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if config.LOG_FORMAT == "json" else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)
    for noisy in ("httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)
