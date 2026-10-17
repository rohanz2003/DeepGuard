"""Synthetic-only regression tests for split provenance and measured evaluation."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ai_models.evaluate import evaluate_arrays, evaluate_dataset, main as evaluate_main
from ai_models.model import (
    ARTIFACT_VERSION, LogisticRegressionModel, ModelError, load_model, save_model,
)
from ai_models.preprocess import extract_features
from ai_models.train import (
    MAX_AUDIO_BYTES, DatasetError, collect_dataset, filename_group_key,
    main as train_main, prepare_records, split_records, train_dataset,
)


def wav_bytes(frequency: float, *, sample_rate: int = 8000, seconds: float = 1.0) -> bytes:
    count = int(sample_rate * seconds)
    time = np.arange(count) / sample_rate
    samples = np.clip(0.5 * np.sin(2 * np.pi * frequency * time), -1, 1)
    pcm = (samples * 32767).astype("<i2").tobytes()
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def with_extra_chunk(audio: bytes) -> bytes:
    """A different valid WAV container with exactly the same decoded PCM."""

    extra = b"JUNK" + (4).to_bytes(4, "little") + b"test"
    result = audio[:12] + extra + audio[12:]
    return result[:4] + (len(result) - 8).to_bytes(4, "little") + result[8:]


def create_dataset(root: Path, *, manifest: bool = False) -> Path:
    records = []
    for class_name, label, start in (("real", 0, 180), ("fake", 1, 750)):
        (root / class_name).mkdir(parents=True)
        for index in range(6):
            audio = wav_bytes(start + index * 23)
            speaker_or_sentence = 1 + index // 2
            if class_name == "real":
                original = f"real/original/sub/{speaker_or_sentence}-20-{index}-soConvert.mp3"
            else:
                original = f"fake/original/sentence_{speaker_or_sentence}_voice_{index % 2 + 1}.mp3"
            filename = hashlib.sha256(original.encode()).hexdigest() if manifest else f"sample-{index}"
            relative = f"{class_name}/{filename}.wav"
            (root / relative).write_bytes(audio)
            with wave.open(io.BytesIO(audio), "rb") as wav:
                pcm_hash = hashlib.sha256(wav.readframes(wav.getnframes())).hexdigest()
            records.append({
                "source": original, "output": relative, "label": label,
                "source_sha256": hashlib.sha256(("synthetic:" + original).encode()).hexdigest(),
                "pcm_sha256": pcm_hash, "duration_seconds": 1.0,
            })
    if manifest:
        (root / "manifest.json").write_text(
            json.dumps({"records": records, "excluded": [], "summary": {}}), encoding="utf-8",
        )
    return root


class BaselineTests(unittest.TestCase):
    def test_features_are_fixed_shape_and_deterministic(self) -> None:
        audio = wav_bytes(220.0)
        first = extract_features(audio)
        np.testing.assert_array_equal(first, extract_features(audio))
        self.assertEqual(first.shape, (36,))
        self.assertTrue(np.isfinite(first).all())

    def test_grouped_split_is_deterministic_disjoint_and_uses_original_manifest_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            records = collect_dataset(root)
            train, test = split_records(records, seed=7, test_size=0.3, group_by="filename")
            train_again, test_again = split_records(list(reversed(records)), seed=7,
                                                    test_size=0.3, group_by="filename")
            self.assertEqual([r.relative_path for r in train], [r.relative_path for r in train_again])
            self.assertEqual([r.relative_path for r in test], [r.relative_path for r in test_again])
            self.assertEqual({r.label for r in train}, {0, 1})
            self.assertEqual({r.label for r in test}, {0, 1})
            for attribute in ("relative_path", "pcm_sha256", "group_id"):
                self.assertFalse({getattr(r, attribute) for r in train}
                                 & {getattr(r, attribute) for r in test})
            all_records = train + test
            self.assertEqual(len({r.group_id for r in all_records}), 6)
            for label in (0, 1):
                group_sizes = [sum(r.group_id == group and r.label == label for r in all_records)
                               for group in {r.group_id for r in all_records if r.label == label}]
                self.assertEqual(sorted(group_sizes), [2, 2, 2])
            self.assertTrue(all("audio_bytes" not in vars(record) for record in records))

    def test_filename_heuristics_normalize_known_content_and_copy_variants(self) -> None:
        cases = [
            ("real/original/12-45-009-soConvert.mp3", 0, "speaker:12"),
            ("fake/clip_0001_en-US-AriaNeural.mp3", 1, "clip:1"),
            ("fake/clip_0001_en-GB-RyanNeural.mp3", 1, "clip:1"),
            ("fake/sentence_1_male_v1.mp3", 1, "sentence:1"),
            ("fake/line_41_voice_1.mp3", 1, "line:41"),
            ("fake/Prompt_21_Voice_1.mp3", 1, "prompt:21"),
            ("fake/_Please verify your account (1).mp3", 1, "text:please verify your account"),
            ("real/Please_verify_your_account.mp3", 0, "text:please verify your account"),
            ("fake/_Hello (2).mp3", 1, "text:hello"),
        ]
        for source, label, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(filename_group_key(source, label)[0], expected)
        opaque_a = "fake/" + "a" * 64 + ".wav"
        opaque_b = "fake/" + "b" * 64 + ".wav"
        self.assertNotEqual(filename_group_key(opaque_a, 1)[0], filename_group_key(opaque_b, 1)[0])
        self.assertEqual(filename_group_key(opaque_a, 1)[1], "opaque_source")
        self.assertEqual(filename_group_key("fake/af02bac9-a102-4111-8210-012345abcdef.mp3", 1)[1],
                         "opaque_source")

    def test_duplicate_wav_containers_are_deduplicated_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data")
            original = root / "real" / "sample-0.wav"
            (root / "real" / "copy.wav").write_bytes(with_extra_chunk(original.read_bytes()))
            model, train, test = train_dataset(root, seed=5)
            training = model.metadata["training"]
            self.assertEqual(training["sample_count"], 13)
            self.assertEqual(training["unique_sample_count"], 12)
            self.assertEqual(training["duplicate_count"], 1)
            self.assertEqual(len(train) + len(test), 12)
            self.assertEqual(len(training["dataset_records"]), 13)
            self.assertFalse({r.pcm_sha256 for r in train} & {r.pcm_sha256 for r in test})

    def test_duplicate_aliases_union_filename_groups_before_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            path = root / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            first = manifest["records"][0]
            bridge = copy.deepcopy(first)
            bridge["source"] = "real/original/2-20-99-soConvert.mp3"
            bridge["source_sha256"] = "f" * 64
            bridge["output"] = "real/duplicate.wav"
            (root / bridge["output"]).write_bytes((root / first["output"]).read_bytes())
            manifest["records"].append(bridge)
            path.write_text(json.dumps(manifest), encoding="utf-8")
            unique, _ = prepare_records(collect_dataset(root), group_by="filename")
            real = [r for r in unique if r.label == 0]
            self.assertEqual(sorted({sum(r.group_id == group for r in real)
                                     for group in {r.group_id for r in real}}), [2, 4])
            train, test = split_records(collect_dataset(root), group_by="filename")
            self.assertFalse({r.group_id for r in train} & {r.group_id for r in test})

    def test_cross_label_decoded_duplicates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data")
            (root / "fake" / "conflict.wav").write_bytes(
                with_extra_chunk((root / "real" / "sample-0.wav").read_bytes()))
            with self.assertRaisesRegex(DatasetError, "conflicting labels"):
                collect_dataset(root)

    def test_converter_excluded_duplicates_still_bridge_original_filename_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            path = root / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            first = manifest["records"][0]
            manifest["excluded"] = [{
                "source": "real/original/2-20-99-soConvert.mp3", "status": "duplicate",
                "reason": "decoded_pcm_duplicate", "duplicate_of": first["source"],
            }]
            manifest["summary"] = {"duplicate_count": 1, "discovered_count": 13,
                                   "prepared_count": 12, "rejected_count": 0}
            path.write_text(json.dumps(manifest), encoding="utf-8")
            model, train, test = train_dataset(root, group_by="filename")
            real = [r for r in train + test if r.label == 0]
            self.assertEqual(sorted({sum(r.group_id == group for r in real)
                                     for group in {r.group_id for r in real}}), [2, 4])
            self.assertFalse({r.group_id for r in train} & {r.group_id for r in test})
            self.assertEqual(model.metadata["training"]["preparation_counts"]["duplicate_count"], 1)
            report = evaluate_dataset(model, root)
            self.assertEqual(report["preparation_counts"]["duplicate_count"], 1)
            self.assertNotIn("2-20-99-soConvert", json.dumps(report))

    def test_split_requires_both_classes_in_train_and_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            records = collect_dataset(root)
            for fraction in (0, 1, -0.1, float("nan"), float("inf")):
                with self.subTest(fraction=fraction), self.assertRaises(DatasetError):
                    split_records(records, test_size=fraction)
            one_group_per_class = [r for r in records if "1-20-" in r.sources[0]["source"]
                                   or "sentence_1_" in r.sources[0]["source"]]
            with self.assertRaisesRegex(DatasetError, "independent groups"):
                split_records(one_group_per_class, group_by="filename")

    def test_training_scaler_uses_training_rows_only_and_model_version_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data")
            model, train, test = train_dataset(root, seed=7, test_size=0.3)
            features = np.vstack([extract_features(r.audio_bytes) for r in train])
            np.testing.assert_allclose(model.feature_mean, features.mean(axis=0))
            expected_scale = features.std(axis=0)
            np.testing.assert_allclose(model.feature_scale, np.where(expected_scale < 1e-12, 1, expected_scale))
            all_features = np.vstack([extract_features(r.audio_bytes) for r in train + test])
            self.assertFalse(np.allclose(model.feature_mean, all_features.mean(axis=0)))
            another, _, _ = train_dataset(root, seed=7, test_size=0.3)
            self.assertNotEqual(model.metadata["model_version"], another.metadata["model_version"])
            self.assertEqual(model.metadata["training_data_fingerprint"], another.metadata["training_data_fingerprint"])
            np.testing.assert_array_equal(model.weights, another.weights)

    def test_saved_model_roundtrip_preserves_metadata_and_exact_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            model, train, test = train_dataset(root, seed=7, test_size=0.3, group_by="filename")
            artifact_path = Path(directory) / "model.json"
            save_model(model, artifact_path)
            loaded = load_model(artifact_path)
            self.assertEqual(model.metadata, loaded.metadata)
            self.assertEqual(model.to_artifact(), loaded.to_artifact())
            self.assertEqual(loaded.artifact_sha256, hashlib.sha256(artifact_path.read_bytes()).hexdigest())
            features = np.vstack([extract_features(r.audio_bytes) for r in test])
            np.testing.assert_array_equal(model.predict_proba(features), loaded.predict_proba(features))
            observed: list[bytes] = []

            def record_extraction(audio: bytes) -> np.ndarray:
                observed.append(audio)
                return extract_features(audio)

            with patch("ai_models.train._split_prepared", side_effect=AssertionError("must not redraw holdout")), \
                    patch("ai_models.evaluate.extract_features", side_effect=record_extraction):
                report = evaluate_dataset(loaded, root)
            self.assertEqual(observed, [r.audio_bytes for r in test])
            self.assertEqual(report["sample_count"], len(test))
            self.assertEqual(report["evaluation_split"]["seed"], 7)
            self.assertEqual(report["evaluation_split"]["test_size"], 0.3)
            self.assertEqual(report["evaluation_split"]["group_by"], "filename")
            self.assertEqual(report["evaluation_split"]["test_fingerprint"],
                             model.metadata["training"]["split"]["test_fingerprint"])
            self.assertEqual(report["artifact_sha256"], loaded.artifact_sha256)
            self.assertEqual(sum(map(sum, report["confusion_matrix"])), len(test))
            tn, fp = report["confusion_matrix"][0]
            fn, tp = report["confusion_matrix"][1]
            self.assertEqual(report["accuracy"], (tn + tp) / (tn + fp + fn + tp))
            self.assertEqual(report["confusion_matrix_labels"], ["real", "fake"])
            self.assertEqual(report["latency_scope"], "audio_read_decode_features_and_batch_prediction")
            rendered = json.dumps(report)
            for record in train + test:
                self.assertNotIn(record.relative_path, rendered)
                self.assertNotIn(record.sources[0]["source"], rendered)
                self.assertNotIn(record.sources[0]["source_sha256"], rendered)

    def test_evaluation_rejects_mismatched_arguments_and_changed_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data")
            model, _, _ = train_dataset(root, seed=7, test_size=0.3)
            for kwargs in ({"seed": 8}, {"test_size": 0.2}, {"group_by": "filename"},
                           {"seed": True}, {"test_size": float("nan")}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(DatasetError, "saved training split"):
                    evaluate_dataset(model, root, **kwargs)
            (root / "real" / "sample-0.wav").write_bytes(wav_bytes(233))
            with self.assertRaisesRegex(DatasetError, "dataset fingerprint differs"):
                evaluate_dataset(model, root)

    def test_evaluation_rejects_added_file_or_manifest_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            model, _, _ = train_dataset(root, group_by="filename")
            manifest_path = root / "manifest.json"
            original = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(original)
            manifest["summary"]["changed"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(DatasetError, "dataset fingerprint differs"):
                evaluate_dataset(model, root)
            manifest_path.write_text(original, encoding="utf-8")
            (root / "fake" / "unmanifested.wav").write_bytes(wav_bytes(1300))
            with self.assertRaisesRegex(DatasetError, "manifest does not cover"):
                evaluate_dataset(model, root)

    def test_metrics_zero_f1_auc_ties_and_brier_are_measured(self) -> None:
        model = LogisticRegressionModel(np.zeros(36), -10, np.zeros(36), np.ones(36))
        x, y = np.zeros((2, 36)), np.array([0, 1])
        report = evaluate_arrays(model, x, y)
        self.assertEqual(report["confusion_matrix"], [[1, 0], [1, 0]])
        self.assertEqual(report["accuracy"], 0.5)
        self.assertEqual(report["precision"], 0.0)
        self.assertEqual(report["recall"], 0.0)
        self.assertEqual(report["f1"], 0.0)
        self.assertEqual(report["roc_auc"], 0.5)
        self.assertAlmostEqual(report["brier_score"], float(np.mean((model.predict_proba(x) - y) ** 2)))
        model.bias = 10
        report = evaluate_arrays(model, x, np.zeros(2))
        self.assertEqual(report["f1"], 0.0)
        self.assertEqual(report["precision"], 0.0)
        self.assertEqual(report["recall"], 0.0)
        self.assertIsNone(report["roc_auc"])

    def test_metrics_validate_labels_rows_and_probabilities(self) -> None:
        model = LogisticRegressionModel(np.zeros(36), 0, np.zeros(36), np.ones(36))
        for labels in (np.array([[0], [1]]), np.array([0]), np.array([0, 0.5]),
                       np.array([0, np.nan]), np.array(["0", "1"]), np.array([])):
            with self.subTest(labels=labels.tolist()), self.assertRaises(ValueError):
                evaluate_arrays(model, np.zeros((2, 36)), labels)
        for probabilities in (np.array([0.1]), np.array([0, float("nan")]), np.array([0, 1.1])):
            with patch.object(model, "predict_proba", return_value=probabilities), self.assertRaises(ValueError):
                evaluate_arrays(model, np.zeros((2, 36)), np.array([0, 1]))
        with self.assertRaises(ModelError):
            LogisticRegressionModel.fit(np.zeros((2, 36)), np.array([[0], [1]]))
        with self.assertRaises(ModelError):
            LogisticRegressionModel.fit(np.zeros((2, 36)), np.array(["0", "1"]))

    def test_latency_includes_prediction_after_preprocessing(self) -> None:
        model = LogisticRegressionModel(np.zeros(36), 0, np.zeros(36), np.ones(36))
        with patch("ai_models.evaluate.time.perf_counter", side_effect=[10.0, 10.2]):
            report = evaluate_arrays(model, np.zeros((2, 36)), np.array([0, 1]), latency_seconds=0.4)
        self.assertAlmostEqual(report["latency_ms_per_sample"], 300)
        self.assertAlmostEqual(report["inference_ms_per_sample"], 100)

    def test_artifact_rejects_version_contract_nonfinite_and_corrupt_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data")
            model, _, _ = train_dataset(root)
            artifact = model.to_artifact()
            self.assertEqual(artifact["artifact_version"], ARTIFACT_VERSION)
            mutations = [
                lambda a: a.update(artifact_version="0.0.0"),
                lambda a: a["feature_contract"]["preprocessing"].update(target_sample_rate_hz=8000),
                lambda a: a["feature_contract"]["input"].update(sample_width_bytes=[2]),
                lambda a: a["weights"].__setitem__(0, float("nan")),
                lambda a: a["weights"].__setitem__(0, "0"),
                lambda a: a["feature_scale"].__setitem__(0, 0),
                lambda a: a.update(threshold=0.6),
                lambda a: a["training"]["split"]["test_pcm_sha256"].append(a["training"]["split"]["train_pcm_sha256"][0]),
                lambda a: a["training"].update(seed=999),
                lambda a: a["label_mapping"].update(real=1),
                lambda a: a["label_mapping"].update(real=False),
                lambda a: a["training"]["class_counts"].update(real=6.0),
                lambda a: a["training"]["split"]["group_ids"].__setitem__(
                    a["training"]["split"]["test_pcm_sha256"][0],
                    a["training"]["split"]["group_ids"][a["training"]["split"]["train_pcm_sha256"][0]]),
            ]
            for mutate in mutations:
                invalid = copy.deepcopy(artifact)
                mutate(invalid)
                with self.subTest(mutate=mutate), self.assertRaises(ModelError):
                    LogisticRegressionModel.from_artifact(invalid)

    def test_missing_data_invalid_manifest_and_bounded_file_reads_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(DatasetError):
                collect_dataset(missing)
            root = create_dataset(Path(directory) / "data", manifest=True)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["records"][0]["pcm_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(DatasetError, "does not match decoded"):
                collect_dataset(root)
            manifest_path.unlink()
            audio_path = root / "real" / "too-large.wav"
            with audio_path.open("wb") as stream:
                stream.truncate(MAX_AUDIO_BYTES + 1)
            with self.assertRaisesRegex(DatasetError, "25 MiB"):
                collect_dataset(root)

    def test_cli_uses_saved_defaults_preserves_existing_outputs_and_prints_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = create_dataset(Path(directory) / "data", manifest=True)
            artifact = Path(directory) / "model.json"
            output = Path(directory) / "evaluation.json"
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(train_main(["--dataset", str(root), "--output", str(artifact),
                                             "--seed", "7", "--test-size", "0.3", "--group-by", "filename"]), 0)
            summary = json.loads(stdout.getvalue())
            self.assertNotIn("accuracy", summary)
            self.assertEqual(summary["group_by"], "filename")
            original = artifact.read_bytes()
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                train_main(["--dataset", str(root), "--output", str(artifact)])
            self.assertEqual(artifact.read_bytes(), original)
            with redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(evaluate_main(["--dataset", str(root), "--model", str(artifact),
                                                "--output", str(output)]), 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
            self.assertEqual(report["evaluation_split"]["seed"], 7)
            self.assertEqual(report["evaluation_split"]["group_by"], "filename")
            original_report = output.read_bytes()
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                evaluate_main(["--dataset", str(root), "--model", str(artifact), "--output", str(output)])
            self.assertEqual(output.read_bytes(), original_report)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(evaluate_main(["--dataset", str(root), "--model", str(artifact),
                                                "--output", str(output), "--overwrite"]), 0)
            loaded = load_model(artifact)
            with self.assertRaisesRegex(ModelError, "already exists"):
                save_model(loaded, artifact)
            save_model(loaded, artifact, overwrite=True)
            self.assertEqual(artifact.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
