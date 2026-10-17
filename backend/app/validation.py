"""Bounded, strict WAV/PCM validation. No audio is persisted."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from werkzeug.utils import secure_filename


ALLOWED_MIMES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}
ALLOWED_RATES = {8000, 16000, 22050, 44100, 48000}
ALLOWED_CHANNELS = {1, 2}
ALLOWED_SAMPLE_WIDTHS = {1, 2, 3, 4}


@dataclass(frozen=True)
class AudioMetadata:
    filename: str
    mime_type: str
    size_bytes: int
    duration_seconds: float
    # Transient bytes are available to the inference boundary only.  The
    # request/scan payload deliberately copies metadata fields, never this
    # field, and repositories therefore cannot persist raw audio.
    audio_bytes: bytes = b""


class AudioValidationError(ValueError):
    pass


def _chunk_end(payload: bytes, start: int, size: int) -> int:
    end = start + size
    if end > len(payload):
        raise AudioValidationError("The WAV file is corrupt or truncated.")
    # RIFF chunks are word aligned; a missing pad byte is malformed.
    padded_end = end + (size & 1)
    if padded_end > len(payload):
        raise AudioValidationError("The WAV file is corrupt or truncated.")
    return padded_end


def validate_wav(file_storage, max_bytes: int, min_seconds: float, max_seconds: float):
    filename = secure_filename(file_storage.filename or "")
    if not filename or "." not in filename or filename.rsplit(".", 1)[1].lower() != "wav":
        raise AudioValidationError("This workspace currently accepts WAV audio only.")

    declared_mime = (file_storage.mimetype or "").lower().strip()
    if declared_mime not in ALLOWED_MIMES:
        raise AudioValidationError("The file MIME type does not match the supported WAV format.")

    payload = file_storage.read(max_bytes + 1)
    if not payload:
        raise AudioValidationError("The selected file is empty.")
    if len(payload) > max_bytes:
        raise AudioValidationError("The audio file is larger than the 25 MB limit.")
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise AudioValidationError("The file signature is not a valid WAV container.")

    riff_size = struct.unpack_from("<I", payload, 4)[0]
    if riff_size != len(payload) - 8:
        raise AudioValidationError("The WAV container is corrupt or truncated.")

    offset = 12
    fmt = None
    data_size = None
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise AudioValidationError("The WAV file is corrupt or truncated.")
        chunk_id = payload[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", payload, offset + 4)[0]
        data_start = offset + 8
        padded_end = _chunk_end(payload, data_start, chunk_size)
        if chunk_id == b"fmt ":
            if fmt is not None or chunk_size < 16:
                raise AudioValidationError("The WAV format metadata is invalid.")
            fmt = struct.unpack_from("<HHIIHH", payload, data_start)
        elif chunk_id == b"data":
            if data_size is not None:
                raise AudioValidationError("The WAV file contains duplicate audio data.")
            data_size = chunk_size
        offset = padded_end

    if fmt is None or data_size is None:
        raise AudioValidationError("The WAV file is missing PCM audio data.")
    audio_format, channels, rate, _byte_rate, block_align, bits = fmt
    sample_width = bits // 8
    if audio_format != 1:
        raise AudioValidationError("Only uncompressed PCM WAV audio is supported.")
    if channels not in ALLOWED_CHANNELS or rate not in ALLOWED_RATES:
        raise AudioValidationError("The WAV channel count or sample rate is unsupported.")
    if bits % 8 or sample_width not in ALLOWED_SAMPLE_WIDTHS:
        raise AudioValidationError("The WAV sample width is unsupported.")
    if (
        block_align != channels * sample_width
        or _byte_rate != rate * block_align
        or not data_size
        or data_size % block_align
    ):
        raise AudioValidationError("The WAV PCM payload is corrupt.")

    duration = data_size / (rate * block_align)
    if duration < min_seconds:
        raise AudioValidationError("Audio must be at least 1 second long.")
    if duration > max_seconds:
        raise AudioValidationError("Audio must be 5 minutes or shorter.")

    return AudioMetadata(
        filename=filename,
        mime_type=declared_mime,
        size_bytes=len(payload),
        duration_seconds=duration,
        audio_bytes=payload,
    )
