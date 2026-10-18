"""Exercise a real JSON runtime through Flask using synthetic, temporary audio."""

import io
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from app import create_app
from app.config import Config
from test_api import MemoryRepository
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_models.model import save_model
from ai_models.train import train_dataset


def tone(frequency):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / 16000)))
            for i in range(16000)
        ))
    return output.getvalue()


class ModelPipelineTests(unittest.TestCase):
    def test_trained_json_runtime_upload_read_back_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, frequencies in (("real", (180, 200, 220)), ("fake", (680, 720, 760))):
                folder = root / "dataset" / name
                folder.mkdir(parents=True)
                for i, frequency in enumerate(frequencies):
                    (folder / f"fixture-{i}.wav").write_bytes(tone(frequency))
            model, _, _ = train_dataset(root / "dataset")
            artifact = root / "model.json"
            save_model(model, artifact)
            repo = MemoryRepository()
            app = create_app({
                "TESTING": True,
                "MODEL_ARTIFACT_PATH": str(artifact),
                "MODEL_THRESHOLD": 0.5,
                "TOKEN_VERIFIER": lambda _token: {"id": "fixture-user"},
                "SCAN_REPOSITORY": repo,
            })
            client = app.test_client()
            headers = {"Authorization": "Bearer synthetic-test-token"}
            health = client.get("/api/health").get_json()
            self.assertEqual(health["model"]["status"], "ready")
            self.assertTrue(health["model"]["research_only"])
            response = client.post("/api/scans", headers=headers, data={
                "consent": "true",
                "audio": (io.BytesIO(tone(240)), "fixture.wav", "audio/wav"),
            })
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["status"], "pending")
            app.extensions["scan_jobs"].wait_for_idle()
            scan_id = response.get_json()["scan"]["id"]
            scan = client.get(f'/api/scans/{scan_id}', headers=headers).get_json()["scan"]
            self.assertEqual(scan["status"], "completed")
            self.assertTrue(0 <= scan["confidence"] <= 1)
            self.assertEqual(scan["scam_status"], "unavailable")
            self.assertIn("uncalibrated", scan["explanation"])
            self.assertNotIn("audio_bytes", scan)
            stored = client.get(f'/api/scans/{scan["id"]}', headers=headers).get_json()["scan"]
            self.assertEqual(stored, scan)
            self.assertNotIn("audio_bytes", next(iter(repo.items.values())))
            self.assertEqual(client.get("/api/scans", headers=headers).get_json()["stats"]["analyzed_results"], 1)
            self.assertEqual(client.delete(f'/api/scans/{scan["id"]}', headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/scans", headers=headers).get_json()["stats"]["total_scans"], 0)

    def test_model_path_is_relative_to_backend_not_shell_directory(self):
        with patch.dict("os.environ", {"MODEL_ARTIFACT_PATH": "../ai_models/artifacts/baseline.json"}):
            self.assertEqual(Path(Config.from_env()["MODEL_ARTIFACT_PATH"]), ROOT / "ai_models" / "artifacts" / "baseline.json")
