"""Evaluate a saved model on a separate, provenance-audited corpus."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .audit import audit_dataset
from .evaluate import evaluate_arrays
from .model import dataset_fingerprint, load_model
from .preprocess import extract_features
from .train import DatasetError, collect_dataset


def evaluate_external(
    model_path: str | Path,
    dataset_dir: str | Path,
    *,
    metadata_path: str | Path | None = None,
    reference_metadata_path: str | Path | None = None,
    allow_provenance_gaps: bool = False,
) -> dict:
    """Evaluate without redrawing the model's training split.

    ``metadata_path`` is required by default.  This prevents a filename-only
    split from being presented as speaker/generator-disjoint validation.
    """

    if reference_metadata_path is None and not allow_provenance_gaps:
        raise DatasetError("reference metadata is required to establish speaker/generator disjointness")
    model = load_model(model_path)
    audit = audit_dataset(
        dataset_dir,
        metadata_path=metadata_path,
        reference_metadata_path=reference_metadata_path,
    )
    if audit["gaps"] and not allow_provenance_gaps:
        raise DatasetError(
            "external corpus is not provenance-ready: " + ", ".join(audit["gaps"])
        )
    records = collect_dataset(dataset_dir)
    fingerprint = dataset_fingerprint(
        [record.provenance() for record in records], records[0].manifest_sha256
    )
    if fingerprint == model.metadata.get("training_data_fingerprint"):
        raise DatasetError("external evaluation dataset must differ from training data")
    training_hashes = {
        record.get("pcm_sha256")
        for record in model.metadata.get("training", {}).get("dataset_records", [])
    }
    if training_hashes & {record.pcm_sha256 for record in records}:
        raise DatasetError("external evaluation dataset contains audio from model training data")
    features = np.vstack([extract_features(record.audio_bytes) for record in records])
    labels = np.asarray([record.label for record in records], dtype=np.int64)
    report = evaluate_arrays(model, features, labels)
    report.update(
        {
            "schema": "deepguard-external-evaluation",
            "version": 1,
            "external_validation": True,
            "model_version": model.metadata.get("model_version", "unknown"),
            "dataset_fingerprint": fingerprint,
            "dataset_directory": str(Path(dataset_dir).resolve()),
            "dataset_audit": audit,
            "speaker_generator_disjoint": not bool(
                reference_metadata_path is None
                or audit["metadata"]["reference_overlaps"]["speaker_id"]
                or audit["metadata"]["reference_overlaps"]["generator_id"]
            ),
            "evaluated_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "limitation": (
                "This report is valid for the supplied corpus only. It does not "
                "establish performance on callers, languages, codecs, or generators "
                "not represented in the audited metadata."
            ),
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--reference-metadata", required=True)
    parser.add_argument("--output")
    parser.add_argument("--allow-provenance-gaps", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = evaluate_external(
            args.model,
            args.dataset,
            metadata_path=args.metadata,
            reference_metadata_path=args.reference_metadata,
            allow_provenance_gaps=args.allow_provenance_gaps,
        )
    except (DatasetError, OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["evaluate_external"]
