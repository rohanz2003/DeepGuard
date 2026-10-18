# Project Memory

## 1. Project and goal

Project: **AI Powered Deepfake Voice and Scam Call Detector for Secure Communication**.

The product helps a person review a consented voice recording for synthetic or
manipulated-audio risk and optionally review a consented transcript for local
scam-language signals. It must present model output as probabilistic/heuristic
evidence, not proof that a voice is fake, that a caller is fraudulent, or that a
call is a scam. Explainability and authorized telephony streaming remain separate
capabilities; telephony is not enabled.

The original project requirements are in:

- `PRD.md`
- `architecture.md`
- `rules.md`
- `phases.doc.md`
- `design.md`
- `Master_Prompt.md`

## 2. Current architecture and decisions

- Frontend: React + Vite, functional components and hooks.
- Frontend routing: React Router.
- Frontend API client: Axios with fresh Firebase ID tokens.
- Authentication: Firebase Authentication with Email/Password.
- Browser auth persistence: Firebase in-memory persistence; local storage is
  limited to non-sensitive theme/sidebar/preferred-tab preferences.
- Backend: Flask application factory and modular API modules.
- Backend auth: Firebase Admin SDK verifies Firebase ID tokens and fails closed.
- Database: Firestore, selected instead of MongoDB or SQLite.
- Storage: scan metadata only under `users/{firebase_uid}/scans/{scan_id}`;
  uploaded audio is never persisted.
- Firestore rules: authenticated clients may read only their own scan metadata;
  client writes are denied and the Flask Firebase Admin backend is the sole
  writer. See `firestore.rules` and `FIRESTORE_SECURITY.md`.
- ML: optional local NumPy research baseline loaded from a validated JSON artifact.
- Transcript analysis: local deterministic regex rules only; no external NLP API.
- Deployment intent: HTTPS in production; current `python run.py` server is for
  local development only.
- Telephony: not implemented; the browser must not be assumed to capture
  arbitrary cellular-call audio.

Firebase and Firestore were chosen by the user. The local SQLite/JWT scaffold
was replaced by Firebase Authentication + Firestore before the current model
integration was completed.

## 3. Implemented application state

### Frontend

Implemented in `frontend/`:

- Firebase setup screen when web Firebase configuration is missing.
- Read-only `/preview` workspace that never calls private APIs or creates data.
- Email/password registration, login, logout, auth-state monitoring, and server
  session verification through `GET /api/auth/me`.
- Protected workspace layout with responsive sidebar/navigation.
- Overview, scan history, scan details, settings, and planned live-calls screen.
- Light/dark theme and sidebar collapse preferences.
- Accessible loading, empty, error, unavailable, success, and delete states.
- WAV upload with extension/MIME/size checks and required consent checkbox.
- Results display that distinguishes:
  - lower signal,
  - needs review,
  - higher signal,
  - inconclusive/unavailable.
- Optional transcript textarea with a 12,000-character limit and separate
  transcript-consent checkbox.
- Separate voice-authenticity and scam-language result cards; scores are never
  combined.
- Upload results are merged into local history state and protected from stale
  list responses during refresh.
- In-flight scans show queued/processing states, poll detail read-back, stop on
  terminal states, and render safe failed-job explanations.
- UI refuses to display invalid confidence values as real scores.
- Dynamic model readiness from `/api/health`.
- Research-only caveats in result explanations.
- Scam-language signal shows unavailable when no consented transcript is supplied.

Important frontend paths:

- `frontend/src/App.jsx`
- `frontend/src/components/WorkspaceLayout.jsx`
- `frontend/src/components/UploadPanel.jsx`
- `frontend/src/components/ResultCard.jsx`
- `frontend/src/pages/OverviewPage.jsx`
- `frontend/src/pages/HistoryPage.jsx`
- `frontend/src/services/firebase.js`
- `frontend/src/services/api.js`
- `frontend/src/styles.css`
- `frontend/src/App.test.jsx`
- `frontend/src/components/ResultCard.test.jsx`

The web upload contract remains **uncompressed PCM WAV only**, 1 second to 5
minutes, maximum 25 MiB. Offline MP3 preparation does not enable MP3 uploads in
the browser.

### Backend

Implemented in `backend/app/`:

- Flask app factory and configuration from environment variables.
- `GET /` API information response.
- `GET /api/health` reports Firebase, Firestore storage, and model readiness.
- Firebase Admin lazy initialization and ID-token verification.
- Firestore read-only readiness probe using a non-reserved probe document ID.
- User-scoped scan create/list/get/delete endpoints.
- Per-process rate limiting and bounded in-memory multipart upload handling.
- Security headers, CORS, no-store API responses, and safe JSON errors.
- WAV container/signature/PCM/channel/sample-rate/sample-width/duration checks.
- In-memory audio handoff to inference; raw audio is not included in persisted scan
  payloads or diagnostic logs.
- 30-day scan metadata expiry field and expiry-on-read behavior.
- Safe JSON model artifact loader with strict feature-contract validation.
- Bounded in-process `ThreadPoolExecutor` scan worker for local research
  inference; `POST /api/scans` admits a validated scan as `pending`, then the
  worker records `processing` and `completed` or `failed` with safe retry/error
  metadata. Raw audio is handed off in memory and released after the worker.
- Consent-gated local transcript analyzer with fixed categories:
  `financial_request`, `credential_request`, `urgency_or_threat`,
  `impersonation`, and `remote_access_request`.
- Stable public scan response boundary for POST, list, and detail endpoints;
  derived scam fields are preserved while raw content is filtered.
- Raw transcript excluded from Firestore persistence, API read-back, normalized
  frontend state, and logs.

Important backend paths:

- `backend/app/__init__.py`
- `backend/app/config.py`
- `backend/app/auth.py`
- `backend/app/firebase.py`
- `backend/app/validation.py`
- `backend/app/inference.py`
- `backend/app/routes.py`
- `backend/app/jobs.py`
- `backend/app/request.py`
- `backend/app/rate_limit.py`
- `backend/tests/test_api.py`
- `backend/tests/test_model_pipeline.py`
- `backend/tests/test_firebase_readiness.py`
- `backend/app/transcript.py`
- `backend/tests/test_transcript.py`
- `backend/tests/test_scan_jobs.py`
- `firestore.rules`
- `firebase.json`
- `FIRESTORE_SECURITY.md`
- `DEPLOYMENT.md`

The model path in private `backend/.env` is relative to `backend/`:
`../ai_models/artifacts/baseline.json`. Credentials and service-account content
must never be copied into project notes or source control.

## 4. Dataset preparation and model state

The user supplied MP3 files under the local `dataset/real/` and `dataset/fake/`
folders. Originals were preserved. The new local-only pipeline is in `ai_models/`:

- `ai_models/prepare.py` converts MP3/WAV recursively using a local FFmpeg
  executable or the pinned `imageio-ffmpeg` wheel.
- Output is mono, 16 kHz, signed-16-bit PCM WAV.
- Duration bounds are 1–300 seconds; no silent padding or truncation.
- Empty, corrupt, undecodable, and duplicate files are recorded in a local
  `manifest.json`.
- Same-class decoded duplicates are removed.
- Cross-label decoded-content conflicts fail closed.
- Prepared data and artifacts are ignored by `.gitignore`.
- Existing output directories are refused to prevent accidental clobbering.

Prepared dataset location:

- `dataset/prepared/real/`
- `dataset/prepared/fake/`
- `dataset/prepared/manifest.json`

Training/evaluation implementation:

- `ai_models/preprocess.py`: deterministic 36-feature log-spectrum/RMS/ZCR
  preprocessing contract.
- `ai_models/model.py`: NumPy logistic regression and safe JSON artifact format.
- `ai_models/train.py`: bounded loading, duplicate-safe grouped split, provenance,
  data fingerprints, and train-only normalization.
- `ai_models/evaluate.py`: exact saved holdout evaluation and aggregate metrics.
- `ai_models/smoke.py`: local Flask create/read/delete model-boundary smoke test.
- `ai_models/README.md`: reproduction commands and limitations.
- `ai_models/artifacts/baseline.json`: local generated artifact, gitignored.
- `ai_models/artifacts/baseline-evaluation.json`: local generated metrics, gitignored.
- `ai_models/artifacts/baseline-evaluation.md`: readable run record.

Current preparation totals:

- Source files discovered: **4,481**.
- Source class counts: **2,224 real**, **2,257 fake**.
- Unique prepared PCM recordings: **4,042**.
- Prepared class counts: **2,122 real**, **1,920 fake**.
- Same-class decoded duplicates excluded: **360**.
- Empty sources excluded: **4**.
- Decoder failures excluded: **75**.
- Cross-label exact-content conflicts: **0**.

Current model:

- Model version: `research-baseline-1-95267cf4e8c8-20261017T120730984654Z-0a3073d7`.
- Classifier: NumPy logistic regression.
- Labels: `real=0`, `fake=1`.
- Threshold: `0.5`.
- Grouping: `filename-v1` heuristic grouping plus exact decoded-content identity.
- Training set: **3,234** clips, **1,253** inferred groups.
- Held-out set: **808** clips, **312** inferred groups.
- Exact saved holdout membership and dataset fingerprints are verified during
  evaluation; evaluation does not create a new random split.

Measured held-out results for this one local dataset split:

- Accuracy: **97.03%**.
- Precision, fake positive: **97.12%**.
- Recall, fake positive: **96.61%**.
- F1, fake positive: **96.87%**.
- ROC-AUC: **0.99740**.
- Brier score: **0.02119**.
- Mean WAV read/preprocessing + batch prediction latency: **62.38 ms/clip**.
- Confusion matrix, rows actual and columns predicted:
  - real → real: 413;
  - real → fake: 11;
  - fake → real: 13;
  - fake → fake: 371.

These are measured research results, not a general accuracy claim. Labels,
licenses, speaker identity, generator identity, near-duplicate status, and
filename grouping have not been independently audited. The score is uncalibrated
and is not a fraud probability. A separately sourced speaker/generator-disjoint
test corpus and calibration study are required before production or high-impact
use.

## 5. API contract

Public:

- `GET /`
- `GET /api/health`

Protected with Firebase Bearer ID token:

- `GET /api/auth/me`
- `GET /api/scans`
- `POST /api/scans` with multipart `audio` and `consent=true`; optional
  `transcript` plus `transcript_consent=true` for local scam-language review.
- `GET /api/scans/<scan_id>`
- `DELETE /api/scans/<scan_id>`

`POST /api/scans` returns HTTP `202` with top-level `status: "pending"`; the
stored scan lifecycle is `pending` → `processing` → `completed` or `failed`.
Scan results include status, model version, confidence when valid, model caveat,
timestamps, expiry, safe attempt/retry metadata, separate voice status, and
separate scam-language status, score, categories, and explanation. Failed scans
include only safe error code/explanation fields. Raw audio and transcript text
are never returned or stored. Firestore readiness is independent from model
readiness.

## 6. How to run locally

Use two PowerShell terminals.

Backend:

```powershell
cd D:\Deepfake_Voice_Scam_Detector_MD_Files\backend
.\.venv\Scripts\python.exe run.py
```

Backend health:

```text
http://127.0.0.1:5000/api/health
```

Expected when Firebase, Firestore, and the local model are available:

```text
firebase.status  = ready
firebase.storage = ready
model.status     = ready
model.research_only = true
```

Frontend:

```powershell
cd D:\Deepfake_Voice_Scam_Detector_MD_Files\frontend
npm run dev
```

Open:

```text
http://localhost:5173
```

Create/sign into a Firebase Email/Password account, then upload a consented
PCM WAV recording. Scan history requires Cloud Firestore to exist in the same
Firebase project and the Firestore API to be enabled. If the backend process is
already running after a configuration or code change, stop it with `Ctrl+C` and
restart it.

## 7. Reproduction and verification evidence

The following checks passed in the current workspace:

### AI preparation/training tests

Working folder: project root.

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s ai_models/tests -v
```

Result: **48 passed, 1 skipped**. The skipped test requires Windows symlink
privileges and is not a product failure.

### Backend tests

Working folder: `backend/`.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Result: **36 passed**. The async scan tests cover pending admission, explicit
processing/completed and failed worker transitions, bounded transient retry,
raw-content privacy, duplicate suppression, and owner isolation.

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests ../ai_models
.\.venv\Scripts\python.exe -m pip check
```

Both passed; no broken Python requirements were reported.

### Frontend checks

Working folder: `frontend/`.

```powershell
npm test
npm run build
npm run lint
```

Results: **26 frontend tests passed**, production build passed, and lint passed.
React Router future-flag notices may appear; they are non-failing warnings.

The transcript persistence regression test submits a consented transcript, then
reads the same scan through both list and detail endpoints. It verifies that
`scam_status`, `scam_score`, `scam_categories`, and `scam_explanation` survive
read-back while transcript and raw-transcript fields remain absent. The frontend
refresh-race test verifies that a stale list response cannot replace fresh upload
scam metadata. The async frontend tests verify queued/processing/failed display,
polling, terminal polling stop, scam metadata preservation, and raw transcript
suppression.

### Model/API smoke check

Working folder: project root.

```powershell
backend\.venv\Scripts\python.exe -m ai_models.smoke --model ai_models/artifacts/baseline.json --dataset dataset/prepared
```

Result: passed. It exercised two saved held-out WAVs through Flask model loading,
pending scan admission, worker completion, score verification, read-back, and
deletion using injected local test identity and an ephemeral repository. It made
**zero cloud writes** and did not validate a live browser Firebase sign-in
session.

### Live Firebase/Firestore readiness

A fresh backend app instance reports Firebase and Firestore storage ready through
a read-only probe when the configured service account and network are available.
The probe uses a valid non-reserved document ID and creates no cloud scan record.
The live signed-in browser upload/read-back flow still requires a user session;
no browser credentials or private tokens are recorded in this memory file.

## 8. Known gaps and constraints

- The baseline is not a CNN/RNN/Wav2Vec production detector.
- Scores are uncalibrated synthetic-audio indicators, not fraud probabilities.
- Scam-language analysis is local rule-based heuristics, not NLP certainty or a
  calibrated probability; its rules require further labeled validation.
- Live call monitoring/Twilio streaming is unavailable and requires consent,
  legal review, and an authorized supported call path.
- Web upload accepts WAV only; MP3 support is offline dataset preparation only.
- Scan inference is asynchronous behind a bounded, process-local
  `ThreadPoolExecutor`. This is not a durable cross-process queue: queued jobs
  are lost on process exit and each Gunicorn process has its own manager.
  A durable shared queue is required before larger or latency-sensitive
  deployment.
- Rate limiting is process-local; multi-worker deployments need a shared gateway.
- Firestore TTL for `expires_at` still needs operational configuration.
- Firebase credentials, service-account JSON, raw audio, dataset files, manifests,
  model artifacts, and evaluation JSON are private/gitignored and must not be
  committed.
- No production deployment, HTTPS hosting, monitoring, drift detection, rollback,
  or incident-response setup has been completed.
- Provider-neutral production artifacts now exist (`backend/Dockerfile`,
  `frontend/Dockerfile`, `Procfile`, and Nginx/Gunicorn configuration), but they
  still require a chosen hosting provider, deployed domains, and an external
  signed-in browser smoke test.
- Browser transcript input is optional and consent-gated; transcript text is not
  retained by default.

## 9. Next concrete steps

1. Audit the dataset source, licenses, labels, speaker identities, and generator
   identities; preserve that audit outside this public memory file if sensitive.
2. Evaluate on a separately sourced, verified speaker/generator-disjoint corpus
   and perform calibration/threshold analysis without using the holdout for tuning.
3. Compare this baseline with a stronger evaluated speech model while retaining
   the same provenance and privacy controls.
4. Replace the bounded in-process worker with a durable shared queue before
   production-scale use; the API now exposes pending/processing/completed/failed
   lifecycle states.
5. Configure Firestore TTL operationally for `expires_at`.
6. Validate and expand the local transcript rules using a labeled,
   privacy-reviewed corpus; do not send transcript text to an external service
   without a separate consent/data-handling decision.
7. Investigate authorized Twilio-supported flows; do not add arbitrary browser
   cellular-call capture or automatic call blocking.
