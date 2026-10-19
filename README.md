# DeepGuard

DeepGuard is a privacy-aware workspace for reviewing suspicious voice recordings. It is the first runnable slice of the product described in the project Markdown files: a React dashboard, a Flask API, authenticated scan history, and strict audio validation.

> **Important:** a local research baseline has now been trained and evaluated on the supplied data. It is not a production detector, and its scores are uncalibrated synthetic-audio indicators, not proof or scam probabilities. Missing/invalid models still produce **inconclusive / model unavailable** results.

## What is included

- React + Vite responsive security-monitoring dashboard
- Firebase Authentication in the React client with Flask ID-token verification
- Firestore metadata storage; raw audio is not persisted
- WAV extension, MIME, signature, size, and duration validation
- Protected scan history with per-user authorization and deletion
- Light/dark theme, collapsible sidebar, and dashboard-tab preferences
- Explicit unavailable, empty, loading, error, and success states
- Backend tests for auth, health, validation, persistence, and authorization
- A dependency-light NumPy research baseline with deterministic preprocessing and JSON artifacts
- Optional consented local transcript analysis with separate scam-language signals

`POST /api/scans` validates the WAV and optional transcript synchronously,
stores only metadata plus derived scam fields, and returns HTTP `202` with a
top-level `status: "pending"`. A bounded in-process `ThreadPoolExecutor`
worker performs voice inference and updates the owner-scoped record through
`processing` to `completed` or `failed`. Raw audio remains in memory only for
the worker handoff and is never stored or returned; transcript text is also
never stored or returned. The worker supports bounded retries for one
dedicated transient inference exception and records safe attempt/error fields.
The dashboard shows queued/processing/failed states and polls the scan detail
endpoint until a terminal state is reached.
This is not a durable cross-process queue: queued work is lost if a process
exits, and each process has its own bounded manager.

## Firebase setup

Create a Firebase project, enable Email/Password authentication, create a web app, and enable Firestore. Copy the web configuration to `frontend/.env`:

```dotenv
VITE_FIREBASE_API_KEY=your-web-api-key
VITE_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
VITE_FIREBASE_PROJECT_ID=your-project-id
VITE_FIREBASE_APP_ID=your-web-app-id
VITE_API_URL=/api
```

For Flask, copy `backend/.env.example` to `backend/.env`, set the same project ID, and point `GOOGLE_APPLICATION_CREDENTIALS` at a service-account JSON file stored outside this workspace. Never commit that file. For local emulator work, set both emulator host variables and `USE_FIREBASE_EMULATORS=true`; production mode rejects emulator settings.

The app intentionally renders a setup screen when the frontend Firebase values are missing, plus a read-only preview at `/preview`. Preview mode never calls private APIs, creates records, or uses sample scans.

Firestore access is configured in [`firestore.rules`](firestore.rules): signed-in
users can read only their own scan metadata, while the Flask backend is the sole
writer for derived results. Deploy the rules with the Firebase CLI as described
in [`FIRESTORE_SECURITY.md`](FIRESTORE_SECURITY.md). Production container and
HTTPS/CORS guidance is in [`DEPLOYMENT.md`](DEPLOYMENT.md); service-account
credentials remain outside the repository.

Telephony remains unavailable. Scam-language analysis is now available as a local,
rule-based heuristic when the user supplies a transcript and explicit consent.
The private local voice model is enabled for research only; the dataset and
generated model are not distributed in source control.

## Consented transcript scam-language analysis

On the upload panel, a user may paste an optional transcript and separately
confirm consent for transcript review. The backend analyzes the text locally in
memory using five transparent categories:

- `financial_request`
- `credential_request`
- `urgency_or_threat`
- `impersonation`
- `remote_access_request`

The transcript is bounded to 12,000 characters. Raw transcript text is not stored
in Firestore, returned in scan history, or logged. The result stores only the
heuristic status, score, categories, and explanation. The voice signal and
scam-language signal are displayed separately and are never combined into an
automatic fraud decision. A scam score is a category-match heuristic, not a
probability.

## Phase 4 model baseline

The repository now contains a model-ready baseline in `ai_models/`. It uses a deterministic 36-feature log-spectrum/RMS/zero-crossing contract and a NumPy logistic-regression classifier serialized as a safe JSON artifact. It is a research baseline, not a validated production detector.

Place authorized labeled MP3 or WAV originals in this local-only layout:

```text
dataset/
├── real/  # MP3 or PCM WAV, nested folders supported
└── fake/  # MP3 or PCM WAV, nested folders supported
```

Then prepare a separate PCM dataset and train/evaluate. These commands have
already been run for the current data; use new output names for another run:

```powershell
cd D:\Deepfake_Voice_Scam_Detector_MD_Files
backend\.venv\Scripts\python.exe -m pip install -r ai_models\requirements.txt -r ai_models\requirements-preparation.txt
backend\.venv\Scripts\python.exe -m ai_models.prepare --source dataset --output dataset/prepared --skip-invalid
backend\.venv\Scripts\python.exe -m ai_models.train --dataset dataset/prepared --output ai_models/artifacts/baseline.json --group-by filename --seed 1337 --test-size 0.2
backend\.venv\Scripts\python.exe -m ai_models.evaluate --model ai_models/artifacts/baseline.json --dataset dataset/prepared --output ai_models/artifacts/baseline-evaluation.json
backend\.venv\Scripts\python.exe -m ai_models.smoke --model ai_models/artifacts/baseline.json --dataset dataset/prepared
```

Only after reviewing held-out evaluation results should you configure the API:

```dotenv
MODEL_ARTIFACT_PATH=../ai_models/artifacts/baseline.json
MODEL_THRESHOLD=0.5
```

Restart Flask after changing `backend/.env`. Relative model paths resolve from `backend/`. The current artifact and evaluation are in gitignored `ai_models/artifacts/`; the conversion audit is `dataset/prepared/manifest.json`. Model readiness is not Firestore readiness. The web uploader still requires PCM WAV; use prepared WAVs, not original MP3s. See `ai_models/README.md` for exact split provenance, limitations, and rerun behavior.

## Run the backend (PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env # then set Firebase Admin credentials
python run.py
```

The API listens on `http://localhost:5000`. Health check: `http://localhost:5000/api/health`.

## Run the frontend (PowerShell)

```powershell
cd frontend
npm install
Copy-Item .env.example .env
npm run dev
```

Open the Vite URL shown in the terminal, normally `http://localhost:5173`.

## Run checks

Backend:

```powershell
cd backend
python -m unittest discover -s tests -v
```

Frontend:

```powershell
cd frontend
npm run build
npm test
npm run lint
```

## Next implementation slice

1. Audit dataset licenses/labels and evaluate on a separate verified speaker/generator-disjoint corpus.
2. Replace the bounded in-process worker with a durable shared job queue before production-scale use; evaluate a stronger model against this baseline.
3. Expand and validate the local transcript rules with a labeled, privacy-reviewed corpus.
4. Configure Firestore TTL operationally for the `expires_at` retention field.
5. Investigate authorized Twilio call flows; do not assume browser call capture.
