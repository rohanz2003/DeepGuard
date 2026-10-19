"""Temporary raw-audio storage boundaries.

The API and worker depend on this small interface rather than importing the
Cloud Storage SDK directly.  Production uses a private Firebase/Cloud Storage
bucket; tests can inject an object with the same ``upload``, ``download`` and
``delete`` methods without contacting Google services.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class StorageUnavailable(RuntimeError):
    """Raised when temporary object storage cannot serve a scan operation."""


class FirebaseObjectStorage:
    """Private short-lived object storage backed by Firebase Admin Storage.

    The class is deliberately lazy.  Constructing an API application must not
    initialize Firebase or contact a bucket until a durable scan is admitted
    or a worker needs an object.  No public URL is generated and ``upload``
    never calls ``make_public``.
    """

    def __init__(self, firebase_services: Any, bucket_name: str = ""):
        self._firebase = firebase_services
        self._bucket_name = str(bucket_name or "").strip()

    def _bucket(self):
        if not self._bucket_name:
            raise StorageUnavailable(
                "Temporary Cloud Storage bucket is not configured."
            )
        try:
            return self._firebase.storage_bucket(self._bucket_name or None)
        except Exception as exc:  # SDK/provider details stay out of responses.
            raise StorageUnavailable("Cloud Storage is unavailable.") from exc

    @staticmethod
    def _metadata(expires_at: datetime) -> dict[str, str]:
        return {
            "deepguard-temporary": "true",
            "deepguard-cleanup": "required",
            "deepguard-expires-at": expires_at.isoformat(),
        }

    def upload(
        self,
        object_name: str,
        payload: bytes,
        *,
        content_type: str,
        expires_at: datetime,
    ) -> str:
        try:
            blob = self._bucket().blob(object_name)
            # Metadata is useful for bucket lifecycle/cleanup tooling.  The
            # object remains private because no ACL or public URL is changed.
            blob.metadata = self._metadata(expires_at)
            # Cloud Storage lifecycle rules can target customTime/age, giving
            # deployment a native backstop in addition to the cleanup marker.
            blob.custom_time = expires_at
            blob.upload_from_string(payload, content_type=content_type)
            return object_name
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable("Cloud Storage is unavailable.") from exc

    def download(self, object_name: str) -> bytes:
        try:
            return bytes(self._bucket().blob(object_name).download_as_bytes())
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable("Cloud Storage is unavailable.") from exc

    def delete(self, object_name: str) -> None:
        try:
            self._bucket().blob(object_name).delete()
        except StorageUnavailable:
            raise
        except Exception as exc:
            # Cleanup is idempotent.  A previous worker or lifecycle rule may
            # already have removed the object, which is a successful outcome.
            if type(exc).__name__ in {"NotFound", "NotFoundError"}:
                return
            raise StorageUnavailable("Cloud Storage is unavailable.") from exc


__all__ = ["FirebaseObjectStorage", "StorageUnavailable"]
