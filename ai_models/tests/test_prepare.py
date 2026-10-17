from __future__ import annotations

import contextlib
import hashlib
import io
import json
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from ai_models import prepare


def wav_bytes(value: int, *, frames: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(struct.pack("<h", value) * frames)
    return output.getvalue()


def read_pcm(path: Path, executable: str, timeout: float) -> bytes:
    # A mocked decoder consuming genuine synthetic WAVs, including fixtures
    # named .mp3; no tests below process the user's audio.
    with wave.open(str(path), "rb") as wav:
        return wav.readframes(wav.getnframes())


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source data"
        (self.source / "real").mkdir(parents=True)
        (self.source / "fake").mkdir()
        self.output = self.source / "prepared"

    def add_audio(self, label: str, name: str, value: int, *, frames: int = 16_000) -> Path:
        path = self.source / label / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav_bytes(value, frames=frames))
        return path

    def add_classes(self) -> None:
        self.add_audio("real", "real.wav", 100)
        self.add_audio("fake", "fake.wav", 200)

    def run_preparation(self, **kwargs: object) -> dict:
        with mock.patch.object(prepare, "resolve_ffmpeg", return_value="mock-ffmpeg"):
            with mock.patch.object(prepare, "decode_pcm", side_effect=read_pcm):
                return prepare.prepare_dataset(self.source, kwargs.pop("output", self.output), **kwargs)

    def read_manifest(self) -> dict:
        return json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))

    def assert_manifest_only(self) -> None:
        self.assertEqual({path.name for path in self.output.iterdir()}, {"manifest.json"})
        self.assertEqual(self.read_manifest()["records"], [])

    def test_preserves_originals_and_writes_finite_pcm_wavs_and_hashes(self) -> None:
        self.add_classes()
        originals = {path: path.read_bytes() for path in self.source.rglob("*.wav")}
        manifest = self.run_preparation()
        self.assertEqual(manifest, self.read_manifest())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["summary"]["prepared_count"], 2)
        self.assertEqual(manifest["summary"]["class_counts"], {"real": 1, "fake": 1})
        self.assertFalse(manifest["summary"]["split_recommended"])
        self.assertEqual(manifest["summary"]["total_duration_seconds"], 2)
        for record in manifest["records"]:
            path = self.output / record["output"]
            data = path.read_bytes()
            with wave.open(str(path), "rb") as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16_000))
                self.assertEqual(wav.getnframes(), 16_000)
                self.assertEqual(wav.getcomptype(), "NONE")
                pcm = wav.readframes(wav.getnframes())
            self.assertEqual(struct.unpack("<I", data[4:8])[0], len(data) - 8)
            self.assertEqual(hashlib.sha256(pcm).hexdigest(), record["pcm_sha256"])
            self.assertEqual(hashlib.sha256((self.source / record["source"]).read_bytes()).hexdigest(), record["source_sha256"])
            self.assertEqual(record["duration_seconds"], 1)
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)

    def test_paths_with_spaces_unicode_and_nested_same_names_are_hashed(self) -> None:
        first = self.add_audio("real", "母 音 ; $(ignored)/same file.mp3", 100)
        second = self.add_audio("real", "other/same file.mp3", 101)
        self.add_audio("fake", "réel fake.wav", 200)
        manifest = self.run_preparation()
        paths = {record["source"]: record["output"] for record in manifest["records"]}
        for source in (first, second):
            relative = source.relative_to(self.source).as_posix()
            self.assertEqual(paths[relative], "real/" + hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".wav")
        self.assertEqual(len(set(paths.values())), 3)
        self.assertTrue(all(Path(path).name.isascii() for path in paths.values()))

    def test_within_class_decoded_duplicates_are_reported_not_written(self) -> None:
        self.add_classes()
        duplicate = self.add_audio("real", "nested/original copy.mp3", 100)
        duplicate.write_bytes(duplicate.read_bytes() + b"different non-audio metadata")
        manifest = self.run_preparation()
        self.assertEqual(manifest["summary"]["prepared_count"], 2)
        self.assertEqual(manifest["summary"]["duplicate_count"], 1)
        self.assertEqual(len(list(self.output.rglob("*.wav"))), 2)
        exclusion = manifest["excluded"][0]
        self.assertEqual(exclusion["status"], "duplicate")
        self.assertEqual(exclusion["reason"], "decoded_pcm_duplicate")
        self.assertNotEqual(exclusion["source"], exclusion["duplicate_of"])

    def test_cross_label_conflicts_always_fail_and_mark_whole_duplicate_group(self) -> None:
        for skip in (False, True):
            with self.subTest(skip_invalid=skip):
                self.add_audio("real", "a.wav", 100)
                self.add_audio("real", "nested/b.mp3", 100)
                self.add_audio("fake", "c.wav", 100)
                output = self.source / f"conflict-{skip}"
                with self.assertRaises(prepare.PreparationError) as caught:
                    self.run_preparation(output=output, skip_invalid=skip)
                manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["records"], [])
                self.assertEqual({path.name for path in output.iterdir()}, {"manifest.json"})
                self.assertEqual(manifest["summary"]["conflict_count"], 3)
                self.assertEqual(manifest["summary"]["conflict_group_count"], 1)
                self.assertIn("cross_label_conflict", caught.exception.summary["failure_reasons"])
                self.assertEqual(len(manifest["excluded"]), 3)
                self.assertTrue(all(entry["reason"] == "cross_label_pcm_conflict" and entry["status"] == "rejected" for entry in manifest["excluded"]))

    def test_invalid_duration_refuses_publication_by_default(self) -> None:
        self.add_classes()
        self.add_audio("real", "too short.wav", 300, frames=15_999)
        self.add_audio("fake", "too long.wav", 400, frames=300 * 16_000 + 1)
        with self.assertRaises(prepare.PreparationError) as caught:
            self.run_preparation()
        self.assertEqual(caught.exception.summary["prepared_count"], 0)
        self.assertEqual(caught.exception.summary["candidate_count"], 2)
        self.assertEqual(caught.exception.summary["rejected_count"], 2)
        self.assert_manifest_only()
        self.assertEqual({entry["reason"] for entry in self.read_manifest()["excluded"]}, {"duration_below_1_second", "duration_above_300_seconds"})

    def test_skip_invalid_publishes_valid_subset_and_private_reasons(self) -> None:
        self.add_classes()
        empty = self.source / "real" / "empty.mp3"
        empty.write_bytes(b"")
        corrupt = self.source / "fake" / "corrupt.mp3"
        corrupt.write_bytes(b"not audio")

        def decoder(path: Path, executable: str, timeout: float) -> bytes:
            if path == corrupt:
                raise prepare.DecodeError("decode_failed")
            self.assertNotEqual(path, empty)
            return read_pcm(path, executable, timeout)

        with mock.patch.object(prepare, "resolve_ffmpeg", return_value="mock-ffmpeg"):
            with mock.patch.object(prepare, "decode_pcm", side_effect=decoder):
                manifest = prepare.prepare_dataset(self.source, self.output, skip_invalid=True)
        self.assertEqual(manifest["summary"]["prepared_count"], 2)
        self.assertEqual(manifest["summary"]["rejected_count"], 2)
        self.assertEqual({entry["reason"] for entry in manifest["excluded"]}, {"empty_source", "decode_failed"})
        self.assertEqual(empty.read_bytes(), b"")
        self.assertEqual(corrupt.read_bytes(), b"not audio")

    def test_class_with_only_invalid_files_still_fails_when_skipping(self) -> None:
        self.add_audio("real", "valid.wav", 100)
        (self.source / "fake" / "empty.mp3").write_bytes(b"")
        with self.assertRaises(prepare.PreparationError) as caught:
            self.run_preparation(skip_invalid=True)
        self.assertIn("empty_valid_class", caught.exception.summary["failure_reasons"])
        self.assert_manifest_only()

    def test_existing_destination_refused_without_touching_any_files(self) -> None:
        self.add_classes()
        self.output.mkdir()
        manifest = self.output / "manifest.json"
        manifest.write_bytes(b"keep existing data")
        with mock.patch.object(prepare, "resolve_ffmpeg") as resolver:
            with mock.patch.object(prepare, "decode_pcm") as decoder:
                with self.assertRaisesRegex(prepare.PreparationError, "output_exists"):
                    prepare.prepare_dataset(self.source, self.output)
                decoder.assert_not_called()
            resolver.assert_not_called()
        self.assertEqual(manifest.read_bytes(), b"keep existing data")

    def test_empty_destination_is_also_refused(self) -> None:
        self.add_classes()
        self.output.mkdir()
        with self.assertRaisesRegex(prepare.PreparationError, "output_exists"):
            self.run_preparation()

    def test_source_output_overlap_is_refused(self) -> None:
        self.add_classes()
        for output in (self.source, self.root, self.source / "real" / "prepared", self.source / "fake" / "nested"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(prepare.PreparationError, "source_output_overlap"):
                    self.run_preparation(output=output)

    def test_ignored_extensions_and_outside_class_audio_are_not_decoded(self) -> None:
        self.add_classes()
        (self.source / "real" / "notes.TXT").write_text("private", encoding="utf-8")
        (self.source / "fake" / "README").write_text("private", encoding="utf-8")
        (self.source / "outside.wav").write_bytes(b"not in a class")
        outside = self.source / "other" / "nested.mp3"
        outside.parent.mkdir()
        outside.write_bytes(b"not in a class")
        manifest = self.run_preparation()
        self.assertEqual(manifest["summary"]["discovered_count"], 2)
        self.assertEqual(manifest["summary"]["ignored_counts_by_extension"], {".txt": 1, "<none>": 1})

    def test_empty_classes_fail_without_needing_decoder_and_without_wavs(self) -> None:
        with mock.patch.object(prepare, "resolve_ffmpeg") as resolver:
            with self.assertRaises(prepare.PreparationError) as caught:
                prepare.prepare_dataset(self.source, self.output)
            resolver.assert_not_called()
        self.assertIn("empty_valid_class", caught.exception.summary["failure_reasons"])
        self.assert_manifest_only()

    def test_missing_class_is_refused(self) -> None:
        (self.source / "fake").rmdir()
        with self.assertRaisesRegex(prepare.PreparationError, "missing_or_linked_class_directory"):
            self.run_preparation()
        self.assertFalse(self.output.exists())

    def test_two_unique_per_class_produces_split_recommendation(self) -> None:
        self.add_classes()
        self.add_audio("real", "second.WAV", 101)
        self.add_audio("fake", "second.MP3", 201)
        summary = self.run_preparation()["summary"]
        self.assertTrue(summary["split_recommended"])
        self.assertEqual(summary["class_counts"], {"real": 2, "fake": 2})

    def test_reruns_to_new_destinations_are_byte_reproducible(self) -> None:
        self.add_classes()
        first = self.run_preparation()
        second_output = self.source / "prepared-again"
        second = self.run_preparation(output=second_output)
        self.assertEqual(first, second)
        for record in first["records"]:
            self.assertEqual((self.output / record["output"]).read_bytes(), (second_output / record["output"]).read_bytes())

    def test_stdout_is_aggregate_only(self) -> None:
        self.add_audio("real", "secret victim 银行.wav", 100)
        self.add_audio("fake", "secret scammer.wav", 200)
        stdout = io.StringIO()
        with mock.patch.object(prepare, "resolve_ffmpeg", return_value="mock-ffmpeg"):
            with mock.patch.object(prepare, "decode_pcm", side_effect=read_pcm):
                with contextlib.redirect_stdout(stdout):
                    code = prepare.main(["--source", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["prepared_count"], 2)
        self.assertNotIn("secret", stdout.getvalue())
        self.assertNotIn("银行", stdout.getvalue())
        self.assertNotIn(str(self.source), stdout.getvalue())
        self.assertNotIn("records", result)

    def test_failed_cli_returns_two_and_reports_only_aggregate_json(self) -> None:
        self.add_classes()
        (self.source / "real" / "private account.mp3").write_bytes(b"")
        stdout = io.StringIO()
        with mock.patch.object(prepare, "resolve_ffmpeg", return_value="mock-ffmpeg"):
            with mock.patch.object(prepare, "decode_pcm", side_effect=read_pcm):
                with contextlib.redirect_stdout(stdout):
                    code = prepare.main(["--source", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 2)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["rejected_count"], 1)
        self.assertEqual(summary["prepared_count"], 0)
        self.assertNotIn("private account", stdout.getvalue())
        self.assertNotIn(str(self.source), stdout.getvalue())
        self.assert_manifest_only()

    def test_source_mutation_during_decode_is_rejected(self) -> None:
        self.add_classes()

        def decoder(path: Path, executable: str, timeout: float) -> bytes:
            pcm = read_pcm(path, executable, timeout)
            if path.parent.name == "real":
                path.write_bytes(wav_bytes(101))
            return pcm

        with mock.patch.object(prepare, "resolve_ffmpeg", return_value="mock-ffmpeg"):
            with mock.patch.object(prepare, "decode_pcm", side_effect=decoder):
                with self.assertRaises(prepare.PreparationError):
                    prepare.prepare_dataset(self.source, self.output)
        self.assert_manifest_only()
        self.assertIn("source_changed_during_decode", {entry["reason"] for entry in self.read_manifest()["excluded"]})

    def test_write_failure_leaves_no_published_dataset_or_staging_files(self) -> None:
        self.add_classes()
        with mock.patch.object(prepare, "_write_wav", side_effect=OSError("private path")):
            with self.assertRaisesRegex(prepare.PreparationError, "preparation_io_failed"):
                self.run_preparation()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.source.glob(".audio-prepare-*")), [])

    def test_linked_audio_is_not_read_or_followed(self) -> None:
        self.add_classes()
        outside = self.root / "outside.wav"
        outside.write_bytes(wav_bytes(300))
        link = self.source / "real" / "linked.wav"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Creating symlinks is unavailable for this user")
        manifest = self.run_preparation(skip_invalid=True)
        self.assertEqual(manifest["summary"]["prepared_count"], 2)
        self.assertEqual(manifest["excluded"][0]["reason"], "linked_source_not_allowed")

    def test_non_finite_or_non_positive_timeouts_are_refused(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(prepare.PreparationError, "timeout_must_be_finite_and_positive"):
                    self.run_preparation(timeout=timeout)
        self.assertFalse(self.output.exists())


class DecoderTests(unittest.TestCase):
    def test_command_is_local_bounded_explicit_and_uses_timeout_without_shell(self) -> None:
        source = Path(tempfile.gettempdir()) / "audio with spaces 银行 ;.mp3"
        pcm = b"\x01\x00" * 16_000
        executable = "C:/decoder with spaces/ffmpeg.exe"
        completed = subprocess.CompletedProcess([], 0, stdout=pcm)
        with mock.patch.object(prepare.subprocess, "run", return_value=completed) as run:
            self.assertEqual(prepare.decode_pcm(source, executable, 2.5), pcm)
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], executable)
        self.assertEqual(command[command.index("-i") + 1], str(source.resolve()))
        for flag, value in (
            ("-protocol_whitelist", "file,pipe"),
            ("-map", "0:a:0"),
            ("-map_metadata", "-1"),
            ("-map_chapters", "-1"),
            ("-ac", "1"),
            ("-ar", "16000"),
            ("-c:a", "pcm_s16le"),
            ("-t", "301"),
            ("-fs", str(prepare.MAX_DECODED_BYTES)),
            ("-f", "s16le"),
        ):
            self.assertEqual(command[command.index(flag) + 1], value)
        for flag in ("-nostdin", "-vn", "-sn", "-dn", "-xerror"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], "pipe:1")
        self.assertEqual(run.call_args.kwargs["timeout"], 2.5)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertLess(prepare.MAX_DECODED_BYTES, 10_000_000)

    def test_timeout_corrupt_and_missing_decoder_have_path_free_reasons(self) -> None:
        errors = (
            (subprocess.TimeoutExpired(["private-file"], 30), "decode_timeout"),
            (subprocess.CalledProcessError(1, ["private-file"]), "decode_failed"),
            (OSError("private-file"), "decoder_unavailable"),
        )
        for error, reason in errors:
            with self.subTest(reason=reason):
                with mock.patch.object(prepare.subprocess, "run", side_effect=error):
                    with self.assertRaises(prepare.DecodeError) as caught:
                        prepare.decode_pcm(Path("private-file.wav"), "ffmpeg", 30)
                self.assertEqual(str(caught.exception), reason)

    def test_pcm_limits_reject_empty_partial_and_oversize_frames(self) -> None:
        cases = (
            (b"", "empty_decoded_audio"),
            (b"a", "incomplete_pcm_frame"),
            (b"\x00\x00" * 15_999, "duration_below_1_second"),
            (b"\x00\x00" * (300 * 16_000 + 1), "duration_above_300_seconds"),
            (b"\x00" * (prepare.MAX_DECODED_BYTES + 2), "decoded_output_too_large"),
        )
        for pcm, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(prepare.DecodeError, reason):
                    prepare._validate_pcm(pcm)
        self.assertEqual(prepare._validate_pcm(b"\x00\x00" * 16_000), 1)
        self.assertEqual(prepare._validate_pcm(b"\x00\x00" * (300 * 16_000)), 300)


class ExecutableResolutionTests(unittest.TestCase):
    def test_explicit_executable_takes_precedence(self) -> None:
        with mock.patch.object(prepare, "_executable", return_value="explicit") as executable:
            with mock.patch.object(prepare.shutil, "which") as which:
                with mock.patch.object(prepare.importlib, "import_module") as imported:
                    self.assertEqual(prepare.resolve_ffmpeg("explicit"), "explicit")
        executable.assert_called_once_with("explicit")
        which.assert_not_called()
        imported.assert_not_called()

    def test_missing_explicit_executable_does_not_fall_back(self) -> None:
        with mock.patch.object(prepare, "_executable", return_value=None):
            with mock.patch.object(prepare.importlib, "import_module") as imported:
                with self.assertRaisesRegex(prepare.PreparationError, "ffmpeg_argument_unavailable"):
                    prepare.resolve_ffmpeg("missing")
                imported.assert_not_called()

    def test_system_path_precedes_optional_package(self) -> None:
        path = str(Path(tempfile.gettempdir()) / "system-ffmpeg.exe")
        with mock.patch.object(prepare.shutil, "which", return_value=path):
            with mock.patch.object(prepare.importlib, "import_module") as imported:
                self.assertEqual(prepare.resolve_ffmpeg(), str(Path(path).resolve()))
                imported.assert_not_called()

    def test_installed_imageio_fallback_only_calls_local_executable_lookup(self) -> None:
        package = mock.Mock()
        package.get_ffmpeg_exe.return_value = "bundled-ffmpeg"
        with mock.patch.object(prepare.shutil, "which", return_value=None):
            with mock.patch.object(prepare.importlib, "import_module", return_value=package) as imported:
                with mock.patch.object(prepare, "_executable", return_value="bundled-ffmpeg"):
                    self.assertEqual(prepare.resolve_ffmpeg(), "bundled-ffmpeg")
        imported.assert_called_once_with("imageio_ffmpeg")
        package.get_ffmpeg_exe.assert_called_once_with()
        self.assertEqual(package.method_calls, [mock.call.get_ffmpeg_exe()])

    def test_no_local_decoder_fails_without_installing_anything(self) -> None:
        with mock.patch.object(prepare.shutil, "which", return_value=None):
            with mock.patch.object(prepare.importlib, "import_module", side_effect=ImportError):
                with self.assertRaisesRegex(prepare.PreparationError, "ffmpeg_not_installed"):
                    prepare.resolve_ffmpeg()


class InstalledFFmpegSmokeTests(unittest.TestCase):
    def test_synthetic_stereo_wavs_are_really_converted_if_decoder_is_installed(self) -> None:
        try:
            executable = prepare.resolve_ffmpeg()
        except prepare.PreparationError:
            self.skipTest("No local FFmpeg is installed; no download attempted")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic source"
            originals = {}
            for name, value in (("real", 100), ("fake", -100)):
                (source / name).mkdir(parents=True)
                path = source / name / "stereo 银行.wav"
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(2)
                    wav.setsampwidth(2)
                    wav.setframerate(8_000)
                    wav.writeframes(struct.pack("<hh", value, value) * 16_000)
                originals[path] = path.read_bytes()
            output = root / "synthetic prepared"
            manifest = prepare.prepare_dataset(source, output, ffmpeg=executable)
            self.assertEqual(manifest["summary"]["class_counts"], {"real": 1, "fake": 1})
            for record in manifest["records"]:
                with wave.open(str(output / record["output"]), "rb") as wav:
                    self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16_000))
                    self.assertEqual(wav.getnframes(), 32_000)
                self.assertEqual(record["duration_seconds"], 2)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
