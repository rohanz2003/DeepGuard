"""Dedicated durable scan worker process.

Run this module separately from the Gunicorn API process.  It uses the same
Firebase project and model configuration, claims Firestore leases atomically,
and recovers processing jobs after their lease expires.
"""

from __future__ import annotations

import logging
import signal
import threading

from app import create_app
from app.jobs import DurableScanQueue


def main() -> None:
    app = create_app()
    queue = app.extensions["scan_jobs"]
    if not isinstance(queue, DurableScanQueue):
        raise RuntimeError("The worker requires the durable Firestore scan queue.")

    worker = app.extensions.get("scan_worker")
    if worker is None:
        worker = queue.create_worker(
            app.extensions["inference"],
            lease_seconds=app.config["SCAN_LEASE_SECONDS"],
            poll_interval_seconds=app.config["SCAN_POLL_INTERVAL_SECONDS"],
        )

    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.getLogger(__name__).info("DeepGuard durable scan worker started")
    worker.run_forever(stop_event)


if __name__ == "__main__":
    main()
