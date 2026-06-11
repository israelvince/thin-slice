"""Structured JSON logger for the Customer 360 data pipeline.

Usage:
    from demo_repo.infra.logger import get_logger
    logger = get_logger(__name__)
    logger.info("aggregate complete", extra={
        "pipeline_run_id": run_id,
        "customers_processed": 99_441,
        "duration_ms": 1_230,
    })
"""
import json
import logging
import sys
import time
from typing import Any, Dict

_SKIP_ATTRS = frozenset({
    "msg", "args", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName", "processName",
    "process", "name", "levelno", "levelname", "pathname", "filename", "module",
    "message", "taskName",
})


class _StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
            "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
        }
        # Attach any extra={} fields the caller passed
        for key, val in record.__dict__.items():
            if key not in _SKIP_ATTRS and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits one structured JSON object per line to stdout."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_StructuredJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
