"""Optional, lazy MongoDB GridFS storage for retained and queued audio.

The local scan worker continues to receive validated audio bytes in memory.
The Redis-backed worker uses this store for temporary normalized audio pointers,
while explicit playback retention uses a separate pointer in the scan record.
Constructing the store does not import or connect to MongoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class AudioStoreUnavailable(RuntimeError):
    """Raised when the optional retained-audio store cannot be used."""


class AudioStoreNotFound(LookupError):
    """Raised when a retained-audio object no longer exists."""


class MongoGridFSAudioStore:
    """A lazy GridFS adapter for temporary jobs and retained playback.

    ``pymongo`` and ``gridfs`` are imported only on the first operation.  A
    missing URI/database/bucket therefore has no effect on local ordinary
    uploads, health checks, or application startup. Redis durable jobs require
    the store when they are admitted.

    The small ``upload``/``open``/``delete`` surface is intentionally easy to
    replace with an in-memory fake in API tests.
    """

    def __init__(
        self,
        uri: str = "",
        database: str = "",
        bucket_name: str = "",
        *,
        client_factory: Any | None = None,
    ):
        self._uri = str(uri or "").strip()
        self._database_name = str(database or "").strip()
        self._bucket_name = str(bucket_name or "").strip()
        self._client_factory = client_factory
        self._client = None
        self._bucket = None

    def _gridfs(self):
        if not self._uri or not self._database_name or not self._bucket_name:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is not configured. Set "
                "MONGODB_URI, MONGODB_DATABASE, and MONGODB_GRIDFS_BUCKET."
            )

        if self._bucket is not None:
            return self._bucket

        try:
            from gridfs import GridFS
            from pymongo import MongoClient

            factory = self._client_factory or MongoClient
            # MongoClient is lazy itself; no server selection happens until a
            # retained-audio operation is requested.
            self._client = factory(self._uri, serverSelectionTimeoutMS=3000)
            database = self._client[self._database_name]
            self._bucket = GridFS(database, collection=self._bucket_name)
            return self._bucket
        except AudioStoreUnavailable:
            raise
        except Exception as exc:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc

    @staticmethod
    def _object_id(storage_id: str):
        try:
            from bson import ObjectId

            return ObjectId(str(storage_id))
        except Exception as exc:
            raise AudioStoreNotFound("Retained audio was not found.") from exc

    def upload(
        self,
        payload: bytes,
        *,
        filename: str,
        content_type: str,
        expires_at: datetime,
    ) -> str:
        try:
            storage_id = self._gridfs().put(
                payload,
                filename=filename,
                content_type=content_type,
                metadata={
                    "deepguard_audio": True,
                    "deepguard_expires_at": expires_at.isoformat(),
                },
            )
            return str(storage_id)
        except AudioStoreUnavailable:
            raise
        except Exception as exc:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc

    def open(self, storage_id: str):
        """Return a GridFS stream with ``read``, ``seek``, and ``length``."""

        try:
            return self._gridfs().get(self._object_id(storage_id))
        except AudioStoreUnavailable:
            raise
        except AudioStoreNotFound:
            raise
        except Exception as exc:
            # GridFS uses ``NoFile`` for an object removed by cleanup.  Do not
            # expose driver/provider details to the API caller.
            if type(exc).__name__ in {"NoFile", "NoFileError"}:
                raise AudioStoreNotFound("Retained audio was not found.") from exc
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc

    def download(self, storage_id: str) -> bytes:
        """Read an object in full for simple adapters and smoke checks."""

        stream = self.open(storage_id)
        try:
            return bytes(stream.read())
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def delete(self, storage_id: str) -> None:
        try:
            self._gridfs().delete(self._object_id(storage_id))
        except AudioStoreUnavailable:
            raise
        except AudioStoreNotFound:
            # A deleted/expired object is already in the desired state.
            return
        except Exception as exc:
            if type(exc).__name__ in {"NoFile", "NoFileError"}:
                return
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc

    def delete_expired(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """Delete temporary/retained objects whose metadata expiry has passed."""

        if limit < 1:
            raise ValueError("cleanup limit must be positive")
        current = now or datetime.now(timezone.utc)
        try:
            bucket = self._gridfs()
            collection = getattr(bucket, "_GridFS__files", None)
            if collection is None:
                collection = getattr(bucket, "_files", None)
            if collection is None:
                raise AudioStoreUnavailable("MongoDB GridFS cleanup is unavailable.")
            expired = collection.find(
                {"metadata.deepguard_expires_at": {"$lte": current.isoformat()}}
            ).limit(limit)
            deleted = 0
            for document in expired:
                bucket.delete(document["_id"])
                deleted += 1
            return deleted
        except AudioStoreUnavailable:
            raise
        except Exception as exc:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention cleanup is unavailable."
            ) from exc


# Short alias for integrations that describe the boundary as a Mongo audio
# store rather than spelling out the GridFS implementation.
MongoAudioStore = MongoGridFSAudioStore


__all__ = [
    "AudioStoreNotFound",
    "AudioStoreUnavailable",
    "MongoAudioStore",
    "MongoGridFSAudioStore",
]
