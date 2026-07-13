from __future__ import annotations

import copy
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


class QuickProfileTests(unittest.TestCase):
    def test_default_auto_model_matches_recommended_profile(self) -> None:
        self.assertEqual(
            mowik.matching_quick_profile("auto", "auto", 2),
            "balanced",
        )

    def test_profile_matching_includes_processing_device(self) -> None:
        self.assertIsNone(
            mowik.matching_quick_profile("large-v3-turbo", "cuda", 2)
        )

    def test_invalid_accuracy_is_custom(self) -> None:
        self.assertIsNone(
            mowik.matching_quick_profile("large-v3-turbo", "auto", "invalid")
        )


class FeedbackConfigTests(unittest.TestCase):
    def test_legacy_config_enables_floating_indicator(self) -> None:
        migrated = mowik.deep_merge(
            mowik.DEFAULT_CONFIG,
            {"feedback": {"sounds": False}},
        )

        self.assertTrue(migrated["feedback"]["floating_indicator"])
        self.assertFalse(migrated["feedback"]["sounds"])

    def test_floating_indicator_opt_out_is_preserved(self) -> None:
        migrated = mowik.deep_merge(
            mowik.DEFAULT_CONFIG,
            {"feedback": {"floating_indicator": False}},
        )

        self.assertFalse(migrated["feedback"]["floating_indicator"])


class StatusIndicatorTests(unittest.TestCase):
    def test_indicator_position_uses_monitor_work_area(self) -> None:
        self.assertEqual(
            mowik.status_indicator_window_position((0, 0, 1920, 1040)),
            (932, 950),
        )
        x, y = mowik.status_indicator_window_position(
            (-1920, 0, 0, 1040)
        )
        self.assertEqual((x, y), (-988, 950))

    def test_indicator_position_clamps_scaled_windows_to_each_work_area(self) -> None:
        cases = (
            ((-1920, 0, 0, 1040), 84, 51),
            ((2560, -300, 4480, 740), 112, 68),
            ((100, 200, 150, 240), 112, 68),
        )
        for work_area, size, margin in cases:
            with self.subTest(work_area=work_area, size=size):
                x, y = mowik.status_indicator_window_position(
                    work_area,
                    size,
                    margin,
                )
                left, top, right, bottom = work_area
                self.assertGreaterEqual(x, left)
                self.assertGreaterEqual(y, top)
                self.assertLessEqual(x, max(left, right - size))
                self.assertLessEqual(y, max(top, bottom - size))

    def test_indicator_frames_are_transparent_rgba_images(self) -> None:
        hidden = mowik.render_status_indicator_frame("hidden")
        self.assertEqual(hidden.mode, "RGBA")
        self.assertEqual(hidden.size, (mowik.STATUS_INDICATOR_SIZE,) * 2)
        self.assertIsNone(hidden.getbbox())

        for state in ("recording", "processing", "success", "error"):
            with self.subTest(state=state):
                frame = mowik.render_status_indicator_frame(state)
                self.assertEqual(frame.mode, "RGBA")
                self.assertIsNotNone(frame.getbbox())

    def test_spinner_animation_changes_between_frames(self) -> None:
        first = mowik.render_status_indicator_frame("processing", 0)
        second = mowik.render_status_indicator_frame("processing", 4)

        self.assertNotEqual(first.tobytes(), second.tobytes())

    def test_disabled_indicator_is_a_no_op(self) -> None:
        indicator = mowik.FloatingStatusIndicator(False)

        self.assertFalse(indicator.start())
        indicator.recording()
        indicator.close()

        self.assertIsNone(indicator._root)

    def test_close_rejects_late_state_commands(self) -> None:
        indicator = mowik.FloatingStatusIndicator(True)

        indicator.close()
        indicator.recording()

        self.assertIsNone(indicator._commands.get_nowait())
        self.assertTrue(indicator._commands.empty())

    def test_state_command_keeps_the_active_monitor_work_area(self) -> None:
        indicator = mowik.FloatingStatusIndicator(True)
        work_area = (-1920, 0, 0, 1040)

        with mock.patch.object(
            mowik,
            "active_monitor_work_area",
            return_value=work_area,
        ):
            indicator.recording()

        self.assertEqual(
            indicator._commands.get_nowait(),
            ("recording", work_area),
        )
        indicator.close()


class DictationIndicatorFlowTests(unittest.TestCase):
    def make_app(self) -> mowik.MowikApp:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        return app

    def test_recording_and_processing_follow_press_and_release(self) -> None:
        app = self.make_app()
        app.model_ready.set()
        app.recorder = mock.Mock()

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik.threading, "Thread"
        ) as thread_class:
            app.begin_dictation()
            app.end_dictation()

        app.recorder.begin.assert_called_once_with()
        app.dictation_indicator.recording.assert_called_once_with()
        app.dictation_indicator.processing.assert_called_once_with()
        thread_class.return_value.start.assert_called_once_with()

    def test_success_is_shown_only_after_text_is_delivered(self) -> None:
        app = self.make_app()
        app.busy = True
        app.transcribe = mock.Mock(return_value="Hello")
        app.jobs.put(np.ones(160, dtype=np.float32))
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "paste_text"
        ) as paste:
            app._job_worker()

        paste.assert_called_once_with("Hello", app.config)
        app.dictation_indicator.success.assert_called_once_with()
        app.dictation_indicator.error.assert_not_called()
        self.assertFalse(app.busy)

    def test_no_speech_finishes_with_error_instead_of_check(self) -> None:
        app = self.make_app()
        app.busy = True
        app.transcribe = mock.Mock(return_value="")
        app.jobs.put(np.ones(160, dtype=np.float32))
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "paste_text"
        ) as paste:
            app._job_worker()

        paste.assert_not_called()
        app.dictation_indicator.error.assert_called_once_with()
        app.dictation_indicator.success.assert_not_called()
        self.assertFalse(app.busy)

    def test_too_short_recording_stops_spinner_without_queueing(self) -> None:
        app = self.make_app()
        app.busy = True
        app.config["post_roll_ms"] = 0
        app.config["minimum_recording_ms"] = 250
        app.recorder = mock.Mock()
        app.recorder.sample_rate = 16_000
        app.recorder.finish.return_value = np.zeros(100, dtype=np.float32)

        with mock.patch.object(app, "beep"):
            app._finish_dictation_after_tail()

        app.dictation_indicator.error.assert_called_once_with()
        self.assertTrue(app.jobs.empty())
        self.assertFalse(app.busy)

    def test_shutdown_closes_indicator_once(self) -> None:
        app = self.make_app()

        app.shutdown()
        app.shutdown()

        app.dictation_indicator.close.assert_called_once_with()


class TrayLifecycleTests(unittest.TestCase):
    def run_tray_until_loop_returns(self, indicator_ready: bool):
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = indicator_ready
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        app.dictation_indicator.start.return_value = indicator_ready
        app.start = mock.Mock()
        tray = mock.Mock()

        with mock.patch.object(mowik.pystray, "Icon", return_value=tray), mock.patch.object(
            app,
            "stop_feedback_sound",
        ):
            app.run_tray()

        return app, tray

    def test_detached_tray_return_triggers_full_shutdown(self) -> None:
        app, tray = self.run_tray_until_loop_returns(True)

        self.assertTrue(app.stop_event.is_set())
        tray.run_detached.assert_called_once_with()
        tray.stop.assert_called_once_with()
        app.dictation_indicator.close.assert_called_once_with()
        self.assertEqual(app.dictation_indicator.run.call_count, 2)

    def test_standard_tray_return_triggers_full_shutdown(self) -> None:
        app, tray = self.run_tray_until_loop_returns(False)

        self.assertTrue(app.stop_event.is_set())
        tray.run.assert_called_once_with()
        tray.stop.assert_called_once_with()
        app.dictation_indicator.close.assert_called_once_with()
        app.dictation_indicator.run.assert_not_called()

    def test_partial_start_failure_still_cleans_up_every_loop(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        app.dictation_indicator.start.return_value = True
        app.start = mock.Mock(side_effect=RuntimeError("partial start"))
        tray = mock.Mock()

        with mock.patch.object(mowik.pystray, "Icon", return_value=tray), mock.patch.object(
            app,
            "stop_feedback_sound",
        ), self.assertRaisesRegex(RuntimeError, "partial start"):
            app.run_tray()

        self.assertTrue(app.stop_event.is_set())
        tray.stop.assert_called_once_with()
        app.dictation_indicator.close.assert_called_once_with()
        app.dictation_indicator.run.assert_called_once_with()


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
