"""Bounded audio-quality and segment helpers.

These checks are evidence-quality signals, not a speech recognizer.  They are
used to avoid presenting a model score for recordings that are silent, clipped,
or too noisy to support a meaningful result.
"""

from __future__ import annotations

import io
import wave
from typing import Any

import numpy as np


QUALITY_VERSION = "wave-quality-v1"
TARGET_SAMPLE_RATE = 16_000
DEFAULT_SEGMENT_SECONDS = 5.0
DEFAULT_MAX_SEGMENTS = 24


def _decode_pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(bytes(wav_bytes)), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            width = wav.getsampwidth()
            frames = wav.getnframes()
            raw = wav.readframes(frames)
    except (TypeError, ValueError, EOFError, wave.Error) as exc:
        raise ValueError("quality analysis requires PCM WAV bytes") from exc
    if channels < 1 or width not in (1, 2, 3, 4) or rate < 1 or frames < 1:
        raise ValueError("quality analysis received an invalid PCM WAV")
    expected = channels * frames * width
    if len(raw) != expected:
        raise ValueError("quality analysis received truncated PCM WAV")
    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 4:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        unsigned = (
            packed[:, 0].astype(np.int32)
            | (packed[:, 1].astype(np.int32) << 8)
            | (packed[:, 2].astype(np.int32) << 16)
        )
        signed = np.where(unsigned & 0x800000, unsigned - 0x1000000, unsigned)
        values = signed.astype(np.float64) / 8388608.0
    if values.size != frames * channels or not np.isfinite(values).all():
        raise ValueError("quality analysis received invalid PCM samples")
    return np.clip(values.reshape(frames, channels).mean(axis=1), -1.0, 1.0), rate


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples.astype(np.float64, copy=True)
    count = max(1, round(samples.size * target_rate / source_rate))
    if samples.size == 1:
        return np.full(count, samples[0], dtype=np.float64)
    return np.interp(
        np.linspace(0.0, samples.size - 1, count),
        np.arange(samples.size),
        samples,
    ).astype(np.float64)


def _frames(samples: np.ndarray, rate: int) -> np.ndarray:
    frame_length = max(1, round(rate * 0.025))
    hop = max(1, round(rate * 0.010))
    if samples.size <= frame_length:
        padded = np.pad(samples, (0, frame_length - samples.size))
        return padded.reshape(1, -1)
    count = 1 + (samples.size - frame_length) // hop
    return np.stack(
        [samples[index * hop : index * hop + frame_length] for index in range(count)]
    )


def analyze_audio_quality(wav_bytes: bytes) -> dict[str, Any]:
    """Return safe quality metrics; no waveform data is returned."""

    samples, source_rate = _decode_pcm(wav_bytes)
    normalized = _resample(samples, source_rate, TARGET_SAMPLE_RATE)
    frames = _frames(normalized, TARGET_SAMPLE_RATE)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    peak = np.max(np.abs(normalized)) if normalized.size else 0.0
    # A constant voiced tone is useful test evidence even though it has no
    # frame-to-frame dynamics. Cap the adaptive threshold so steady speech or
    # tones are not mislabeled as silence.
    active_threshold = max(0.008, min(float(np.percentile(rms, 75)) * 0.5, 0.05))
    active = rms >= active_threshold
    activity_ratio = float(np.mean(active))
    active_rms = float(np.mean(rms[active])) if np.any(active) else 0.0
    quiet_rms = float(np.mean(rms[~active])) if np.any(~active) else active_rms
    snr_db = 20.0 * np.log10(max(active_rms, 1e-6) / max(quiet_rms, 1e-6))
    clipping_ratio = float(np.mean(np.abs(normalized) >= 0.98))
    reasons: list[str] = []
    status = "usable"
    if normalized.size / TARGET_SAMPLE_RATE < 1.0 or activity_ratio < 0.05 or active_rms < 0.008:
        status = "insufficient_evidence"
        reasons.append("mostly_silent_or_no_detectable_activity")
    if clipping_ratio > 0.02:
        status = "review" if status == "usable" else status
        reasons.append("clipping_detected")
    if snr_db < 3.0 and status == "usable":
        status = "review"
        reasons.append("low_signal_to_noise_ratio")
    return {
        "version": QUALITY_VERSION,
        "status": status,
        "duration_seconds": round(float(normalized.size / TARGET_SAMPLE_RATE), 3),
        "sample_rate_hz": TARGET_SAMPLE_RATE,
        "activity_ratio": round(activity_ratio, 4),
        "clipping_ratio": round(clipping_ratio, 4),
        "snr_db": round(float(snr_db), 2),
        "peak_amplitude": round(float(peak), 4),
        "reasons": reasons,
    }


def _samples_to_wav(samples: np.ndarray) -> bytes:
    values = np.asarray(samples, dtype=np.float64)
    output = io.BytesIO()
    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(TARGET_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def segment_wavs(
    wav_bytes: bytes,
    *,
    segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> list[tuple[float, float, bytes]]:
    """Return bounded mono 16 kHz windows for segment-level inference."""

    if segment_seconds <= 0 or max_segments < 1:
        raise ValueError("segment settings are invalid")
    samples, source_rate = _decode_pcm(wav_bytes)
    normalized = _resample(samples, source_rate, TARGET_SAMPLE_RATE)
    window = max(1, round(segment_seconds * TARGET_SAMPLE_RATE))
    starts = list(range(0, max(1, normalized.size - 1), window))
    if normalized.size > window and len(starts) > max_segments:
        positions = np.linspace(0, len(starts) - 1, max_segments).round().astype(int)
        starts = [starts[index] for index in sorted(set(positions.tolist()))]
    result: list[tuple[float, float, bytes]] = []
    for start in starts[:max_segments]:
        end = min(normalized.size, start + window)
        segment = normalized[start:end]
        if segment.size / TARGET_SAMPLE_RATE < 1.0:
            if result:
                break
            segment = np.pad(segment, (0, TARGET_SAMPLE_RATE - segment.size))
            end = min(normalized.size, start + segment.size)
        result.append((
            round(start / TARGET_SAMPLE_RATE, 3),
            round(end / TARGET_SAMPLE_RATE, 3),
            _samples_to_wav(segment),
        ))
    return result


__all__ = [
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_SEGMENT_SECONDS",
    "QUALITY_VERSION",
    "analyze_audio_quality",
    "segment_wavs",
]
