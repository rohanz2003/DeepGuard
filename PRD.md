# Product Requirement Document (PRD)

## 1. What to Build
An AI-powered Deepfake Voice and Scam Call Detector designed to help identify AI-generated voices, manipulated audio, and potentially fraudulent speech patterns. The proposed system uses audio features such as spectrograms and Mel-frequency cepstral coefficients (MFCCs), with a planned CNN-RNN model and/or a pretrained speech representation model. NLP can help identify suspicious scam language, and call-stream integration may be added where legally and technically supported.

**Important:** Detection results are probabilistic indicators, not proof that a voice is fake or a call is fraudulent.

## 2. Target Users
- **Individuals:** People seeking protection against voice-cloning scams and social engineering.
- **Enterprise and Telecom:** Customer-support centers, financial services, and telecom teams exploring fraud-risk detection.
- **Security and Forensics:** Analysts examining potentially manipulated audio.

## 3. Features
- **Audio Upload and Scan:** Upload supported audio files for analysis.
- **Live Call Monitoring (planned):** Investigate supported telephony integrations and real-time audio streams; do not assume ordinary mobile calls can be captured by a web app.
- **Audio Classification:** Distinguish real and synthetic/manipulated audio using a model trained and evaluated on appropriate datasets.
- **Scam Phrase Detection:** Identify potentially suspicious language using rules and/or an NLP service.
- **Explainability Dashboard:** Show relevant audio regions, features, or text cues that contributed to a result where technically supported.
- **Scan History and Logs:** Store scan metadata and results in Firebase or MongoDB, subject to privacy and retention settings.

## 4. Success Criteria
- Users can securely upload valid audio and receive a structured result.
- Invalid or unsupported files return understandable errors.
- Results include model/version information and clearly communicate uncertainty.
- Performance is measured on a documented evaluation dataset using precision, recall, F1-score, confusion matrix, and latency.
- No accuracy target (including 98%) is claimed unless verified by reproducible evaluation.

## 5. Out of Scope for Initial Version
- Automatically blocking or disconnecting calls.
- Claiming that a voice is definitively fake based only on a model score.
- Recording calls without consent and appropriate legal authorization.
