"""Dataset provenance and quality audit reports.

The audit intentionally reports missing provenance instead of inferring it from
filenames.  A production validation corpus should provide a sidecar CSV or
JSON with ``path``, ``label``, ``speaker_id``, ``generator_id``, ``language``,
``license`` and ``codec`` columns.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import wave
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REQUIRED_METADATA_FIELDS = (
    "path",
    "label",
    "speaker_id",
    "generator_id",
    "language",
    "license",
    "codec",
)


def _metadata_rows(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source.suffix.lower() == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    raw = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("records", raw.get("items", []))
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise ValueError("metadata must be a CSV or a JSON list of objects")
    return [dict(row) for row in raw]


def _label(value: Any) -> int | None:
    if value in (0, "0", "real", "bonafide", "bonafide_voice"):
        return 0
    if value in (1, "1", "fake", "synthetic", "spoof"):
        return 1
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wav_info(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as wav:
            return {
                "sample_rate": wav.getframerate(),
                "channels": wav.getnchannels(),
                "sample_width": wav.getsampwidth(),
                "duration_seconds": round(wav.getnframes() / wav.getframerate(), 4),
            }
    except (OSError, EOFError, wave.Error):
        return {"invalid_wav": True}


def audit_dataset(
    dataset_dir: str | Path,
    *,
    metadata_path: str | Path | None = None,
    reference_metadata_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a finite, JSON-safe audit report for source or prepared data."""

    root = Path(dataset_dir).resolve()
    if not root.is_dir():
        raise ValueError("dataset directory does not exist")
    files = sorted(
        path
        for label in ("real", "fake")
        for path in (root / label).rglob("*")
        if path.is_file() and path.suffix.lower() in {".wav", ".mp3"}
    )
    if not files:
        raise ValueError("dataset contains no WAV or MP3 files")

    duplicate_paths: dict[str, list[str]] = defaultdict(list)
    class_counts = Counter()
    extension_counts = Counter()
    audio_stats: Counter[str] = Counter()
    invalid_files: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        class_name = relative.split("/", 1)[0]
        class_counts[class_name] += 1
        extension_counts[path.suffix.lower().lstrip(".")] += 1
        digest = _sha256(path)
        duplicate_paths[digest].append(relative)
        if path.suffix.lower() == ".wav":
            info = _wav_info(path)
            if info.get("invalid_wav"):
                invalid_files.append(relative)
            else:
                audio_stats[f"sample_rate_{info['sample_rate']}"] += 1
                audio_stats[f"channels_{info['channels']}"] += 1
                audio_stats[f"sample_width_{info['sample_width']}"] += 1

    metadata_report: dict[str, Any] = {
        "status": "missing",
        "missing_fields": list(REQUIRED_METADATA_FIELDS),
        "rows": 0,
        "missing_paths": [path.relative_to(root).as_posix() for path in files],
        "label_mismatches": [],
        "duplicate_metadata_paths": [],
        "unique_values": {},
        "reference_overlaps": {"speaker_id": [], "generator_id": []},
    }
    metadata_rows: list[dict[str, Any]] = []
    if metadata_path:
        metadata_rows = _metadata_rows(metadata_path)
        metadata_report["status"] = "present"
        metadata_report["rows"] = len(metadata_rows)
        keys = set().union(*(row.keys() for row in metadata_rows)) if metadata_rows else set()
        metadata_report["missing_fields"] = [field for field in REQUIRED_METADATA_FIELDS if field not in keys]
        by_path: dict[str, dict[str, Any]] = {}
        for row in metadata_rows:
            value = str(row.get("path", row.get("relative_path", ""))).replace("\\", "/")
            if not value:
                continue
            if value in by_path:
                metadata_report["duplicate_metadata_paths"].append(value)
            by_path[value] = row
        metadata_report["missing_paths"] = [
            path.relative_to(root).as_posix()
            for path in files
            if path.relative_to(root).as_posix() not in by_path
        ]
        for file in files:
            relative = file.relative_to(root).as_posix()
            row = by_path.get(relative)
            if not row:
                continue
            expected = 0 if relative.split("/", 1)[0] == "real" else 1
            if _label(row.get("label")) != expected:
                metadata_report["label_mismatches"].append(relative)
        for field in REQUIRED_METADATA_FIELDS[2:]:
            metadata_report["unique_values"][field] = sorted({
                str(row[field]).strip()
                for row in metadata_rows
                if str(row.get(field, "")).strip()
            })

        if reference_metadata_path:
            reference = _metadata_rows(reference_metadata_path)
            for field in ("speaker_id", "generator_id"):
                current = {
                    str(row.get(field)).strip()
                    for row in metadata_rows
                    if str(row.get(field, "")).strip()
                }
                previous = {
                    str(row.get(field)).strip()
                    for row in reference
                    if str(row.get(field, "")).strip()
                }
                metadata_report["reference_overlaps"][field] = sorted(current & previous)

    duplicate_groups = [sorted(values) for values in duplicate_paths.values() if len(values) > 1]
    gaps = []
    if metadata_report["status"] == "missing":
        gaps.append("provenance_metadata_missing")
    gaps.extend(f"metadata_field_missing:{field}" for field in metadata_report["missing_fields"])
    if metadata_report["missing_paths"]:
        gaps.append("metadata_path_coverage_incomplete")
    if metadata_report["label_mismatches"]:
        gaps.append("label_folder_mismatch")
    if metadata_report["reference_overlaps"]["speaker_id"]:
        gaps.append("speaker_overlap_with_reference")
    if metadata_report["reference_overlaps"]["generator_id"]:
        gaps.append("generator_overlap_with_reference")

    return {
        "schema": "deepguard-dataset-audit",
        "version": 1,
        "dataset": str(root),
        "file_count": len(files),
        "class_counts": dict(sorted(class_counts.items())),
        "extension_counts": dict(sorted(extension_counts.items())),
        "duplicate_groups": duplicate_groups,
        "duplicate_file_count": sum(len(group) - 1 for group in duplicate_groups),
        "invalid_wav_files": invalid_files,
        "audio_stats": dict(sorted(audio_stats.items())),
        "metadata": metadata_report,
        "gaps": sorted(set(gaps)),
        "ready_for_disjoint_validation": not gaps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit dataset provenance and audio quality")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--metadata")
    parser.add_argument("--reference-metadata")
    parser.add_argument("--output")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit_dataset(
            args.dataset,
            metadata_path=args.metadata,
            reference_metadata_path=args.reference_metadata,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 2 if args.strict and report["gaps"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["REQUIRED_METADATA_FIELDS", "audit_dataset"]
