"""Lazy Firebase Admin integration and the durable Firestore scan store."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class FirebaseUnavailable(RuntimeError):
    """Raised when Firebase cannot serve a protected request."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


class FirestoreScanRepository:
    """Stores owner-scoped scan metadata and durable job state.

    Scan documents live at ``users/{uid}/scans/{scan_id}``.  The same document
    is the queue authority: status, attempts, retry timing, and a lease token
    are updated transactionally.  Raw audio is represented only by a private
    temporary Storage object name; raw bytes and transcript text never reach
    Firestore.
    """

    _RAW_CONTENT_FIELDS = frozenset(
        {"transcript", "raw_transcript", "audio", "audio_bytes"}
    )
    _INTERNAL_QUEUE_FIELDS = frozenset(
        {
            "raw_audio_path",
            "raw_audio_expires_at",
            "cleanup_required",
            "next_attempt_at",
            "lease_id",
            "lease_expires_at",
            "worker_id",
        }
    )

    def __init__(self, firestore_client):
        self._client = firestore_client

    def _collection(self, user_id: str):
        return self._client.collection("users").document(user_id).collection("scans")

    def healthcheck(self) -> None:
        """Perform a bounded read without creating a document or user record."""
        next(
            # Firestore reserves resource IDs matching __.*__.
            self._collection("deepguard-healthcheck")
            .limit(1)
            .stream(timeout=3, retry=None),
            None,
        )

    @staticmethod
    def _metadata_only(data: dict) -> dict:
        return {
            key: value
            for key, value in data.items()
            if key not in FirestoreScanRepository._RAW_CONTENT_FIELDS
        }

    @classmethod
    def _public(cls, document) -> dict:
        data = cls._metadata_only(document.to_dict() or {})
        for key in cls._INTERNAL_QUEUE_FIELDS:
            data.pop(key, None)
        data["id"] = document.id
        for key in ("created_at", "updated_at", "expires_at"):
            if key in data:
                data[key] = _as_iso(data[key])
        return data

    @classmethod
    def _internal(cls, document) -> dict:
        """Return worker-only data while still filtering raw content fields."""

        data = cls._metadata_only(document.to_dict() or {})
        data["id"] = document.id
        for key in (
            "created_at",
            "updated_at",
            "expires_at",
            "raw_audio_expires_at",
            "next_attempt_at",
            "lease_expires_at",
        ):
            if key in data and data[key] is not None:
                data[key] = _as_iso(data[key])
        return data

    @staticmethod
    def _stored_value(key: str, value: Any) -> Any:
        if value is None:
            return None
        if key.endswith("_at") and isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @classmethod
    def _stored_fields(cls, data: dict) -> dict:
        return {
            key: cls._stored_value(key, value)
            for key, value in cls._metadata_only(data).items()
        }

    def create(self, user_id: str, scan: dict) -> dict:
        reference = self._collection(user_id).document(scan["id"])
        stored = self._stored_fields(scan)
        stored["created_at"] = datetime.fromisoformat(scan["created_at"])
        stored["updated_at"] = datetime.fromisoformat(
            scan.get("updated_at", scan["created_at"])
        )
        stored["expires_at"] = datetime.fromisoformat(scan["expires_at"])
        reference.set(stored)
        return dict(
            stored,
            created_at=scan["created_at"],
            updated_at=scan.get("updated_at", scan["created_at"]),
            expires_at=scan["expires_at"],
        )

    def update(self, user_id: str, scan_id: str, updates: dict) -> dict | None:
        """Update safe metadata and return the owner-scoped record."""
        reference = self._collection(user_id).document(scan_id)
        document = reference.get()
        if not document.exists:
            return None

        stored = self._stored_fields(updates)
        reference.update(stored)
        updated = reference.get()
        if not updated.exists:
            return None
        return self._public(updated)

    def list(self, user_id: str, now: datetime) -> list[dict]:
        now = _utc_time(now)
        items = []
        for document in self._collection(user_id).stream():
            scan = self._public(document)
            if _parse_time(scan["expires_at"]) <= now:
                document.reference.delete()
                continue
            items.append(scan)
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def get(self, user_id: str, scan_id: str, now: datetime) -> dict | None:
        now = _utc_time(now)
        document = self._collection(user_id).document(scan_id).get()
        if not document.exists:
            return None
        scan = self._public(document)
        if _parse_time(scan["expires_at"]) <= now:
            document.reference.delete()
            return None
        return scan

    def get_internal(self, user_id: str, scan_id: str, now: datetime) -> dict | None:
        """Read a worker record with queue fields, scoped to one owner."""

        now = _utc_time(now)
        document = self._collection(user_id).document(scan_id).get()
        if not document.exists:
            return None
        scan = self._internal(document)
        if _parse_time(scan["expires_at"]) <= now:
            document.reference.delete()
            return None
        return scan

    def delete(self, user_id: str, scan_id: str) -> bool:
        reference = self._collection(user_id).document(scan_id)
        document = reference.get()
        if not document.exists:
            return False
        reference.delete()
        return True

    @staticmethod
    def _owner_from_document(document) -> str | None:
        """Get ``uid`` from a collection-group document reference."""

        reference = getattr(document, "reference", None)
        try:
            owner = reference.parent.parent
            return str(owner.id)
        except Exception:
            path = str(getattr(reference, "path", "")).split("/")
            if len(path) >= 4 and path[-2] == "scans":
                return path[-3]
        return None

    @staticmethod
    def _eligible(data: dict, now: datetime) -> bool:
        expires_at = data.get("expires_at")
        if expires_at is not None and _parse_time(_as_iso(expires_at)) <= now:
            return False
        status = data.get("status")
        if status == "pending":
            next_attempt = data.get("next_attempt_at")
            return next_attempt is None or _parse_time(_as_iso(next_attempt)) <= now
        if status == "processing":
            lease_expires = data.get("lease_expires_at")
            return lease_expires is not None and _parse_time(_as_iso(lease_expires)) <= now
        return False

    def _run_transaction(self, callback):
        """Run a Firestore transaction, with a small injectable seam for tests."""

        run_transaction = getattr(self._client, "run_transaction", None)
        if callable(run_transaction):
            return run_transaction(callback)

        transaction = self._client.transaction()
        try:
            from google.cloud import firestore

            return firestore.transactional(callback)(transaction)
        except ImportError:
            # This branch is useful for tiny local fakes that expose a
            # transaction object but do not install google-cloud-firestore.
            return callback(transaction)

    def _claim_reference(
        self,
        reference,
        user_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> dict | None:
        lease_id = uuid4().hex
        lease_expires = now.timestamp() + float(lease_seconds)
        lease_expires_at = datetime.fromtimestamp(lease_expires, timezone.utc)

        def callback(transaction):
            try:
                snapshot = transaction.get(reference)
            except (AttributeError, TypeError):
                snapshot = reference.get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                return None
            data = self._metadata_only(snapshot.to_dict() or {})
            if not self._eligible(data, now):
                return None
            attempt = int(data.get("attempt") or 0) + 1
            updates = {
                "status": "processing",
                "attempt": attempt,
                "retry_count": max(0, attempt - 1),
                "max_attempts": int(data.get("max_attempts") or 1),
                "lease_id": lease_id,
                "worker_id": str(worker_id),
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            }
            transaction.update(reference, updates)
            claimed = dict(data)
            claimed.update(updates)
            claimed["id"] = snapshot.id
            claimed["user_id"] = str(user_id)
            return self._internal_from_data(claimed)

        return self._run_transaction(callback)

    @classmethod
    def _internal_from_data(cls, data: dict) -> dict:
        result = cls._metadata_only(data)
        for key in (
            "created_at",
            "updated_at",
            "expires_at",
            "raw_audio_expires_at",
            "next_attempt_at",
            "lease_expires_at",
        ):
            if key in result and result[key] is not None:
                result[key] = _as_iso(result[key])
        return result

    def _candidate_documents(self):
        try:
            query = self._client.collection_group("scans")
            stream = getattr(query, "stream", None)
            return list(stream()) if callable(stream) else list(query)
        except AttributeError:
            return []

    def claim_next(
        self,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict | None:
        """Atomically claim one pending or expired-processing scan.

        Candidate discovery is intentionally outside the transaction.  The
        transaction re-reads each candidate and checks its status/lease, so
        concurrent workers cannot claim the same record.
        """

        now = _utc_time(now or utc_now())
        candidates = []
        for document in self._candidate_documents():
            data = self._metadata_only(document.to_dict() or {})
            if self._eligible(data, now):
                candidates.append(
                    (
                        _as_iso(data.get("updated_at") or data.get("created_at") or ""),
                        document,
                        self._owner_from_document(document),
                    )
                )
        candidates.sort(key=lambda item: item[0])
        for _sort_key, document, owner in candidates:
            if not owner:
                continue
            claimed = self._claim_reference(
                document.reference,
                owner,
                worker_id,
                lease_seconds,
                now,
            )
            if claimed is not None:
                return {
                    "user_id": owner,
                    "scan_id": document.id,
                    "lease_id": claimed["lease_id"],
                    "attempt": claimed["attempt"],
                    "record": claimed,
                }
        return None

    def claim(
        self,
        user_id: str,
        scan_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict | None:
        """Owner-scoped variant used by tests and targeted worker adapters."""

        now = _utc_time(now or utc_now())
        document = self._collection(user_id).document(scan_id)
        claimed = self._claim_reference(
            document,
            str(user_id),
            worker_id,
            lease_seconds,
            now,
        )
        if claimed is None:
            return None
        return {
            "user_id": str(user_id),
            "scan_id": str(scan_id),
            "lease_id": claimed["lease_id"],
            "attempt": claimed["attempt"],
            "record": claimed,
        }

    def transition_claim(
        self,
        user_id: str,
        scan_id: str,
        lease_id: str,
        updates: dict,
        now: datetime | None = None,
    ) -> bool:
        """Apply a terminal/retry update only for the current lease token."""

        reference = self._collection(user_id).document(scan_id)
        now = _utc_time(now or utc_now())

        def callback(transaction):
            try:
                snapshot = transaction.get(reference)
            except (AttributeError, TypeError):
                snapshot = reference.get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                return False
            data = snapshot.to_dict() or {}
            if data.get("status") != "processing" or data.get("lease_id") != lease_id:
                return False
            lease_expires = data.get("lease_expires_at")
            if lease_expires is not None and _parse_time(_as_iso(lease_expires)) <= now:
                return False
            stored = self._stored_fields(updates)
            stored.setdefault("updated_at", now)
            transaction.update(reference, stored)
            return True

        return bool(self._run_transaction(callback))

    # Descriptive aliases make the boundary straightforward to use from
    # worker integrations without duplicating the lease-guarded implementation.
    complete_claim = transition_claim
    retry_claim = transition_claim
    fail_claim = transition_claim

    def mark_cleanup_complete(
        self,
        user_id: str,
        scan_id: str,
        object_name: str | None = None,
    ) -> bool:
        """Clear the temporary-object cleanup marker after deletion."""

        reference = self._collection(user_id).document(scan_id)

        def callback(transaction):
            try:
                snapshot = transaction.get(reference)
            except (AttributeError, TypeError):
                snapshot = reference.get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                return False
            data = snapshot.to_dict() or {}
            if data.get("status") not in {"completed", "failed"}:
                return False
            if object_name and data.get("raw_audio_path") not in {None, object_name}:
                return False
            transaction.update(reference, {"cleanup_required": False})
            return True

        return bool(self._run_transaction(callback))

    def list_cleanup_candidates(self, now: datetime | None = None) -> list[dict]:
        """Return terminal records whose temporary object still needs cleanup."""

        now = _utc_time(now or utc_now())
        results = []
        for document in self._candidate_documents():
            data = self._internal(document)
            if data.get("status") not in {"completed", "failed"}:
                continue
            if not data.get("cleanup_required") or not data.get("raw_audio_path"):
                continue
            if "expires_at" in data and _parse_time(data["expires_at"]) <= now:
                # The metadata retention path will remove this document on a
                # normal read; cleanup still gets the object name here.
                pass
            owner = self._owner_from_document(document)
            if owner:
                results.append(
                    {
                        "user_id": owner,
                        "scan_id": document.id,
                        "record": data,
                    }
                )
        return results

    # Friendly aliases for adapters/tests that describe the operation as a
    # pending-job claim rather than a queue-wide next-job claim.
    claim_pending = claim_next
    claim_job = claim
    update_claim = transition_claim


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utc_time(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FirebaseServices:
    """Initializes firebase-admin only when health or a protected route needs it."""

    def __init__(self, settings: dict):
        self.settings = settings
        self._app = None
        self._auth = None
        self._repository = None
        self._failure: str | None = None

    @property
    def configured(self) -> bool:
        if self.settings["USE_FIREBASE_EMULATORS"]:
            return bool(
                self.settings["FIREBASE_PROJECT_ID"]
                and self.settings["FIREBASE_AUTH_EMULATOR_HOST"]
                and self.settings["FIRESTORE_EMULATOR_HOST"]
            )
        credential_file = self.settings["GOOGLE_APPLICATION_CREDENTIALS"]
        return bool(
            self.settings["FIREBASE_PROJECT_ID"]
            and (not credential_file or Path(credential_file).is_file())
        )

    def _ensure_initialized(self) -> None:
        if self._app is not None:
            return
        if not self.configured:
            raise FirebaseUnavailable("Firebase is not configured.")
        try:
            import firebase_admin
            from firebase_admin import credentials

            if self.settings["USE_FIREBASE_EMULATORS"]:
                os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = self.settings[
                    "FIREBASE_AUTH_EMULATOR_HOST"
                ]
                os.environ["FIRESTORE_EMULATOR_HOST"] = self.settings[
                    "FIRESTORE_EMULATOR_HOST"
                ]
                if self.settings.get("STORAGE_EMULATOR_HOST"):
                    os.environ["STORAGE_EMULATOR_HOST"] = self.settings[
                        "STORAGE_EMULATOR_HOST"
                    ]
                credential = credentials.AnonymousCredentials()
            elif self.settings["GOOGLE_APPLICATION_CREDENTIALS"]:
                credential = credentials.Certificate(
                    self.settings["GOOGLE_APPLICATION_CREDENTIALS"]
                )
            else:
                credential = credentials.ApplicationDefault()

            try:
                self._app = firebase_admin.get_app()
            except ValueError:
                options = {"projectId": self.settings["FIREBASE_PROJECT_ID"]}
                if self.settings.get("FIREBASE_STORAGE_BUCKET"):
                    options["storageBucket"] = self.settings["FIREBASE_STORAGE_BUCKET"]
                self._app = firebase_admin.initialize_app(
                    credential,
                    options,
                )
            from firebase_admin import auth, firestore

            self._auth = auth
            self._repository = FirestoreScanRepository(firestore.client(self._app))
        except Exception as exc:  # SDK errors must not expose credential details.
            self._failure = type(exc).__name__
            self._app = None
            raise FirebaseUnavailable("Firebase is unavailable.") from exc

    def readiness(self) -> dict:
        if not self.configured:
            return {"status": "unavailable", "configured": False}
        try:
            self._ensure_initialized()
        except FirebaseUnavailable:
            return {
                "status": "unavailable",
                "configured": True,
                "storage": "unavailable",
            }
        try:
            self._repository.healthcheck()
        except Exception as exc:
            # Keep provider details and project internals out of the response.
            self._failure = type(exc).__name__
            return {
                "status": "unavailable",
                "configured": True,
                "storage": "unavailable",
                "reason": "firestore-unavailable",
            }
        return {
            "status": "ready",
            "configured": True,
            "emulators": bool(self.settings["USE_FIREBASE_EMULATORS"]),
            "storage": "ready",
        }

    def verify_token(self, token: str) -> dict:
        self._ensure_initialized()
        try:
            decoded = self._auth.verify_id_token(token, check_revoked=True)
        except Exception as exc:
            # Callers map all verifier failures to a generic 401.
            raise ValueError("Invalid Firebase ID token.") from exc
        uid = decoded.get("uid") or decoded.get("sub")
        if not uid:
            raise ValueError("Invalid Firebase ID token.")
        return {"id": str(uid), "email": str(decoded.get("email") or "")}

    @property
    def repository(self) -> FirestoreScanRepository:
        self._ensure_initialized()
        return self._repository

    def storage_bucket(self, bucket_name: str | None = None):
        """Return a Firebase Admin Storage bucket lazily.

        Bucket access is kept separate from the Firestore readiness probe so a
        health check remains a bounded metadata check.  The scan admission and
        worker paths surface a generic temporary-storage error if the bucket or
        deployment IAM configuration is unavailable.
        """

        self._ensure_initialized()
        try:
            from firebase_admin import storage

            return storage.bucket(bucket_name, app=self._app)
        except Exception as exc:
            self._failure = type(exc).__name__
            raise FirebaseUnavailable("Firebase Storage is unavailable.") from exc
