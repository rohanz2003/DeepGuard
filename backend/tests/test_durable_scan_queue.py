import io
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from app import create_app
from app.inference import TransientInferenceError
from app.jobs import DurableScanWorker
from test_api import make_wav


UTC = timezone.utc


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


class MemoryObjectStorage:
    """Test-only object boundary; no Firebase or Cloud Storage calls."""

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.downloads = []
        self.deletions = []

    def upload(self, object_name, payload, *, content_type, expires_at):
        self.objects[object_name] = bytes(payload)
        self.uploads.append(
            {
                "name": object_name,
                "bytes": bytes(payload),
                "content_type": content_type,
                "expires_at": expires_at,
            }
        )
        return object_name

    def download(self, object_name):
        self.downloads.append(object_name)
        return self.objects[object_name]

    def delete(self, object_name):
        self.deletions.append(object_name)
        self.objects.pop(object_name, None)


class MemoryDurableRepository:
    """Small owner-scoped transactional repository for queue unit tests."""

    def __init__(self):
        self.items = {}
        self.lock = threading.RLock()

    def create(self, user_id, scan):
        with self.lock:
            self.items[(str(user_id), scan["id"])] = deepcopy(scan)
            return deepcopy(scan)

    def get(self, user_id, scan_id, now=None):
        with self.lock:
            scan = self.items.get((str(user_id), str(scan_id)))
            return deepcopy(scan) if scan else None

    def get_internal(self, user_id, scan_id, now=None):
        return self.get(user_id, scan_id, now)

    def list(self, user_id, now):
        with self.lock:
            return [
                deepcopy(value)
                for (owner, _scan_id), value in self.items.items()
                if owner == str(user_id)
            ]

    def delete(self, user_id, scan_id):
        with self.lock:
            return self.items.pop((str(user_id), str(scan_id)), None) is not None

    @staticmethod
    def _time(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def claim_next(self, worker_id, lease_seconds, now):
        with self.lock:
            ordered = sorted(
                self.items.items(),
                key=lambda item: item[1].get("updated_at", item[1].get("created_at", "")),
            )
            for (owner, scan_id), scan in ordered:
                status = scan.get("status")
                if status == "pending":
                    next_attempt = self._time(scan.get("next_attempt_at"))
                    if next_attempt and next_attempt > now:
                        continue
                elif status == "processing":
                    lease_expires = self._time(scan.get("lease_expires_at"))
                    if lease_expires is None or lease_expires > now:
                        continue
                else:
                    continue
                attempt = int(scan.get("attempt") or 0) + 1
                lease_id = f"{worker_id}-{attempt}-{len(scan_id)}"
                scan.update(
                    {
                        "status": "processing",
                        "attempt": attempt,
                        "retry_count": attempt - 1,
                        "lease_id": lease_id,
                        "worker_id": worker_id,
                        "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
                        "updated_at": now.isoformat(),
                    }
                )
                return {
                    "user_id": owner,
                    "scan_id": scan_id,
                    "lease_id": lease_id,
                    "attempt": attempt,
                    "record": deepcopy(scan),
                }
        return None

    def transition_claim(self, user_id, scan_id, lease_id, updates, now):
        with self.lock:
            scan = self.items.get((str(user_id), str(scan_id)))
            if not scan or scan.get("status") != "processing":
                return False
            if scan.get("lease_id") != lease_id:
                return False
            scan.update(deepcopy(updates))
            scan["updated_at"] = iso(updates.get("updated_at", now))
            return True

    def mark_cleanup_complete(self, user_id, scan_id, object_name=None):
        with self.lock:
            scan = self.items.get((str(user_id), str(scan_id)))
            if not scan or scan.get("status") not in {"completed", "failed"}:
                return False
            if object_name and scan.get("raw_audio_path") != object_name:
                return False
            scan["cleanup_required"] = False
            return True

    def list_cleanup_candidates(self, now):
        with self.lock:
            return [
                {
                    "user_id": owner,
                    "scan_id": scan_id,
                    "record": deepcopy(scan),
                }
                for (owner, scan_id), scan in self.items.items()
                if scan.get("status") in {"completed", "failed"}
                and scan.get("cleanup_required")
            ]


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryDurableRepository()
        self.storage = MemoryObjectStorage()
        self.now = datetime.now(UTC) + timedelta(seconds=1)

        class Inference:
            @staticmethod
            def analyze(_metadata):
                return {
                    "status": "completed",
                    "deepfake_status": "low",
                    "confidence": 0.1,
                    "model_version": "test-v1",
                    "explanation": "test result",
                }

        self.inference = Inference()
        self.app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda token: {"id": token},
                "SCAN_REPOSITORY": self.repository,
                "SCAN_STORAGE": self.storage,
                "SCAN_DURABLE_QUEUE": True,
                "INFERENCE_SERVICE": self.inference,
                "SCAN_RAW_RETENTION_SECONDS": 60,
                "SCAN_LEASE_SECONDS": 10,
            }
        )
        self.client = self.app.test_client()

    def post(self, token="alice", transcript=None):
        data = {
            "consent": "true",
            "audio": (make_wav(), "sample.wav", "audio/wav"),
        }
        if transcript is not None:
            data["transcript"] = transcript
            data["transcript_consent"] = "true"
        return self.client.post(
            "/api/scans",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            content_type="multipart/form-data",
        )

    def test_durable_admission_uploads_private_object_and_only_metadata(self):
        response = self.post(transcript="Please send me money.")
        self.assertEqual(response.status_code, 202)
        scan_id = response.get_json()["scan"]["id"]
        stored = self.repository.items[("alice", scan_id)]
        self.assertEqual(stored["status"], "pending")
        self.assertTrue(stored["cleanup_required"])
        self.assertIn("raw_audio_path", stored)
        self.assertNotIn("audio_bytes", stored)
        self.assertNotIn("audio", stored)
        self.assertNotIn("transcript", stored)
        self.assertNotIn("raw_transcript", stored)
        self.assertEqual(len(self.storage.uploads), 1)
        self.assertTrue(self.storage.uploads[0]["bytes"].startswith(b"RIFF"))
        self.assertNotIn("raw_audio_path", response.get_json()["scan"])

    def test_claim_lease_duplicate_prevention_and_expired_recovery(self):
        self.post()
        first = self.app.extensions["scan_worker"]._claim()
        self.assertIsNotNone(first)
        duplicate_worker = DurableScanWorker(
            self.repository,
            self.storage,
            self.inference,
            worker_id="worker-b",
            lease_seconds=10,
            clock=lambda: self.now,
        )
        self.assertIsNone(duplicate_worker._claim())

        expired = self.now + timedelta(seconds=11)
        recovering_worker = DurableScanWorker(
            self.repository,
            self.storage,
            self.inference,
            worker_id="worker-c",
            lease_seconds=10,
            clock=lambda: expired,
        )
        recovered = recovering_worker._claim()
        self.assertIsNotNone(recovered)
        self.assertNotEqual(first.lease_id, recovered.lease_id)
        self.assertEqual(recovered.attempt, 2)

    def test_transient_retry_then_completion_deletes_object(self):
        calls = []

        class RetryingInference:
            @staticmethod
            def analyze(_metadata):
                calls.append(True)
                if len(calls) == 1:
                    raise TransientInferenceError("temporary")
                return {"deepfake_status": "high", "confidence": 0.9}

        self.app.extensions["scan_jobs"].max_attempts = 2
        worker = DurableScanWorker(
            self.repository,
            self.storage,
            RetryingInference(),
            worker_id="worker-a",
            lease_seconds=10,
            max_attempts=2,
            clock=lambda: self.now,
        )
        self.post()
        self.assertTrue(worker.run_once())
        scan_id = next(iter(self.repository.items))[1]
        self.assertEqual(self.repository.items[("alice", scan_id)]["status"], "pending")
        self.assertEqual(self.storage.deletions, [])
        self.assertTrue(worker.run_once())
        self.assertEqual(self.repository.items[("alice", scan_id)]["status"], "completed")
        self.assertEqual(len(self.storage.deletions), 1)
        self.assertFalse(self.repository.items[("alice", scan_id)]["cleanup_required"])
        self.assertEqual(len(calls), 2)

    def test_generic_failure_is_terminal_and_object_is_cleaned(self):
        class FailingInference:
            @staticmethod
            def analyze(_metadata):
                raise RuntimeError("provider secret")

        worker = DurableScanWorker(
            self.repository,
            self.storage,
            FailingInference(),
            worker_id="worker-a",
            lease_seconds=10,
            max_attempts=3,
            clock=lambda: self.now,
        )
        self.post()
        self.assertTrue(worker.run_once())
        scan_id = next(iter(self.repository.items))[1]
        scan = self.repository.items[("alice", scan_id)]
        self.assertEqual(scan["status"], "failed")
        self.assertEqual(scan["error_code"], "inference_failed")
        self.assertNotIn("provider secret", str(scan))
        self.assertEqual(len(self.storage.deletions), 1)

    def test_owner_isolation_for_metadata_and_targeted_reads(self):
        response = self.post(token="alice")
        scan_id = response.get_json()["scan"]["id"]
        self.assertEqual(
            self.client.get(
                f"/api/scans/{scan_id}",
                headers={"Authorization": "Bearer bob"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.repository.get_internal("bob", scan_id, self.now),
            None,
        )


if __name__ == "__main__":
    unittest.main()
