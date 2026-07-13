from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest
from unittest import mock
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mowik


class BuiltinSoundTests(unittest.TestCase):
    def test_builtin_sounds_are_quiet_click_free_pcm(self) -> None:
        for kind, notes in mowik.BUILTIN_SOUND_NOTES.items():
            with self.subTest(kind=kind):
                with wave.open(io.BytesIO(mowik.builtin_sound_wav(kind)), "rb") as wav:
                    self.assertEqual(wav.getnchannels(), 1)
                    self.assertEqual(wav.getsampwidth(), 2)
                    self.assertEqual(wav.getframerate(), 44_100)
                    pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)

                expected_ms = sum(duration + gap for _, duration, gap in notes)
                actual_ms = len(pcm) * 1000 / 44_100
                self.assertAlmostEqual(actual_ms, expected_ms, delta=1.0)
                self.assertEqual(int(pcm[0]), 0)
                self.assertEqual(int(pcm[-1]), 0)
                self.assertLessEqual(int(np.max(np.abs(pcm.astype(np.int32)))), 3_100)


class RuntimeSelectionTests(unittest.TestCase):
    def test_auto_cpu_threads_uses_physical_core_estimate(self) -> None:
        with mock.patch.object(mowik.os, "cpu_count", return_value=32):
            self.assertEqual(mowik.resolve_cpu_threads({"cpu_threads": 0}), 16)
        self.assertEqual(mowik.resolve_cpu_threads({"cpu_threads": 7}), 7)

    def test_cuda_warmup_runs_encoder_without_decoder(self) -> None:
        model = mock.Mock()
        model.feature_extractor.return_value = np.zeros((80, 50), dtype=np.float32)

        mowik.warm_up_cuda_model(model, {})

        model.encode.assert_called_once()
        encoded_input = model.encode.call_args.args[0]
        self.assertEqual(encoded_input.shape, (80, 3000))
        self.assertFalse(model.transcribe.called)


class InternationalDictationTests(unittest.TestCase):
    def test_old_config_keeps_transcription_language_and_gains_ui_language(self) -> None:
        migrated = mowik.deep_merge(
            mowik.DEFAULT_CONFIG,
            {"language": "pl", "trigger": "keyboard:f8"},
        )

        self.assertEqual(migrated["language"], "pl")
        self.assertEqual(migrated["ui_language"], "auto")

    def test_english_voice_commands(self) -> None:
        config = {
            "language": "en",
            "voice_commands": {"enabled": True},
        }

        result = mowik.apply_voice_commands(
            "First sentence new paragraph second sentence new line third",
            config,
        )

        self.assertEqual(
            result,
            "First sentence\n\nsecond sentence\nthird",
        )

    def test_auto_voice_commands_supports_polish_and_english(self) -> None:
        config = {
            "language": "auto",
            "voice_commands": {"enabled": True},
        }

        result = mowik.apply_voice_commands(
            "Pierwsza nowa linia second new paragraph third",
            config,
        )

        self.assertEqual(result, "Pierwsza\nsecond\n\nthird")

    def test_other_transcription_languages_accept_bilingual_commands(self) -> None:
        config = {
            "language": "de",
            "voice_commands": {"enabled": True},
        }

        result = mowik.apply_voice_commands(
            "Erste Zeile new line druga nowa linia trzecia",
            config,
        )

        self.assertEqual(result, "Erste Zeile\ndruga\ntrzecia")

    def test_llm_wrapper_and_english_negation_are_safe(self) -> None:
        self.assertEqual(
            mowik.strip_llm_wrapping("Corrected text: This is ready."),
            "This is ready.",
        )
        self.assertFalse(
            mowik.llm_result_is_safe(
                "This should not change.",
                "This should change.",
            )
        )

    def test_llm_safety_preserves_negations_in_supported_languages(self) -> None:
        examples = (
            ("Das Ergebnis ist nicht korrekt.", "Das Ergebnis ist korrekt."),
            ("Ce résultat n’est jamais correct.", "Ce résultat est correct."),
            ("Este resultado no es correcto.", "Este resultado es correcto."),
            ("Цей результат не є правильним.", "Цей результат є правильним."),
            ("Залишити без змін.", "Залишити зі змінами."),
            ("This is not required.", "This is never required."),
        )

        for original, corrected in examples:
            with self.subTest(original=original):
                self.assertFalse(mowik.llm_result_is_safe(original, corrected))


class RecorderTests(unittest.TestCase):
    def test_pre_roll_keeps_exact_number_of_samples(self) -> None:
        recorder = mowik.ContinuousRecorder({"pre_roll_ms": 300, "microphone": None})
        chunk = np.ones((1024, 1), dtype=np.float32)

        for _ in range(10):
            recorder._callback(chunk, len(chunk), None, 0)

        self.assertEqual(recorder.pre_roll_samples, 4_800)
        self.assertEqual(recorder._ring_samples, recorder.pre_roll_samples)
        self.assertEqual(sum(len(part) for part in recorder._ring), 4_800)


if __name__ == "__main__":
    unittest.main()
