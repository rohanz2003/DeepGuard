"""Dedicated worker entry point for ``QUEUE_BACKEND=redis``."""

from __future__ import annotations

import logging
import os
import signal
import threading

from . import create_app
from .jobs import RedisScanJobManager


def main() -> int:
    app = create_app()
    manager = app.extensions.get("scan_jobs")
    if not isinstance(manager, RedisScanJobManager):
        raise RuntimeError("QUEUE_BACKEND must be redis when starting the durable worker")
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_args: stop_event.set())
    recovered = manager.recover_inflight()
    logging.getLogger(__name__).info("DeepGuard durable scan worker started; recovered=%s", recovered)
    manager.run_forever(
        timeout=int(os.getenv("QUEUE_BLOCK_TIMEOUT_SECONDS", "5")),
        stop_event=stop_event,
    )
    manager.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
