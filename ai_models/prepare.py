"""Reproducible, local-only MP3/WAV preparation; originals are never modified.

The decoder returns raw PCM so that Python's wave writer can create finite RIFF
headers. Publication is transactional. Failed validation publishes only a private
failure manifest, never a partially usable dataset. Existing destinations are
always refused, including destinations from an earlier failed run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 300
DECODE_LIMIT_SECONDS = 301  # Oversize sentinel, not an accepted truncation.
MAX_DECODED_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * DECODE_LIMIT_SECONDS
DEFAULT_TIMEOUT_SECONDS = 30.0
CLASS_LABELS = {"real": 0, "fake": 1}
SUPPORTED_EXTENSIONS = {".mp3", ".wav"}


class PreparationError(ValueError):
    """A path-free error code, optionally with a published failure summary."""

    def __init__(self, reason: str, *, summary: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.summary = summary


class DecodeError(ValueError):
    """A path-free decoder/PCM rejection reason, safe for a private manifest."""


@dataclass(frozen=True)
class SourceAudio:
    path: Path
    relative: str
    label: int
    linked: bool = False


def _executable(candidate: str | Path) -> str | None:
    value = str(candidate)
    located = shutil.which(value)
    if located:
        return str(Path(located).resolve())
    path = Path(value).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.resolve())
    return None


def resolve_ffmpeg(explicit: str | Path | None = None) -> str:
    """Use an explicit executable, PATH, or an already installed wheel binary.

    No installer, download helper, or network operation is used here.
    """
    if explicit is not None:
        found = _executable(explicit)
        if not found:
            raise PreparationError("ffmpeg_argument_unavailable")
        return found
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).resolve())
    try:
        package = importlib.import_module("imageio_ffmpeg")
        found = _executable(package.get_ffmpeg_exe())
    except (ImportError, RuntimeError, OSError, AttributeError):
        found = None
    if not found:
        raise PreparationError("ffmpeg_not_installed")
    return found


def decode_pcm(source: Path, executable: str, timeout: float) -> bytes:
    """Decode one local input to at most ~9.6 MB of mono 16 kHz s16le PCM."""
    command = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-xerror",
        "-err_detect", "explode",
        "-protocol_whitelist", "file,pipe",
        "-i", str(source.resolve()),
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-t", str(DECODE_LIMIT_SECONDS),
        "-fs", str(MAX_DECODED_BYTES),
        "-f", "s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DecodeError("decode_timeout") from exc
    except subprocess.CalledProcessError as exc:
        raise DecodeError("decode_failed") from exc
    except OSError as exc:
        raise DecodeError("decoder_unavailable") from exc
    return result.stdout


def _validate_pcm(pcm: bytes) -> float:
    if not pcm:
        raise DecodeError("empty_decoded_audio")
    if len(pcm) > MAX_DECODED_BYTES:
        raise DecodeError("decoded_output_too_large")
    if len(pcm) % SAMPLE_WIDTH:
        raise DecodeError("incomplete_pcm_frame")
    duration = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
    if duration < MIN_DURATION_SECONDS:
        raise DecodeError("duration_below_1_second")
    if duration > MAX_DURATION_SECONDS:
        raise DecodeError("duration_above_300_seconds")
    return duration


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _linked(path: Path) -> bool:
    # resolve() also catches Windows junctions on Python versions lacking
    # Path.is_junction(). Never traverse links outside a class directory.
    return path.is_symlink() or path.resolve() != path


def _paths(source: str | Path, output: str | Path) -> tuple[Path, Path]:
    try:
        source_path = Path(source).expanduser().resolve(strict=True)
        raw_output = Path(output).expanduser()
        if raw_output.is_symlink():
            raise PreparationError("output_exists")
        output_path = raw_output.resolve()
    except (OSError, RuntimeError) as exc:
        raise PreparationError("invalid_source_or_output") from exc
    if not source_path.is_dir():
        raise PreparationError("source_not_directory")
    if output_path == source_path or source_path.is_relative_to(output_path):
        raise PreparationError("source_output_overlap")
    if output_path.is_relative_to(source_path):
        first_component = output_path.relative_to(source_path).parts[0].lower()
        if first_component in CLASS_LABELS:
            raise PreparationError("source_output_overlap")
    if output_path.exists():
        raise PreparationError("output_exists")
    for name in CLASS_LABELS:
        class_path = source_path / name
        if not class_path.is_dir() or _linked(class_path):
            raise PreparationError("missing_or_linked_class_directory")
    return source_path, output_path


def _discover(source: Path) -> tuple[list[SourceAudio], dict[str, int]]:
    inputs: list[SourceAudio] = []
    ignored: Counter[str] = Counter()

    def walk_error(_: OSError) -> None:
        raise PreparationError("source_unreadable")

    for name, label in CLASS_LABELS.items():
        root = source / name
        for current, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
            current_path = Path(current)
            safe_directories = []
            for directory in sorted(directories):
                if _linked(current_path / directory):
                    ignored["<linked_directory>"] += 1
                else:
                    safe_directories.append(directory)
            directories[:] = safe_directories
            for filename in sorted(files):
                path = current_path / filename
                extension = path.suffix.lower()
                if extension not in SUPPORTED_EXTENSIONS:
                    ignored[extension or "<none>"] += 1
                    continue
                inputs.append(SourceAudio(path, path.relative_to(source).as_posix(), label, _linked(path)))
    inputs.sort(key=lambda item: item.relative)
    return inputs, dict(sorted(ignored.items()))


def _write_wav(path: Path, pcm: bytes) -> None:
    with path.open("xb") as handle:
        with wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)


def _decode_source(item: SourceAudio, executable: str, timeout: float) -> tuple[bytes, str, float]:
    if item.linked:
        raise DecodeError("linked_source_not_allowed")
    if not item.path.is_file():
        raise DecodeError("source_not_regular_file")
    if item.path.stat().st_size == 0:
        raise DecodeError("empty_source")
    source_hash = _sha256(item.path)
    pcm = decode_pcm(item.path, executable, timeout)
    duration = _validate_pcm(pcm)
    if _sha256(item.path) != source_hash:
        raise DecodeError("source_changed_during_decode")
    return pcm, source_hash, duration


def _mark_conflicts(
    groups: dict[str, list[dict[str, Any]]], excluded: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str], int]:
    conflicts = [members for members in groups.values() if len({member["label"] for member in members}) > 1]
    sources = {member["source"] for members in conflicts for member in members}
    result = [entry for entry in excluded if entry["source"] not in sources]
    for members in conflicts:
        for member in members:
            opposite = next(other for other in members if other["label"] != member["label"])
            result.append({
                "source": member["source"],
                "reason": "cross_label_pcm_conflict",
                "status": "rejected",
                "duplicate_of": opposite["source"],
            })
    return sorted(result, key=lambda entry: entry["source"]), sources, len(conflicts)


def _summary(
    inputs: list[SourceAudio],
    candidates: list[dict[str, Any]],
    records: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    ignored: dict[str, int],
    failures: list[str],
    conflict_sources: set[str],
    conflict_groups: int,
) -> dict[str, Any]:
    counts = {name: sum(record["label"] == label for record in records) for name, label in CLASS_LABELS.items()}
    durations = {
        name: sum(record["duration_seconds"] for record in records if record["label"] == label)
        for name, label in CLASS_LABELS.items()
    }
    return {
        "status": "failed" if failures else "prepared",
        "discovered_count": len(inputs),
        "discovered_class_counts": {
            name: sum(item.label == label for item in inputs) for name, label in CLASS_LABELS.items()
        },
        "prepared_count": len(records),
        "class_counts": counts,
        "total_duration_seconds": sum(durations.values()),
        "class_duration_seconds": durations,
        "candidate_count": len(candidates),
        "candidate_class_counts": {
            name: sum(record["label"] == label for record in candidates) for name, label in CLASS_LABELS.items()
        },
        "duplicate_count": sum(entry["status"] == "duplicate" for entry in excluded),
        "rejected_count": sum(entry["status"] == "rejected" for entry in excluded),
        "conflict_count": len(conflict_sources),
        "conflict_group_count": conflict_groups,
        "ignored_counts_by_extension": ignored,
        "split_recommended": not failures and all(count >= 2 for count in counts.values()),
        "minimum_per_class_for_split": 2,
        "failure_reasons": failures,
    }


def prepare_dataset(
    source: str | Path,
    output: str | Path,
    *,
    ffmpeg: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    skip_invalid: bool = False,
) -> dict[str, Any]:
    """Publish schema-1 data or a failure manifest, then return/raise accordingly.

    A successful return is the full private manifest. A validation failure raises
    PreparationError with its path-free summary after publishing a manifest with
    no records or class directories. Setup/I/O failures may publish nothing.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise PreparationError("timeout_must_be_finite_and_positive")
    source_path, output_path = _paths(source, output)
    inputs, ignored = _discover(source_path)
    executable = resolve_ffmpeg(ffmpeg) if inputs else ""
    records: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    manifest: dict[str, Any]
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".audio-prepare-", dir=output_path.parent) as temporary:
            staged = Path(temporary) / "prepared"
            staged.mkdir()
            for name in CLASS_LABELS:
                (staged / name).mkdir()
            for item in inputs:
                try:
                    pcm, source_hash, duration = _decode_source(item, executable, timeout)
                except (DecodeError, OSError) as exc:
                    reason = str(exc) if isinstance(exc, DecodeError) else "source_unreadable"
                    excluded.append({"source": item.relative, "reason": reason, "status": "rejected"})
                    continue
                pcm_hash = hashlib.sha256(pcm).hexdigest()
                member = {"source": item.relative, "label": item.label}
                if pcm_hash in groups:
                    canonical = groups[pcm_hash][0]
                    groups[pcm_hash].append(member)
                    excluded.append({
                        "source": item.relative,
                        "reason": "decoded_pcm_duplicate",
                        "status": "duplicate",
                        "duplicate_of": canonical["source"],
                    })
                    del pcm
                    continue
                groups[pcm_hash] = [member]
                class_name = "real" if item.label == 0 else "fake"
                name_hash = hashlib.sha256(item.relative.encode("utf-8")).hexdigest()
                relative_output = f"{class_name}/{name_hash}.wav"
                _write_wav(staged / relative_output, pcm)
                records.append({
                    "source": item.relative,
                    "output": relative_output,
                    "label": item.label,
                    "source_sha256": source_hash,
                    "pcm_sha256": pcm_hash,
                    "duration_seconds": duration,
                })
                del pcm

            excluded, conflicting, conflict_group_count = _mark_conflicts(groups, excluded)
            candidates = [record for record in records if record["source"] not in conflicting]
            failures = []
            if conflicting:
                failures.append("cross_label_conflict")
            if not skip_invalid and any(entry["status"] == "rejected" for entry in excluded):
                failures.append("invalid_inputs")
            if any(not any(record["label"] == label for record in candidates) for label in CLASS_LABELS.values()):
                failures.append("empty_valid_class")
            published_records = [] if failures else candidates
            summary = _summary(
                inputs, candidates, published_records, excluded, ignored, failures, conflicting, conflict_group_count
            )
            manifest = {"schema_version": 1, "records": published_records, "excluded": excluded, "summary": summary}
            if failures:
                for name in CLASS_LABELS:
                    shutil.rmtree(staged / name)
            with (staged / "manifest.json").open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            if output_path.exists() or output_path.is_symlink():
                raise PreparationError("output_exists")
            os.rename(staged, output_path)
    except OSError as exc:
        raise PreparationError("preparation_io_failed") from exc
    if manifest["summary"]["status"] == "failed":
        raise PreparationError("dataset_validation_failed", summary=manifest["summary"])
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local root containing real/ and fake/.")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing destinations are refused.")
    parser.add_argument("--ffmpeg", help="Explicit local FFmpeg executable; otherwise PATH, then installed imageio-ffmpeg.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-file decoder timeout in seconds (default: 30).")
    parser.add_argument("--skip-invalid", action="store_true", help="Publish valid files while reporting rejects; never bypass label conflicts.")
    arguments = parser.parse_args(argv)
    try:
        manifest = prepare_dataset(
            arguments.source,
            arguments.output,
            ffmpeg=arguments.ffmpeg,
            timeout=arguments.timeout,
            skip_invalid=arguments.skip_invalid,
        )
    except PreparationError as exc:
        print(json.dumps(exc.summary or {"status": "failed", "failure_reasons": [exc.reason]}, sort_keys=True))
        return 2
    print(json.dumps(manifest["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
