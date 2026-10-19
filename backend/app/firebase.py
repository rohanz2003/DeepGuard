"""Lazy Firebase Admin integration and Firestore scan repository."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    """Stores metadata at users/{uid}/scans/{scan_id}; audio never reaches it."""

    _RAW_CONTENT_FIELDS = frozenset(
        {"transcript", "raw_transcript", "audio", "audio_bytes"}
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
        data["id"] = document.id
        for key in ("created_at", "updated_at", "expires_at"):
            if key in data:
                data[key] = _as_iso(data[key])
        return data

    def create(self, user_id: str, scan: dict) -> dict:
        reference = self._collection(user_id).document(scan["id"])
        stored = self._metadata_only(scan)
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

        stored = self._metadata_only(updates)
        if "updated_at" in stored:
            if isinstance(stored["updated_at"], str):
                stored["updated_at"] = datetime.fromisoformat(stored["updated_at"])
        reference.update(stored)
        updated = reference.get()
        if not updated.exists:
            return None
        return self._public(updated)

    def list(self, user_id: str, now: datetime) -> list[dict]:
        items = []
        for document in self._collection(user_id).stream():
            scan = self._public(document)
            if _parse_time(scan["expires_at"]) <= now:
                document.reference.delete()
                continue
            items.append(scan)
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def get(self, user_id: str, scan_id: str, now: datetime) -> dict | None:
        document = self._collection(user_id).document(scan_id).get()
        if not document.exists:
            return None
        scan = self._public(document)
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


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
                self._app = firebase_admin.initialize_app(
                    credential,
                    {"projectId": self.settings["FIREBASE_PROJECT_ID"]},
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
