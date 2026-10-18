# DeepGuard backend

## Local setup

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
```

Copy `.env.example` to `.env`. Firebase Authentication ID tokens are issued
and refreshed by the frontend Firebase SDK; this API only verifies those
tokens. Set `GOOGLE_APPLICATION_CREDENTIALS` to an external service-account
JSON path for deployed Firebase, or set `USE_FIREBASE_EMULATORS=true` together
with the Authentication, Firestore, and (when exercising the queue locally)
Storage emulator hosts. Emulator mode is rejected when `ENVIRONMENT=production`.

Before using scans, create a **Cloud Firestore database** and a **Cloud Storage
bucket** in the same Firebase project, and ensure the Google Cloud
**Cloud Firestore API** and **Cloud Storage API** are enabled. Set
`FIREBASE_STORAGE_BUCKET` to that bucket. The API health check performs a
read-only Firestore probe; a missing database or unavailable bucket produces
HTTP 503 for scan admission rather than silently losing data.

The API stores only owner-scoped scan metadata and durable job state in
Firestore under `users/{firebase_uid}/scans/{scan_id}`. After synchronous
PCM/WAV validation, raw audio is uploaded to a private temporary Cloud Storage
object with a cleanup marker and `SCAN_RAW_RETENTION_SECONDS` expiry. The
separate worker downloads it only while processing, deletes it after terminal
success/failure, and can retry cleanup from the marker. Raw audio and raw
transcript text are never stored in Firestore, returned, or logged.
`expires_at` is written for a 30-day default metadata retention period and
expired documents are removed when read. Configure a Firestore TTL policy for
eventual metadata cleanup as described in deployment documentation.

## Optional model configuration

Set `MODEL_ARTIFACT_PATH` to a JSON artifact produced for the backend. Relative
paths resolve from `backend/`; the local value `../ai_models/artifacts/baseline.json`
does not depend on the shell working directory. Keep `MODEL_THRESHOLD=0.5` for
the bundled evaluated baseline. The artifact must declare the
supported `deepguard-model` schema (or the `audio_binary_logistic_regression`
artifact type used by the bundled model), version `1`, a model version, and a
versioned feature contract. The backend imports `ai_models.model` and
`ai_models.preprocess` only
after the JSON has passed validation; missing NumPy/model dependencies,
missing files, malformed JSON, and contract mismatches leave model health as
`unavailable` and preserve the existing inconclusive scan response. The model
score is an uncalibrated synthetic-audio indicator: it is mapped to `low`,
`review`, or `high` around the configured threshold, not a probability of fraud.
Model health explicitly returns `research_only: true`.

## Local transcript analysis

`POST /api/scans` accepts optional `transcript` and `transcript_consent=true`
multipart fields. Non-empty transcript text without affirmative consent, or text
over 12,000 characters, returns HTTP 400. The local analyzer in
`backend/app/transcript.py` uses five fixed categories and returns only:

- `scam_status`
- `scam_score`
- `scam_categories`
- `scam_explanation`

Raw transcript text is never stored, returned, or logged. A transcript score is a
transparent category-match heuristic, not a calibrated probability. It is kept
separate from `deepfake_status` and `confidence`; the API never combines the two.
When no transcript is supplied, scam status is `unavailable`.

`POST /api/scans` returns HTTP `202` with a top-level `status: "pending"` after
synchronous WAV/transcript validation, private-object upload, and Firestore
metadata/job admission. The record then transitions through `pending` →
`processing` → `completed` or `failed`.
`updated_at`, `attempt`, `retry_count`, and `max_attempts` are safe lifecycle
fields; failed records expose only a bounded `error_code` and safe
`error_explanation`. A dedicated transient inference exception may retry up to
`SCAN_MAX_ATTEMPTS`; validation and consent errors never enter the worker.

Run `python worker.py` from `backend/` (or the root `Procfile` worker command)
as a separate long-lived process. It claims pending Firestore records with a
transactional lease, recovers `processing` records whose lease expired, and
guards terminal updates with the lease token so duplicate workers cannot
finish the same claim. Only `TransientInferenceError` is retryable; ordinary
inference/storage errors fail safely. The worker command can be configured via
`SCAN_WORKER_COMMAND`, and the API process never runs inference inline.

The limiter is intentionally process-local and bounded. Deployments with more
than one worker or instance must also enforce limits at a shared gateway.
