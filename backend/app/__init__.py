from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request as flask_request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from .config import Config
from .firebase import FirebaseServices
from .inference import ModelInferenceService
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
    app.config["MAX_FORM_PARTS"] = 4
    app.extensions["firebase"] = FirebaseServices(app.config)
    app.extensions["inference"] = (
        app.config["INFERENCE_SERVICE"]
        if "INFERENCE_SERVICE" in app.config
        else ModelInferenceService(app.config)
    )
    app.extensions["rate_limiter"] = RateLimiter()
    CORS(
        app,
        resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}},
        methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
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
