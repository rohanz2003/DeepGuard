"""Bounded dataset loading, duplicate-safe grouped splits, and reproducible training."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import unicodedata
import uuid
import wave
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from .model import (
    GROUPING_VERSIONS, LABEL_MAPPING, MODEL_VERSION, PCM_HASH_STRATEGY,
    LogisticRegressionModel, dataset_fingerprint, fingerprint_payload,
    membership_fingerprint, save_model,
)
from .preprocess import decode_and_resample, extract_features

MAX_AUDIO_BYTES = 25 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DatasetError(ValueError):
    """Raised when the dataset or saved held-out split contract is not met."""


def _read_audio(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise DatasetError("a dataset WAV exceeds the 25 MiB per-file limit")
        with path.open("rb") as stream:
            audio = stream.read(MAX_AUDIO_BYTES + 1)
        if len(audio) > MAX_AUDIO_BYTES:
            raise DatasetError("a dataset WAV exceeds the 25 MiB per-file limit")
        return audio
    except OSError as exc:
        raise DatasetError("could not read a dataset WAV") from exc


@dataclass(frozen=True)
class DatasetRecord:
    path: Path
    relative_path: str
    label: int
    pcm_sha256: str
    wav_sha256: str
    duration_seconds: float
    sources: tuple[dict[str, Any], ...]
    source_aliases: tuple[str, ...] = ()
    manifest_sha256: str | None = None
    preparation_counts: tuple[tuple[str, int], ...] = ()
    group_id: str = ""

    @property
    def audio_bytes(self) -> bytes:
        """Read one bounded file on demand instead of retaining the whole corpus."""

        audio = _read_audio(self.path)
        if hashlib.sha256(audio).hexdigest() != self.wav_sha256:
            raise DatasetError("dataset WAV changed after collection")
        return audio

    def provenance(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path, "label": self.label,
            "pcm_sha256": self.pcm_sha256, "wav_sha256": self.wav_sha256,
            "duration_seconds": self.duration_seconds,
            "sources": [dict(source) for source in self.sources],
            "source_aliases": list(self.source_aliases),
        }


def _safe_relative(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value) and "\\" not in value
        and ":" not in value and "\x00" not in value
        and not PurePosixPath(value).is_absolute()
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def _read_manifest(root: Path) -> tuple[
    dict[str, list[dict[str, Any]]], str | None, dict[str, list[str]], dict[str, int],
]:
    path = root / "manifest.json"
    if not path.exists():
        return {}, None, {}, {}
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
        if (not isinstance(manifest, dict) or not isinstance(manifest["records"], list)
                or not isinstance(manifest["excluded"], list) or not isinstance(manifest["summary"], dict)):
            raise DatasetError("conversion manifest has an incompatible schema")
        if ("schema_version" in manifest
                and (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1)):
            raise DatasetError("conversion manifest schema version is unsupported")
        if manifest["summary"].get("status", "prepared") != "prepared":
            raise DatasetError("conversion manifest does not describe a successfully prepared dataset")
        by_output: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sources: set[str] = set()
        source_outputs: dict[str, str] = {}
        for record in manifest["records"]:
            label, source, output = record["label"], record["source"], record["output"]
            if (type(label) is not int or label not in (0, 1) or not _safe_relative(source)
                    or not _safe_relative(output) or source in sources
                    or output.split("/")[0] != ("real" if label == 0 else "fake")
                    or source.split("/")[0] != ("real" if label == 0 else "fake")
                    or PurePosixPath(output).suffix.lower() != ".wav"
                    or not isinstance(record["source_sha256"], str)
                    or not _SHA256.fullmatch(record["source_sha256"])
                    or not isinstance(record["pcm_sha256"], str)
                    or not _SHA256.fullmatch(record["pcm_sha256"])
                    or type(record["duration_seconds"]) not in (int, float)
                    or not math.isfinite(record["duration_seconds"]) or record["duration_seconds"] <= 0):
                raise DatasetError("conversion manifest contains an invalid provenance record")
            sources.add(source)
            source_outputs[source] = output
            by_output[output].append({
                key: record[key] for key in
                ("source", "label", "source_sha256", "pcm_sha256", "duration_seconds")
            })
        aliases: dict[str, list[str]] = defaultdict(list)
        # The converter may remove duplicate WAVs before training. Their original
        # names still bridge filename groups and must not be silently forgotten.
        pending = []
        for excluded in manifest["excluded"]:
            if not isinstance(excluded, dict):
                raise DatasetError("conversion manifest exclusions are malformed")
            if excluded.get("reason") == "cross_label_pcm_conflict":
                raise DatasetError("conversion manifest reports conflicting decoded PCM labels")
            if excluded.get("status") == "duplicate":
                if (not _safe_relative(excluded.get("source"))
                        or not _safe_relative(excluded.get("duplicate_of"))
                        or excluded["source"] in sources):
                    raise DatasetError("conversion manifest duplicate aliases are malformed")
                sources.add(excluded["source"])
                pending.append(excluded)
        while pending:
            resolved = []
            for excluded in pending:
                target = source_outputs.get(excluded["duplicate_of"])
                if target is None:
                    continue
                alias = excluded["source"]
                if alias.split("/")[0] != target.split("/")[0]:
                    raise DatasetError("conversion manifest duplicate aliases have conflicting labels")
                source_outputs[alias] = target
                aliases[target].append(alias)
                resolved.append(excluded)
            if not resolved:
                raise DatasetError("conversion manifest has unresolved duplicate aliases")
            pending = [entry for entry in pending if entry not in resolved]
        counts = {key: manifest["summary"][key] for key in (
            "discovered_count", "prepared_count", "duplicate_count", "rejected_count",
        ) if key in manifest["summary"]}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise DatasetError("conversion manifest summary counts are malformed")
        counts.setdefault("duplicate_count", sum(len(values) for values in aliases.values()))
        return dict(by_output), hashlib.sha256(raw).hexdigest(), dict(aliases), counts
    except DatasetError:
        raise
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise DatasetError("could not read a valid conversion manifest") from exc


def collect_dataset(dataset_dir: str | Path) -> list[DatasetRecord]:
    root = Path(dataset_dir).resolve()
    if not root.is_dir():
        raise DatasetError("dataset directory does not exist")
    manifest, manifest_hash, aliases, preparation_counts = _read_manifest(root)
    records: list[DatasetRecord] = []
    observed: set[str] = set()
    content_labels: dict[str, int] = {}
    for class_name, label in LABEL_MAPPING.items():
        class_dir = root / class_name
        if not class_dir.is_dir():
            raise DatasetError("dataset requires real/ and fake/ class directories")
        files = sorted((path for path in class_dir.rglob("*")
                        if path.is_file() and path.suffix.lower() == ".wav"),
                       key=lambda path: path.relative_to(root).as_posix())
        if not files:
            raise DatasetError("each dataset class must contain WAV files")
        for path in files:
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                raise DatasetError("dataset WAV paths must remain inside the dataset")
            relative = path.relative_to(root).as_posix()
            observed.add(relative)
            audio = _read_audio(path)
            try:
                samples = decode_and_resample(audio)
                pcm_hash = hashlib.sha256(samples.astype("<f8", copy=False).tobytes()).hexdigest()
                with wave.open(io.BytesIO(audio), "rb") as wav:
                    duration = wav.getnframes() / wav.getframerate()
                    rate = wav.getframerate()
                    raw_pcm_hash = hashlib.sha256(wav.readframes(wav.getnframes())).hexdigest()
            except (ValueError, wave.Error, EOFError) as exc:
                raise DatasetError("a dataset WAV failed bounded PCM validation") from exc
            if pcm_hash in content_labels and content_labels[pcm_hash] != label:
                raise DatasetError("conflicting labels for identical decoded PCM audio")
            content_labels[pcm_hash] = label
            wav_hash = hashlib.sha256(audio).hexdigest()
            if manifest_hash is not None:
                if relative not in manifest:
                    raise DatasetError("conversion manifest does not cover every dataset WAV")
                provenance = manifest[relative]
                if any(source["label"] != label
                       or source["pcm_sha256"] not in (raw_pcm_hash, pcm_hash)
                       or abs(source["duration_seconds"] - duration) > max(1e-6, 1 / rate)
                       for source in provenance):
                    raise DatasetError("conversion manifest does not match decoded WAV provenance")
            else:
                provenance = [{
                    "source": relative, "label": label, "source_sha256": wav_hash,
                    "pcm_sha256": raw_pcm_hash, "duration_seconds": duration,
                }]
            records.append(DatasetRecord(
                path=path, relative_path=relative, label=label, pcm_sha256=pcm_hash,
                wav_sha256=wav_hash, duration_seconds=duration,
                sources=tuple(sorted(provenance, key=lambda source: source["source"])),
                source_aliases=tuple(sorted(aliases.get(relative, []))),
                manifest_sha256=manifest_hash, preparation_counts=tuple(sorted(preparation_counts.items())),
            ))
    if manifest_hash is not None and set(manifest) != observed:
        raise DatasetError("conversion manifest references missing dataset WAVs")
    return sorted(records, key=lambda record: record.relative_path)


def filename_group_key(source: str, label: int) -> tuple[str, str]:
    """Conservative grouping hints only; source text is never a model feature."""

    stem = unicodedata.normalize("NFKC", PurePosixPath(source).stem).casefold().lstrip("_ ")
    stem = re.sub(r"(?:\s*[\[(]\s*\d+\s*[\])]\s*)+$", "", stem).strip("_ -")
    speaker = re.fullmatch(r"(\d+)[_-]\d+[_-]\d+(?:[_-].*)?", stem)
    if label == 0 and speaker:
        return "speaker:" + str(int(speaker[1])), "speaker_id"
    for prefix in ("clip", "sentence", "line", "prompt"):
        match = re.fullmatch(prefix + r"[ _-]*(\d+)(?:[ _-].*)?", stem)
        if match:
            return prefix + ":" + str(int(match[1])), prefix + "_id"
    normalized = re.sub(r"[_\s-]+", " ", stem).strip()
    # Text variants with voice/version suffixes belong to the same content group.
    normalized = re.sub(r"(?:\s+(?:voice\s*\d+|male|female|v\d+))+$", "", normalized).strip()
    opaque = (re.fullmatch(r"[0-9a-f_-]{16,}", stem) is not None
              or (len(normalized.split()) == 1 and not normalized.isalpha()))
    if not opaque and len(normalized) >= 2 and any(char.isalpha() for char in normalized):
        return "text:" + normalized, "normalized_text"
    return "opaque:" + source, "opaque_source"


def prepare_records(
    records: list[DatasetRecord], *, group_by: str = "content",
) -> tuple[list[DatasetRecord], dict[str, Any]]:
    """Union content and filename aliases before deduplication, retaining bridges."""

    if group_by not in GROUPING_VERSIONS:
        raise DatasetError("group_by must be content or filename")
    if not records:
        raise DatasetError("dataset has no records")
    ordered = sorted(records, key=lambda record: record.relative_path)
    parents = list(range(len(ordered)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    by_pcm: dict[str, int] = {}
    by_filename: dict[str, int] = {}
    methods: Counter[str] = Counter()
    for index, record in enumerate(ordered):
        if record.label not in (0, 1):
            raise DatasetError("dataset labels must be binary")
        if record.pcm_sha256 in by_pcm:
            previous = by_pcm[record.pcm_sha256]
            if ordered[previous].label != record.label:
                raise DatasetError("conflicting labels for identical decoded PCM audio")
            union(index, previous)
        else:
            by_pcm[record.pcm_sha256] = index
        if group_by == "filename":
            for source_name in [source["source"] for source in record.sources] + list(record.source_aliases):
                key, method = filename_group_key(source_name, record.label)
                methods[method] += 1
                if key in by_filename:
                    union(index, by_filename[key])
                else:
                    by_filename[key] = index
    components: dict[int, set[str]] = defaultdict(set)
    for index, record in enumerate(ordered):
        components[find(index)].add(record.pcm_sha256)
    group_ids = {component: fingerprint_payload(sorted(hashes))
                 for component, hashes in components.items()}
    unique = [replace(ordered[index], group_id=group_ids[find(index)])
              for index in by_pcm.values()]
    limitations = [
        "Exact decoded-PCM duplicates are removed; near duplicates are not detected.",
        "This is a research split, not evidence of performance on unseen real-world callers.",
    ]
    if group_by == "filename":
        limitations += [
            "Filename groups are heuristics, not verified speaker or transcript identities.",
            "Clip/sentence/line/prompt IDs are content proxies; opaque names form independent groups.",
            "Unknown naming conventions and unmarked paraphrases may still leak speaker/content information.",
        ]
    else:
        limitations.append("Content grouping prevents exact duplicates only, not speaker or transcript overlap.")
    return unique, {
        "strategy": group_by, "version": GROUPING_VERSIONS[group_by],
        "method_counts": dict(sorted(methods.items())), "heuristic_limitations": limitations,
    }


def _split_prepared(
    records: list[DatasetRecord], *, seed: int, test_size: float,
) -> tuple[list[DatasetRecord], list[DatasetRecord]]:
    if type(seed) is not int or seed < 0:
        raise DatasetError("seed must be a non-negative integer")
    if type(test_size) not in (int, float) or not math.isfinite(test_size) or not 0 < test_size < 1:
        raise DatasetError("test_size must be finite and strictly between 0 and 1")
    groups: dict[str, list[DatasetRecord]] = defaultdict(list)
    for record in records:
        groups[record.group_id].append(record)
    keys = sorted(groups)
    rng = np.random.default_rng(seed)
    keys = [keys[index] for index in rng.permutation(len(keys))]
    counts = {key: np.array([sum(record.label == label for record in groups[key])
                            for label in (0, 1)], dtype=np.int64) for key in keys}
    total = np.sum(list(counts.values()), axis=0)
    if any(sum(counts[key][label] > 0 for key in keys) < 2 for label in (0, 1)):
        raise DatasetError("each class needs at least two independent groups for train and held-out data")
    target = total * test_size

    def score(value: np.ndarray) -> float:
        return float(np.sum(((value - target) / total) ** 2))

    # A feasible seed is either one mixed-class group, or one pure group per class.
    # With two binary labels this covers every feasible class-preserving split.
    candidates = [[key] for key in keys
                  if np.all(counts[key] > 0) and np.all(total - counts[key] > 0)]
    pure = [[key for key in keys if counts[key][label] > 0 and counts[key][1 - label] == 0]
            for label in (0, 1)]
    if all(pure):
        pair = [min(pure[label], key=lambda key: abs(counts[key][label] - target[label]))
                for label in (0, 1)]
        if np.all(total - sum((counts[key] for key in pair), np.zeros(2, dtype=np.int64)) > 0):
            candidates.append(pair)
    if not candidates:
        raise DatasetError("grouping cannot produce disjoint train and held-out sets with both classes")
    initial = min(candidates, key=lambda candidate: score(
        sum((counts[key] for key in candidate), np.zeros(2, dtype=np.int64))))
    selected = set(initial)
    heldout_counts = sum((counts[key] for key in initial), np.zeros(2, dtype=np.int64))
    for key in keys:
        if key in selected:
            continue
        proposed = heldout_counts + counts[key]
        if np.all(total - proposed > 0) and score(proposed) < score(heldout_counts) - 1e-15:
            selected.add(key)
            heldout_counts = proposed
    train = [record for record in records if record.group_id not in selected]
    test = [record for record in records if record.group_id in selected]
    return (sorted(train, key=lambda record: record.relative_path),
            sorted(test, key=lambda record: record.relative_path))


def split_records(
    records: list[DatasetRecord], *, seed: int = 1337,
    test_size: float = 0.2, group_by: str = "content",
) -> tuple[list[DatasetRecord], list[DatasetRecord]]:
    unique, _grouping = prepare_records(records, group_by=group_by)
    return _split_prepared(unique, seed=seed, test_size=test_size)


def _counts(records: list[DatasetRecord]) -> dict[str, int]:
    return {name: sum(record.label == label for record in records)
            for name, label in LABEL_MAPPING.items()}


def train_dataset(
    dataset_dir: str | Path, *, seed: int = 1337, test_size: float = 0.2,
    group_by: str = "content",
) -> tuple[LogisticRegressionModel, list[DatasetRecord], list[DatasetRecord]]:
    records = collect_dataset(dataset_dir)
    unique, grouping = prepare_records(records, group_by=group_by)
    train, test = _split_prepared(unique, seed=seed, test_size=test_size)
    try:
        features = np.vstack([extract_features(record.audio_bytes) for record in train])
    except ValueError as exc:
        raise DatasetError("training feature extraction failed") from exc
    labels = np.asarray([record.label for record in train], dtype=np.int64)
    model = LogisticRegressionModel.fit(features, labels)
    provenance = [record.provenance() for record in records]
    manifest_hash = records[0].manifest_sha256
    fingerprint = dataset_fingerprint(provenance, manifest_hash)
    trained_at = datetime.now(timezone.utc)
    labels_by_pcm = {record.pcm_sha256: record.label for record in unique}
    split: dict[str, Any] = {
        "strategy": "group-stratified-greedy-v1", "seed": seed, "test_size": test_size,
        "group_by": group_by, "grouping_version": GROUPING_VERSIONS[group_by],
        "group_ids": {record.pcm_sha256: record.group_id for record in unique},
    }
    for name, membership in (("train", train), ("test", test)):
        hashes = [record.pcm_sha256 for record in membership]
        split[name + "_pcm_sha256"] = hashes
        split[name + "_fingerprint"] = membership_fingerprint(hashes, labels_by_pcm)
    model.metadata = {
        "model_version": (f"{MODEL_VERSION}-{fingerprint[:12]}-"
                          f"{trained_at:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"),
        "trained_at_utc": trained_at.isoformat().replace("+00:00", "Z"),
        "label_mapping": dict(LABEL_MAPPING), "training_data_fingerprint": fingerprint,
        "training": {
            "data_fingerprint": fingerprint, "manifest_sha256": manifest_hash,
            "pcm_hash_strategy": PCM_HASH_STRATEGY, "dataset_records": provenance,
            "preparation_counts": dict(records[0].preparation_counts),
            "sample_count": len(records), "unique_sample_count": len(unique),
            "duplicate_count": len(records) - len(unique), "class_counts": _counts(records),
            "unique_class_counts": _counts(unique), "train_count": len(train),
            "test_count": len(test), "train_class_counts": _counts(train),
            "test_class_counts": _counts(test), "group_count": len({r.group_id for r in unique}),
            "train_group_count": len({r.group_id for r in train}),
            "test_group_count": len({r.group_id for r in test}),
            "seed": seed, "test_size": test_size, "group_by": group_by,
            "grouping": grouping, "split": split,
            "optimizer": {"epochs": 1000, "learning_rate": 0.1, "l2": 1e-3},
        },
    }
    return model, train, test


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the NumPy audio research baseline")
    parser.add_argument("--dataset", default="dataset", help="converted PCM WAV dataset with real/ and fake/")
    parser.add_argument("--output", default="ai_models/artifacts/baseline.json")
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--group-by", choices=sorted(GROUPING_VERSIONS), default="content")
    parser.add_argument("--overwrite", action="store_true", help="explicitly replace the model output")
    args = parser.parse_args(argv)
    if Path(args.output).exists() and not args.overwrite:
        parser.error("model output already exists; use --overwrite explicitly")
    try:
        model, train, test = train_dataset(
            args.dataset, seed=args.seed, test_size=args.test_size, group_by=args.group_by,
        )
        save_model(model, args.output, overwrite=args.overwrite)
    except (DatasetError, OSError, ValueError) as exc:
        parser.error(str(exc) if isinstance(exc, ValueError) else "could not write model output")
    print(json.dumps({
        "model_artifact": str(args.output), "model_version": model.metadata["model_version"],
        "artifact_sha256": model.artifact_sha256, "train_count": len(train), "test_count": len(test),
        "duplicate_count": model.metadata["training"]["duplicate_count"],
        "group_by": args.group_by, "group_count": model.metadata["training"]["group_count"],
        "training_data_fingerprint": model.metadata["training_data_fingerprint"],
        "note": "Research baseline trained; performance must be measured on the saved held-out set.",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
