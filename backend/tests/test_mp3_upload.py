import io
import subprocess
import unittest
from unittest.mock import patch

from app import create_app
from app.validation import decode_mp3_to_wav
from test_api import MemoryRepository, make_wav


class MemoryAudioStore:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def upload(self, payload, *, filename, content_type, expires_at):
        storage_id = f"audio-{len(self.objects) + 1}"
        self.objects[storage_id] = {
            "payload": bytes(payload),
            "filename": filename,
            "content_type": content_type,
            "expires_at": expires_at,
        }
        self.uploads.append(storage_id)
        return storage_id


class Mp3UploadTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.audio_store = MemoryAudioStore()
        self.received = []

        received = self.received

        class Inference:
            @staticmethod
            def analyze(metadata):
                received.append(metadata)
                return {
                    "status": "completed",
                    "deepfake_status": "low",
                    "confidence": 0.1,
                    "model_version": "test-v1",
                    "explanation": "Test inference result.",
                }

        self.app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": self.repository,
                "AUDIO_STORE": self.audio_store,
                "INFERENCE_SERVICE": Inference(),
                "MODEL_ARTIFACT_PATH": "",
            }
        )
        self.client = self.app.test_client()

    def post(self, payload, *, filename="sample.mp3", mimetype="audio/mpeg", retain=False):
        return self.client.post(
            "/api/scans",
            headers={"Authorization": "Bearer token"},
            data={
                "consent": "true",
                "retain_audio": "true" if retain else "false",
                "audio": (io.BytesIO(payload), filename, mimetype),
            },
            content_type="multipart/form-data",
        )

    def test_valid_mp3_is_normalized_for_inference(self):
        original = b"ID3" + b"deterministic mocked MP3 source"
        decoded_wav = make_wav(1.2).getvalue()

        with patch("app.validation.decode_mp3_to_wav", return_value=decoded_wav):
            response = self.post(original)

        self.assertEqual(response.status_code, 202)
        scan = response.get_json()["scan"]
        self.assertEqual(scan["filename"], "sample.mp3")
        self.assertEqual(scan["mime_type"], "audio/mpeg")
        self.assertEqual(scan["size_bytes"], len(original))
        self.assertEqual(scan["duration_seconds"], 1.2)
        self.app.extensions["scan_jobs"].wait_for_idle()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].audio_bytes, decoded_wav)
        self.assertEqual(self.received[0].original_bytes, original)
        self.assertNotIn("audio_bytes", scan)
        self.assertNotIn("original_bytes", scan)
        self.assertNotIn(
            "original_bytes", self.repository.items[("alice", scan["id"])]
        )

    def test_invalid_mp3_is_rejected_without_decoder_details(self):
        with patch("app.validation.resolve_ffmpeg", return_value="ffmpeg"), patch(
            "app.validation.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["ffmpeg"]),
        ):
            response = self.post(b"not an MP3 stream")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "The MP3 file is corrupt, unsupported, or could not be decoded.",
        )
        self.assertNotIn("ffmpeg", response.get_json()["error"])

    def test_original_mp3_upload_limit_is_enforced_before_decode(self):
        self.app.config["MAX_UPLOAD_BYTES"] = 8
        with patch("app.validation.decode_mp3_to_wav") as decoder:
            response = self.post(b"0123456789")

        self.assertEqual(response.status_code, 400)
        decoder.assert_not_called()

    def test_decoded_mp3_duration_over_300_seconds_is_rejected(self):
        oversized_pcm = b"\x00" * (301 * 16_000 * 2)
        completed = subprocess.CompletedProcess(
            ["ffmpeg"], 0, stdout=oversized_pcm
        )
        with patch("app.validation.resolve_ffmpeg", return_value="ffmpeg"), patch(
            "app.validation.subprocess.run", return_value=completed
        ):
            response = self.post(b"synthetic MP3 bytes")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Audio must be 5 minutes or shorter.")

    def test_decoded_mp3_duration_under_one_second_is_rejected(self):
        decoded_wav = make_wav(0.2).getvalue()
        with patch("app.validation.decode_mp3_to_wav", return_value=decoded_wav):
            response = self.post(b"synthetic MP3 bytes")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Audio must be at least 1 second long.")

    def test_retention_keeps_original_mp3_bytes_and_content_type(self):
        original = b"ID3" + b"original compressed bytes"
        decoded_wav = make_wav(1.2).getvalue()

        with patch("app.validation.decode_mp3_to_wav", return_value=decoded_wav):
            response = self.post(original, retain=True)

        self.assertEqual(response.status_code, 202)
        scan = response.get_json()["scan"]
        storage_id = self.audio_store.uploads[0]
        stored_audio = self.audio_store.objects[storage_id]
        self.assertEqual(stored_audio["payload"], original)
        self.assertEqual(stored_audio["filename"], "sample.mp3")
        self.assertEqual(stored_audio["content_type"], "audio/mpeg")
        self.assertEqual(self.repository.items[("alice", scan["id"])]["audio_storage_id"], storage_id)
        self.assertNotIn("audio_bytes", self.repository.items[("alice", scan["id"])])
        self.app.extensions["scan_jobs"].wait_for_idle()
        self.assertEqual(self.received[0].audio_bytes, decoded_wav)

    def test_wav_upload_still_passes_original_pcm_bytes_to_inference(self):
        wav_bytes = make_wav(1.2).getvalue()
        response = self.post(wav_bytes, filename="sample.wav", mimetype="audio/wav")

        self.assertEqual(response.status_code, 202)
        self.app.extensions["scan_jobs"].wait_for_idle()
        self.assertEqual(self.received[0].audio_bytes, wav_bytes)
        self.assertEqual(self.received[0].original_bytes, wav_bytes)
        self.assertEqual(self.audio_store.uploads, [])


class Mp3DecoderBoundaryTests(unittest.TestCase):
    def test_decoder_boundary_produces_strict_target_wav(self):
        pcm = b"\x00\x00" * 16_000
        completed = subprocess.CompletedProcess(["ffmpeg"], 0, stdout=pcm)
        with patch("app.validation.resolve_ffmpeg", return_value="ffmpeg"), patch(
            "app.validation.subprocess.run", return_value=completed
        ):
            wav_bytes = decode_mp3_to_wav(b"synthetic MP3 bytes", 300)

        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertEqual(wav_bytes[8:12], b"WAVE")
        self.assertEqual(len(wav_bytes), 32_044)


if __name__ == "__main__":
    unittest.main()
