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
for a retention request when using the local queue. Normal local-queue uploads do not require MongoDB. Redis durable-queue uploads do require GridFS because the normalized audio is stored temporarily for the dedicated worker. `expires_at` is written
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

An optional `MODEL_POLICY_PATH` points to a separately generated calibration
policy. The API keeps `synthetic_audio_score` (raw) and `calibrated_score`
(policy output) separate, records `score_type`, and applies the policy's
cost-selected threshold only after the policy model version matches the loaded
artifact. Both values remain review evidence, never fraud probabilities.

## Local transcript analysis

`POST /api/scans` accepts optional `transcript` and `transcript_consent=true`
multipart fields. Non-empty transcript text without affirmative consent, or text
over 12,000 characters, returns HTTP 400. The local analyzer in
`backend/app/transcript.py` uses five fixed categories with English, Hindi, and
Spanish rule packs and returns only:

- `scam_status`
- `scam_score`
- `scam_categories`
- `scam_explanation`

Raw transcript text is never stored, returned, or logged. A transcript score is a
transparent category-match heuristic, not a calibrated probability. It is kept
separate from `deepfake_status` and `confidence`; the API never combines the two.
When no transcript is supplied, scam status is `unavailable`.

The optional `TRANSCRIPTION_ENABLED=true` path uses a local Whisper-compatible
model selected by `TRANSCRIPTION_MODEL_PATH`. It requires explicit frontend
consent, never calls a remote speech API, and passes transient text directly to
the local analyzer without storing it. Install and validate the optional
`faster-whisper` dependency separately; it is intentionally not part of the
default API image because local model size and hardware vary.

`POST /api/scans` returns HTTP `202` with a top-level `status: "pending"` after
synchronous WAV/MP3/transcript validation and metadata admission. The record then
transitions through `pending` → `processing` → `completed` or `failed`.
`updated_at`, `attempt`, `retry_count`, and `max_attempts` are safe lifecycle
fields; failed records expose only a bounded `error_code` and safe
`error_explanation`. A dedicated transient inference exception may retry up to
`SCAN_MAX_ATTEMPTS`; validation and consent errors never enter the worker.

The worker is a bounded in-process `ThreadPoolExecutor` with configurable
worker count and queue capacity for local development. Production can use the
Redis durable queue described below; it prevents duplicate admission, stores
only temporary audio pointers outside Redis, supports crash recovery, retry
backoff, dead-letter monitoring, and releases temporary audio after processing.

The limiter is intentionally process-local and bounded. Deployments with more
than one worker or instance must also enforce limits at a shared gateway.

## Production queue and observability

Set these values in the deployment environment to use the durable queue:

```dotenv
QUEUE_BACKEND=redis
QUEUE_REDIS_URL=redis://redis.internal:6379/0
REDIS_URL=redis://redis.internal:6379/0
RATE_LIMIT_BACKEND=redis
QUEUE_NAME=deepguard-scans
```

The API writes normalized inference audio to the configured MongoDB GridFS
store with a short expiry and places only a private pointer in Redis. Start one
or more dedicated workers with:

```sh
python -m app.queue_worker
```

The worker uses a Redis processing list for crash recovery, exponential retry
backoff for transient inference failures, and a dead-letter list for malformed
or unavailable queue payloads. `/api/health` reports queue depth and
`GET /api/metrics` exposes authenticated operational counters/timers.

The local `ThreadPoolExecutor` remains the default for development and unit
tests. Do not run multiple production Gunicorn workers with `QUEUE_BACKEND=local`.

Audio quality is checked before model scoring. Silent or unusable recordings
return `deepfake_status=insufficient_evidence` with no score. Usable recordings
include bounded five-second segment signals when segment analysis is enabled.
Quality diagnostics and segment scores contain derived metadata only.

Optional local speech-to-text is enabled with `TRANSCRIPTION_ENABLED=true` and a
local `TRANSCRIPTION_MODEL_PATH`. The frontend requires a separate explicit
checkbox, and transcript text is analyzed in memory using the local multilingual
rules (`en`, `hi`, and `es`) and is never stored.
