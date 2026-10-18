import io
import json
import os
import struct
import tempfile
import unittest
import wave
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import create_app
from app.config import Config
from app.inference import (
    JsonModelArtifactLoader,
    ModelArtifact,
    ModelInferenceService,
)


def make_wav(duration=1.2, noisy=False, sample_rate=16000):
    buffer = io.BytesIO()
    frames = bytearray()
    for index in range(int(sample_rate * duration)):
        value = (index % 127) if noisy else 0
        frames.extend(struct.pack("<h", value))
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(frames))
    buffer.seek(0)
    return buffer


class MemoryRepository:
    def __init__(self):
        self.items = {}

    def create(self, user_id, scan):
        self.items[(user_id, scan["id"])] = dict(scan)
        return dict(scan)

    def update(self, user_id, scan_id, updates):
        scan = self.items.get((user_id, scan_id))
        if scan is None:
            return None
        scan.update({key: value for key, value in updates.items() if key not in {
            "audio", "audio_bytes", "transcript", "raw_transcript"
        }})
        return dict(scan)

    def list(self, user_id, now):
        result = []
        for key, scan in list(self.items.items()):
            if key[0] != user_id:
                continue
            if datetime.fromisoformat(scan["expires_at"]) <= now:
                del self.items[key]
                continue
            result.append(dict(scan))
        return sorted(result, key=lambda scan: scan["created_at"], reverse=True)

    def get(self, user_id, scan_id, now):
        scan = self.items.get((user_id, scan_id))
        if scan is None:
            return None
        if datetime.fromisoformat(scan["expires_at"]) <= now:
            del self.items[(user_id, scan_id)]
            return None
        return dict(scan)

    def delete(self, user_id, scan_id):
        return self.items.pop((user_id, scan_id), None) is not None


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()

        def verify(token):
            if token == "alice-token":
                return {"id": "alice", "email": "alice@example.test"}
            if token == "bob-token":
                return {"id": "bob", "email": "bob@example.test"}
            raise ValueError("invalid")

        self.app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": verify,
                "SCAN_REPOSITORY": self.repository,
                "MAX_REQUEST_BYTES": 27 * 1024 * 1024,
                "MODEL_ARTIFACT_PATH": "",
            }
        )
        self.client = self.app.test_client()

    def auth(self, token="alice-token"):
        return {"Authorization": f"Bearer {token}"}

    def upload(self, payload=None, filename="sample.wav", mimetype="audio/wav", consent="true"):
        return self.client.post(
            "/api/scans",
            headers=self.auth(),
            data={"consent": consent, "audio": (payload or make_wav(), filename, mimetype)},
            content_type="multipart/form-data",
        )

    def wait_for_jobs(self):
        self.app.extensions["scan_jobs"].wait_for_idle()

    def test_public_health_reports_model_and_injected_firebase_readiness(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["model"]["status"], "unavailable")
        self.assertEqual(body["firebase"]["status"], "ready")

    def test_firestore_storage_failures_are_explained_without_provider_details(self):
        class FailingRepository(MemoryRepository):
            def list(self, _user_id, _now):
                raise PermissionError("provider-specific details must not be returned")

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": FailingRepository(),
            }
        )
        response = app.test_client().get("/api/scans", headers=self.auth())
        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertIn("Firestore storage is unavailable", body["error"])
        self.assertNotIn("provider-specific", body["error"])

    def test_health_reports_injected_model_readiness_and_version(self):
        class ReadyInference:
            def readiness(self):
                return {"status": "ready", "version": "fake-v1"}

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": MemoryRepository(),
                "INFERENCE_SERVICE": ReadyInference(),
            }
        )
        model = app.test_client().get("/api/health").get_json()["model"]
        self.assertEqual(model, {"status": "ready", "version": "fake-v1"})

    def test_root_route_explains_that_this_is_the_api(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["health_url"], "/api/health")

    def test_auth_absent_and_invalid(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.auth("bad")).status_code, 401)

    def test_auth_me_contains_only_firebase_identity(self):
        response = self.client.get("/api/auth/me", headers=self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"user": {"id": "alice", "email": "alice@example.test"}})

    def test_consent_and_file_validation_errors(self):
        self.assertEqual(self.upload(consent="false").status_code, 400)
        self.assertEqual(self.upload(io.BytesIO(), "empty.wav").status_code, 400)
        self.assertEqual(self.upload(make_wav(), "sample.mp3").status_code, 400)
        self.assertEqual(self.upload(make_wav(), "sample.wav", "audio/mpeg").status_code, 400)
        self.assertEqual(self.upload(io.BytesIO(b"not wav"), "sample.wav").status_code, 400)

    def test_corrupt_truncated_short_long_and_noisy_audio(self):
        corrupt = io.BytesIO(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        self.assertEqual(self.upload(corrupt).status_code, 400)
        truncated = make_wav()
        truncated.seek(0)
        self.assertEqual(self.upload(io.BytesIO(truncated.read()[:-5])).status_code, 400)
        self.assertEqual(self.upload(make_wav(0.2)).status_code, 400)
        self.assertEqual(self.upload(make_wav(300.1)).status_code, 400)
        noisy = self.upload(make_wav(noisy=True))
        self.assertEqual(noisy.status_code, 202)
        self.assertEqual(noisy.get_json()["status"], "pending")

    def test_upload_contract_has_null_model_output_and_no_audio_storage(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "pending")
        scan_id = response.get_json()["scan"]["id"]
        self.wait_for_jobs()
        scan = self.client.get(f"/api/scans/{scan_id}", headers=self.auth()).get_json()["scan"]
        self.assertEqual(scan["status"], "completed")
        self.assertEqual(scan["deepfake_status"], "inconclusive")
        self.assertEqual(scan["scam_status"], "unavailable")
        self.assertIsNone(scan["scam_score"])
        self.assertEqual(scan["scam_categories"], [])
        self.assertIn("no transcript", scan["scam_explanation"].lower())
        self.assertIsNone(scan["confidence"])
        self.assertEqual(scan["model_version"], "not-configured")
        self.assertNotIn("audio", self.repository.items)

    def test_injected_inference_receives_bytes_but_scan_does_not_store_them(self):
        received = []

        class FakeInference:
            def analyze(self, metadata):
                received.append(metadata.audio_bytes)
                return {
                    "status": "complete",
                    "deepfake_status": "low",
                    "scam_status": "unavailable",
                    "confidence": 0.1,
                    "model_version": "fake-v1",
                    "explanation": "Probabilistic test result.",
                }

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": self.repository,
                "INFERENCE_SERVICE": FakeInference(),
            }
        )
        response = app.test_client().post(
            "/api/scans",
            headers=self.auth(),
            data={"consent": "true", "audio": (make_wav(), "sample.wav", "audio/wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        app.extensions["scan_jobs"].wait_for_idle()
        self.assertTrue(received and received[0].startswith(b"RIFF"))
        self.assertNotIn("audio_bytes", response.get_json()["scan"])

    def test_ownership_list_get_and_delete(self):
        scan_id = self.upload().get_json()["scan"]["id"]
        self.assertEqual(self.client.get("/api/scans", headers=self.auth()).get_json()["stats"]["total_scans"], 1)
        self.assertEqual(self.client.get(f"/api/scans/{scan_id}", headers=self.auth("bob-token")).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/scans/{scan_id}", headers=self.auth("bob-token")).status_code, 404)
        deleted = self.client.delete(f"/api/scans/{scan_id}", headers=self.auth())
        self.assertEqual(deleted.get_json(), {"deleted": True, "scan_id": scan_id})
        self.assertEqual(self.client.get(f"/api/scans/{scan_id}", headers=self.auth()).status_code, 404)

    def test_retention_is_enforced_on_reads(self):
        scan_id = self.upload().get_json()["scan"]["id"]
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.repository.items[("alice", scan_id)]["expires_at"] = old
        listed = self.client.get("/api/scans", headers=self.auth())
        self.assertEqual(listed.get_json()["stats"]["total_scans"], 0)

    def test_file_limit_and_total_request_limit(self):
        self.app.config["MAX_UPLOAD_BYTES"] = 1024
        self.assertEqual(self.upload(make_wav()).status_code, 400)
        bounded = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice", "email": "a@example.test"},
                "SCAN_REPOSITORY": MemoryRepository(),
                "MAX_UPLOAD_BYTES": 512,
                "MAX_REQUEST_BYTES": 1024,
            }
        )
        response = bounded.test_client().post(
            "/api/scans",
            headers=self.auth(),
            data={"consent": "true", "audio": (make_wav(), "sample.wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)


class ModelLoadingTestCase(unittest.TestCase):
    def test_missing_artifact_is_unavailable(self):
        loader = JsonModelArtifactLoader("")
        self.assertFalse(loader.load())
        self.assertFalse(loader.ready)

    def test_malformed_artifact_is_unavailable_without_importing_runtime(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as artifact:
            json.dump({"schema": "not-deepguard", "version": 1}, artifact)
            path = artifact.name
        try:
            loader = JsonModelArtifactLoader(path)
            self.assertFalse(loader.load())
            self.assertFalse(loader.ready)
        finally:
            os.unlink(path)

    def test_injected_fake_model_maps_score_and_keeps_scam_unavailable(self):
        class FakeLoader:
            ready = True
            artifact = ModelArtifact(
                model_version="fake-v1",
                feature_contract={"version": 1, "name": "pcm"},
            )
            preprocessor = staticmethod(lambda audio, _contract: audio)
            model = staticmethod(lambda features, _contract: 0.8)

        # The adapter accepts a model instance with the declared predictor.
        class FakeModel:
            @staticmethod
            def predict(features, _contract):
                return 0.8

        FakeLoader.model = FakeModel()
        service = ModelInferenceService({"MODEL_THRESHOLD": 0.7}, loader=FakeLoader())
        result = service.analyze(type("Metadata", (), {"audio_bytes": b"RIFF"})())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["deepfake_status"], "high")
        self.assertEqual(result["scam_status"], "unavailable")
        self.assertEqual(result["model_version"], "fake-v1")

class ConfigurationTestCase(unittest.TestCase):
    def test_production_cannot_enable_emulators(self):
        with patch.dict(
            os.environ,
            {"ENVIRONMENT": "production", "USE_FIREBASE_EMULATORS": "true", "FIREBASE_PROJECT_ID": "p"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_unconfigured_firebase_returns_503_for_protected_route(self):
        with patch.dict(
            os.environ,
            {
                "FIREBASE_PROJECT_ID": "",
                "GOOGLE_APPLICATION_CREDENTIALS": "",
                "USE_FIREBASE_EMULATORS": "false",
            },
            clear=False,
        ):
            app = create_app({"TESTING": True})
            self.assertEqual(app.test_client().get("/api/health").get_json()["firebase"]["status"], "unavailable")
            self.assertEqual(
                app.test_client().get("/api/auth/me", headers={"Authorization": "Bearer token"}).status_code,
                503,
            )


if __name__ == "__main__":
    unittest.main()
