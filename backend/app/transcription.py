"""Optional local, consent-gated speech-to-text adapter.

The adapter never sends audio to a remote API.  ``faster-whisper`` is imported
only when a local model directory is configured and the upload explicitly asks
for transcription.  The returned text is immediately passed to the local
transcript analyzer and is never persisted or logged.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any


class LocalSpeechToText:
    def __init__(
        self,
        model_path: str = "",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        self.model_path = str(model_path or "").strip()
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._lock = threading.Lock()
        self._failure: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.model_path and Path(self.model_path).is_dir())

    def readiness(self) -> dict[str, Any]:
        return {
            "enabled": self.configured,
            "provider": "local_faster_whisper",
            "model": self.model_path or "not-configured",
            "status": "ready" if self.configured else "unavailable",
        }

    def _load(self):
        if self._model is not None:
            return self._model
        if not self.configured:
            return None
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_path,
                    device=self.device,
                    compute_type=self.compute_type,
                    local_files_only=True,
                )
            except Exception:
                self._failure = "unavailable"
                return None
        return self._model

    def __call__(self, wav_bytes: bytes, *, language: str = "auto") -> dict[str, Any]:
        model = self._load()
        if model is None:
            return {"text": "", "language": language}
        try:
            # faster-whisper accepts a file-like WAV source and performs all
            # decoding locally. Segments are joined in memory only.
            segments, info = model.transcribe(
                io.BytesIO(bytes(wav_bytes)),
                language=None if language == "auto" else language,
                vad_filter=True,
            )
            text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
            detected = getattr(info, "language", None) or language
            return {"text": text, "language": detected}
        except Exception:
            return {"text": "", "language": language}


__all__ = ["LocalSpeechToText"]
