import io
import unittest
from datetime import datetime, timedelta, timezone

from app import create_app
from app.audio_store import AudioStoreUnavailable
from app.firebase import FirestoreScanRepository
from test_api import MemoryRepository, make_wav


class FakeAudioStore:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.deleted = []
        self.fail_upload = False
        self.fail_open = False

    def upload(self, payload, *, filename, content_type, expires_at):
        if self.fail_upload:
            raise AudioStoreUnavailable("fake store unavailable")
        storage_id = f"fake-{len(self.objects) + 1}"
        self.objects[storage_id] = {
            "payload": bytes(payload),
            "filename": filename,
            "content_type": content_type,
            "expires_at": expires_at,
        }
        self.uploads.append(storage_id)
        return storage_id

    def open(self, storage_id):
        if self.fail_open:
            raise AudioStoreUnavailable("fake store unavailable")
        try:
            return io.BytesIO(self.objects[storage_id]["payload"])
        except KeyError as exc:
            raise FileNotFoundError(storage_id) from exc

    def delete(self, storage_id):
        self.deleted.append(storage_id)
        self.objects.pop(storage_id, None)


class AudioRetentionApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.audio_store = FakeAudioStore()

        def verify(token):
            if token in {"alice-token", "bob-token"}:
                return {"id": token.removesuffix("-token")}
            raise ValueError("invalid")

        self.app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": verify,
                "SCAN_REPOSITORY": self.repository,
                "AUDIO_STORE": self.audio_store,
                "MODEL_ARTIFACT_PATH": "",
            }
        )
        self.client = self.app.test_client()

    def post(self, *, token="alice-token", retain=False):
        return self.client.post(
            "/api/scans",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "consent": "true",
                "transcript": "",
                "transcript_consent": "false",
                "retain_audio": "true" if retain else "false",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )

    def test_normal_upload_does_not_call_audio_store(self):
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.audio_store.uploads, [])
        scan = response.get_json()["scan"]
        self.assertFalse(scan["audio_retained"])
        self.assertIsNone(scan["audio_expires_at"])
        self.assertIsNone(scan["audio_storage"])
        self.assertNotIn("audio_storage_id", scan)

    def test_retained_upload_admits_pointer_metadata_without_raw_bytes(self):
        response = self.post(retain=True)
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        scan = body["scan"]
        self.assertTrue(scan["audio_retained"])
        self.assertEqual(scan["audio_retention_days"], 30)
        self.assertEqual(scan["audio_storage"], "mongodb_gridfs")
        self.assertIsNotNone(scan["audio_expires_at"])
        self.assertNotIn("audio_storage_id", scan)
        self.assertEqual(len(self.audio_store.uploads), 1)

        stored = self.repository.items[("alice", scan["id"])]
        self.assertEqual(stored["audio_storage_id"], self.audio_store.uploads[0])
        self.assertNotIn("audio", stored)
        self.assertNotIn("audio_bytes", stored)
        self.assertNotIn("raw_audio", stored)

    def test_audio_playback_is_owner_scoped_and_supports_ranges(self):
        created = self.post(retain=True)
        scan_id = created.get_json()["scan"]["id"]

        other = self.client.get(
            f"/api/scans/{scan_id}/audio",
            headers={"Authorization": "Bearer bob-token"},
        )
        self.assertEqual(other.status_code, 404)

        full = self.client.get(
            f"/api/scans/{scan_id}/audio",
            headers={"Authorization": "Bearer alice-token"},
        )
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.data, self.audio_store.objects["fake-1"]["payload"])
        self.assertEqual(full.headers["Accept-Ranges"], "bytes")

        partial = self.client.get(
            f"/api/scans/{scan_id}/audio",
            headers={
                "Authorization": "Bearer alice-token",
                "Range": "bytes=0-3",
            },
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.data, self.audio_store.objects["fake-1"]["payload"][:4])
        self.assertEqual(partial.headers["Content-Range"], "bytes 0-3/38444")

    def test_expired_audio_is_unavailable_and_missing_mongo_only_affects_retention(self):
        created = self.post(retain=True)
        scan_id = created.get_json()["scan"]["id"]
        self.repository.items[("alice", scan_id)]["audio_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        expired = self.client.get(
            f"/api/scans/{scan_id}/audio",
            headers={"Authorization": "Bearer alice-token"},
        )
        self.assertEqual(expired.status_code, 410)

        no_store_app = create_app(
            {
                "TESTING": True,
                "TOKEN_VERIFIER": lambda _token: {"id": "alice"},
                "SCAN_REPOSITORY": MemoryRepository(),
                "MODEL_ARTIFACT_PATH": "",
                "MONGODB_URI": "",
            }
        )
        normal = no_store_app.test_client().post(
            "/api/scans",
            headers={"Authorization": "Bearer token"},
            data={
                "consent": "true",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(normal.status_code, 202)
        retained = no_store_app.test_client().post(
            "/api/scans",
            headers={"Authorization": "Bearer token"},
            data={
                "consent": "true",
                "retain_audio": "true",
                "audio": (make_wav(), "sample.wav", "audio/wav"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(retained.status_code, 503)
        self.assertIn("MongoDB audio retention is unavailable", retained.get_json()["error"])

    def test_delete_removes_retained_audio(self):
        created = self.post(retain=True)
        scan_id = created.get_json()["scan"]["id"]
        storage_id = self.audio_store.uploads[0]
        deleted = self.client.delete(
            f"/api/scans/{scan_id}",
            headers={"Authorization": "Bearer alice-token"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.audio_store.deleted, [storage_id])
        self.assertNotIn(storage_id, self.audio_store.objects)


class FirestoreAudioPrivacyTests(unittest.TestCase):
    def test_firestore_keeps_pointer_but_never_audio_bytes_or_public_pointer(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        reference = (
            client.collection.return_value.document.return_value.collection.return_value.document.return_value
        )
        now = datetime.now(timezone.utc)
        scan = {
            "id": "scan-1",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "audio_retained": True,
            "audio_retention_days": 30,
            "audio_expires_at": (now + timedelta(days=30)).isoformat(),
            "audio_storage": "mongodb_gridfs",
            "audio_storage_id": "private-gridfs-id",
            "audio_bytes": b"secret raw audio",
            "original_bytes": b"secret original mp3",
        }
        stored = FirestoreScanRepository(client).create("alice", scan)
        payload = reference.set.call_args.args[0]
        self.assertEqual(payload["audio_storage_id"], "private-gridfs-id")
        self.assertNotIn("audio_bytes", payload)
        self.assertNotIn("original_bytes", payload)
        self.assertNotIn("audio_bytes", stored)
        self.assertNotIn("original_bytes", stored)


if __name__ == "__main__":
    unittest.main()
