import io
import threading
import unittest

from app import create_app
from app.inference import TransientInferenceError
from test_api import MemoryRepository, make_wav


class ManualFuture:
    def __init__(self):
        self._event = threading.Event()
        self._result = None
        self._exception = None

    def set_result(self, result):
        self._result = result
        self._event.set()

    def set_exception(self, exception):
        self._exception = exception
        self._event.set()

    def result(self, timeout=None):
        if not self._event.wait(timeout):
            raise TimeoutError("manual job did not finish")
        if self._exception is not None:
            raise self._exception
        return self._result


class ManualExecutor:
    """Deterministic executor used to test the production job boundary."""

    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = ManualFuture()
        self.jobs.append((function, args, future))
        return future

    def run_next(self):
        function, args, future = self.jobs.pop(0)
        try:
            future.set_result(function(*args))
        except Exception as exc:  # pragma: no cover - defensive test helper
            future.set_exception(exc)


class JobApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.executor = ManualExecutor()

        def verify(token):
            return {"id": token, "email": f"{token}@example.test"}

        self.app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": verify,
                "SCAN_REPOSITORY": self.repository,
                "SCAN_JOB_EXECUTOR": self.executor,
                "SCAN_MAX_ATTEMPTS": 2,
                "MODEL_ARTIFACT_PATH": "",
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

    def read(self, scan_id, token="alice"):
        return self.client.get(
            f"/api/scans/{scan_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_post_is_pending_until_injected_executor_runs(self):
        response = self.post(transcript="Please send me money.")
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["scan"]["status"], "pending")
        self.assertEqual(body["scan"]["attempt"], 0)
        self.assertEqual(body["scan"]["retry_count"], 0)
        self.assertEqual(len(self.executor.jobs), 1)

        stored = next(iter(self.repository.items.values()))
        self.assertNotIn("audio", stored)
        self.assertNotIn("audio_bytes", stored)
        self.assertNotIn("transcript", stored)
        self.assertNotIn("raw_transcript", stored)

    def test_worker_records_processing_then_completed_and_keeps_signals_separate(self):
        observed_statuses = []

        class Inference:
            def analyze(inner, metadata):
                observed_statuses.append(
                    next(iter(self.repository.items.values()))["status"]
                )
                self.assertTrue(metadata.audio_bytes.startswith(b"RIFF"))
                return {
                    "status": "completed",
                    "deepfake_status": "high",
                    "confidence": 0.91,
                    "model_version": "test-v1",
                    "explanation": "Independent audio score.",
                }

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": self.repository,
                "INFERENCE_SERVICE": Inference(),
                "SCAN_JOB_EXECUTOR": self.executor,
            }
        )
        client = app.test_client()
        response = client.post(
            "/api/scans",
            headers={"Authorization": "Bearer alice"},
            data={
                "consent": "true",
                "transcript": "Please install AnyDesk.",
                "transcript_consent": "true",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )
        scan_id = response.get_json()["scan"]["id"]
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["scan"]["scam_categories"], [
            "remote_access_request"
        ])

        self.executor.run_next()
        completed = client.get(
            f"/api/scans/{scan_id}", headers={"Authorization": "Bearer alice"}
        ).get_json()["scan"]
        self.assertEqual(observed_statuses, ["processing"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["deepfake_status"], "high")
        self.assertEqual(completed["confidence"], 0.91)
        self.assertEqual(completed["scam_categories"], ["remote_access_request"])
        self.assertEqual(completed["attempt"], 1)
        self.assertEqual(completed["retry_count"], 0)
        self.assertNotIn("transcript", completed)
        self.assertNotIn("raw_transcript", completed)
        self.assertNotIn("audio_bytes", completed)

    def test_worker_failure_is_safe_and_does_not_retry_generic_errors(self):
        calls = []

        class FailingInference:
            @staticmethod
            def analyze(_metadata):
                calls.append(True)
                raise RuntimeError("provider secret should not be exposed")

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": self.repository,
                "INFERENCE_SERVICE": FailingInference(),
                "SCAN_JOB_EXECUTOR": self.executor,
            }
        )
        response = app.test_client().post(
            "/api/scans",
            headers={"Authorization": "Bearer alice"},
            data={
                "consent": "true",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.executor.jobs), 1)
        scan_id = response.get_json()["scan"]["id"]
        self.executor.run_next()
        scan = app.test_client().get(
            f"/api/scans/{scan_id}", headers={"Authorization": "Bearer alice"}
        ).get_json()["scan"]
        self.assertEqual(calls, [True])
        self.assertEqual(scan["status"], "failed")
        self.assertEqual(scan["error_code"], "inference_failed")
        self.assertIn("could not be completed", scan["error_explanation"])
        self.assertNotIn("provider secret", str(scan))
        self.assertEqual(scan["attempt"], 1)
        self.assertEqual(scan["retry_count"], 0)

    def test_only_dedicated_transient_error_retries_and_completes(self):
        calls = []

        class RetryingInference:
            @staticmethod
            def analyze(_metadata):
                calls.append(True)
                if len(calls) == 1:
                    raise TransientInferenceError("temporary internal detail")
                return {
                    "status": "completed",
                    "deepfake_status": "low",
                    "confidence": 0.12,
                    "model_version": "retry-v1",
                    "explanation": "Completed after a transient retry.",
                }

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": self.repository,
                "INFERENCE_SERVICE": RetryingInference(),
                "SCAN_JOB_EXECUTOR": self.executor,
                "SCAN_MAX_ATTEMPTS": 2,
            }
        )
        response = app.test_client().post(
            "/api/scans",
            headers={"Authorization": "Bearer alice"},
            data={
                "consent": "true",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.executor.jobs), 1)
        scan_id = response.get_json()["scan"]["id"]
        self.executor.run_next()
        scan = app.test_client().get(
            f"/api/scans/{scan_id}", headers={"Authorization": "Bearer alice"}
        ).get_json()["scan"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(scan["status"], "completed")
        self.assertEqual(scan["attempt"], 2)
        self.assertEqual(scan["retry_count"], 1)
        self.assertNotIn("error_code", scan)

    def test_owner_isolation_applies_to_pending_and_completed_records(self):
        response = self.post(token="alice")
        scan_id = response.get_json()["scan"]["id"]
        self.assertEqual(self.read(scan_id, token="bob").status_code, 404)
        self.executor.run_next()
        self.assertEqual(self.read(scan_id, token="bob").status_code, 404)
        self.assertEqual(self.read(scan_id, token="alice").status_code, 200)

    def test_duplicate_enqueue_for_one_scan_runs_once(self):
        metadata = type("Metadata", (), {"audio_bytes": b"RIFF"})()
        calls = []

        class Inference:
            @staticmethod
            def analyze(_metadata):
                calls.append(True)
                return {"deepfake_status": "low", "confidence": 0.1}

        manager = self.app.extensions["scan_jobs"]
        # The repository record is required for the worker's ownership check.
        now = "2026-01-01T00:00:00+00:00"
        self.repository.create(
            "alice",
            {
                "id": "duplicate",
                "created_at": now,
                "updated_at": now,
                "expires_at": "2030-01-01T00:00:00+00:00",
                "status": "pending",
            },
        )
        manager._inference = Inference()
        first = manager.enqueue("alice", "duplicate", metadata)
        second = manager.enqueue("alice", "duplicate", metadata)
        self.assertIs(first, second)
        self.assertEqual(len(self.executor.jobs), 1)
        self.executor.run_next()
        self.assertEqual(len(calls), 1)
        third = manager.enqueue("alice", "duplicate", metadata)
        self.assertIs(third, first)
        self.assertEqual(len(self.executor.jobs), 0)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
