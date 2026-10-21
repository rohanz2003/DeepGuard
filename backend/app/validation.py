"""Bounded WAV/MP3 validation with transient byte handoff.

WAV uploads keep their existing strict container checks. MP3 uploads are
decoded through a local FFmpeg executable into a bounded, mono 16 kHz,
signed-16-bit PCM WAV before they cross the inference boundary. The original
upload bytes remain available only to the explicit retention boundary.
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from werkzeug.utils import secure_filename


ALLOWED_MIMES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}
ALLOWED_MP3_MIMES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/x-mpeg",
    "audio/x-mp3",
    "audio/mpeg3",
    "audio/x-mpeg-3",
}
ALLOWED_RATES = {8000, 16000, 22050, 44100, 48000}
ALLOWED_CHANNELS = {1, 2}
ALLOWED_SAMPLE_WIDTHS = {1, 2, 3, 4}
_MP3_SAMPLE_RATE = 16000
_MP3_SAMPLE_WIDTH = 2
_MP3_DECODE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class AudioMetadata:
    filename: str
    mime_type: str
    size_bytes: int
    duration_seconds: float
    # ``audio_bytes`` is always a valid PCM WAV for the in-memory inference
    # worker. The request/scan payload copies metadata fields, never either
    # byte field, and repositories therefore cannot persist raw audio.
    audio_bytes: bytes = b""
    # Original upload bytes are used only when the caller explicitly requests
    # retained playback. For WAV this equals ``audio_bytes``; for MP3 it keeps
    # the compressed source while inference receives the decoded WAV.
    original_bytes: bytes = b""


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


def _read_upload(file_storage, max_bytes: int):
    filename = secure_filename(file_storage.filename or "")
    extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if not filename or extension not in {"wav", "mp3"}:
        raise AudioValidationError("This workspace currently accepts WAV or MP3 audio.")

    declared_mime = (file_storage.mimetype or "").lower().strip()
    allowed_mimes = ALLOWED_MIMES if extension == "wav" else ALLOWED_MP3_MIMES
    if declared_mime not in allowed_mimes:
        format_name = "WAV" if extension == "wav" else "MP3"
        raise AudioValidationError(
            f"The file MIME type does not match the supported {format_name} format."
        )

    payload = file_storage.read(max_bytes + 1)
    if not payload:
        raise AudioValidationError("The selected file is empty.")
    if len(payload) > max_bytes:
        raise AudioValidationError("The audio file is larger than the 25 MB limit.")
    return filename, extension, declared_mime, bytes(payload)


def _validate_wav_payload(
    filename: str,
    mime_type: str,
    payload: bytes,
    min_seconds: float,
    max_seconds: float,
    *,
    max_bytes: int | None = None,
    source_size_bytes: int | None = None,
    original_bytes: bytes | None = None,
):
    if max_bytes is not None and len(payload) > max_bytes:
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
        mime_type=mime_type,
        size_bytes=len(payload) if source_size_bytes is None else source_size_bytes,
        duration_seconds=duration,
        audio_bytes=payload,
        original_bytes=payload if original_bytes is None else original_bytes,
    )


def resolve_ffmpeg() -> str:
    """Resolve a local FFmpeg executable without downloading or shelling out."""

    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).resolve())

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError, AttributeError):
        bundled = None

    if bundled:
        bundled_path = Path(str(bundled)).expanduser()
        if bundled_path.is_file():
            return str(bundled_path.resolve())

    raise AudioValidationError("MP3 decoding is unavailable on this server.")


def _encode_pcm_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    try:
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(_MP3_SAMPLE_WIDTH)
            wav_file.setframerate(_MP3_SAMPLE_RATE)
            wav_file.writeframes(pcm)
    except (OSError, wave.Error) as exc:
        raise AudioValidationError("The decoded MP3 audio is invalid.") from exc
    return output.getvalue()


def decode_mp3_to_wav(
    payload: bytes,
    max_seconds: float,
    *,
    ffmpeg_executable: str | None = None,
    runner=None,
) -> bytes:
    """Decode bounded MP3 bytes to a mono 16 kHz signed-16-bit WAV.

    The decoder receives bytes through stdin and emits raw signed-16-bit PCM
    through stdout. ``-t`` and ``-fs`` provide an oversize sentinel, while the
    length check below rejects anything beyond the configured duration rather
    than silently truncating it. ``runner`` is injectable for deterministic
    tests without requiring a system or bundled FFmpeg binary.
    """

    if not payload:
        raise AudioValidationError("The selected file is empty.")
    if max_seconds <= 0:
        raise AudioValidationError("The configured audio duration limit is invalid.")

    executable = ffmpeg_executable or resolve_ffmpeg()
    decode_limit_seconds = max_seconds + 1.0
    max_pcm_bytes = math.ceil(
        decode_limit_seconds * _MP3_SAMPLE_RATE * _MP3_SAMPLE_WIDTH
    )
    command = [
        str(executable),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-ac",
        "1",
        "-ar",
        str(_MP3_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-t",
        str(decode_limit_seconds),
        "-fs",
        str(max_pcm_bytes),
        "-f",
        "s16le",
        "pipe:1",
    ]
    process_runner = runner or subprocess.run
    try:
        result = process_runner(
            command,
            input=bytes(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=True,
            timeout=_MP3_DECODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioValidationError("The MP3 file could not be decoded safely.") from exc
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        raise AudioValidationError(
            "The MP3 file is corrupt, unsupported, or could not be decoded."
        ) from exc
    except Exception as exc:
        # The decoder is an external boundary. Do not expose provider or
        # executable details in the API response if an injected/runtime runner
        # fails unexpectedly.
        raise AudioValidationError("The MP3 file could not be decoded safely.") from exc

    pcm = result.stdout
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise AudioValidationError("The decoded MP3 audio is invalid.")
    pcm = bytes(pcm)
    if not pcm or len(pcm) % _MP3_SAMPLE_WIDTH:
        raise AudioValidationError("The decoded MP3 audio is invalid.")
    if len(pcm) > max_pcm_bytes:
        raise AudioValidationError("Audio must be 5 minutes or shorter.")

    duration = len(pcm) / (_MP3_SAMPLE_RATE * _MP3_SAMPLE_WIDTH)
    if duration > max_seconds:
        raise AudioValidationError("Audio must be 5 minutes or shorter.")
    return _encode_pcm_wav(pcm)


def validate_wav(file_storage, max_bytes: int, min_seconds: float, max_seconds: float):
    """Validate one strict PCM WAV upload."""

    filename, extension, declared_mime, payload = _read_upload(
        file_storage, max_bytes
    )
    if extension != "wav":
        raise AudioValidationError("This workspace currently accepts WAV audio only.")
    return _validate_wav_payload(
        filename,
        declared_mime,
        payload,
        min_seconds,
        max_seconds,
        max_bytes=max_bytes,
    )


def validate_mp3(file_storage, max_bytes: int, min_seconds: float, max_seconds: float):
    """Validate and normalize one MP3 upload."""

    filename, extension, declared_mime, payload = _read_upload(
        file_storage, max_bytes
    )
    if extension != "mp3":
        raise AudioValidationError("This validator expects an MP3 audio file.")
    decoded_wav = decode_mp3_to_wav(payload, max_seconds)
    return _validate_wav_payload(
        filename,
        declared_mime,
        decoded_wav,
        min_seconds,
        max_seconds,
        # The original upload limit applies to the source bytes. The decoded
        # representation is bounded separately by duration and sample format.
        max_bytes=None,
        source_size_bytes=len(payload),
        original_bytes=payload,
    )


def validate_audio(file_storage, max_bytes: int, min_seconds: float, max_seconds: float):
    """Validate WAV or MP3 and return inference/retention metadata."""

    filename = secure_filename(file_storage.filename or "")
    extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if extension == "wav":
        return validate_wav(file_storage, max_bytes, min_seconds, max_seconds)
    if extension == "mp3":
        return validate_mp3(file_storage, max_bytes, min_seconds, max_seconds)
    raise AudioValidationError("This workspace currently accepts WAV or MP3 audio.")
