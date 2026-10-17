# Project Rules and Guidelines

## 1. What to Use
- Use React functional components and hooks.
- Use Flask for modular REST API endpoints.
- Use Librosa for audio loading and feature extraction when suitable.
- Keep frontend/backend communication on HTTPS in deployed environments.
- Validate uploaded files by extension, MIME type, file signature where feasible, duration, and size.
- Keep model inference separate from request-handling code when processing is resource-intensive.

## 2. What to Avoid
- Do not block the main thread during long-running inference; use a job queue or background worker when needed.
- Do not permanently store sensitive audio without a clear purpose, consent, access controls, and retention policy.
- Do not treat model scores as certainty or use them as the sole basis for high-impact decisions.
- Do not claim explainability is guaranteed merely because LIME or SHAP is present; verify explanations are meaningful for the selected model.
- Do not assume a browser application can capture arbitrary cellular call audio. Confirm platform, telephony, consent, and legal requirements first.
- Do not commit API keys, secrets, credentials, private audio, or real user data to source control.

## 3. Libraries and Dependencies
- **Frontend:** `react`, `react-router-dom`, `axios`; optional charting library.
- **Backend:** `flask`, `flask-cors`, `python-dotenv`; database SDK chosen by the project.
- **Audio/ML:** `librosa`, `numpy`, `scikit-learn`, and either `tensorflow` or `torch` based on the chosen model.
- **Optional:** `lime`, `shap`, `openai`, `twilio` only when the relevant feature is implemented.

Pin and document compatible dependency versions. Avoid installing multiple large ML frameworks without a demonstrated need.

## 4. Error Handling
- Return HTTP 400 for invalid, corrupt, too-short, or unsupported audio.
- Return clear, non-sensitive error messages to the UI.
- Handle timeouts and external API failures gracefully.
- Log diagnostic details server-side without logging secrets or unnecessary personal data.
- Keep offline upload analysis usable if an optional telephony integration is unavailable.

## 5. AI Boundaries and Privacy
- Present outputs as risk estimates, with limitations and uncertainty clearly explained.
- Never automatically block or disconnect calls unless a separately reviewed, explicitly configured feature supports it.
- Obtain consent and follow applicable laws before recording or analyzing calls.
- Apply authentication, authorization, rate limiting, encryption in transit, and appropriate storage controls.
- Provide a deletion/retention policy for uploaded audio and scan history.

## 6. General Engineering Rules
- Follow PEP 8 for Python and standard ESLint conventions for React.
- Add tests for API validation, audio preprocessing, model outputs, and key UI flows.
- Use descriptive commits, for example: `feat(ml): add MFCC extraction`.
- Document datasets, licenses, model versions, thresholds, and evaluation methodology.
- Optimize for measured latency and reliability rather than assuming real-time performance.
