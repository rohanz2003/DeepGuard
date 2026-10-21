from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request as flask_request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from .config import Config
from .audio_store import MongoGridFSAudioStore
from .firebase import FirebaseServices
from .inference import ModelInferenceService
from .jobs import ScanJobManager
from .rate_limit import RateLimiter
from .request import InMemoryRequest
from .routes import api


def create_app(test_config=None):
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    app = Flask(__name__)
    app.request_class = InMemoryRequest
    app.config.from_mapping(Config.from_env())
    if test_config:
        app.config.update(test_config)
    Config.validate(app.config)

    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_REQUEST_BYTES"]
    app.config["MAX_FORM_MEMORY_SIZE"] = 128 * 1024
    # UploadScan sends audio plus consent, transcript, transcript consent, and
    # retention preference. Keep a small margin for future non-file fields.
    app.config["MAX_FORM_PARTS"] = 8
    app.extensions["firebase"] = FirebaseServices(app.config)
    injected_audio_store = app.config.get("AUDIO_STORE")
    if injected_audio_store is None:
        injected_audio_store = app.config.get("SCAN_AUDIO_STORE")
    if injected_audio_store is None:
        injected_audio_store = app.config.get("MONGO_AUDIO_STORE")
    if injected_audio_store is None:
        injected_audio_store = app.config.get("AUDIO_STORAGE")
    if injected_audio_store is None:
        injected_audio_store = app.config.get("SCAN_AUDIO_STORAGE")
    app.extensions["audio_store"] = (
        injected_audio_store
        if injected_audio_store is not None
        else MongoGridFSAudioStore(
            app.config.get("MONGODB_URI", ""),
            app.config.get("MONGODB_DATABASE", ""),
            app.config.get("MONGODB_GRIDFS_BUCKET", ""),
        )
    )
    app.extensions["audio_storage"] = app.extensions["audio_store"]
    app.extensions["inference"] = (
        app.config["INFERENCE_SERVICE"]
        if "INFERENCE_SERVICE" in app.config
        else ModelInferenceService(app.config)
    )

    def repository_provider():
        injected = app.config.get("SCAN_REPOSITORY")
        if injected is not None:
            return injected
        return app.extensions["firebase"].repository

    injected_manager = app.config.get("SCAN_JOB_MANAGER")
    if injected_manager is None:
        injected_manager = app.config.get("JOB_MANAGER")
    injected_executor = app.config.get("SCAN_JOB_EXECUTOR")
    if injected_executor is None:
        injected_executor = app.config.get("JOB_EXECUTOR")
    app.extensions["scan_jobs"] = (
        injected_manager
        if injected_manager is not None
        else ScanJobManager(
            repository_provider,
            app.extensions["inference"],
            executor=injected_executor,
            max_workers=app.config["SCAN_MAX_WORKERS"],
            max_queue=app.config["SCAN_MAX_QUEUE"],
            max_attempts=app.config["SCAN_MAX_ATTEMPTS"],
        )
    )
    # Keep a descriptive alias for integrations that refer to the boundary as
    # a job manager rather than a scan extension.
    app.extensions["job_manager"] = app.extensions["scan_jobs"]
    app.extensions["rate_limiter"] = RateLimiter()
    CORS(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Range"],
        expose_headers=["Accept-Ranges", "Content-Length", "Content-Range"],
    )

    @app.before_request
    def bound_request():
        if flask_request.content_length and flask_request.content_length > app.config["MAX_REQUEST_BYTES"]:
            return jsonify({"error": "The request is larger than the allowed limit."}), 400

    @app.get("/")
    def root():
        """Give people opening the API host directly a useful response."""
        return jsonify(
            {
                "service": "deepguard-api",
                "status": "ok",
                "message": "DeepGuard API is running. Use /api/health for the health check.",
                "health_url": "/api/health",
            }
        )

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if app.config["ENVIRONMENT"] in {"prod", "production"}:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if flask_request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "The request or audio file exceeds the allowed size or form limits."}), 400

    @app.errorhandler(400)
    def bad_request(_error):
        return jsonify({"error": "The request could not be understood."}), 400

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "Resource not found."}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "Method not allowed."}), 405

    @app.errorhandler(Exception)
    def unexpected_error(_error):
        if isinstance(_error, HTTPException):
            return jsonify({"error": "The request could not be completed."}), _error.code
        return jsonify({"error": "The server could not complete the request."}), 500

    app.register_blueprint(api)

    return app
