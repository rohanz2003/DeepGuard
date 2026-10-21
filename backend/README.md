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
with both emulator hosts for local emulators. Emulator mode is rejected when
`ENVIRONMENT=production`.

Before using scan history, create a **Cloud Firestore database** in the same
Firebase project and ensure the Google Cloud **Cloud Firestore API** is enabled.
The API health check performs a read-only Firestore probe and reports
`storage: "ready"` only after this succeeds. A disabled API or missing
database produces HTTP 503 for scan routes rather than silently losing data.

The API stores only user-scoped scan metadata in Firestore under
`users/{firebase_uid}/scans/{scan_id}`. Raw audio is read into bounded memory
for synchronous WAV validation or bounded MP3 decoding and is handed to the
bounded worker only; it is never stored or logged unless the upload explicitly
includes `retain_audio=true`. Retained bytes are stored in MongoDB GridFS, while
Firestore receives only the private `audio_storage_id` pointer and the safe
retention fields `audio_retained`, `audio_retention_days`, `audio_expires_at`,
and `audio_storage`. `audio_storage_id` is never returned by the API. Set
`MONGODB_URI`, `MONGODB_DATABASE`, and `MONGODB_GRIDFS_BUCKET` before accepting
retained uploads; missing or unavailable MongoDB produces a clear HTTP 503 only
for a retention request. Normal uploads do not require MongoDB. `expires_at` is written
for a 30-day default retention period and expired documents are removed when
read. A separate Firestore TTL policy should be configured operationally for
eventual cleanup.

Retained recordings are available to their owner at
`GET /api/scans/{scan_id}/audio`. The endpoint supports HTTP byte ranges for
audio players and returns no audio for another user's scan. Deleting a scan
deletes its retained GridFS object before removing the Firestore metadata.

Uploads may be PCM WAV or MP3. WAV files keep the strict PCM/container checks.
MP3 files are decoded with the local FFmpeg binary supplied by `imageio-ffmpeg`
when a system `ffmpeg` executable is unavailable. Decoded audio must be between
1 and 300 seconds and is normalized to mono, 16 kHz, signed-16-bit PCM WAV for
inference. If retention is requested, GridFS receives the original upload
bytes and MIME type so MP3 playback remains MP3; the normalized inference WAV
is never included in API responses or Firestore.

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
synchronous WAV/MP3/transcript validation and metadata admission. The record then
transitions through `pending` → `processing` → `completed` or `failed`.
`updated_at`, `attempt`, `retry_count`, and `max_attempts` are safe lifecycle
fields; failed records expose only a bounded `error_code` and safe
`error_explanation`. A dedicated transient inference exception may retry up to
`SCAN_MAX_ATTEMPTS`; validation and consent errors never enter the worker.

The worker is a bounded in-process `ThreadPoolExecutor` with configurable
worker count and queue capacity. It prevents duplicate processing for a scan
within one process and releases raw audio after the worker finishes. This is
not a durable cross-process queue: queued work is lost on process exit and
each Gunicorn process has its own manager. Use a durable queue before relying
on this lifecycle across restarts or multiple processes.

The limiter is intentionally process-local and bounded. Deployments with more
than one worker or instance must also enforce limits at a shared gateway.
