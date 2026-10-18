"""Check the saved research artifact through Flask without cloud writes or real tokens."""

import argparse
import io
import json
from collections import Counter
from pathlib import Path

from .model import load_model
from .train import collect_dataset


class _EphemeralRepository:
    """Strictly local test double; never installed by the running application."""

    def __init__(self):
        self.items = {}

    def create(self, user_id, scan):
        self.items[(user_id, scan["id"])] = dict(scan)
        return dict(scan)

    def update(self, user_id, scan_id, updates):
        scan = self.items.get((user_id, scan_id))
        if scan is None:
            return None
        scan.update({
            key: value
            for key, value in updates.items()
            if key not in {"audio", "audio_bytes", "transcript", "raw_transcript"}
        })
        return dict(scan)

    def get(self, user_id, scan_id, _now):
        return self.items.get((user_id, scan_id))

    def list(self, user_id, _now):
        return [scan for (owner, _), scan in self.items.items() if owner == user_id]

    def delete(self, user_id, scan_id):
        return self.items.pop((user_id, scan_id), None) is not None


def smoke_model(model_path, dataset_dir):
    from backend.app import create_app

    model = load_model(model_path)
    heldout = set(model.metadata["training"]["split"]["test_pcm_sha256"])
    selected = {}
    for record in collect_dataset(dataset_dir):
        if record.pcm_sha256 in heldout:
            selected.setdefault(record.label, record)
    if set(selected) != {0, 1}:
        raise ValueError("both saved held-out classes must be present")
    repo = _EphemeralRepository()
    app = create_app({
        "TESTING": True,
        "MODEL_ARTIFACT_PATH": str(Path(model_path).resolve()),
        "MODEL_THRESHOLD": model.threshold,
        "TOKEN_VERIFIER": lambda _token: {"id": "local-model-smoke"},
        "SCAN_REPOSITORY": repo,
    })
    client = app.test_client()
    health = client.get("/api/health").get_json()
    if health["model"]["status"] != "ready":
        raise ValueError("Flask could not load the saved model")
    headers = {"Authorization": "Bearer local-test-fixture"}
    for record in selected.values():
        response = client.post("/api/scans", headers=headers, data={
            "consent": "true",
            "audio": (io.BytesIO(record.audio_bytes), "smoke.wav", "audio/wav"),
        })
        if response.status_code != 202 or response.get_json().get("status") != "pending":
            raise ValueError("Flask scan creation failed")
        scan_id = response.get_json()["scan"]["id"]
        app.extensions["scan_jobs"].wait_for_idle()
        detail = client.get(f'/api/scans/{scan_id}', headers=headers)
        if detail.status_code != 200:
            raise ValueError("Flask scan read-back failed")
        scan = detail.get_json()["scan"]
        if scan["status"] != "completed" or not isinstance(scan["confidence"], (int, float)):
            raise ValueError("model did not produce a finite research score")
        if not 0 <= scan["confidence"] <= 1 or "audio_bytes" in scan:
            raise ValueError("invalid score or unsafe stored metadata")
        if scan["model_version"] != model.metadata["model_version"]:
            raise ValueError("model version changed during scan creation")
        if detail.get_json()["scan"] != scan:
            raise ValueError("scan read-back failed")
        if client.delete(f'/api/scans/{scan["id"]}', headers=headers).status_code != 200:
            raise ValueError("scan deletion failed")
    if repo.items:
        raise ValueError("ephemeral scan deletion was incomplete")
    result = {
        "status": "passed", "completed_scans": len(selected),
        "model_version": health["model"]["version"],
        "cloud_writes": 0, "authentication": "injected-local-test-only",
        "note": "This verifies the model/API boundary, not live Firebase sign-in or Firestore persistence.",
    }
    manifest_path = Path(dataset_dir) / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result["excluded_reason_counts"] = dict(Counter(
            item["reason"] for item in manifest["excluded"]
        ))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True)
    args = parser.parse_args()
    try:
        result = smoke_model(args.model, args.dataset)
    except (ValueError, OSError, KeyError):
        parser.exit(2, "Model/API smoke check failed; run the unit tests for diagnosis.\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
