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
for synchronous PCM validation/inference and is never stored or logged. `expires_at` is written
for a 30-day default retention period and expired documents are removed when
read. A separate Firestore TTL policy should be configured operationally for
eventual cleanup.

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

The current baseline runs the bounded inference call synchronously in the Flask
request for small uploads. This is not a production worker architecture;
larger or latency-sensitive deployments should move inference to a queue.

The limiter is intentionally process-local and bounded. Deployments with more
than one worker or instance must also enforce limits at a shared gateway.
