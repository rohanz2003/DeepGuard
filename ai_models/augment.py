"""Deterministic audio augmentations used only on training rows.

The augmentation boundary is deliberately NumPy-only and local.  It never
changes the prepared source files and it is designed to be called after the
train/holdout split so augmented copies cannot leak into evaluation.
"""

from __future__ import annotations

import io
import wave
from typing import Iterable

import numpy as np

from .preprocess import TARGET_SAMPLE_RATE, decode_and_resample


AUGMENTATION_VERSION = "wave-augment-v1"
AUGMENTATION_NAMES = (
    "noise",
    "telephone",
    "reverb",
    "compression",
    "pitch",
)


def _finite_samples(samples: np.ndarray) -> np.ndarray:
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("audio samples must be a finite non-empty vector")
    return np.clip(values, -1.0, 1.0)


def _resample(samples: np.ndarray, target_count: int) -> np.ndarray:
    values = _finite_samples(samples)
    target_count = int(target_count)
    if target_count < 1:
        raise ValueError("target sample count must be positive")
    if target_count == values.size:
        return values.copy()
    if values.size == 1:
        return np.full(target_count, values[0], dtype=np.float64)
    return np.interp(
        np.linspace(0.0, values.size - 1, target_count),
        np.arange(values.size),
        values,
    ).astype(np.float64)


def _noise(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    values = _finite_samples(samples)
    signal_rms = float(np.sqrt(np.mean(values * values)))
    # Keep quiet fixtures quiet enough to remain bounded while still making
    # the augmentation useful for ordinary speech recordings.
    snr_db = float(rng.uniform(10.0, 28.0))
    noise_rms = max(signal_rms, 1e-3) / (10.0 ** (snr_db / 20.0))
    noise = rng.normal(0.0, noise_rms, values.size)
    return np.clip(values + noise, -1.0, 1.0)


def _telephone(samples: np.ndarray, _rng: np.random.Generator | None = None) -> np.ndarray:
    values = _finite_samples(samples)
    # A narrow 300--3400 Hz band and 8 kHz resampling approximate the most
    # important speech-channel degradation without introducing an external
    # codec dependency into training.
    downsampled = _resample(values, max(1, round(values.size * 8000 / TARGET_SAMPLE_RATE)))
    spectrum = np.fft.rfft(downsampled)
    frequencies = np.fft.rfftfreq(downsampled.size, d=1.0 / 8000.0)
    spectrum[(frequencies < 300.0) | (frequencies > 3400.0)] = 0
    band_limited = np.fft.irfft(spectrum, n=downsampled.size).real
    restored = _resample(band_limited, values.size)
    # µ-law-style quantisation gives the signal a small codec-like footprint.
    magnitude = np.log1p(127.0 * np.abs(restored)) / np.log1p(127.0)
    encoded = np.sign(restored) * magnitude
    quantized = np.round(encoded * 127.0) / 127.0
    decoded_magnitude = (np.expm1(np.abs(quantized) * np.log1p(127.0)) / 127.0)
    return np.clip(np.sign(quantized) * decoded_magnitude, -1.0, 1.0)


def _reverb(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    values = _finite_samples(samples)
    output = values.copy()
    delays = [
        max(1, int(rng.uniform(0.025, 0.055) * TARGET_SAMPLE_RATE)),
        max(1, int(rng.uniform(0.075, 0.13) * TARGET_SAMPLE_RATE)),
        max(1, int(rng.uniform(0.16, 0.24) * TARGET_SAMPLE_RATE)),
    ]
    gains = [0.20, 0.11, 0.06]
    for delay, gain in zip(delays, gains):
        if delay >= values.size:
            continue
        output[delay:] += values[:-delay] * gain
    return np.clip(output, -1.0, 1.0)


def _compression(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    values = _finite_samples(samples)
    bits = int(rng.choice([7, 8, 10]))
    levels = float(2**bits - 1)
    quantized = np.round(((values + 1.0) * 0.5) * levels) / levels
    return np.clip(quantized * 2.0 - 1.0, -1.0, 1.0)


def _pitch(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    values = _finite_samples(samples)
    semitones = float(rng.uniform(-2.5, 2.5))
    factor = 2.0 ** (semitones / 12.0)
    # Resampling changes pitch while keeping the result within the duration
    # bounds accepted by the existing preprocessing contract.
    target_count = max(1, round(values.size / factor))
    warped = _resample(values, target_count)
    # Restore the original duration so the existing 1--300 second contract
    # remains valid for every augmented training row.
    return _resample(warped, values.size)


def augment_samples(
    samples: np.ndarray,
    kind: str,
    *,
    seed: int | None = None,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Return one augmented sample vector without mutating ``samples``."""

    if kind not in AUGMENTATION_NAMES:
        raise ValueError(f"unsupported audio augmentation: {kind}")
    generator = rng or np.random.default_rng(seed)
    operations = {
        "noise": _noise,
        "telephone": _telephone,
        "reverb": _reverb,
        "compression": _compression,
        "pitch": _pitch,
    }
    return operations[kind](_finite_samples(samples), generator)


def samples_to_wav(samples: np.ndarray, *, sample_rate: int = TARGET_SAMPLE_RATE) -> bytes:
    """Encode a finite mono floating-point vector as signed-16-bit PCM WAV."""

    if type(sample_rate) is not int or sample_rate <= 0:
        raise ValueError("sample_rate must be a positive integer")
    values = _finite_samples(samples)
    pcm = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def augment_wav(audio: bytes, kind: str, *, seed: int | None = None) -> bytes:
    """Decode one prepared WAV, apply an augmentation, and return PCM WAV."""

    samples = decode_and_resample(audio)
    augmented = augment_samples(samples, kind, seed=seed)
    return samples_to_wav(augmented)


def augmentation_metadata(kinds: Iterable[str], *, seed: int | None = None) -> dict:
    """Return stable provenance metadata for a training run."""

    selected = list(kinds)
    if any(kind not in AUGMENTATION_NAMES for kind in selected):
        raise ValueError("unsupported audio augmentation")
    return {
        "version": AUGMENTATION_VERSION,
        "methods": selected,
        "seed": seed,
        "applied_to": "training_split_only",
    }


__all__ = [
    "AUGMENTATION_NAMES",
    "AUGMENTATION_VERSION",
    "augment_samples",
    "augment_wav",
    "augmentation_metadata",
    "samples_to_wav",
]
