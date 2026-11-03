"""Structured JSON logs with secret redaction on every record."""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from app.core.security import redact


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
               "level": record.levelname.lower(), "logger": record.name, "msg": redact(record.getMessage())}
        for k in ("signal_id", "order_id", "position_id", "category"):
            if hasattr(record, k):
                out[k] = str(getattr(record, k))
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out)


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "sqlalchemy.engine", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
