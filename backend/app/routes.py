"""HTTP routes and response contract for the backend API."""

from __future__ import annotations

import uuid
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify, request

from .auth import token_required
from .firebase import FirebaseUnavailable, utc_now
from .transcript import (
    TranscriptValidationError,
    analyze_transcript,
    unavailable_scam_result,
)
from .validation import AudioValidationError, validate_wav


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
    "expires_at",
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


def repository():
    injected = current_app.config.get("SCAN_REPOSITORY")
    if injected is not None:
        return injected
    return current_app.extensions["firebase"].repository


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


def _scan_payload(
    metadata,
    inference,
    scan_id: str,
    created_at: str,
    expires_at: str,
    scam_result: dict | None = None,
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
    return {
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
        "expires_at": expires_at,
    }


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
                    1 for scan in scans if scan.get("status") == "unavailable"
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
        metadata = validate_wav(
            audio,
            current_app.config["MAX_UPLOAD_BYTES"],
            current_app.config["MIN_DURATION_SECONDS"],
            current_app.config["MAX_DURATION_SECONDS"],
        )
    except AudioValidationError as exc:
        return error(str(exc))

    created = utc_now()
    expires = created + timedelta(days=current_app.config["RETENTION_DAYS"])
    inference = current_app.extensions["inference"].analyze(metadata)
    scan = _scan_payload(
        metadata,
        inference,
        str(uuid.uuid4()),
        created.isoformat(),
        expires.isoformat(),
        scam_result,
    )
    try:
        stored = repository().create(g.current_user["id"], scan)
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)
    return jsonify({"scan": _public_scan(stored)}), 201


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


@api.delete("/scans/<scan_id>")
@token_required
def delete_scan(scan_id):
    try:
        deleted = repository().delete(g.current_user["id"], scan_id)
    except FirebaseUnavailable as exc:
        return storage_error(exc)
    except Exception as exc:
        return storage_error(exc)
    if not deleted:
        return error("Scan not found.", 404)
    return jsonify({"deleted": True, "scan_id": scan_id})
