import io
import struct
import unittest
import wave

from app.audio_quality import analyze_audio_quality, segment_wavs
from app.inference import ModelArtifact, ModelInferenceService
from app.transcript import analyze_transcript


def wav(value=0, seconds=1.2):
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        frames = b"".join(struct.pack("<h", value) for _ in range(int(16000 * seconds)))
        handle.writeframes(frames)
    return output.getvalue()


class QualityAndLanguageTests(unittest.TestCase):
    def test_quality_distinguishes_silence_and_segments_audio(self):
        silent = analyze_audio_quality(wav())
        self.assertEqual(silent["status"], "insufficient_evidence")
        tone = analyze_audio_quality(wav(value=9000))
        self.assertNotEqual(tone["status"], "insufficient_evidence")
        segments = segment_wavs(wav(value=9000, seconds=12.0), segment_seconds=5, max_segments=3)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0][0], 0.0)
        self.assertTrue(segments[-1][1] > segments[0][1])

    def test_local_rules_support_hindi_and_spanish_signals(self):
        hindi = analyze_transcript("कृपया अपना पासवर्ड और ओटीपी बताएं", consented=True, language="hi")
        self.assertIn("credential_request", hindi["scam_categories"])
        self.assertEqual(hindi["transcript_language"], "hi")
        spanish = analyze_transcript("Envíame dinero ahora mismo", consented=True, language="es")
        self.assertIn("financial_request", spanish["scam_categories"])
        self.assertIn("urgency_or_threat", spanish["scam_categories"])

    def test_model_returns_insufficient_evidence_without_a_score(self):
        class FakeLoader:
            ready = True
            artifact = ModelArtifact(model_version="quality-v1", feature_contract={"version": 1, "name": "pcm"})
            preprocessor = staticmethod(lambda audio, _contract: audio)

            class Model:
                @staticmethod
                def predict(features, _contract):
                    return 0.9

            model = Model()

        service = ModelInferenceService(
            {"MODEL_THRESHOLD": 0.5, "AUDIO_QUALITY_ENABLED": True},
            loader=FakeLoader(),
        )
        metadata = type(
            "Metadata",
            (),
            {"audio_bytes": wav(), "audio_quality": analyze_audio_quality(wav())},
        )()
        result = service.analyze(metadata)
        self.assertEqual(result["deepfake_status"], "insufficient_evidence")
        self.assertIsNone(result["confidence"])
        self.assertEqual(result["segment_scores"], [])


if __name__ == "__main__":
    unittest.main()
