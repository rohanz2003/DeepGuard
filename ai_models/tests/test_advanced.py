import io
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from ai_models.audit import audit_dataset
from ai_models.augment import AUGMENTATION_NAMES, augment_samples
from ai_models.calibration import apply_calibrator, fit_platt_calibrator, select_threshold


def wav(seconds=1.0, value=0.2):
    samples = np.full(int(16000 * seconds), value, dtype=np.float64)
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    return output.getvalue()


class AdvancedModelTests(unittest.TestCase):
    def test_augmentations_are_deterministic_bounded_and_length_preserving(self):
        source = np.linspace(-0.5, 0.5, 16000)
        for kind in AUGMENTATION_NAMES:
            with self.subTest(kind=kind):
                first = augment_samples(source, kind, seed=17)
                second = augment_samples(source, kind, seed=17)
                np.testing.assert_array_equal(first, second)
                self.assertEqual(first.shape, source.shape)
                self.assertTrue(np.isfinite(first).all())
                self.assertTrue(np.all(np.abs(first) <= 1.0))

    def test_calibration_and_cost_threshold_are_explicit(self):
        raw = np.array([0.05, 0.2, 0.35, 0.7, 0.85, 0.95])
        labels = np.array([0, 0, 0, 1, 1, 1])
        calibrator = fit_platt_calibrator(raw, labels)
        calibrated = apply_calibrator(raw, calibrator)
        self.assertTrue(np.isfinite(calibrated).all())
        self.assertTrue(np.all((calibrated >= 0) & (calibrated <= 1)))
        decision = select_threshold(
            calibrated,
            labels,
            false_positive_cost=4,
            false_negative_cost=1,
        )
        self.assertEqual(decision["false_positive_cost"], 4.0)
        self.assertIn("threshold", decision)

    def test_audit_requires_provenance_for_disjoint_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dataset"
            (root / "real").mkdir(parents=True)
            (root / "fake").mkdir(parents=True)
            (root / "real" / "real.wav").write_bytes(wav(value=0.1))
            (root / "fake" / "fake.wav").write_bytes(wav(value=0.2))
            report = audit_dataset(root)
            self.assertIn("provenance_metadata_missing", report["gaps"])
            self.assertFalse(report["ready_for_disjoint_validation"])
            metadata = Path(directory) / "metadata.json"
            metadata.write_text(json.dumps([
                {"path": "real/real.wav", "label": "real", "speaker_id": "r1", "generator_id": "none", "language": "en", "license": "test", "codec": "pcm"},
                {"path": "fake/fake.wav", "label": "fake", "speaker_id": "f1", "generator_id": "g1", "language": "en", "license": "test", "codec": "pcm"},
            ]), encoding="utf-8")
            complete = audit_dataset(root, metadata_path=metadata)
            self.assertEqual(complete["gaps"], [])
            self.assertTrue(complete["ready_for_disjoint_validation"])


if __name__ == "__main__":
    unittest.main()
