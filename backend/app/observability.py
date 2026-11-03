"""Small dependency-free structured logging and metrics boundary."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit one bounded JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "deepguard", None)
        if isinstance(fields, dict):
            payload.update({str(key): value for key, value in fields.items()})
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, default=str)


class Metrics:
    """In-process counters/timers with a stable export contract.

    Production deployments should scrape or replace this boundary with a
    shared metrics backend.  Keeping the interface local makes tests and the
    health endpoint useful without forcing a metrics dependency into the API.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = Counter()
        self._timers: dict[str, list[float]] = {}

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += int(value)

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            values = self._timers.setdefault(key, [])
            values.append(max(0.0, float(seconds)))
            if len(values) > 1000:
                del values[: len(values) - 1000]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timers = {
                key: {
                    "count": len(values),
                    "sum_seconds": round(sum(values), 6),
                    "max_seconds": round(max(values), 6) if values else 0.0,
                }
                for key, values in self._timers.items()
            }
            return {"counters": dict(self._counters), "timers": timers}

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> str:
        suffix = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
        return f"{name}{':' + suffix if suffix else ''}"


def configure_logging(enabled: bool = True) -> None:
    if not enabled:
        return
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def log_event(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.info(message, extra={"deepguard": fields})


__all__ = ["JsonFormatter", "Metrics", "configure_logging", "log_event"]
