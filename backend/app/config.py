"""Environment-backed application settings.

dotenv is loaded by the app factory before this class is instantiated. Keeping
settings construction explicit prevents Firebase credentials from being read
before local development environment files have been applied.
"""

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
            os.getenv(key)
            for key in (
                "FIREBASE_AUTH_EMULATOR_HOST",
                "FIRESTORE_EMULATOR_HOST",
                "STORAGE_EMULATOR_HOST",
            )
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

        return {
            "ENVIRONMENT": environment,
            "FIREBASE_PROJECT_ID": os.getenv("FIREBASE_PROJECT_ID", "").strip(),
            # Firebase Admin uses this as the default bucket when configured.
            # Keep the alias for deployments that already name it STORAGE_BUCKET.
            "FIREBASE_STORAGE_BUCKET": os.getenv(
                "FIREBASE_STORAGE_BUCKET",
                os.getenv("SCAN_STORAGE_BUCKET", os.getenv("STORAGE_BUCKET", "")),
            ).strip(),
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
            "STORAGE_EMULATOR_HOST": os.getenv("STORAGE_EMULATOR_HOST", "").strip(),
            "MAX_UPLOAD_BYTES": max_upload,
            "MAX_REQUEST_BYTES": max_request,
            "MIN_DURATION_SECONDS": float(os.getenv("MIN_DURATION_SECONDS", "1")),
            "MAX_DURATION_SECONDS": float(os.getenv("MAX_DURATION_SECONDS", "300")),
            "RETENTION_DAYS": int(os.getenv("RETENTION_DAYS", "30")),
            # An empty path deliberately means that inference is unavailable.
            # The API must remain usable when the optional ML stack is absent.
            "MODEL_ARTIFACT_PATH": artifact_path,
            "MODEL_THRESHOLD": threshold,
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
            # Legacy in-process settings remain available for injected test
            # adapters.  Production uses the durable Firestore queue below.
            "SCAN_MAX_WORKERS": int(os.getenv("SCAN_MAX_WORKERS", "2")),
            "SCAN_MAX_QUEUE": int(os.getenv("SCAN_MAX_QUEUE", "8")),
            "SCAN_MAX_ATTEMPTS": int(os.getenv("SCAN_MAX_ATTEMPTS", "2")),
            "SCAN_RAW_RETENTION_SECONDS": int(
                os.getenv(
                    "SCAN_RAW_RETENTION_SECONDS",
                    os.getenv(
                        "RAW_AUDIO_RETENTION_SECONDS",
                        os.getenv("SCAN_OBJECT_RETENTION_SECONDS", "3600"),
                    ),
                )
            ),
            "SCAN_LEASE_SECONDS": int(
                os.getenv(
                    "SCAN_LEASE_SECONDS",
                    os.getenv(
                        "SCAN_LEASE_DURATION_SECONDS",
                        os.getenv("SCAN_LEASE_DURATION", "300"),
                    ),
                )
            ),
            "SCAN_POLL_INTERVAL_SECONDS": float(
                os.getenv(
                    "SCAN_POLL_INTERVAL_SECONDS", os.getenv("SCAN_POLL_INTERVAL", "5")
                )
            ),
            "SCAN_WORKER_COMMAND": os.getenv(
                "SCAN_WORKER_COMMAND", "python -m worker"
            ).strip(),
        }

    @staticmethod
    def validate(settings):
        if settings["ENVIRONMENT"] not in {"development", "test", "production", "prod"}:
            raise ValueError("ENVIRONMENT must be development, test, or production.")
        if settings["ENVIRONMENT"] in {"production", "prod"} and (
            settings["USE_FIREBASE_EMULATORS"]
            or settings["FIREBASE_AUTH_EMULATOR_HOST"]
            or settings["FIRESTORE_EMULATOR_HOST"]
            or settings.get("STORAGE_EMULATOR_HOST")
        ):
            raise ValueError("Firebase emulators cannot be enabled in production.")
        if settings["ENVIRONMENT"] in {"production", "prod"} and not settings.get(
            "FIREBASE_STORAGE_BUCKET"
        ):
            raise ValueError(
                "FIREBASE_STORAGE_BUCKET must be configured in production."
            )
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
        raw_retention = settings.get("SCAN_RAW_RETENTION_SECONDS", 3600)
        lease_seconds = settings.get("SCAN_LEASE_SECONDS", 300)
        poll_interval = settings.get("SCAN_POLL_INTERVAL_SECONDS", 5)
        if not 1 <= raw_retention <= 7 * 24 * 60 * 60:
            raise ValueError("SCAN_RAW_RETENTION_SECONDS must be within 1 second and 7 days.")
        if not 1 <= lease_seconds <= 24 * 60 * 60:
            raise ValueError("SCAN_LEASE_SECONDS must be within 1 second and 24 hours.")
        if not 0.1 <= poll_interval <= 3600:
            raise ValueError("SCAN_POLL_INTERVAL_SECONDS must be between 0.1 and 3600 seconds.")
        if not settings.get("SCAN_WORKER_COMMAND"):
            raise ValueError("SCAN_WORKER_COMMAND must not be empty.")
