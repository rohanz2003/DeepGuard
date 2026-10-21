"""HTTP routes and response contract for the backend API."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request

from .auth import token_required
from .audio_store import AudioStoreNotFound, AudioStoreUnavailable
from .firebase import FirebaseUnavailable, utc_now
from .jobs import JobQueueFull
from .transcript import (
    TranscriptValidationError,
    analyze_transcript,
    unavailable_scam_result,
)
from .validation import AudioValidationError, validate_audio


api = Blueprint("api", __name__, url_prefix="/api")

_PUBLIC_SCAN_FIELDS = (
    "id",
    "filename",
    "mime_type",
    "size_bytes",
    "duration_seconds",
    "status",
    "deepfake_status",
    "scam_status",
    "scam_score",
    "scam_categories",
    "scam_explanation",
    "confidence",
    "model_version",
    "explanation",
    "created_at",
    "updated_at",
    "expires_at",
    "attempt",
    "retry_count",
    "max_attempts",
    "error_code",
    "error_explanation",
    "audio_retained",
    "audio_retention_days",
    "audio_expires_at",
    "audio_storage",
)
_SCAM_FIELDS = (
    "scam_status",
    "scam_score",
    "scam_categories",
    "scam_explanation",
)


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def storage_error(exception: Exception):
    current_app.logger.warning(
        "Firestore operation failed (%s)", type(exception).__name__
    )
    return error(
        "Firestore storage is unavailable. Enable Cloud Firestore for this Firebase project, then try again.",
        503,
    )


def audio_storage_error(exception: Exception):
    current_app.logger.warning(
        "MongoDB audio retention operation failed (%s)", type(exception).__name__
    )
    return error(
        "MongoDB audio retention is unavailable. Configure MONGODB_URI, "
        "MONGODB_DATABASE, and MONGODB_GRIDFS_BUCKET, then try again.",
        503,
    )


def repository():
    injected = current_app.config.get("SCAN_REPOSITORY")
    if injected is not None:
        return injected
    return current_app.extensions["firebase"].repository


def audio_store():
    return current_app.extensions["audio_store"]


def _internal_scan(scan_repository, user_id: str, scan_id: str):
    getter = getattr(scan_repository, "get_internal", None)
    if callable(getter):
        return getter(user_id, scan_id, utc_now())
    return scan_repository.get(user_id, scan_id, utc_now())


def _upload_retained_audio(store: Any, metadata: Any, expires_at) -> str:
    """Upload through the small injectable store boundary."""

    upload = getattr(store, "upload", None)
    if not callable(upload):
        upload = getattr(store, "put", None)
    if not callable(upload):
        raise AudioStoreUnavailable("The audio retention store is not configured.")
    # MP3 inference uses normalized WAV bytes, but retained playback should
    # preserve the user's original compressed upload and MIME type. Older
    # injected metadata objects may only expose ``audio_bytes``.
    payload = getattr(metadata, "original_bytes", None) or metadata.audio_bytes
    try:
        value = upload(
            payload,
            filename=metadata.filename,
            content_type=metadata.mime_type,
            expires_at=expires_at,
        )
    except AudioStoreUnavailable:
        raise
    except Exception as exc:
        raise AudioStoreUnavailable(
            "MongoDB GridFS audio retention is unavailable."
        ) from exc
    if value is None or value == "":
        raise AudioStoreUnavailable("The audio retention store did not return an object id.")
    return str(value)


def _delete_retained_audio(store: Any, storage_id: str) -> None:
    deleter = getattr(store, "delete", None)
    if not callable(deleter):
        raise AudioStoreUnavailable("The audio retention store is not configured.")
    try:
        deleter(storage_id)
    except AudioStoreUnavailable:
        raise
    except Exception as exc:
        raise AudioStoreUnavailable(
            "MongoDB GridFS audio retention is unavailable."
        ) from exc


def _open_retained_audio(store: Any, storage_id: str):
    opener = getattr(store, "open", None)
    if not callable(opener):
        opener = getattr(store, "get", None)
    if callable(opener):
        try:
            return opener(storage_id)
        except AudioStoreNotFound:
            raise
        except AudioStoreUnavailable:
            raise
        except (FileNotFoundError, KeyError) as exc:
            raise AudioStoreNotFound("Retained audio was not found.") from exc
        except Exception as exc:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc

    downloader = getattr(store, "download", None)
    if callable(downloader):
        try:
            payload = downloader(storage_id)
        except AudioStoreNotFound:
            raise
        except AudioStoreUnavailable:
            raise
        except (FileNotFoundError, KeyError) as exc:
            raise AudioStoreNotFound("Retained audio was not found.") from exc
        except Exception as exc:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc
        if payload is None:
            raise AudioStoreNotFound("Retained audio was not found.")
        return payload if hasattr(payload, "read") else bytes(payload)

    raise AudioStoreUnavailable("The audio retention store is not configured.")


def _stream_details(source):
    """Return a stream-like source and its total size."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), len(source)

    length = getattr(source, "length", None)
    if length is None:
        length = getattr(source, "content_length", None)
    if length is None:
        try:
            position = source.tell()
            source.seek(0, 2)
            length = source.tell()
            source.seek(position)
        except Exception:
            # Small injected fakes may only implement read().  The production
            # GridFS stream always exposes length, so this fallback is bounded
            # by the same upload limit as validation.
            payload = source.read(current_app.config["MAX_UPLOAD_BYTES"] + 1)
            if len(payload) > current_app.config["MAX_UPLOAD_BYTES"]:
                raise AudioStoreUnavailable("Retained audio is too large to stream.")
            return bytes(payload), len(payload)
    return source, int(length)


def _parse_audio_range(value: str | None, total: int):
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        return None
    spec = value[6:].strip()
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, total - suffix)
            end = total - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else total - 1
            if start < 0 or end < start or start >= total:
                return None
            end = min(end, total - 1)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return start, end


def _audio_response(source, total: int, scan: dict):
    range_header = request.headers.get("Range")
    requested_range = _parse_audio_range(range_header, total)
    if range_header and requested_range is None:
        close = getattr(source, "close", None)
        if callable(close):
            close()
        response = error("The requested audio range is not satisfiable.", 416)
        response[0].headers["Content-Range"] = f"bytes */{total}"
        return response

    if requested_range is None:
        start, end = 0, total - 1
        status = 200
    else:
        start, end = requested_range
        status = 206

    length = end - start + 1
    if isinstance(source, bytes):
        body = source[start : end + 1]

        def generate_bytes():
            yield body

    else:
        try:
            source.seek(start)
        except Exception as exc:
            raise AudioStoreUnavailable(
                "MongoDB GridFS audio retention is unavailable."
            ) from exc

        def generate_stream():
            remaining = length
            try:
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    close()

        generate_bytes = generate_stream

    response = Response(
        generate_bytes(),
        status=status,
        mimetype=scan.get("mime_type") or "audio/wav",
        direct_passthrough=True,
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    if requested_range is not None:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return response


def _public_scan(scan: dict) -> dict:
    """Return the stable scan contract without raw-content fields.

    Repositories may contain records written by an older version or by a test
    adapter.  Shape every response at the HTTP boundary so POST, list, and
    detail endpoints expose the same derived scam fields and never forward
    transcript/audio content.
    """

    public = {key: scan[key] for key in _PUBLIC_SCAN_FIELDS if key in scan}
    defaults = unavailable_scam_result()
    public["scam_status"] = public.get("scam_status") or defaults["scam_status"]
    public["scam_score"] = public.get("scam_score")
    categories = public.get("scam_categories")
    public["scam_categories"] = (
        list(categories) if isinstance(categories, (list, tuple)) else []
    )
    public["scam_explanation"] = (
        public.get("scam_explanation") or defaults["scam_explanation"]
    )
    return public


def _pending_inference() -> dict:
    return {
        "status": "pending",
        "deepfake_status": "inconclusive",
        "confidence": None,
        "model_version": current_app.config.get("MODEL_VERSION", "not-configured"),
        "explanation": (
            "The upload was validated and queued for bounded in-process voice analysis."
        ),
    }


def _scan_payload(
    metadata,
    inference,
    scan_id: str,
    created_at: str,
    expires_at: str,
    scam_result: dict | None = None,
    *,
    attempt: int = 0,
    retry_count: int = 0,
    max_attempts: int | None = None,
    updated_at: str | None = None,
    audio_retained: bool = False,
    audio_expires_at: str | None = None,
    audio_storage_id: str | None = None,
):
    # Transcript analysis is deliberately kept outside the audio inference
    # result.  This prevents the two independent signals from being combined.
    scam = unavailable_scam_result()
    scam.update(
        {field: inference[field] for field in _SCAM_FIELDS if field in inference}
    )
    if scam_result is not None:
        scam.update(
            {
                field: scam_result[field]
                for field in _SCAM_FIELDS
                if field in scam_result
            }
        )
    payload = {
        "id": scan_id,
        "filename": metadata.filename,
        "mime_type": metadata.mime_type,
        "size_bytes": metadata.size_bytes,
        "duration_seconds": round(metadata.duration_seconds, 2),
        "status": inference["status"],
        "deepfake_status": inference["deepfake_status"],
        **scam,
        "confidence": inference["confidence"],
        "model_version": inference["model_version"],
        "explanation": inference["explanation"],
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "expires_at": expires_at,
        "attempt": attempt,
        "retry_count": retry_count,
        "audio_retained": bool(audio_retained),
        "audio_retention_days": 30,
        "audio_expires_at": audio_expires_at,
        "audio_storage": "mongodb_gridfs" if audio_retained else None,
    }
    if audio_storage_id is not None:
        # This is intentionally a Firestore-only pointer.  The HTTP response
        # whitelist never exposes it.
        payload["audio_storage_id"] = audio_storage_id
    if max_attempts is not None:
        payload["max_attempts"] = max_attempts
    return payload


@api.get("/health")
def health():
    services = current_app.extensions["firebase"]
    injected_ready = current_app.config.get("TOKEN_VERIFIER") is not None and current_app.config.get(
        "SCAN_REPOSITORY"
    ) is not None
    firebase_status = (
        {"status": "ready", "configured": True, "injected": True}
        if injected_ready
        else services.readiness()
    )
    inference = current_app.extensions["inference"]
    model_status = (
        inference.readiness()
        if hasattr(inference, "readiness")
        else {
            "status": "unavailable",
            "version": current_app.config.get("MODEL_VERSION", "not-configured"),
        }
    )
    return jsonify(
        {
            "status": "ok",
            "service": "deepguard-api",
            "firebase": firebase_status,
            "model": model_status,
            "message": (
                "API is ready, but Firestore storage is unavailable."
                if firebase_status.get("status") != "ready"
                else (
                    "API is ready. A configured research inference model is available."
                    if model_status.get("status") == "ready"
                    else "API is ready. A validated inference model has not been configured."
                )
            ),
        }
    )


@api.get("/auth/me")
@token_required
def me():
    return jsonify({"user": g.current_user})


@api.get("/scans")
@token_required
def list_scans():
    try:
        scans = [
            _public_scan(scan)
            for scan in repository().list(g.current_user["id"], utc_now())
        ]
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)

    suspicious = sum(
        1
        for scan in scans
        if scan.get("deepfake_status") in {"high", "review"}
        or scan.get("scam_status") in {"high", "review"}
    )
    return jsonify(
        {
            "scans": scans,
            "stats": {
                "total_scans": len(scans),
                "suspicious_results": suspicious,
                "analyzed_results": sum(
                    1 for scan in scans if scan.get("status") == "completed"
                ),
                "inconclusive_results": sum(
                    1
                    for scan in scans
                    if scan.get("status") == "unavailable"
                    or (
                        scan.get("status") == "completed"
                        and scan.get("deepfake_status") == "inconclusive"
                    )
                ),
            },
        }
    )


@api.post("/scans")
@token_required
def create_scan():
    if not current_app.extensions["rate_limiter"].allow(
        "upload:" + (request.remote_addr or "unknown"),
        current_app.config["UPLOAD_RATE_LIMIT_PER_MINUTE"],
    ):
        return error("Too many uploads. Please try again shortly.", 429)
    if request.form.get("consent") != "true":
        return error("Explicit consent is required before analyzing audio.")

    transcript = request.form.get("transcript")
    try:
        scam_result = analyze_transcript(
            transcript,
            consented=(request.form.get("transcript_consent") == "true"),
        )
    except TranscriptValidationError as exc:
        return error(str(exc))

    audio = request.files.get("audio")
    if audio is None:
        return error("Attach an audio file in the 'audio' field.")

    try:
        metadata = validate_audio(
            audio,
            current_app.config["MAX_UPLOAD_BYTES"],
            current_app.config["MIN_DURATION_SECONDS"],
            current_app.config["MAX_DURATION_SECONDS"],
        )
    except AudioValidationError as exc:
        return error(str(exc))

    created = utc_now()
    expires = created + timedelta(days=current_app.config["RETENTION_DAYS"])
    retain_audio = request.form.get("retain_audio") == "true"
    audio_expires = created + timedelta(days=30) if retain_audio else None
    scan_id = str(uuid.uuid4())
    audio_storage_id = None
    if retain_audio:
        try:
            audio_storage_id = _upload_retained_audio(
                audio_store(), metadata, audio_expires
            )
        except AudioStoreUnavailable as exc:
            return audio_storage_error(exc)

    job_manager = current_app.extensions["scan_jobs"]
    scan = _scan_payload(
        metadata,
        _pending_inference(),
        scan_id,
        created.isoformat(),
        expires.isoformat(),
        scam_result,
        attempt=0,
        retry_count=0,
        max_attempts=getattr(
            job_manager, "max_attempts", current_app.config["SCAN_MAX_ATTEMPTS"]
        ),
        audio_retained=retain_audio,
        audio_expires_at=audio_expires.isoformat() if audio_expires else None,
        audio_storage_id=audio_storage_id,
    )
    try:
        stored = repository().create(g.current_user["id"], scan)
    except FirebaseUnavailable as exc:
        if audio_storage_id is not None:
            try:
                _delete_retained_audio(audio_store(), audio_storage_id)
            except Exception as cleanup_exc:
                current_app.logger.warning(
                    "Could not remove retained audio after Firestore failure (%s)",
                    type(cleanup_exc).__name__,
                )
        return storage_error(exc)
    except Exception as exc:
        if audio_storage_id is not None:
            try:
                _delete_retained_audio(audio_store(), audio_storage_id)
            except Exception as cleanup_exc:
                current_app.logger.warning(
                    "Could not remove retained audio after Firestore failure (%s)",
                    type(cleanup_exc).__name__,
                )
        return storage_error(exc)
    try:
        enqueue = getattr(job_manager, "enqueue", None) or getattr(
            job_manager, "submit"
        )
        if not callable(enqueue):
            raise RuntimeError("The scan job manager is not configured.")
        enqueue(g.current_user["id"], scan["id"], metadata)
    except JobQueueFull:
        # Do not leave an accepted-looking pending record when the bounded
        # worker boundary cannot accept its audio handoff.
        try:
            repository().delete(g.current_user["id"], scan["id"])
        except Exception as exc:
            current_app.logger.warning(
                "Could not remove rejected scan (%s)", type(exc).__name__
            )
        if audio_storage_id is not None:
            try:
                _delete_retained_audio(audio_store(), audio_storage_id)
            except Exception as exc:
                current_app.logger.warning(
                    "Could not remove rejected retained audio (%s)",
                    type(exc).__name__,
                )
        return error("Scan processing is busy. Please try again shortly.", 503)
    except Exception as exc:
        current_app.logger.warning(
            "Could not enqueue scan worker (%s)", type(exc).__name__
        )
        failure_updates = {
            "status": "failed",
            "deepfake_status": "inconclusive",
            "confidence": None,
            "explanation": "The scan could not be queued for processing.",
            "error_code": "queue_unavailable",
            "error_explanation": "The scan worker was unavailable.",
            "attempt": 0,
            "retry_count": 0,
            "max_attempts": scan.get("max_attempts"),
            "updated_at": utc_now().isoformat(),
        }
        failed = dict(stored)
        failed.update(failure_updates)
        try:
            updated = repository().update(
                g.current_user["id"],
                scan["id"],
                failure_updates,
            )
            if updated is not None:
                failed = updated
        except Exception as update_exc:
            current_app.logger.warning(
                "Could not record enqueue failure (%s)", type(update_exc).__name__
            )
        return jsonify({"status": "failed", "scan": _public_scan(failed)}), 503
    # Always return the persisted admission state.  A fast worker may finish
    # before jsonify runs, but POST remains a stable 202/pending contract.
    return jsonify({"status": "pending", "scan": _public_scan(stored)}), 202


@api.get("/scans/<scan_id>")
@token_required
def get_scan(scan_id):
    try:
        scan = repository().get(g.current_user["id"], scan_id, utc_now())
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)
    if scan is None:
        return error("Scan not found.", 404)
    return jsonify({"scan": _public_scan(scan)})


@api.get("/scans/<scan_id>/audio")
@token_required
def get_scan_audio(scan_id):
    try:
        scan = _internal_scan(repository(), g.current_user["id"], scan_id)
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)
    if scan is None:
        return error("Scan not found.", 404)
    if not scan.get("audio_retained"):
        return error("Audio retention was not requested for this scan.", 404)

    expires_at = scan.get("audio_expires_at")
    if expires_at:
        try:
            if _parse_audio_time(expires_at) <= utc_now():
                storage_id = scan.get("audio_storage_id")
                if storage_id:
                    try:
                        _delete_retained_audio(audio_store(), storage_id)
                    except Exception as exc:
                        current_app.logger.warning(
                            "Could not clean up expired retained audio (%s)",
                            type(exc).__name__,
                        )
                return error("The retained audio has expired.", 410)
        except (TypeError, ValueError):
            return error("The retained audio is unavailable.", 410)

    storage_id = scan.get("audio_storage_id")
    if not storage_id or scan.get("audio_storage") != "mongodb_gridfs":
        return error("The retained audio is unavailable.", 404)
    try:
        source = _open_retained_audio(audio_store(), storage_id)
        source, total = _stream_details(source)
        if total <= 0:
            close = getattr(source, "close", None)
            if callable(close):
                close()
            return error("The retained audio is unavailable.", 404)
        return _audio_response(source, total, scan)
    except AudioStoreNotFound as exc:
        current_app.logger.info("Retained audio object was not found (%s)", type(exc).__name__)
        return error("The retained audio is unavailable.", 404)
    except AudioStoreUnavailable as exc:
        return audio_storage_error(exc)


@api.delete("/scans/<scan_id>")
@token_required
def delete_scan(scan_id):
    try:
        existing = _internal_scan(repository(), g.current_user["id"], scan_id)
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)
    if existing is None:
        return error("Scan not found.", 404)

    storage_id = existing.get("audio_storage_id")
    if existing.get("audio_retained") and storage_id:
        try:
            _delete_retained_audio(audio_store(), storage_id)
        except AudioStoreUnavailable as exc:
            return audio_storage_error(exc)
        except Exception as exc:
            return audio_storage_error(exc)
    try:
        deleted = repository().delete(g.current_user["id"], scan_id)
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)
    if not deleted:
        return error("Scan not found.", 404)
    return jsonify({"deleted": True, "scan_id": scan_id})


def _parse_audio_time(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
