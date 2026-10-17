"""Deterministic, bounded PCM-WAV preprocessing and feature extraction."""

from __future__ import annotations

import io
import wave
from typing import Any

import numpy as np

TARGET_SAMPLE_RATE = 16_000
MIN_DURATION_SECONDS = 1.0
MAX_DURATION_SECONDS = 300.0
FRAME_LENGTH = 400  # 25 ms at 16 kHz
HOP_LENGTH = 160  # 10 ms at 16 kHz
FFT_SIZE = 512
SPECTRAL_BANDS = 32
FEATURE_VERSION = "log-spectrum-rms-zcr-v1"
_EPSILON = 1e-12


class AudioValidationError(ValueError):
    """Raised when bytes are not a supported bounded PCM WAV."""


def feature_names() -> list[str]:
    """Return the ordered names for the feature vector."""

    return [
        *(f"log_spectrum_band_{index:02d}" for index in range(SPECTRAL_BANDS)),
        "rms_mean",
        "rms_std",
        "zero_crossing_rate_mean",
        "zero_crossing_rate_std",
    ]


def feature_contract() -> dict[str, Any]:
    """Return the serializable contract used by feature extraction."""

    return {
        "version": FEATURE_VERSION,
        "feature_names": feature_names(),
        "feature_count": SPECTRAL_BANDS + 4,
        "input": {
            "container": "RIFF/WAVE",
            "encoding": "uncompressed PCM",
            "sample_width_bytes": [1, 2, 3, 4],
            "channels": "one or more; averaged to mono",
            "duration_seconds": [MIN_DURATION_SECONDS, MAX_DURATION_SECONDS],
        },
        "preprocessing": {
            "target_sample_rate_hz": TARGET_SAMPLE_RATE,
            "resampling": "linear interpolation",
            "mono_conversion": "arithmetic mean across channels",
        },
        "log_spectrum": {
            "frame_length_samples": FRAME_LENGTH,
            "hop_length_samples": HOP_LENGTH,
            "fft_size": FFT_SIZE,
            "bands": SPECTRAL_BANDS,
            "power_floor": _EPSILON,
            "dc_bin": "excluded",
        },
    }


def _decode_pcm_samples(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    if not isinstance(wav_bytes, (bytes, bytearray, memoryview)):
        raise AudioValidationError("audio must be provided as in-memory WAV bytes")
    raw_bytes = bytes(wav_bytes)
    if not raw_bytes:
        raise AudioValidationError("audio bytes are empty")

    try:
        wav = wave.open(io.BytesIO(raw_bytes), "rb")
    except (wave.Error, EOFError) as exc:
        raise AudioValidationError("invalid WAV container") from exc

    try:
        with wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            frame_count = wav.getnframes()
            compression = wav.getcomptype()
            if compression != "NONE":
                raise AudioValidationError("only uncompressed PCM WAV is supported")
            if channels < 1 or channels > 8:
                raise AudioValidationError("WAV channel count must be between 1 and 8")
            if sample_rate < 1 or sample_width not in (1, 2, 3, 4):
                raise AudioValidationError("unsupported PCM WAV format")
            if frame_count < 1:
                raise AudioValidationError("audio contains no frames")
            duration = frame_count / sample_rate
            if duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
                raise AudioValidationError(
                    f"audio duration must be between {MIN_DURATION_SECONDS:g} and "
                    f"{MAX_DURATION_SECONDS:g} seconds"
                )

            pcm = wav.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise AudioValidationError("invalid WAV container") from exc

    expected_bytes = frame_count * channels * sample_width
    if len(pcm) != expected_bytes:
        raise AudioValidationError("WAV frame data is truncated")

    sample_count = frame_count * channels
    if sample_width == 1:
        values = (np.frombuffer(pcm, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif sample_width == 2:
        values = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    elif sample_width == 4:
        values = np.frombuffer(pcm, dtype="<i4").astype(np.float64) / 2147483648.0
    else:  # 24-bit PCM has no NumPy scalar dtype.
        packed = np.frombuffer(pcm, dtype=np.uint8).reshape(-1, 3)
        unsigned = (
            packed[:, 0].astype(np.int32)
            | (packed[:, 1].astype(np.int32) << 8)
            | (packed[:, 2].astype(np.int32) << 16)
        )
        signed = np.where(unsigned & 0x800000, unsigned - 0x1000000, unsigned)
        values = signed.astype(np.float64) / 8388608.0

    if values.size != sample_count or not np.isfinite(values).all():
        raise AudioValidationError("PCM sample data is invalid")
    mono = values.reshape(frame_count, channels).mean(axis=1)
    return np.clip(mono, -1.0, 1.0), sample_rate


def _resample_linear(samples: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == TARGET_SAMPLE_RATE:
        return samples.astype(np.float64, copy=True)
    target_count = max(1, int(round(samples.size * TARGET_SAMPLE_RATE / source_rate)))
    if samples.size == 1:
        return np.full(target_count, samples[0], dtype=np.float64)
    source_positions = np.linspace(0.0, samples.size - 1, target_count)
    return np.interp(source_positions, np.arange(samples.size), samples).astype(np.float64)


def decode_and_resample(wav_bytes: bytes) -> np.ndarray:
    """Validate WAV bytes, convert to mono, and return a 16 kHz signal."""

    samples, source_rate = _decode_pcm_samples(wav_bytes)
    return _resample_linear(samples, source_rate)


def extract_features(wav_bytes: bytes) -> np.ndarray:
    """Extract the fixed 36-value feature vector from in-memory WAV bytes."""

    samples = decode_and_resample(wav_bytes)
    if samples.size < FRAME_LENGTH:
        frame_count = 1
    else:
        frame_count = 1 + (samples.size - FRAME_LENGTH + HOP_LENGTH - 1) // HOP_LENGTH
    padded_length = (frame_count - 1) * HOP_LENGTH + FRAME_LENGTH
    padded = np.pad(samples, (0, max(0, padded_length - samples.size)))
    frames = np.stack(
        [padded[start : start + FRAME_LENGTH] for start in range(0, frame_count * HOP_LENGTH, HOP_LENGTH)]
    )

    window = np.hanning(FRAME_LENGTH)
    spectrum = np.abs(np.fft.rfft(frames * window, n=FFT_SIZE)) ** 2
    spectrum /= max(float(np.sum(window**2)), _EPSILON)
    spectrum = spectrum[:, 1:]  # omit DC; 256 bins remain.
    bands = np.array_split(spectrum, SPECTRAL_BANDS, axis=1)
    log_summary = np.array(
        [np.log10(np.maximum(band, _EPSILON)).mean() for band in bands], dtype=np.float64
    )

    rms = np.sqrt(np.mean(frames**2, axis=1) + _EPSILON)
    zcr = np.mean(frames[:, 1:] * frames[:, :-1] < 0.0, axis=1)
    features = np.concatenate(
        [
            log_summary,
            np.array([rms.mean(), rms.std(), zcr.mean(), zcr.std()], dtype=np.float64),
        ]
    )
    if features.shape != (SPECTRAL_BANDS + 4,) or not np.isfinite(features).all():
        raise AudioValidationError("feature extraction produced an invalid vector")
    return features
