"""Optional, bounded model inference.

The baseline API intentionally loads a JSON *description* of a model.  It
never unpickles or executes code named by an artifact.  Runtime model and
preprocessing modules are imported lazily from the project's ``ai_models``
package, so a normal API install does not require NumPy or an ML framework.
The scan job manager calls this service behind a bounded in-process worker
boundary; a dedicated transient exception is the only retryable failure.
"""

from __future__ import annotations

import importlib
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .audio_quality import segment_wavs
from .transcript import analyze_transcript, unavailable_scam_result


ARTIFACT_SCHEMA = "deepguard-model"
ARTIFACT_SCHEMAS = {ARTIFACT_SCHEMA, "deepguard.model"}
ARTIFACT_VERSION = 1
FEATURE_CONTRACT_VERSION = 1
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ArtifactUnavailable(RuntimeError):
    """Raised internally when a model artifact cannot safely be used."""


class TransientInferenceError(RuntimeError):
    """Raised by an inference adapter when a bounded retry may succeed."""


@dataclass(frozen=True)
class ModelArtifact:
    """Validated, non-executable metadata loaded from a JSON artifact."""

    model_version: str
    feature_contract: dict[str, Any]
    model_module: str = "ai_models.model"
    preprocess_module: str = "ai_models.preprocess"
    model_factory: str = "load_model"
    predictor: str = "predict"


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactUnavailable(f"Model artifact {label} is missing or invalid.")
    return value.strip()


def _safe_ai_models_name(value: Any, label: str, default: str) -> str:
    name = default if value is None else _required_string(value, label)
    if not name.startswith("ai_models."):
        raise ArtifactUnavailable(f"Model artifact {label} must use ai_models modules.")
    parts = name.split(".")
    if any(not _SAFE_NAME.fullmatch(part) for part in parts):
        raise ArtifactUnavailable(f"Model artifact {label} is invalid.")
    return name


def _safe_callable_name(value: Any, label: str, default: str) -> str:
    name = default if value is None else _required_string(value, label)
    if not _SAFE_NAME.fullmatch(name):
        raise ArtifactUnavailable(f"Model artifact {label} is invalid.")
    return name


def _validate_feature_contract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ArtifactUnavailable("Model artifact feature_contract is missing or invalid.")
    version = raw.get("version", raw.get("contract_version"))
    supported_version = version == FEATURE_CONTRACT_VERSION or (
        isinstance(version, str)
        and (version.startswith("log-spectrum-") or version.startswith("1."))
    )
    if not supported_version:
        raise ArtifactUnavailable("Model artifact feature contract version is unsupported.")
    # At least one stable feature declaration is required.  The remaining
    # fields are intentionally model-owned, but common PCM constraints are
    # checked when present so a bad artifact fails closed.
    features = raw.get("name", raw.get("features", raw.get("feature_names")))
    if not isinstance(features, (str, list)) or not features:
        raise ArtifactUnavailable("Model artifact feature contract is missing features.")
    if isinstance(features, list) and (
        not all(isinstance(item, str) and item.strip() for item in features)
    ):
        raise ArtifactUnavailable("Model artifact feature contract features are invalid.")
    if "feature_count" in raw and (
        not isinstance(raw["feature_count"], int)
        or raw["feature_count"] <= 0
        or isinstance(features, list) and raw["feature_count"] != len(features)
    ):
        raise ArtifactUnavailable("Model artifact feature count is invalid.")
    for key in ("sample_rate", "channels", "sample_width"):
        if key in raw and (not isinstance(raw[key], int) or raw[key] <= 0):
            raise ArtifactUnavailable(f"Model artifact feature contract {key} is invalid.")
    return dict(raw)


def validate_artifact(raw: Any) -> ModelArtifact:
    """Validate artifact metadata without importing optional dependencies."""

    if not isinstance(raw, dict):
        raise ArtifactUnavailable("Model artifact must contain a JSON object.")
    artifact_type = raw.get("artifact_type")
    if raw.get("schema") not in ARTIFACT_SCHEMAS and artifact_type != "audio_binary_logistic_regression":
        raise ArtifactUnavailable("Model artifact schema is unsupported.")
    version = raw.get("version", raw.get("artifact_version", raw.get("schema_version")))
    if not (
        version == ARTIFACT_VERSION
        or version == "1"
        or (isinstance(version, str) and version.startswith("1."))
    ):
        raise ArtifactUnavailable("Model artifact version is unsupported.")
    model_version = _required_string(raw.get("model_version"), "model_version")
    contract = _validate_feature_contract(raw.get("feature_contract"))
    runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    return ModelArtifact(
        model_version=model_version,
        feature_contract=contract,
        model_module=_safe_ai_models_name(runtime.get("model_module"), "runtime.model_module", "ai_models.model"),
        preprocess_module=_safe_ai_models_name(
            runtime.get("preprocess_module"), "runtime.preprocess_module", "ai_models.preprocess"
        ),
        model_factory=_safe_callable_name(runtime.get("model_factory"), "runtime.model_factory", "load_model"),
        predictor=_safe_callable_name(runtime.get("predictor"), "runtime.predictor", "predict"),
    )


class JsonModelArtifactLoader:
    """Load one validated JSON artifact and lazily bind its model runtime."""

    def __init__(self, path: str | Path):
        self.path = str(path or "").strip()
        self.artifact: ModelArtifact | None = None
        self.model: Any = None
        self.preprocessor: Callable[..., Any] | None = None
        self.failure: str | None = None

    @property
    def ready(self) -> bool:
        return self.artifact is not None and self.model is not None and self.preprocessor is not None

    def load(self) -> bool:
        if not self.path:
            self.failure = "not-configured"
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            artifact = validate_artifact(raw)
            preprocess_module = _import_ai_models(artifact.preprocess_module)
            model_module = _import_ai_models(artifact.model_module)
            preprocessor = getattr(preprocess_module, "preprocess_audio", None)
            if preprocessor is None:
                preprocessor = getattr(preprocess_module, "preprocess", None)
            if preprocessor is None:
                preprocessor = getattr(preprocess_module, "extract_features", None)
            factory = getattr(model_module, artifact.model_factory, None)
            predictor = getattr(model_module, artifact.predictor, None)
            if not callable(preprocessor) or (factory is not None and not callable(factory)):
                raise ArtifactUnavailable("Model runtime does not expose the required functions.")
            if callable(factory):
                try:
                    model = factory(self.path)
                except (TypeError, AttributeError):
                    # Small injected adapters commonly accept the validated
                    # JSON object instead of a filesystem path.
                    model = factory(raw)
            else:
                model = model_module
            if not callable(getattr(model, artifact.predictor, None)) and not callable(predictor):
                raise ArtifactUnavailable("Model runtime does not expose a predictor.")
            self.artifact = artifact
            self.preprocessor = preprocessor
            self.model = model
            self.failure = None
            return True
        except Exception:
            # Keep details out of the API response and logs.  A malformed or
            # unavailable optional model is a normal readiness state.
            self.artifact = None
            self.model = None
            self.preprocessor = None
            self.failure = "unavailable"
            return False


def _import_ai_models(module_name: str):
    """Import the optional sibling package only when an artifact requests it."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if not (exc.name == "ai_models" or exc.name.startswith("ai_models.")):
            raise
        project_root = str(Path(__file__).resolve().parents[2])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        return importlib.import_module(module_name)


class UnavailableInferenceService:
    def __init__(self, version: str = "not-configured"):
        self.version = version

    def readiness(self) -> dict[str, Any]:
        return {"status": "unavailable", "version": self.version}

    def analyze(self, _metadata):
        return unavailable_result(self.version)


def unavailable_result(version: str = "not-configured") -> dict[str, Any]:
    result = {
        "status": "unavailable",
        "deepfake_status": "inconclusive",
        "confidence": None,
        "synthetic_audio_score": None,
        "calibrated_score": None,
        "score_type": "uncalibrated_synthetic_audio_score",
        "audio_quality": {},
        "segment_scores": [],
        "model_version": version,
        "explanation": (
            "Upload validated successfully, but no valid inference model is available. "
            "No risk claim was generated."
        ),
    }
    result.update(unavailable_scam_result())
    return result


def _call_with_optional_contract(function: Callable[..., Any], value: Any, artifact: ModelArtifact):
    """Support the small model adapter contract without requiring NumPy here."""
    try:
        return function(value, artifact.feature_contract)
    except TypeError:
        return function(value)


def _score_from_prediction(prediction: Any) -> float:
    # NumPy is deliberately optional here; this handles a one-element ndarray
    # without importing NumPy in the backend process.
    tolist = getattr(prediction, "tolist", None)
    if callable(tolist):
        prediction = tolist()
    if isinstance(prediction, dict):
        prediction = prediction.get("score", prediction.get("deepfake_score"))
    elif isinstance(prediction, (tuple, list)) and prediction:
        prediction = prediction[0]
        if isinstance(prediction, (tuple, list)) and prediction:
            prediction = prediction[0]
    try:
        score = float(prediction)
    except (TypeError, ValueError) as exc:
        raise ValueError("Model returned an invalid score.") from exc
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Model returned an invalid score.")
    return score


def _risk_status(score: float, threshold: float) -> str:
    if score >= threshold:
        return "high"
    # A narrow band below the configured cutoff is intentionally review-only;
    # it avoids presenting a borderline probabilistic score as a clean result.
    if score >= max(0.0, threshold - 0.15):
        return "review"
    return "low"


class ModelInferenceService:
    """Adapter for the optional ai_models package used by scan workers."""

    def __init__(self, settings: dict[str, Any], loader: JsonModelArtifactLoader | None = None):
        self.threshold = float(settings.get("MODEL_THRESHOLD", 0.5))
        self.loader = loader or JsonModelArtifactLoader(settings.get("MODEL_ARTIFACT_PATH", ""))
        self.policy = self._load_policy(settings.get("MODEL_POLICY_PATH", ""))
        self.quality_enabled = bool(settings.get("AUDIO_QUALITY_ENABLED", True))
        # Direct adapter users/tests that provide only a model threshold retain
        # the small legacy contract; app configuration explicitly enables it.
        self.segment_enabled = bool(settings.get("SEGMENT_ANALYSIS_ENABLED", False))
        self.transcription_enabled = bool(settings.get("TRANSCRIPTION_ENABLED", False))
        self.transcriber = settings.get("TRANSCRIPTION_SERVICE")
        self.metrics = settings.get("METRICS")
        load = getattr(self.loader, "load", None)
        if callable(load):
            load()
        if (
            self.policy
            and self.loader.artifact is not None
            and self.policy.get("model_version") != self.loader.artifact.model_version
        ):
            self.policy = None

    @staticmethod
    def _load_policy(path: str | Path) -> dict[str, Any] | None:
        if not str(path or "").strip():
            return None
        try:
            with open(path, "r", encoding="utf-8") as stream:
                policy = json.load(stream)
            if (
                policy.get("schema") != "deepguard-score-policy"
                or policy.get("version") != 1
                or policy.get("calibration", {}).get("method") != "platt-v1"
            ):
                return None
            return policy
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def readiness(self) -> dict[str, Any]:
        if not self.loader.ready:
            return {"status": "unavailable", "version": "not-configured"}
        return {
            "status": "ready",
            "version": self.loader.artifact.model_version,
            "research_only": True,
            "score_type": "calibrated_synthetic_audio_score" if self.policy else "uncalibrated_synthetic_audio_score",
            "calibration": bool(self.policy),
            "quality_checks": self.quality_enabled,
            "segment_analysis": self.segment_enabled,
            "transcription": self.transcription_enabled,
        }

    def _predict_bytes(self, audio_bytes: bytes, artifact: ModelArtifact) -> float:
        features = _call_with_optional_contract(self.loader.preprocessor, audio_bytes, artifact)
        predictor = getattr(self.loader.model, "predict_proba", None)
        if not callable(predictor):
            predictor = getattr(self.loader.model, artifact.predictor, None)
        if not callable(predictor):
            module = _import_ai_models(artifact.model_module)
            predictor = getattr(module, artifact.predictor)
        return _score_from_prediction(_call_with_optional_contract(predictor, features, artifact))

    def _calibrate(self, score: float) -> float | None:
        if not self.policy:
            return None
        try:
            from ai_models.calibration import apply_calibrator

            calibrated = apply_calibrator(score, self.policy["calibration"])
            item = calibrated.item() if hasattr(calibrated, "item") else calibrated
            return round(float(item), 4)
        except (ImportError, KeyError, TypeError, ValueError, IndexError):
            return None

    def _transcribe(self, metadata) -> dict[str, Any] | None:
        if not self.transcription_enabled or not getattr(metadata, "transcribe_audio", False):
            return None
        transcriber = self.transcriber
        if not callable(transcriber):
            return {
                "scam_status": "unavailable",
                "scam_score": None,
                "scam_categories": [],
                "transcript_language": getattr(metadata, "transcript_language", "auto"),
                "transcript_source": "local_unavailable",
                "scam_explanation": "Local speech-to-text is not configured; no transcript signal was generated.",
            }
        try:
            result = transcriber(
                metadata.audio_bytes,
                language=getattr(metadata, "transcript_language", "auto"),
            )
            if not isinstance(result, dict):
                return None
            text = result.get("text")
            if not isinstance(text, str) or not text.strip():
                return {
                    **unavailable_scam_result(),
                    "transcript_language": result.get("language", "auto"),
                    "transcript_source": "local",
                    "scam_explanation": "Local speech-to-text returned no usable transcript.",
                }
            return analyze_transcript(
                text,
                consented=True,
                language=result.get("language", getattr(metadata, "transcript_language", "auto")),
                source="local",
            )
        except Exception:
            return {
                **unavailable_scam_result(),
                "transcript_language": getattr(metadata, "transcript_language", "auto"),
                "transcript_source": "local_error",
                "scam_explanation": "Local speech-to-text could not complete; no transcript signal was generated.",
            }

    def _record_metrics(self, started: float, outcome: str, model_version: str) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.observe(
                "inference_seconds",
                time.perf_counter() - started,
                outcome=outcome,
                model=model_version,
            )
            self.metrics.increment("inference_total", outcome=outcome, model=model_version)
        except Exception:
            pass

    def analyze(self, metadata):
        if not self.loader.ready:
            return unavailable_result()
        artifact = self.loader.artifact
        started = time.perf_counter()
        quality = getattr(metadata, "audio_quality", None) or {}
        if self.quality_enabled and quality.get("status") == "insufficient_evidence":
            self._record_metrics(started, "insufficient_evidence", artifact.model_version)
            return {
                "status": "completed",
                "deepfake_status": "insufficient_evidence",
                "confidence": None,
                "synthetic_audio_score": None,
                "calibrated_score": None,
                "score_type": "uncalibrated_synthetic_audio_score",
                "model_version": artifact.model_version,
                "audio_quality": quality,
                "segment_scores": [],
                "explanation": (
                    "The recording did not contain enough usable speech evidence for a voice score. "
                    "No authenticity claim was generated."
                ),
                **(self._transcribe(metadata) or unavailable_scam_result()),
            }
        try:
            score = self._predict_bytes(metadata.audio_bytes, artifact)
            segment_scores = []
            if self.segment_enabled:
                for start, end, segment in segment_wavs(metadata.audio_bytes):
                    segment_score = self._predict_bytes(segment, artifact)
                    segment_scores.append({
                        "start_seconds": start,
                        "end_seconds": end,
                        "synthetic_audio_score": round(segment_score, 4),
                        "status": _risk_status(segment_score, self.threshold),
                    })
        except TransientInferenceError:
            # The scan worker owns the retry policy.  Do not turn this
            # dedicated signal into an inconclusive successful result.
            raise
        except Exception:
            # Inference failures fail closed and never expose model internals.
            self._record_metrics(started, "failed", artifact.model_version)
            return unavailable_result(artifact.model_version)
        status = _risk_status(score, self.threshold)
        calibrated = self._calibrate(score)
        if self.policy and calibrated is not None:
            policy_threshold = self.policy.get("decision", {}).get("threshold")
            if isinstance(policy_threshold, (int, float)):
                status = _risk_status(calibrated, float(policy_threshold))
        transcript = self._transcribe(metadata)
        explanation = (
            "Research baseline only: this uncalibrated synthetic-audio score is not the probability "
            "that a caller is fraudulent and is not proof that a voice is fake. "
            f"The raw synthetic-audio score was {score:.3f}; the configured threshold was "
            f"{self.threshold:.3f}."
        )
        if calibrated is not None:
            explanation += " A separately fitted calibration policy supplied a review score; it is not fraud probability."
        if quality.get("status") == "review":
            explanation += " Audio-quality checks indicate that human review should consider recording quality."
        self._record_metrics(started, "completed", artifact.model_version)
        return {
            "status": "completed",
            "deepfake_status": status,
            "confidence": round(score, 4),
            "synthetic_audio_score": round(score, 4),
            "calibrated_score": calibrated,
            "score_type": "calibrated_synthetic_audio_score" if calibrated is not None else "uncalibrated_synthetic_audio_score",
            "model_version": artifact.model_version,
            "audio_quality": quality,
            "segment_scores": segment_scores,
            "inference_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "explanation": explanation,
            **(transcript or unavailable_scam_result()),
        }
