# Master Prompt for Code Generation

Copy this prompt into an AI coding assistant and provide the six project markdown files alongside it.

> I am building a research project titled **“AI Powered Deepfake Voice and Scam Call Detector for Secure Communication.”** Read `PRD.md`, `architecture.md`, `rules.md`, `phases.doc.md`, `design.md`, and `memory.md` thoroughly before coding.
>
> **Your tasks**
> 1. Initialize the repository using the folder structure in `architecture.md`.
> 2. Create a React frontend with routing and a Flask backend with a health-check endpoint.
> 3. Begin Phase 1 in `phases.doc.md`: implement the project skeleton and authentication flow. Recommend one authentication approach (Firebase Auth or JWT) and explain the trade-offs before implementing it.
> 4. Follow the privacy, security, error-handling, and design requirements in `rules.md` and `design.md`.
> 5. Provide complete starting files, dependency manifests, `.env.example` files, setup commands, and run instructions.
> 6. Add basic tests and explain how to run them.
> 7. Do not hard-code credentials or commit secrets. Do not pretend that an untrained model is functional; clearly mark ML and telephony integrations as stubs until they are implemented and tested.
> 8. Treat deepfake/scam outputs as probabilistic indicators. Do not claim 98% accuracy without reproducible evaluation. Do not implement unauthorized call recording or assume a browser can capture arbitrary cellular calls.
>
> **Working style**
> - Work phase by phase.
> - Before editing, summarize the plan and list files to be created.
> - Produce complete files rather than disconnected snippets.
> - Include exact commands for Windows PowerShell and, where useful, macOS/Linux.
> - After each phase, summarize what works, what remains, and how to verify it.
> - Keep the implementation beginner-friendly and explain important decisions.
