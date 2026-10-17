"""Firebase ID-token protection for API routes."""

from functools import wraps

from flask import current_app, g, jsonify, request

from .firebase import FirebaseUnavailable


def token_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        limiter = current_app.extensions["rate_limiter"]
        if not limiter.allow(
            "api:" + (request.remote_addr or "unknown"),
            current_app.config["RATE_LIMIT_PER_MINUTE"],
        ):
            return jsonify({"error": "Too many requests. Please try again shortly."}), 429

        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return jsonify({"error": "Authentication required."}), 401
        token = token.strip()
        if not token:
            return jsonify({"error": "Authentication required."}), 401

        verifier = current_app.config.get("TOKEN_VERIFIER")
        if verifier is None:
            verifier = current_app.extensions["firebase"].verify_token
        try:
            user = verifier(token)
        except FirebaseUnavailable:
            return jsonify({"error": "Authentication service is unavailable."}), 503
        except (ValueError, TypeError, KeyError):
            return jsonify({"error": "Your Firebase session is invalid or expired."}), 401
        except Exception:
            # A verifier outage is intentionally not distinguishable from an
            # invalid token to the client, while the service remains fail-closed.
            return jsonify({"error": "Authentication service is unavailable."}), 503

        if not isinstance(user, dict):
            return jsonify({"error": "Your Firebase session is invalid or expired."}), 401
        user_id = user.get("id") or user.get("uid") or user.get("sub")
        if not user_id:
            return jsonify({"error": "Your Firebase session is invalid or expired."}), 401
        g.current_user = {"id": str(user_id), "email": str(user.get("email") or "")}
        return view(*args, **kwargs)

    return wrapped
