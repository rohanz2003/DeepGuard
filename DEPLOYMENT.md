# DeepGuard production deployment

This repository provides provider-neutral production artifacts for a split
deployment: a Flask API served by Gunicorn and a Vite bundle served as static
files. The artifacts do not contain credentials and do not assume a particular
hosting provider.

## Deployment shape

- **Backend:** `backend/Dockerfile` builds a WSGI container. The root
  `Procfile` uses the same Gunicorn configuration for a process-based host.
  The container listens on `PORT` (default `8080`) and exposes the existing
  `/api/health` readiness endpoint.
- **Frontend:** `frontend/Dockerfile` builds with `npm ci` from the committed
  lockfile and serves `dist/` with Nginx. `frontend/nginx.conf` includes the
  client-side route fallback and `/healthz` liveness response.
- **TLS:** the containers speak HTTP on their internal ports. Put them behind
  an HTTPS-capable ingress, reverse proxy, or static host, redirect HTTP to
  HTTPS, and do not copy certificates or private keys into either image.

The backend image is built from the repository root so the optional
`ai_models/` runtime package is available when a separately supplied model
artifact is configured. The model artifact itself is excluded from the image
context and must be mounted or supplied by the runtime. The image defaults to
`ENVIRONMENT=production` and an empty allowlist for `CORS_ORIGINS`, so a
deployment must explicitly provide its public frontend origin.

Scan inference is currently a bounded in-process worker. Each Gunicorn process
has its own manager, and queued in-memory audio is lost if that process exits.
This lifecycle is suitable for the current bounded MVP, but a durable shared
queue and retry store are required before relying on processing across restarts
or multiple independently scaled instances.

## Backend process and container

`backend/requirements.txt` pins `gunicorn==23.0.0`. The production WSGI target
is `run:app`, which loads the existing `create_app()` application factory. The
worker and timeout defaults can be adjusted without changing application code:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8080` | Internal listener port supplied to the process/container |
| `WEB_CONCURRENCY` | `2` | Gunicorn worker count |
| `GUNICORN_THREADS` | `4` | Threads per worker |
| `GUNICORN_TIMEOUT` | `120` | Request timeout in seconds |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Graceful shutdown window |
| `GUNICORN_KEEPALIVE` | `5` | Keep-alive seconds |
| `GUNICORN_LOG_LEVEL` | `info` | Gunicorn log level |

Build and run the portable image from the repository root:

```sh
docker build -f backend/Dockerfile -t deepguard-backend:production .
docker run --rm -p 8080:8080 \
  --env-file /secure/deepguard/backend-production.env \
  --mount type=bind,src=/secure/deepguard/firebase-service-account.json,dst=/run/secrets/firebase-service-account.json,readonly \
  -e GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/firebase-service-account.json \
  deepguard-backend:production
```

For a process-based host, install the pinned requirements and use the checked-in
Procfile from the repository root:

```sh
python -m pip install -r backend/requirements.txt
gunicorn --chdir backend --config backend/gunicorn.conf.py run:app
```

The `PORT` value supplied by the host is honored by both commands. The Docker
image defaults to the same port when no value is supplied.

## Frontend static build

`VITE_*` values are read by Vite while the bundle is built. Changing a
container's environment after `npm run build` does not change the already
generated JavaScript. `VITE_API_URL` supports either deployment shape:

- Use an absolute HTTPS URL such as `https://api.example.test/api` when the API
  and static site have different public origins.
- Use `/api` when the HTTPS edge routes that path to the backend on the same
  public origin.

Build the static image with the API URL and the Firebase web-app values for the
target environment:

```sh
docker build -f frontend/Dockerfile -t deepguard-frontend:production \
  --build-arg VITE_API_URL=https://api.example.test/api \
  --build-arg VITE_FIREBASE_API_KEY="$VITE_FIREBASE_API_KEY" \
  --build-arg VITE_FIREBASE_AUTH_DOMAIN="$VITE_FIREBASE_AUTH_DOMAIN" \
  --build-arg VITE_FIREBASE_PROJECT_ID="$VITE_FIREBASE_PROJECT_ID" \
  --build-arg VITE_FIREBASE_APP_ID="$VITE_FIREBASE_APP_ID" \
  frontend
docker run --rm -p 8081:8080 deepguard-frontend:production
```

The `VITE_FIREBASE_*` values are public browser configuration, not Firebase
Admin credentials. Never pass a service-account JSON, private key, or other
server secret as a `VITE_*` build argument. A non-container static host can use
the same reproducible build with `npm ci` followed by `npm run build`; publish
the resulting `frontend/dist/` directory and configure its fallback to
`index.html`.

## Production environment variables

Inject backend settings through the hosting environment or a secret file kept
outside the checkout. Do not create or commit a production `.env` file. The
tracked `backend/.env.example` documents the application defaults.

Required or production-specific backend settings:

```dotenv
ENVIRONMENT=production
FIREBASE_PROJECT_ID=your-firebase-project-id
GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/firebase-service-account.json
USE_FIREBASE_EMULATORS=false
CORS_ORIGINS=https://frontend.example.test
```

Keep `FIREBASE_AUTH_EMULATOR_HOST` and `FIRESTORE_EMULATOR_HOST` unset in
production. The application rejects emulator settings when
`ENVIRONMENT=production`. Set the upload, retention, rate-limit, and model
variables from `backend/.env.example` as needed for the deployment; the
existing validation bounds still apply. If the runtime provides an
appropriate application identity, `GOOGLE_APPLICATION_CREDENTIALS` may be
left empty and Application Default Credentials can be used instead.

Frontend build-time settings are:

```dotenv
VITE_API_URL=https://api.example.test/api
VITE_FIREBASE_API_KEY=your-web-api-key
VITE_FIREBASE_AUTH_DOMAIN=your-project.firebaseapp.com
VITE_FIREBASE_PROJECT_ID=your-project-id
VITE_FIREBASE_APP_ID=your-web-app-id
VITE_USE_FIREBASE_EMULATORS=false
```

Only the frontend's public Firebase web configuration belongs in these build
variables. Do not put Firebase Admin credentials, API tokens, or service
account JSON in the frontend environment.

## Firebase service-account handling

Keep the Firebase Admin service-account JSON outside Git and outside the image
build context. Supply it at runtime using a read-only secret mount or the
hosting runtime's identity mechanism, then set
`GOOGLE_APPLICATION_CREDENTIALS` to the mounted path. Do not paste the JSON
into an environment variable, Dockerfile, Procfile, frontend bundle, or
documentation. Restrict the file permissions, rotate it through the
credential-management process used by the deployment environment, and remove
the file from a runtime when the service is retired.

The `.gitignore` excludes `.env` files, but that is only a guardrail: review
the build context and Git status before building or publishing an image. The
repository's Docker ignore files also exclude local environment files, private
datasets, model artifacts, and frontend dependencies.

## Restricted CORS

Set `CORS_ORIGINS` to the exact public frontend origin or a comma-separated
list of exact origins, for example:

```dotenv
CORS_ORIGINS=https://frontend.example.test,https://admin.example.test
```

Use the scheme and host only; do not include a path, use `*`, or include a
development origin in production. Keep the API URL and each CORS origin on
HTTPS. If the frontend and API share one public origin, `/api` can be used as
`VITE_API_URL` and the single origin should still be configured explicitly.

## HTTPS and smoke checks

Terminate TLS at the public edge for both services, redirect plaintext HTTP to
HTTPS, and configure the Firebase project's authorized web domains for the
frontend origin. The backend emits HSTS when `ENVIRONMENT=production`; enable
HSTS at the HTTPS edge only after the domain is serving HTTPS reliably.

After deployment, verify the public endpoints from outside the private network:

```sh
curl --fail --silent --show-error https://api.example.test/api/health
curl --fail --silent --show-error --head https://frontend.example.test/
curl --fail --silent --show-error https://frontend.example.test/preview
```

The backend health response should report Firebase/Firestore readiness before
protected scan routes are considered ready. The frontend check should return
the static application shell over HTTPS, and a browser sign-in/upload smoke
check should use the same HTTPS origin configured in `CORS_ORIGINS`.
