"""Score calibration and cost-based threshold selection.

Calibration must use a separately reserved corpus.  The CLI refuses to use the
training dataset fingerprint recorded in a model artifact, which prevents the
most common accidental leakage path.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .evaluate import evaluate_arrays
from .model import dataset_fingerprint, load_model
from .preprocess import extract_features
from .train import DatasetError, collect_dataset


CALIBRATION_SCHEMA = "deepguard-score-policy"
CALIBRATION_VERSION = 1


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -60.0, 60.0)))


def _logit(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(values, dtype=np.float64), 1e-6, 1.0 - 1e-6)
    return np.log(clipped / (1.0 - clipped))


def fit_platt_calibrator(
    scores: np.ndarray,
    labels: np.ndarray,
    *,
    epochs: int = 800,
    learning_rate: float = 0.05,
    l2: float = 1e-3,
) -> dict[str, Any]:
    """Fit a deterministic logistic calibration over raw model scores."""

    raw = np.asarray(scores, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    if raw.ndim != 1 or y.ndim != 1 or raw.size != y.size or raw.size < 4:
        raise ValueError("calibration scores and labels must be aligned vectors")
    if not np.isfinite(raw).all() or not np.isfinite(y).all():
        raise ValueError("calibration data must be finite")
    if not np.all((raw >= 0.0) & (raw <= 1.0)) or set(y.tolist()) != {0.0, 1.0}:
        raise ValueError("calibration requires scores in [0, 1] and both labels")
    if type(epochs) is not int or epochs < 1 or learning_rate <= 0 or l2 < 0:
        raise ValueError("calibration optimization parameters are invalid")

    x = _logit(raw)
    scale = max(float(np.std(x)), 1.0)
    x = x / scale
    weight = 1.0
    bias = 0.0
    for _ in range(epochs):
        error = _sigmoid(weight * x + bias) - y
        weight -= learning_rate * (float(np.mean(error * x)) + l2 * weight)
        bias -= learning_rate * float(np.mean(error))
    # Convert the standardized fit back to the original logit coordinate.
    return {
        "method": "platt-v1",
        "a": float(weight / scale),
        "b": float(bias),
        "source": "separate_calibration_corpus",
    }


def apply_calibrator(scores: np.ndarray | float, calibrator: dict[str, Any]) -> np.ndarray:
    """Apply a validated Platt calibrator to raw scores."""

    if not isinstance(calibrator, dict) or calibrator.get("method") != "platt-v1":
        raise ValueError("unsupported score calibrator")
    a, b = calibrator.get("a"), calibrator.get("b")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise ValueError("calibrator parameters are invalid")
    if not math.isfinite(float(a)) or not math.isfinite(float(b)):
        raise ValueError("calibrator parameters must be finite")
    return _sigmoid(float(a) * _logit(np.asarray(scores, dtype=np.float64)) + float(b))


def select_threshold(
    scores: np.ndarray,
    labels: np.ndarray,
    *,
    false_positive_cost: float = 1.0,
    false_negative_cost: float = 1.0,
) -> dict[str, Any]:
    """Select the threshold minimizing the explicitly supplied review costs."""

    values = np.asarray(scores, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int64)
    if values.ndim != 1 or y.ndim != 1 or values.size != y.size or values.size == 0:
        raise ValueError("threshold scores and labels must be aligned vectors")
    if not np.isfinite(values).all() or not np.all((values >= 0) & (values <= 1)):
        raise ValueError("threshold scores must be finite values in [0, 1]")
    if set(y.tolist()) != {0, 1}:
        raise ValueError("threshold selection requires both labels")
    if false_positive_cost < 0 or false_negative_cost < 0:
        raise ValueError("classification costs must be non-negative")

    candidates = np.unique(np.concatenate((values, np.array([0.0, 0.5, 1.0]))))
    best: tuple[float, float, int, int, int] | None = None
    for threshold in candidates:
        predictions = values >= threshold
        fp = int(np.sum((y == 0) & predictions))
        fn = int(np.sum((y == 1) & ~predictions))
        cost = float(fp * false_positive_cost + fn * false_negative_cost)
        # Deterministic ties prefer the threshold nearest 0.5, then the higher
        # threshold (a conservative choice when the costs are equal).
        key = (cost, abs(float(threshold) - 0.5), -float(threshold), fp, fn)
        if best is None or key < best:
            best = key
            selected = float(threshold)
    assert best is not None
    return {
        "threshold": selected,
        "false_positive_cost": float(false_positive_cost),
        "false_negative_cost": float(false_negative_cost),
        "weighted_cost": best[0],
        "false_positives": best[3],
        "false_negatives": best[4],
    }


def build_score_policy(
    model,
    features: np.ndarray,
    labels: np.ndarray,
    *,
    false_positive_cost: float = 1.0,
    false_negative_cost: float = 1.0,
) -> dict[str, Any]:
    """Create a versioned policy from raw model output and a calibration set."""

    raw_scores = np.asarray(model.predict_proba(features), dtype=np.float64)
    calibrator = fit_platt_calibrator(raw_scores, labels)
    calibrated = apply_calibrator(raw_scores, calibrator)
    threshold = select_threshold(
        calibrated,
        labels,
        false_positive_cost=false_positive_cost,
        false_negative_cost=false_negative_cost,
    )
    measured = evaluate_arrays(
        model,
        features,
        labels,
    )
    calibrated_predictions = calibrated >= threshold["threshold"]
    tp = int(np.sum((labels == 1) & calibrated_predictions))
    fp = int(np.sum((labels == 0) & calibrated_predictions))
    fn = int(np.sum((labels == 1) & ~calibrated_predictions))
    tn = int(np.sum((labels == 0) & ~calibrated_predictions))
    calibrated_report = {
        "accuracy": (tp + tn) / int(labels.size),
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "brier_score": float(np.mean((calibrated - labels) ** 2)),
    }
    return {
        "schema": CALIBRATION_SCHEMA,
        "version": CALIBRATION_VERSION,
        "model_version": model.metadata.get("model_version", "unknown"),
        "score_type": "calibrated_synthetic_audio_score",
        "raw_score_type": "uncalibrated_synthetic_audio_score",
        "calibration": calibrator,
        "decision": threshold,
        "calibration_sample_count": int(labels.size),
        "raw_metrics": measured,
        "calibrated_metrics": calibrated_report,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def calibrate_external_dataset(
    model_path: str | Path,
    dataset_dir: str | Path,
    *,
    false_positive_cost: float = 1.0,
    false_negative_cost: float = 1.0,
) -> dict[str, Any]:
    model = load_model(model_path)
    records = collect_dataset(dataset_dir)
    fingerprint = dataset_fingerprint(
        [record.provenance() for record in records], records[0].manifest_sha256
    )
    if fingerprint == model.metadata.get("training_data_fingerprint"):
        raise DatasetError("calibration dataset must be separate from model training data")
    training_hashes = {
        record.get("pcm_sha256")
        for record in model.metadata.get("training", {}).get("dataset_records", [])
    }
    if training_hashes & {record.pcm_sha256 for record in records}:
        raise DatasetError("calibration dataset contains audio from model training data")
    features = np.vstack([extract_features(record.audio_bytes) for record in records])
    labels = np.asarray([record.label for record in records], dtype=np.int64)
    if set(labels.tolist()) != {0, 1}:
        raise DatasetError("calibration dataset must contain both classes")
    policy = build_score_policy(
        model,
        features,
        labels,
        false_positive_cost=false_positive_cost,
        false_negative_cost=false_negative_cost,
    )
    policy["dataset_fingerprint"] = fingerprint
    policy["dataset_count"] = len(records)
    policy["dataset_class_counts"] = {
        "real": int(np.sum(labels == 0)),
        "fake": int(np.sum(labels == 1)),
    }
    return policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate a model on a separate prepared dataset")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--false-positive-cost", type=float, default=1.0)
    parser.add_argument("--false-negative-cost", type=float, default=1.0)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    output = Path(args.output)
    if output.exists() and not args.overwrite:
        parser.error("policy output already exists; use --overwrite explicitly")
    try:
        policy = calibrate_external_dataset(
            args.model,
            args.dataset,
            false_positive_cost=args.false_positive_cost,
            false_negative_cost=args.false_negative_cost,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (DatasetError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(policy, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CALIBRATION_SCHEMA",
    "CALIBRATION_VERSION",
    "apply_calibrator",
    "build_score_policy",
    "calibrate_external_dataset",
    "fit_platt_calibrator",
    "select_threshold",
]
