"""Portable production settings for the Gunicorn WSGI process."""

import os


bind = f"0.0.0.0:{os.getenv('PORT', '8080')}"
queue_backend = os.getenv("QUEUE_BACKEND", "local").strip().lower()
redis_configured = bool(
    os.getenv("QUEUE_REDIS_URL", "").strip()
    or os.getenv("REDIS_URL", "").strip()
)
default_workers = "2" if queue_backend == "redis" and redis_configured else "1"
workers = int(os.getenv("WEB_CONCURRENCY", default_workers))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "5"))
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
