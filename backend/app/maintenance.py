"""Scheduled privacy and retention maintenance commands."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .firebase import FirebaseUnavailable, utc_now


logger = logging.getLogger(__name__)


def cleanup_retained_audio(store: Any, *, now: datetime | None = None, limit: int = 100) -> int:
    """Delete expired GridFS objects through the configured store boundary."""

    cleaner = getattr(store, "delete_expired", None)
    if not callable(cleaner):
        return 0
    return int(cleaner(now=now or utc_now(), limit=limit))


def configure_firestore_ttl(client: Any, *, field: str = "expires_at") -> dict[str, Any]:
    """Return the operational TTL instruction for deployment automation.

    Firestore TTL policies are configured in the Google Cloud control plane;
    the Admin SDK does not safely create them as part of every web startup.
    This function keeps the exact field and collection scope in one place for
    a deployment job or Terraform module.
    """

    if client is None:
        raise FirebaseUnavailable("Firestore client is unavailable.")
    return {
        "status": "configuration_required",
        "collection_group": "scans",
        "field": field,
        "scope": "users/{uid}/scans/{scan_id}",
        "command_hint": "gcloud firestore fields ttls update expires_at --collection-group=scans --enable-ttl",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run DeepGuard retention maintenance")
    parser.add_argument("--cleanup-gridfs", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    if not args.cleanup_gridfs:
        parser.error("select a maintenance operation")
    from . import create_app

    app = create_app()
    with app.app_context():
        try:
            deleted = cleanup_retained_audio(
                app.extensions["audio_store"],
                now=datetime.now(timezone.utc),
                limit=args.limit,
            )
        except Exception:
            logger.exception("retained audio cleanup failed")
            return 2
    print(json.dumps({"status": "ok", "deleted_audio_objects": deleted}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["cleanup_retained_audio", "configure_firestore_ttl"]
