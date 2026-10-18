FROM python:3.12.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    USE_FIREBASE_EMULATORS=false \
    CORS_ORIGINS=

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/app /app/backend/app
COPY backend/run.py /app/backend/run.py
COPY backend/worker.py /app/backend/worker.py
COPY backend/gunicorn.conf.py /app/backend/gunicorn.conf.py
# The optional JSON model loader imports this package when MODEL_ARTIFACT_PATH
# is configured. The artifact itself is supplied separately at runtime.
COPY ai_models /app/ai_models

WORKDIR /app/backend
EXPOSE 8080

CMD ["gunicorn", "--config", "/app/backend/gunicorn.conf.py", "run:app"]
