import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app import create_app
from app.firebase import FirestoreScanRepository
from app.transcript import (
    MAX_TRANSCRIPT_CHARS,
    TranscriptValidationError,
    analyze_transcript,
)
from test_api import MemoryRepository, make_wav


class TranscriptAnalyzerTests(unittest.TestCase):
    def test_each_fixed_category_has_a_deterministic_match(self):
        examples = {
            "financial_request": "Please send me money.",
            "credential_request": "Please provide your password.",
            "urgency_or_threat": "Act immediately or your account will be closed.",
            "impersonation": "This is your bank calling.",
            "remote_access_request": "Please install AnyDesk.",
        }
        for expected, transcript in examples.items():
            with self.subTest(category=expected):
                result = analyze_transcript(transcript)
                self.assertEqual(result["scam_status"], "review")
                self.assertEqual(result["scam_categories"], [expected])
                self.assertEqual(result["scam_score"], 0.2)
                self.assertIn("heuristic signal, not a probability", result["scam_explanation"])
                self.assertNotIn(transcript, result["scam_explanation"])

    def test_no_match_is_lower_signal_and_empty_transcript_is_unavailable(self):
        result = analyze_transcript("The weather is pleasant today.")
        self.assertEqual(result["scam_status"], "lower_signal")
        self.assertEqual(result["scam_score"], 0.0)
        self.assertEqual(result["scam_categories"], [])

        for empty in (None, "", " \n\t "):
            with self.subTest(empty=repr(empty)):
                unavailable = analyze_transcript(empty)
                self.assertEqual(unavailable["scam_status"], "unavailable")
                self.assertIsNone(unavailable["scam_score"])
                self.assertEqual(unavailable["scam_categories"], [])

    def test_analyzer_rejects_missing_consent_and_unicode_over_limit(self):
        with self.assertRaisesRegex(
            TranscriptValidationError, "Transcript consent must be true"
        ):
            analyze_transcript("Please send me money.", consented=False)
        with self.assertRaisesRegex(
            TranscriptValidationError, "exceeds the 12000 character limit"
        ):
            analyze_transcript("é" * (MAX_TRANSCRIPT_CHARS + 1))
        with self.assertRaisesRegex(
            TranscriptValidationError, "exceeds the 12000 character limit"
        ):
            analyze_transcript(" " * (MAX_TRANSCRIPT_CHARS + 1))


class TranscriptApiTests(unittest.TestCase):
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
                "MODEL_ARTIFACT_PATH": "",
            }
        )
        self.client = self.app.test_client()

    def post_scan(self, transcript=None, transcript_consent=None, token="alice-token"):
        data = {"consent": "true", "audio": (make_wav(), "sample.wav", "audio/wav")}
        if transcript is not None:
            data["transcript"] = transcript
        if transcript_consent is not None:
            data["transcript_consent"] = transcript_consent
        return self.client.post(
            "/api/scans",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            content_type="multipart/form-data",
        )

    def test_empty_transcript_remains_unavailable_without_transcript_consent(self):
        response = self.post_scan(transcript="")
        self.assertEqual(response.status_code, 201)
        scan = response.get_json()["scan"]
        self.assertEqual(scan["scam_status"], "unavailable")
        self.assertIsNone(scan["scam_score"])
        self.assertEqual(scan["scam_categories"], [])
        self.assertNotIn("transcript", scan)

    def test_nonempty_transcript_requires_consent_and_over_limit_is_rejected(self):
        missing_consent = self.post_scan(transcript="Please send me money.")
        self.assertEqual(missing_consent.status_code, 400)
        self.assertIn("Transcript consent must be true", missing_consent.get_json()["error"])
        self.assertNotIn("Please send me money.", missing_consent.get_json()["error"])

        over_limit = self.post_scan(
            transcript="x" * (MAX_TRANSCRIPT_CHARS + 1), transcript_consent="true"
        )
        self.assertEqual(over_limit.status_code, 400)
        self.assertIn("12000 character limit", over_limit.get_json()["error"])

    def test_transcript_signal_is_separate_from_deepfake_signal(self):
        class HighDeepfakeInference:
            def analyze(self, _metadata):
                return {
                    "status": "completed",
                    "deepfake_status": "high",
                    "confidence": 0.95,
                    "model_version": "fake-v1",
                    "explanation": "Independent audio score.",
                }

        app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": MemoryRepository(),
                "INFERENCE_SERVICE": HighDeepfakeInference(),
            }
        )
        client = app.test_client()
        response = client.post(
            "/api/scans",
            headers={"Authorization": "Bearer token"},
            data={
                "consent": "true",
                "transcript": "The weather is pleasant today.",
                "transcript_consent": "true",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        scan = response.get_json()["scan"]
        self.assertEqual(scan["deepfake_status"], "high")
        self.assertEqual(scan["confidence"], 0.95)
        self.assertEqual(scan["scam_status"], "lower_signal")
        self.assertEqual(scan["scam_score"], 0.0)
        self.assertEqual(scan["scam_categories"], [])
        self.assertIn("independent of the deepfake score", scan["scam_explanation"])

    def test_scan_is_readable_only_by_the_owner_and_returns_metadata(self):
        created = self.post_scan(
            transcript="Please install AnyDesk.", transcript_consent="true"
        )
        self.assertEqual(created.status_code, 201)
        scan = created.get_json()["scan"]
        self.assertEqual(scan["scam_categories"], ["remote_access_request"])
        self.assertNotIn("transcript", scan)

        own_read = self.client.get(
            f"/api/scans/{scan['id']}",
            headers={"Authorization": "Bearer alice-token"},
        )
        self.assertEqual(own_read.status_code, 200)
        self.assertEqual(own_read.get_json()["scan"], scan)

        other_read = self.client.get(
            f"/api/scans/{scan['id']}",
            headers={"Authorization": "Bearer bob-token"},
        )
        self.assertEqual(other_read.status_code, 404)

    def test_consented_transcript_result_survives_post_list_and_detail_without_raw_text(self):
        transcript = "Please send me money and provide your password."
        expected = {
            "scam_status": "review",
            "scam_score": 0.4,
            "scam_categories": ["financial_request", "credential_request"],
        }

        created = self.post_scan(transcript=transcript, transcript_consent="true")
        self.assertEqual(created.status_code, 201)
        created_scan = created.get_json()["scan"]
        expected["scam_explanation"] = created_scan["scam_explanation"]
        self.assertIn("independent of the deepfake score", expected["scam_explanation"])
        for field, value in expected.items():
            self.assertEqual(created_scan[field], value)
        self.assertNotIn(transcript, created.get_data(as_text=True))
        self.assertNotIn("transcript", created_scan)
        self.assertNotIn("raw_transcript", created_scan)

        # Exercise the response boundary against a legacy/adapter record that
        # contains fields which must never be exposed by scan read endpoints.
        stored = self.repository.items[("alice", created_scan["id"])]
        stored["transcript"] = transcript
        stored["raw_transcript"] = transcript

        listed = self.client.get("/api/scans", headers={"Authorization": "Bearer alice-token"})
        self.assertEqual(listed.status_code, 200)
        listed_scan = listed.get_json()["scans"][0]
        detail = self.client.get(
            f"/api/scans/{created_scan['id']}",
            headers={"Authorization": "Bearer alice-token"},
        )
        self.assertEqual(detail.status_code, 200)
        detail_scan = detail.get_json()["scan"]

        for scan, response in ((listed_scan, listed), (detail_scan, detail)):
            for field, value in expected.items():
                self.assertEqual(scan[field], value)
            self.assertIn("scam_explanation", scan)
            self.assertNotIn(transcript, response.get_data(as_text=True))
            self.assertNotIn("transcript", scan)
            self.assertNotIn("raw_transcript", scan)
        self.assertEqual(detail_scan, listed_scan)


class FirestoreTranscriptStorageTests(unittest.TestCase):
    def test_repository_create_strips_raw_transcript_from_storage_and_return(self):
        client = MagicMock()
        reference = (
            client.collection.return_value.document.return_value.collection.return_value.document.return_value
        )
        now = datetime.now(timezone.utc).isoformat()
        expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        scan = {
            "id": "scan-1",
            "created_at": now,
            "expires_at": expires,
            "transcript": "secret transcript that must never be stored",
            "scam_status": "review",
            "scam_score": 0.2,
            "scam_categories": ["financial_request"],
            "scam_explanation": "fixed explanation",
        }

        stored = FirestoreScanRepository(client).create("alice", scan)

        payload = reference.set.call_args.args[0]
        self.assertNotIn("transcript", payload)
        self.assertNotIn("transcript", stored)
        self.assertEqual(payload["scam_categories"], ["financial_request"])


if __name__ == "__main__":
    unittest.main()
