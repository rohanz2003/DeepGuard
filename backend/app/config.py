"""Environment-backed application settings.

dotenv is loaded by the app factory before this class is instantiated. Keeping
settings construction explicit prevents Firebase credentials from being read
before local development environment files have been applied.
"""

import math
import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    @classmethod
    def from_env(cls) -> dict:
        environment = os.getenv("ENVIRONMENT", "development").strip().lower()
        use_emulators = _bool("USE_FIREBASE_EMULATORS")
        if environment in {"prod", "production"} and use_emulators:
            raise ValueError("Firebase emulators cannot be enabled in production.")
        if environment in {"prod", "production"} and any(
            os.getenv(key) for key in ("FIREBASE_AUTH_EMULATOR_HOST", "FIRESTORE_EMULATOR_HOST")
        ):
            raise ValueError("Emulator host variables must be unset in production.")

        max_upload = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
        max_request = int(os.getenv("MAX_REQUEST_BYTES", str(max_upload + 512 * 1024)))
        if max_upload <= 0 or max_request < max_upload:
            raise ValueError("MAX_REQUEST_BYTES must be at least MAX_UPLOAD_BYTES.")

        threshold_value = os.getenv("MODEL_THRESHOLD", "").strip()
        threshold = float(threshold_value or "0.5")
        artifact_path = os.getenv("MODEL_ARTIFACT_PATH", "").strip()
        if artifact_path:
            candidate = Path(artifact_path).expanduser()
            if not candidate.is_absolute():
                candidate = Path(__file__).resolve().parents[1] / candidate
            artifact_path = str(candidate.resolve())
        policy_path = os.getenv("MODEL_POLICY_PATH", "").strip()
        if policy_path:
            candidate = Path(policy_path).expanduser()
            if not candidate.is_absolute():
                candidate = Path(__file__).resolve().parents[1] / candidate
            policy_path = str(candidate.resolve())

        queue_redis_url = os.getenv("QUEUE_REDIS_URL", "").strip()
        redis_url = os.getenv("REDIS_URL", "").strip()
        requested_queue_backend = os.getenv("QUEUE_BACKEND", "local").strip().lower()
        requested_rate_limit_backend = os.getenv(
            "RATE_LIMIT_BACKEND", "local"
        ).strip().lower()
        # A single Render web service can run the bounded local worker without
        # Redis or a paid background worker. Keep an explicitly configured
        # Redis deployment durable whenever a connection URL is present, but
        # fail over to the local development-safe path instead of crashing at
        # import time when the optional Redis service has not been provisioned.
        queue_backend = (
            "local"
            if requested_queue_backend == "redis"
            and not (queue_redis_url or redis_url)
            else requested_queue_backend
        )
        rate_limit_backend = (
            "local"
            if requested_rate_limit_backend == "redis" and not redis_url
            else requested_rate_limit_backend
        )

        return {
            "ENVIRONMENT": environment,
            "FIREBASE_PROJECT_ID": os.getenv("FIREBASE_PROJECT_ID", "").strip(),
            "GOOGLE_APPLICATION_CREDENTIALS": os.getenv(
                "GOOGLE_APPLICATION_CREDENTIALS", ""
            ).strip(),
            "USE_FIREBASE_EMULATORS": use_emulators,
            "FIREBASE_AUTH_EMULATOR_HOST": os.getenv(
                "FIREBASE_AUTH_EMULATOR_HOST", ""
            ).strip(),
            "FIRESTORE_EMULATOR_HOST": os.getenv(
                "FIRESTORE_EMULATOR_HOST", ""
            ).strip(),
            # MongoDB is optional.  The lazy audio store reports a clear
            # admission error only when a caller requests audio retention.
            "MONGODB_URI": os.getenv("MONGODB_URI", "").strip(),
            "MONGODB_DATABASE": os.getenv("MONGODB_DATABASE", "deepguard").strip(),
            "MONGODB_GRIDFS_BUCKET": os.getenv(
                "MONGODB_GRIDFS_BUCKET", "audio"
            ).strip(),
            "MAX_UPLOAD_BYTES": max_upload,
            "MAX_REQUEST_BYTES": max_request,
            "MIN_DURATION_SECONDS": float(os.getenv("MIN_DURATION_SECONDS", "1")),
            "MAX_DURATION_SECONDS": float(os.getenv("MAX_DURATION_SECONDS", "300")),
            "RETENTION_DAYS": int(os.getenv("RETENTION_DAYS", "30")),
            # An empty path deliberately means that inference is unavailable.
            # The API must remain usable when the optional ML stack is absent.
            "MODEL_ARTIFACT_PATH": artifact_path,
            "MODEL_POLICY_PATH": policy_path,
            "MODEL_THRESHOLD": threshold,
            "MODEL_FALSE_POSITIVE_COST": float(os.getenv("MODEL_FALSE_POSITIVE_COST", "1")),
            "MODEL_FALSE_NEGATIVE_COST": float(os.getenv("MODEL_FALSE_NEGATIVE_COST", "1")),
            "MODEL_VERSION": "not-configured",
            "CORS_ORIGINS": [
                origin.strip()
                for origin in os.getenv(
                    "CORS_ORIGINS",
                    "http://localhost:5173,http://127.0.0.1:5173",
                ).split(",")
                if origin.strip()
            ],
            "RATE_LIMIT_PER_MINUTE": int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")),
            "UPLOAD_RATE_LIMIT_PER_MINUTE": int(
                os.getenv("UPLOAD_RATE_LIMIT_PER_MINUTE", "10")
            ),
            # Scan processing is deliberately bounded and process-local.  A
            # worker process restart drops queued jobs, so this is not a
            # durable queue configuration.
            "SCAN_MAX_WORKERS": int(os.getenv("SCAN_MAX_WORKERS", "2")),
            "SCAN_MAX_QUEUE": int(os.getenv("SCAN_MAX_QUEUE", "8")),
            "SCAN_MAX_ATTEMPTS": int(os.getenv("SCAN_MAX_ATTEMPTS", "2")),
            "QUEUE_BACKEND": queue_backend,
            "QUEUE_BACKEND_FALLBACK": queue_backend != requested_queue_backend,
            "QUEUE_REDIS_URL": queue_redis_url,
            "QUEUE_NAME": os.getenv("QUEUE_NAME", "deepguard-scans").strip(),
            "QUEUE_RETRY_BACKOFF_SECONDS": float(os.getenv("QUEUE_RETRY_BACKOFF_SECONDS", "2")),
            "REDIS_URL": redis_url,
            "RATE_LIMIT_BACKEND": rate_limit_backend,
            "RATE_LIMIT_BACKEND_FALLBACK": rate_limit_backend != requested_rate_limit_backend,
            "METRICS_ENABLED": _bool("METRICS_ENABLED", True),
            "AUDIO_QUALITY_ENABLED": _bool("AUDIO_QUALITY_ENABLED", True),
            "SEGMENT_ANALYSIS_ENABLED": _bool("SEGMENT_ANALYSIS_ENABLED", True),
            "TRANSCRIPTION_ENABLED": _bool("TRANSCRIPTION_ENABLED", False),
            "TRANSCRIPTION_MODEL_PATH": os.getenv("TRANSCRIPTION_MODEL_PATH", "").strip(),
            "TRANSCRIPTION_DEVICE": os.getenv("TRANSCRIPTION_DEVICE", "cpu").strip(),
            "TRANSCRIPTION_COMPUTE_TYPE": os.getenv("TRANSCRIPTION_COMPUTE_TYPE", "int8").strip(),
            "RETENTION_CLEANUP_BATCH_SIZE": int(os.getenv("RETENTION_CLEANUP_BATCH_SIZE", "100")),
        }

    @staticmethod
    def validate(settings):
        if settings["ENVIRONMENT"] not in {"development", "test", "production", "prod"}:
            raise ValueError("ENVIRONMENT must be development, test, or production.")
        if settings["ENVIRONMENT"] in {"production", "prod"} and (
            settings["USE_FIREBASE_EMULATORS"]
            or settings["FIREBASE_AUTH_EMULATOR_HOST"]
            or settings["FIRESTORE_EMULATOR_HOST"]
        ):
            raise ValueError("Firebase emulators cannot be enabled in production.")
        if not 0 < settings["MAX_UPLOAD_BYTES"] <= 25 * 1024 * 1024:
            raise ValueError("MAX_UPLOAD_BYTES must be at most 25 MiB.")
        if not 0 < settings["MAX_REQUEST_BYTES"] <= 27 * 1024 * 1024:
            raise ValueError("MAX_REQUEST_BYTES must be at most 27 MiB.")
        if settings["MAX_REQUEST_BYTES"] < settings["MAX_UPLOAD_BYTES"]:
            raise ValueError("MAX_REQUEST_BYTES must be at least MAX_UPLOAD_BYTES.")
        if not 1 <= settings["MIN_DURATION_SECONDS"] <= settings["MAX_DURATION_SECONDS"] <= 300:
            raise ValueError("Audio duration bounds must be within 1 to 300 seconds.")
        if not 1 <= settings["RETENTION_DAYS"] <= 365:
            raise ValueError("RETENTION_DAYS must be within 1 to 365 days.")
        if not 0 <= settings["MODEL_THRESHOLD"] <= 1:
            raise ValueError("MODEL_THRESHOLD must be between 0 and 1.")
        if (
            not math.isfinite(settings["MODEL_FALSE_POSITIVE_COST"])
            or not math.isfinite(settings["MODEL_FALSE_NEGATIVE_COST"])
            or settings["MODEL_FALSE_POSITIVE_COST"] < 0
            or settings["MODEL_FALSE_NEGATIVE_COST"] < 0
        ):
            raise ValueError("Model classification costs must be non-negative.")
        if not all(0 < settings[key] <= 10000 for key in (
            "RATE_LIMIT_PER_MINUTE", "UPLOAD_RATE_LIMIT_PER_MINUTE"
        )):
            raise ValueError("Rate limits must be within 1 to 10000 per minute.")
        if not 1 <= settings["SCAN_MAX_WORKERS"] <= 32:
            raise ValueError("SCAN_MAX_WORKERS must be between 1 and 32.")
        if not 0 <= settings["SCAN_MAX_QUEUE"] <= 1000:
            raise ValueError("SCAN_MAX_QUEUE must be between 0 and 1000.")
        if not 1 <= settings["SCAN_MAX_ATTEMPTS"] <= 5:
            raise ValueError("SCAN_MAX_ATTEMPTS must be between 1 and 5.")
        if settings["QUEUE_BACKEND"] not in {"local", "redis"}:
            raise ValueError("QUEUE_BACKEND must be local or redis.")
        if settings["RATE_LIMIT_BACKEND"] not in {"local", "redis"}:
            raise ValueError("RATE_LIMIT_BACKEND must be local or redis.")
        if not math.isfinite(settings["QUEUE_RETRY_BACKOFF_SECONDS"]) or settings["QUEUE_RETRY_BACKOFF_SECONDS"] < 0:
            raise ValueError("QUEUE_RETRY_BACKOFF_SECONDS must be non-negative.")
        if settings["RETENTION_CLEANUP_BATCH_SIZE"] < 1:
            raise ValueError("RETENTION_CLEANUP_BATCH_SIZE must be positive.")
