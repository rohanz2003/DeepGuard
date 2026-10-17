"""Measured metrics on the exact, provenance-verified holdout saved during training."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .model import (
    GROUPING_VERSIONS, LABEL_MAPPING, LogisticRegressionModel, dataset_fingerprint,
    fingerprint_payload, load_model, membership_fingerprint, serialized_model,
)
from .preprocess import extract_features
from .train import DatasetError, collect_dataset, prepare_records


def _roc_auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives, negatives = labels == 1, labels == 0
    positive_count, negative_count = int(positives.sum()), int(negatives.sum())
    if positive_count == 0 or negative_count == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(scores.size, dtype=np.float64)
    start = 0
    while start < scores.size:
        end = start + 1
        while end < scores.size and sorted_scores[end] == sorted_scores[start]:
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end
    return float((ranks[positives].sum() - positive_count * (positive_count + 1) / 2)
                 / (positive_count * negative_count))


def evaluate_arrays(
    model: LogisticRegressionModel, features: np.ndarray, labels: np.ndarray, *,
    latency_seconds: float | None = None,
) -> dict[str, Any]:
    """Score aligned rows; optional latency is preprocessing time, before prediction."""

    y = np.asarray(labels)
    x = np.asarray(features)
    if (y.ndim != 1 or y.size == 0 or y.dtype.kind not in "biuf"
            or not np.isfinite(y).all() or not np.isin(y, (0, 1)).all()):
        raise ValueError("evaluation labels must be a non-empty one-dimensional binary array")
    if x.ndim != 2 or x.shape[0] != y.size:
        raise ValueError("evaluation features and labels must have aligned rows")
    if latency_seconds is not None and (
        not isinstance(latency_seconds, (int, float)) or not math.isfinite(latency_seconds)
        or latency_seconds < 0
    ):
        raise ValueError("preprocessing latency must be finite and non-negative")
    y = y.astype(np.int64)
    started = time.perf_counter()
    probabilities = np.asarray(model.predict_proba(x), dtype=np.float64)
    inference_seconds = time.perf_counter() - started
    if (probabilities.shape != y.shape or not np.isfinite(probabilities).all()
            or np.any(probabilities < 0) or np.any(probabilities > 1)):
        raise ValueError("model probabilities must be aligned finite values between 0 and 1")
    predictions = (probabilities >= model.threshold).astype(np.int64)
    tn = int(np.sum((y == 0) & (predictions == 0)))
    fp = int(np.sum((y == 0) & (predictions == 1)))
    fn = int(np.sum((y == 1) & (predictions == 0)))
    tp = int(np.sum((y == 1) & (predictions == 1)))
    # zero_division=0: zero true positives must not turn a measured zero F1 into null.
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0
    total_seconds = (latency_seconds or 0.0) + inference_seconds
    return {
        "sample_count": int(y.size), "threshold": model.threshold,
        "label_mapping": dict(LABEL_MAPPING), "positive_label": "fake",
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "confusion_matrix_labels": ["real", "fake"],
        "confusion_matrix_axes": {"rows": "actual", "columns": "predicted"},
        "accuracy": (tn + tp) / int(y.size), "precision": precision,
        "recall": recall, "f1": f1, "zero_division": 0,
        "roc_auc": _roc_auc(y, probabilities),
        "brier_score": float(np.mean((probabilities - y) ** 2)),
        "latency_ms_per_sample": total_seconds * 1000.0 / y.size,
        "inference_ms_per_sample": inference_seconds * 1000.0 / y.size,
        "latency_scope": ("audio_read_decode_features_and_batch_prediction"
                          if latency_seconds is not None else "batch_prediction_only"),
    }


def evaluate_dataset(
    model: LogisticRegressionModel, dataset_dir: str | Path, *, seed: int | None = None,
    test_size: float | None = None, group_by: str | None = None,
) -> dict[str, Any]:
    # Validate the in-memory model as strictly as a loaded one, including split disjointness.
    artifact_text = serialized_model(model)
    training = model.metadata["training"]
    saved = training["split"]
    for name, requested in (("seed", seed), ("test_size", test_size), ("group_by", group_by)):
        if requested is not None and (type(requested) is not type(saved[name]) or requested != saved[name]):
            # int fractions are equivalent to float fractions only when actually equal.
            if name != "test_size" or type(requested) not in (int, float) or requested != saved[name]:
                raise DatasetError(f"evaluation {name} does not match the saved training split")
    records = collect_dataset(dataset_dir)
    fingerprint = dataset_fingerprint([record.provenance() for record in records], records[0].manifest_sha256)
    if fingerprint != training["data_fingerprint"]:
        raise DatasetError("dataset fingerprint differs from the saved training dataset; retrain explicitly")
    unique, grouping = prepare_records(records, group_by=saved["group_by"])
    by_pcm = {record.pcm_sha256: record for record in unique}
    if {key: record.group_id for key, record in by_pcm.items()} != saved["group_ids"]:
        raise DatasetError("dataset groups differ from the saved training groups")
    # Never draw a new random split during evaluation, even if the splitter changes later.
    test = [by_pcm[value] for value in saved["test_pcm_sha256"]]
    labels = np.asarray([record.label for record in test], dtype=np.int64)
    if set(labels.tolist()) != {0, 1}:
        raise DatasetError("saved held-out membership must contain both classes")
    labels_by_pcm = {record.pcm_sha256: record.label for record in unique}
    test_fingerprint = membership_fingerprint(saved["test_pcm_sha256"], labels_by_pcm)
    if test_fingerprint != saved["test_fingerprint"]:
        raise DatasetError("held-out membership fingerprint is inconsistent")
    started = time.perf_counter()
    features = np.vstack([extract_features(record.audio_bytes) for record in test])
    preprocessing_seconds = time.perf_counter() - started
    report = evaluate_arrays(model, features, labels, latency_seconds=preprocessing_seconds)
    report.update({
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model_version": model.metadata["model_version"],
        "artifact_sha256": model.artifact_sha256 or hashlib.sha256(artifact_text.encode("utf-8")).hexdigest(),
        "artifact_fingerprint": fingerprint_payload(json.loads(artifact_text)),
        "training_data_fingerprint": training["data_fingerprint"],
        "current_dataset_fingerprint": fingerprint,
        "evaluation_split": {
            "seed": saved["seed"], "test_size": saved["test_size"], "group_by": saved["group_by"],
            "membership": "exact_saved_holdout", "test_fingerprint": test_fingerprint,
            "train_fingerprint": saved["train_fingerprint"], "test_count": len(test),
            "test_class_counts": training["test_class_counts"],
            "train_count": training["train_count"], "train_class_counts": training["train_class_counts"],
            "test_group_count": training["test_group_count"],
            "train_group_count": training["train_group_count"],
        },
        "dataset_counts": {key: training[key] for key in (
            "sample_count", "unique_sample_count", "duplicate_count", "class_counts",
            "unique_class_counts", "group_count",
        )},
        "preparation_counts": training["preparation_counts"],
        "grouping": grouping,
        "research_limitation": (
            "Measured performance applies only to this saved research holdout. "
            "Filename grouping is heuristic; exact-duplicate removal does not rule out "
            "near-duplicate, speaker, generator, or text leakage. This does not establish "
            "real-world scam detection or generalization to unseen callers."
        ),
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the exact saved audio baseline holdout")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", default="dataset")
    parser.add_argument("--seed", type=int, default=None, help="must equal the artifact; defaults to saved seed")
    parser.add_argument("--test-size", type=float, default=None, help="must equal the artifact; defaults to saved fraction")
    parser.add_argument("--group-by", choices=sorted(GROUPING_VERSIONS), default=None,
                        help="must equal the artifact; defaults to saved grouping")
    parser.add_argument("--output", help="optional aggregate JSON report path")
    parser.add_argument("--overwrite", action="store_true", help="explicitly replace the report output")
    args = parser.parse_args(argv)
    if args.output:
        if Path(args.output).resolve() == Path(args.model).resolve():
            parser.error("report output must differ from the model artifact")
        if Path(args.output).exists() and not args.overwrite:
            parser.error("report output already exists; use --overwrite explicitly")
    try:
        report = evaluate_dataset(load_model(args.model), args.dataset, seed=args.seed,
                                  test_size=args.test_size, group_by=args.group_by)
        rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w" if args.overwrite else "x", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
    except (DatasetError, ValueError, OSError) as exc:
        parser.error(str(exc) if isinstance(exc, ValueError) else "could not write evaluation report")
    print(rendered, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
