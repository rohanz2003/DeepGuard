# Architecture and Tech Stack

## 1. Architecture
The application uses a client-server architecture with a separate audio-analysis pipeline.

- **Frontend:** React application for authentication, audio upload, scan results, history, and dashboard visualizations. Communicates with the API over HTTPS.
- **Backend API:** Flask application exposing endpoints for authentication integration, uploads, prediction jobs, and scan history.
- **AI/ML Pipeline:** Python services for audio validation, resampling/normalization, MFCC and spectrogram extraction, model inference, and optional explainability.
- **NLP Service:** Optional service for detecting suspicious language in a transcript. Avoid sending sensitive content to third-party APIs unless the user has consented and data handling has been reviewed.
- **Telephony Integration:** Optional Twilio integration for supported call flows and audio streams. Feasibility depends on the call path, platform permissions, and applicable laws.
- **Database and Storage:** Firebase or MongoDB for user/scan metadata; private object storage for audio only when needed and with a defined retention policy.
- **Deployment:** HTTPS-enabled hosting. Google Cloud is one possible deployment target; frontend hosting may use a static hosting provider.

## 2. Suggested Folder Structure
```text
project-root/
├── frontend/
│   ├── public/
│   └── src/
│       ├── components/
│       ├── pages/
│       ├── services/
│       ├── hooks/
│       └── App.jsx
├── backend/
│   ├── app/
│   │   ├── routes/
│   │   ├── controllers/
│   │   ├── services/
│   │   └── __init__.py
│   ├── tests/
│   ├── config.py
│   ├── requirements.txt
│   └── run.py
├── ai_models/
│   ├── preprocessing/
│   ├── models/
│   ├── explainability/
│   └── README.md
├── docs/
│   ├── PRD.md
│   ├── architecture.md
│   ├── rules.md
│   ├── phases.doc.md
│   ├── design.md
│   └── memory.md
└── README.md
```

## 3. Tech Stack
- **Frontend:** React, React Router, Axios, Chart.js (optional)
- **Backend:** Python, Flask, Flask-CORS
- **Audio/ML:** Librosa, NumPy, scikit-learn, TensorFlow/Keras or PyTorch; model choice should be finalized after evaluating compute and dataset requirements
- **Optional speech model:** Wav2Vec 2.0 or a suitable pretrained model
- **Explainability:** LIME/SHAP where their explanations are appropriate for the model and modality
- **Database:** Choose Firebase or MongoDB for the initial version rather than implementing both without a need
- **Telephony:** Twilio, only for supported and authorized flows
- **Hosting:** Google Cloud or another HTTPS-capable provider
