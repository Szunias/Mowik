from __future__ import annotations

import copy
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
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

    def test_model_startup_failure_closes_the_microphone(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        recorder = mock.Mock()

        with mock.patch.object(
            mowik,
            "ContinuousRecorder",
            return_value=recorder,
        ), mock.patch.object(
            mowik,
            "create_model",
            side_effect=RuntimeError("model failed"),
        ), mock.patch.object(app, "set_status"):
            app._load_runtime()

        recorder.start.assert_called_once_with()
        recorder.close.assert_called_once_with()
        self.assertIsNone(app.recorder)
        self.assertFalse(app.model_ready.is_set())


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
    def test_deep_merge_does_not_alias_factory_defaults_or_loaded_values(self) -> None:
        defaults = copy.deepcopy(mowik.DEFAULT_CONFIG)
        loaded = {
            "feedback": {"sounds": False},
            "future_section": {"items": ["keep"]},
        }

        merged = mowik.deep_merge(defaults, loaded)
        merged["vad"]["threshold"] = 0.99
        merged["custom_commands"]["items"].append({"phrase": "test"})
        merged["future_section"]["items"].append("changed")

        self.assertEqual(defaults["vad"]["threshold"], 0.45)
        self.assertEqual(defaults["custom_commands"]["items"], [])
        self.assertEqual(loaded["future_section"]["items"], ["keep"])

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


class CustomCommandConfigTests(unittest.TestCase):
    def test_foreign_schema_is_not_enriched_by_default_merge(self) -> None:
        foreign = {
            "schema_version": 2,
            "future_payload": {"keep": [1, 2]},
        }

        migrated = mowik.deep_merge(
            mowik.DEFAULT_CONFIG,
            {"custom_commands": foreign},
        )

        self.assertEqual(migrated["custom_commands"], foreign)
        self.assertIsNot(migrated["custom_commands"], foreign)

    def test_legacy_config_gains_disabled_f7_command_mode(self) -> None:
        migrated = mowik.deep_merge(
            mowik.DEFAULT_CONFIG,
            {"trigger": "keyboard:f8"},
        )

        self.assertFalse(migrated["custom_commands"]["enabled"])
        self.assertEqual(migrated["custom_commands"]["trigger"], "keyboard:f7")
        self.assertEqual(migrated["custom_commands"]["items"], [])

    def test_phrase_normalization_handles_unicode_case_and_punctuation(self) -> None:
        self.assertEqual(
            mowik.normalize_custom_command_phrase(
                "  „WSTAW—MO\u0301J,\u00a0ADRES…!”  "
            ),
            "wstaw mój adres",
        )
        self.assertNotEqual(
            mowik.normalize_custom_command_phrase("mój"),
            mowik.normalize_custom_command_phrase("moj"),
        )

    def test_match_requires_the_whole_utterance(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["custom_commands"]["items"] = [
            {
                "phrase": "wklej adres",
                "action": "paste_text",
                "value": "Example Street 1",
            }
        ]

        self.assertIsNotNone(mowik.match_custom_command("Wklej adres!", config))
        self.assertIsNone(
            mowik.match_custom_command("proszę wklej adres", config)
        )
        self.assertIsNone(mowik.match_custom_command("wklej adres teraz", config))

    def test_ambiguous_duplicates_are_excluded_but_unique_items_survive(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["custom_commands"]["items"] = [
            {"phrase": "Wklej adres", "text": "first"},
            {"phrase": "wklej, ADRES!", "text": "second"},
            {
                "phrase": "otwórz stronę",
                "action": "open",
                "value": "https://example.com",
            },
        ]

        commands = mowik.configured_custom_commands(config)

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["action"], "open")
        self.assertIsNone(mowik.match_custom_command("wklej adres", config))

    def test_open_defaults_to_confirmation_and_legacy_shell_is_disabled(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["custom_commands"]["items"] = [
            {
                "phrase": "otwórz notatnik",
                "action": "open",
                "value": r"C:\Windows\System32\notepad.exe",
                "confirm": False,
            },
            {
                "phrase": "sprawdź repozytorium",
                "action": "run_command",
                "value": "git status",
                "confirm": "invalid",
            },
            {
                "phrase": "wklej podpis",
                "action": "paste_text",
                "value": "Best regards",
            },
        ]

        commands = {
            item["action"]: item for item in mowik.configured_custom_commands(config)
        }

        self.assertTrue(commands["open"]["confirm"])
        self.assertNotIn("run_command", commands)
        self.assertFalse(commands["paste_text"]["confirm"])
        _, _, unmanaged = mowik.partition_custom_command_items(config)
        self.assertEqual([item["action"] for item in unmanaged], ["run_command"])

    def test_unmanaged_entries_are_partitioned_for_lossless_settings_save(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        future = {
            "phrase": "future action",
            "action": "future_action",
            "value": "opaque",
            "future_metadata": {"keep": True},
        }
        config["custom_commands"]["items"] = [
            {
                "phrase": "valid action",
                "action": "paste_text",
                "value": "ready",
                "extra": "preserve",
            },
            future,
        ]

        valid, originals, unmanaged = mowik.partition_custom_command_items(config)

        self.assertEqual(len(valid), 1)
        self.assertEqual(
            originals[mowik.normalize_custom_command_phrase("valid action")][
                "extra"
            ],
            "preserve",
        )
        self.assertEqual(unmanaged, [future])

    def test_exact_and_prefix_variants_are_each_preserved_once(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        exact = {
            "id": "terminal_exact",
            "phrase": "otwórz terminal",
            "match": "exact",
            "action": "open_terminal",
            "value": "",
        }
        prefix = {
            "id": "terminal_prefix",
            "phrase": "otwórz terminal",
            "match": "prefix_tail",
            "action": "open_terminal",
            "value": "",
        }
        config["custom_commands"]["items"] = [exact, prefix]

        valid, originals, unmanaged = mowik.partition_custom_command_items(config)

        self.assertEqual(len(valid), 2)
        self.assertEqual(originals["id:terminal_exact"], exact)
        self.assertEqual(originals["id:terminal_prefix"], prefix)
        self.assertEqual(unmanaged, [])

    def test_foreign_schema_is_opaque_and_settings_preserve_it_exactly(self) -> None:
        future = {
            "schema_version": 2,
            "enabled": True,
            "trigger": "keyboard:f7",
            "items": [
                {
                    "phrase": "future action",
                    "action": "future_action",
                    "value": {"opaque": True},
                }
            ],
            "future_metadata": {"keep": [1, 2, 3]},
        }
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["custom_commands"] = copy.deepcopy(future)

        valid, originals, unmanaged = mowik.partition_custom_command_items(config)
        saved = mowik.custom_commands_settings_for_save(
            future,
            enabled=False,
            trigger="keyboard:f9",
            items=[],
        )

        self.assertEqual(valid, [])
        self.assertEqual(originals, {})
        self.assertEqual(unmanaged, future["items"])
        self.assertEqual(saved, future)
        self.assertIsNot(saved, future)

    def test_legacy_settings_save_upgrades_to_current_schema(self) -> None:
        saved = mowik.custom_commands_settings_for_save(
            {"enabled": False, "future_metadata": "preserve"},
            enabled=True,
            trigger="keyboard:f9",
            items=[{"phrase": "hello"}],
        )

        self.assertEqual(
            saved["schema_version"],
            mowik.command_engine.CUSTOM_COMMANDS_SCHEMA_VERSION,
        )
        self.assertIs(saved["enabled"], True)
        self.assertEqual(saved["trigger"], "keyboard:f9")
        self.assertEqual(saved["items"], [{"phrase": "hello"}])
        self.assertEqual(saved["future_metadata"], "preserve")

    def test_open_target_must_be_one_line_and_legacy_shell_is_rejected(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["custom_commands"]["items"] = [
            {
                "phrase": "bad open",
                "action": "open",
                "value": "first\nsecond",
            },
            {
                "phrase": "too long",
                "action": "run_command",
                "value": "x" * (mowik.MAX_CUSTOM_COMMAND_LINE_LENGTH + 1),
            },
        ]

        self.assertEqual(mowik.configured_custom_commands(config), [])

    def test_open_target_allows_only_https_or_existing_safe_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            document = root / "notes.txt"
            executable = root / "trusted.exe"
            script = root / "unsafe.cmd"
            document.write_text("notes", encoding="utf-8")
            executable.write_bytes(b"MZ")
            script.write_text("whoami", encoding="utf-8")

            self.assertEqual(
                mowik.resolve_custom_command_open_target(str(document)),
                str(document),
            )
            self.assertEqual(
                mowik.resolve_custom_command_open_target(str(executable)),
                str(executable),
            )
            self.assertEqual(
                mowik.resolve_custom_command_open_target(str(root)),
                str(root),
            )
            self.assertEqual(
                mowik.resolve_custom_command_open_target("https://example.com/docs"),
                "https://example.com/docs",
            )
            with mock.patch.object(
                mowik.windows_actions,
                "is_local_filesystem_path",
                return_value=False,
            ):
                with self.assertRaises(mowik.CustomOpenTargetError):
                    mowik.resolve_custom_command_open_target(str(document))

            unsafe = (
                str(script),
                "notepad.exe",
                "http://example.com",
                "file:///C:/Windows/notepad.exe",
                "https://user:secret@example.com",
                "https://example.com\\path",
                "https://exa\tmple.com",
                "https://example.com/hidden\u2028line",
                "https://example.com/hidden\u2029line",
                "https://example.com/hidden\u200btext",
                "https://example.com/hidden\u2066text",
                "https://example.com/hidden\x1btext",
                r"\\server\share\tool.exe",
                str(document) + ":payload.exe",
                str(document) + ".",
                str(root / "missing.txt"),
            )
            for target in unsafe:
                with self.subTest(target=target):
                    with self.assertRaises(mowik.CustomOpenTargetError):
                        mowik.resolve_custom_command_open_target(target)

    def test_open_target_blocklist_matches_the_pure_command_engine(self) -> None:
        self.assertEqual(
            mowik.BLOCKED_CUSTOM_OPEN_SUFFIXES,
            mowik.command_engine.BLOCKED_OPEN_SUFFIXES,
        )

    def test_open_target_executor_passes_only_the_resolved_value_to_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory).resolve() / "notes.txt"
            target.write_text("notes", encoding="utf-8")
            with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
                mowik.os,
                "startfile",
                create=True,
            ) as startfile:
                mowik.open_custom_command_target(str(target))

        startfile.assert_called_once_with(str(target))


class CustomCommandDeliverySafetyTests(unittest.TestCase):
    @staticmethod
    def paste_config() -> dict:
        return {
            "ui_language": "en",
            "paste": {
                "enabled": True,
                "copy_to_clipboard": True,
                "append_space": False,
                "delay_ms": 25,
            },
        }

    def test_confirmation_preview_exposes_exact_boundaries_and_line_endings(
        self,
    ) -> None:
        preview = mowik.format_custom_command_confirmation_preview(
            "paste_text",
            "\r\nsafe\n",
            mowik.Translator("en"),
        )

        self.assertIn("⟦␍␊\nsafe␊\n⟧", preview)
        self.assertIn("␍ means CR", preview)
        self.assertIn("␊ means LF/Enter", preview)

    def test_command_context_requires_finite_fresh_time_and_positive_target(
        self,
    ) -> None:
        valid = mowik.command_engine.ExecutionContext(101, 202, None, 100.0, False)
        self.assertIsNone(
            mowik.custom_command_context_denial(
                valid,
                now=220.0,
                require_foreground=True,
            )
        )
        self.assertEqual(
            mowik.custom_command_context_denial(
                valid,
                now=220.001,
                require_foreground=True,
            ),
            "stale_command_context",
        )
        self.assertEqual(
            mowik.custom_command_context_denial(
                valid,
                now=99.0,
                require_foreground=True,
            ),
            "stale_command_context",
        )

        for captured_at in (float("nan"), float("inf"), -1.0, 0.0, True, "1"):
            with self.subTest(captured_at=repr(captured_at)):
                context = mowik.command_engine.ExecutionContext(
                    101,
                    202,
                    None,
                    captured_at,
                    False,
                )
                self.assertEqual(
                    mowik.custom_command_context_denial(
                        context,
                        now=100.0,
                        require_foreground=True,
                    ),
                    "invalid_command_context",
                )

        for hwnd, pid in ((0, 202), (-1, 202), (None, 202), (True, 202), (101, 0)):
            with self.subTest(hwnd=hwnd, pid=pid):
                context = mowik.command_engine.ExecutionContext(
                    hwnd,
                    pid,
                    None,
                    100.0,
                    False,
                )
                self.assertEqual(
                    mowik.custom_command_context_denial(
                        context,
                        now=101.0,
                        require_foreground=True,
                    ),
                    "command_target_unavailable",
                )

    def test_clipboard_is_written_late_and_substitution_aborts_ctrl_v(self) -> None:
        events: list[str] = []

        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            side_effect=(True, True, True, True),
        ), mock.patch.object(
            mowik.time,
            "sleep",
            side_effect=lambda delay: events.append("sleep"),
        ), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
            side_effect=lambda *args: events.append("set"),
        ), mock.patch.object(
            mowik,
            "windows_get_clipboard_text",
            side_effect=lambda *args: events.append("get") or "substituted",
        ), mock.patch.object(mowik.keyboard, "Controller") as controller:
            with self.assertRaises(mowik.AppError):
                mowik.paste_text(
                    "expected",
                    self.paste_config(),
                    append_space_override=False,
                    expected_foreground=(101, 202),
                    verify_clipboard_before_paste=True,
                )

        self.assertEqual(events, ["sleep", "set", "get"])
        controller.assert_not_called()

    def test_focus_change_after_clipboard_readback_aborts_ctrl_v(self) -> None:
        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            side_effect=(True, True, True, False),
        ), mock.patch.object(mowik.time, "sleep"), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ), mock.patch.object(
            mowik,
            "windows_get_clipboard_text",
            return_value="expected",
        ), mock.patch.object(mowik.keyboard, "Controller") as controller:
            with self.assertRaises(mowik.AppError):
                mowik.paste_text(
                    "expected",
                    self.paste_config(),
                    append_space_override=False,
                    expected_foreground=(101, 202),
                    verify_clipboard_before_paste=True,
                )

        controller.assert_not_called()

    def test_command_paste_without_clipboard_fails_closed(self) -> None:
        config = self.paste_config()
        config["paste"]["copy_to_clipboard"] = False

        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            side_effect=AssertionError("focus must not be queried"),
        ), mock.patch.object(
            mowik,
            "windows_type_unicode_text",
        ) as type_text:
            with self.assertRaises(mowik.AppError):
                mowik.paste_text(
                    "expected",
                    config,
                    append_space_override=False,
                    expected_foreground=(101, 202),
                    verify_clipboard_before_paste=True,
                )

        type_text.assert_not_called()

    def test_shutdown_during_paste_delay_aborts_before_delivery(self) -> None:
        cancelled = threading.Event()

        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=True,
        ), mock.patch.object(
            mowik.time,
            "sleep",
            side_effect=lambda delay: cancelled.set(),
        ), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(mowik.keyboard, "Controller") as controller:
            with self.assertRaises(mowik.OperationCancelled):
                mowik.paste_text(
                    "expected",
                    self.paste_config(),
                    append_space_override=False,
                    expected_foreground=(101, 202),
                    verify_clipboard_before_paste=True,
                    cancel_event=cancelled,
                )

        copy_text.assert_not_called()
        controller.assert_not_called()

    def test_focus_change_after_delay_aborts_before_clipboard_or_keyboard(self) -> None:
        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            side_effect=(True, False),
        ), mock.patch.object(mowik.time, "sleep"), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(
            mowik,
            "windows_type_unicode_text",
        ) as type_text, mock.patch.object(mowik.keyboard, "Controller") as controller:
            with self.assertRaises(mowik.AppError):
                mowik.paste_text(
                    "expected",
                    self.paste_config(),
                    append_space_override=False,
                    expected_foreground=(101, 202),
                    verify_clipboard_before_paste=True,
                )

        copy_text.assert_not_called()
        type_text.assert_not_called()
        controller.assert_not_called()


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

    def test_command_mode_uses_distinct_indicator_colors(self) -> None:
        dictation = mowik.render_status_indicator_frame("recording", 3)
        command = mowik.render_status_indicator_frame("command_recording", 3)
        command_processing = mowik.render_status_indicator_frame(
            "command_processing", 4
        )
        command_success = mowik.render_status_indicator_frame("command_success")
        dictation_processing = mowik.render_status_indicator_frame("processing", 4)
        dictation_success = mowik.render_status_indicator_frame("success")

        self.assertNotEqual(dictation.tobytes(), command.tobytes())
        self.assertNotEqual(
            dictation_processing.tobytes(), command_processing.tobytes()
        )
        self.assertNotEqual(dictation_success.tobytes(), command_success.tobytes())

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
        app.recorder.mark_release.assert_called_once_with()
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

        paste.assert_called_once_with(
            "Hello",
            app.config,
            cancel_event=app.stop_event,
        )
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

    def test_minimum_recording_duration_excludes_pre_roll(self) -> None:
        app = self.make_app()
        app.busy = True
        app.config["post_roll_ms"] = 0
        app.config["minimum_recording_ms"] = 250
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 300, "microphone": None}
        )
        pre_roll = np.ones((4_800, 1), dtype=np.float32)
        recorder._callback(pre_roll, len(pre_roll), None, 0)
        recorder.begin()
        pressed_audio = np.ones((1_024, 1), dtype=np.float32)
        recorder._callback(pressed_audio, len(pressed_audio), None, 0)
        app.recorder = recorder

        with mock.patch.object(app, "beep"):
            app._finish_dictation_after_tail()

        self.assertEqual(recorder.last_recording_samples, 1_024)
        self.assertGreater(4_800 + 1_024, 4_000)
        app.dictation_indicator.error.assert_called_once_with()
        self.assertTrue(app.jobs.empty())
        self.assertFalse(app.busy)

    def test_minimum_recording_duration_excludes_post_roll(self) -> None:
        app = self.make_app()
        app.busy = True
        app.config["post_roll_ms"] = 0
        app.config["minimum_recording_ms"] = 250
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        recorder.begin()
        recorder.mark_release()
        post_roll = np.ones((4_000, 1), dtype=np.float32)
        recorder._callback(post_roll, len(post_roll), None, 0)
        app.recorder = recorder

        with mock.patch.object(app, "beep"):
            app._finish_dictation_after_tail()

        self.assertEqual(recorder.last_recording_samples, 0)
        app.dictation_indicator.error.assert_called_once_with()
        self.assertTrue(app.jobs.empty())
        self.assertFalse(app.busy)

    def test_shutdown_closes_indicator_once(self) -> None:
        app = self.make_app()

        app.shutdown()
        app.shutdown()

        app.dictation_indicator.close.assert_called_once_with()


class CustomCommandFlowTests(unittest.TestCase):
    @staticmethod
    def fresh_context(
        explorer_path: str | None = None,
    ) -> mowik.command_engine.ExecutionContext:
        return mowik.command_engine.ExecutionContext(
            foreground_hwnd=101,
            foreground_pid=202,
            explorer_path=explorer_path,
            captured_at=time.monotonic(),
            process_elevated=False,
        )

    def make_app(
        self,
        *,
        action: str = "paste_text",
        value: str = "Hello\nworld",
        confirm: bool = False,
        command_trigger: str = "keyboard:f7",
        match: str = "exact",
        options: dict | None = None,
    ) -> mowik.MowikApp:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        item = {
            "phrase": "moja komenda",
            "action": action,
            "value": value,
            "confirm": confirm,
            "match": match,
        }
        if options is not None:
            item["options"] = options
        config["custom_commands"] = {
            "schema_version": 1,
            "enabled": True,
            "trigger": command_trigger,
            "items": [item],
        }
        app = mowik.MowikApp(config)
        # GitHub-hosted Windows runners execute under an elevated token.  Keep
        # these command-flow tests independent of the host account; dedicated
        # elevation tests override this value explicitly.
        app.process_elevated = False
        app.dictation_indicator = mock.Mock()
        return app

    def test_f7_and_f8_route_to_separate_modes_without_cross_release(self) -> None:
        app = self.make_app()
        app.begin_dictation = mock.Mock()
        app.end_dictation = mock.Mock()
        app._begin_command_context_capture = mock.Mock()

        app._handle_input_event("keyboard", "f7", True)
        app._handle_input_event("keyboard", "f7", True)  # key autorepeat
        app._handle_input_event("keyboard", "f8", False)

        app.begin_dictation.assert_called_once_with("custom_command")
        app._begin_command_context_capture.assert_called_once_with()
        app.end_dictation.assert_not_called()

        app._handle_input_event("keyboard", "f7", False)
        app.end_dictation.assert_called_once_with()

        app._handle_input_event("keyboard", "f8", True)
        app._handle_input_event("keyboard", "f8", False)
        self.assertEqual(
            app.begin_dictation.call_args_list,
            [mock.call("custom_command"), mock.call("dictation")],
        )

    def test_command_capture_uses_violet_indicator_states(self) -> None:
        app = self.make_app()
        app.model_ready.set()
        app.recorder = mock.Mock()

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik.threading, "Thread"
        ) as thread_class:
            app.begin_dictation("custom_command")
            app.end_dictation()

        app.dictation_indicator.recording.assert_called_once_with(command=True)
        app.dictation_indicator.processing.assert_called_once_with(command=True)
        thread_class.return_value.start.assert_called_once_with()

    def test_conflicting_manual_shortcut_disables_only_command_mode(self) -> None:
        with mock.patch.object(mowik.logging, "error") as log_error:
            app = self.make_app(command_trigger="keyboard:f8")

        self.assertFalse(app._command_mode_enabled())
        self.assertEqual(app._mode_for_input(("keyboard", "f8")), "dictation")
        log_error.assert_called_once()

    def test_only_literal_true_enables_custom_command_mode(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        config["custom_commands"] = {
            "schema_version": 1,
            "enabled": "false",
            "trigger": "keyboard:f7",
            "items": [
                {
                    "phrase": "moja komenda",
                    "action": "paste_text",
                    "value": "safe",
                }
            ],
        }

        app = mowik.MowikApp(config)

        self.assertFalse(app._command_mode_enabled())

    def test_foreign_schema_never_enables_custom_command_mode(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        config["custom_commands"] = {
            "schema_version": 2,
            "enabled": True,
            "trigger": "keyboard:f7",
            "items": [
                {
                    "phrase": "moja komenda",
                    "action": "paste_text",
                    "value": "safe",
                }
            ],
        }

        app = mowik.MowikApp(config)

        self.assertFalse(app._custom_command_registry.definitions)
        self.assertFalse(app._command_mode_enabled())

    def test_exact_command_pastes_literal_payload_without_extra_space(self) -> None:
        app = self.make_app(value="Hello world")
        app.busy = True
        app.transcribe = mock.Mock(return_value="Moja komenda.")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                "custom_command",
                execution_context=self.fresh_context(),
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "paste_text"
        ) as paste, mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=True,
        ):
            app._job_worker()

        paste.assert_called_once_with(
            "Hello world",
            app.config,
            append_space_override=False,
            expected_foreground=(101, 202),
            verify_clipboard_before_paste=True,
            cancel_event=app.stop_event,
        )
        app.dictation_indicator.success.assert_called_once_with(command=True)
        app.dictation_indicator.error.assert_not_called()
        self.assertFalse(app.busy)

    def test_no_match_never_falls_back_to_dictation(self) -> None:
        app = self.make_app()
        app.busy = True
        app.transcribe = mock.Mock(return_value="inna wypowiedź")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                "custom_command",
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "paste_text"
        ) as paste:
            app._job_worker()

        paste.assert_not_called()
        app.dictation_indicator.error.assert_called_once_with()
        app.dictation_indicator.success.assert_not_called()
        self.assertFalse(app.busy)

    def test_open_action_requires_configured_confirmation(self) -> None:
        app = self.make_app(
            action="open",
            value=r"C:\Windows\System32\notepad.exe",
            confirm=True,
        )
        app.busy = True
        app.transcribe = mock.Mock(return_value="moja komenda")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                "custom_command",
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "confirm_custom_command_action", return_value=True
        ) as confirm, mock.patch.object(mowik, "open_custom_command_target") as opened:
            app._job_worker()

        confirm.assert_called_once_with(
            "open",
            r"C:\Windows\System32\notepad.exe",
            app.translator,
        )
        opened.assert_called_once_with(r"C:\Windows\System32\notepad.exe")
        app.dictation_indicator.success.assert_called_once_with(command=True)

    def test_cancelled_open_action_is_not_started(self) -> None:
        app = self.make_app(
            action="open",
            value=r"C:\Windows\System32\notepad.exe",
            confirm=True,
        )
        app.busy = True
        app.transcribe = mock.Mock(return_value="moja komenda")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                "custom_command",
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "confirm_custom_command_action", return_value=False
        ), mock.patch.object(mowik, "open_custom_command_target") as opened:
            app._job_worker()

        opened.assert_not_called()
        app.dictation_indicator.hide.assert_called_once_with()
        app.dictation_indicator.error.assert_not_called()
        app.dictation_indicator.success.assert_not_called()
        self.assertFalse(app.busy)

    def test_shutdown_during_confirmation_prevents_open_action(self) -> None:
        app = self.make_app(
            action="open",
            value=r"C:\Windows\System32\notepad.exe",
            confirm=True,
        )

        def confirm_then_stop(*args, **kwargs):
            app.stop_event.set()
            return True

        with mock.patch.object(
            mowik,
            "confirm_custom_command_action",
            side_effect=confirm_then_stop,
        ), mock.patch.object(mowik, "open_custom_command_target") as opened:
            result = app._deliver_custom_command("moja komenda")

        self.assertFalse(result)
        opened.assert_not_called()

    def test_shutdown_during_recognition_prevents_delayed_action(self) -> None:
        app = self.make_app(
            action="open",
            value=r"C:\Windows\System32\notepad.exe",
            confirm=True,
        )
        app.busy = True

        def stop_then_return(*args, **kwargs):
            app.stop_event.set()
            return "moja komenda"

        app.transcribe = mock.Mock(side_effect=stop_then_return)
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                "custom_command",
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "confirm_custom_command_action"
        ) as confirm, mock.patch.object(mowik, "open_custom_command_target") as opened:
            app._job_worker()

        confirm.assert_not_called()
        opened.assert_not_called()
        app.dictation_indicator.success.assert_not_called()
        self.assertFalse(app.busy)

    def test_legacy_shell_command_is_never_registered_or_executed(self) -> None:
        app = self.make_app(action="run_command", value="whoami", confirm=True)

        self.assertFalse(app._custom_command_registry.definitions)
        self.assertFalse(hasattr(mowik, "run_custom_command_line"))
        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik, "open_custom_command_target"
        ) as opened:
            result = app._deliver_custom_command("moja komenda")

        self.assertFalse(result)
        opened.assert_not_called()
        app.dictation_indicator.error.assert_called_once_with()

    def test_multiline_paste_requires_confirmation_and_is_only_copied(self) -> None:
        app = self.make_app(action="paste_text", value="first\nsecond", confirm=False)
        context = self.fresh_context()

        with mock.patch.object(
            mowik, "confirm_custom_command_action", return_value=True
        ) as confirm, mock.patch.object(mowik, "paste_text") as paste, mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=True,
        ):
            result = app._deliver_custom_command("moja komenda", context)

        self.assertTrue(result)
        confirm.assert_called_once_with(
            "paste_text",
            "first\nsecond",
            app.translator,
        )
        copy_text.assert_called_once_with(
            "first\nsecond",
            app.translator,
        )
        paste.assert_not_called()

    def test_multiline_paste_fails_closed_when_clipboard_copy_is_disabled(
        self,
    ) -> None:
        app = self.make_app(action="paste_text", value="first\nsecond")
        app.config["paste"]["copy_to_clipboard"] = False

        with mock.patch.object(
            mowik,
            "confirm_custom_command_action",
            return_value=True,
        ), mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=True,
        ), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(mowik, "paste_text") as paste, mock.patch.object(
            app,
            "beep",
        ):
            result = app._deliver_custom_command(
                "moja komenda",
                self.fresh_context(),
            )

        self.assertFalse(result)
        copy_text.assert_not_called()
        paste.assert_not_called()
        app.dictation_indicator.error.assert_called_once_with()

    def test_focus_change_during_multiline_confirmation_aborts_before_copy(
        self,
    ) -> None:
        app = self.make_app(action="paste_text", value="first\nsecond")

        with mock.patch.object(
            mowik,
            "confirm_custom_command_action",
            return_value=True,
        ), mock.patch.object(
            mowik,
            "foreground_identity_matches",
            side_effect=(True, False),
        ), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(app, "beep"):
            result = app._deliver_custom_command(
                "moja komenda",
                self.fresh_context(),
            )

        self.assertFalse(result)
        copy_text.assert_not_called()

    def test_stale_single_line_context_is_denied_before_focus_or_paste(self) -> None:
        app = self.make_app(action="paste_text", value="safe")
        stale = mowik.command_engine.ExecutionContext(
            101,
            202,
            None,
            time.monotonic() - mowik.MAX_CUSTOM_COMMAND_CONTEXT_AGE_SECONDS - 1,
            False,
        )

        with mock.patch.object(mowik, "paste_text") as paste, mock.patch.object(
            mowik,
            "foreground_identity_matches",
            side_effect=AssertionError("stale context must be rejected first"),
        ), mock.patch.object(app, "beep"):
            result = app._deliver_custom_command("moja komenda", stale)

        self.assertFalse(result)
        paste.assert_not_called()

    def test_terminal_tail_opens_captured_folder_and_only_updates_clipboard(self) -> None:
        app = self.make_app(
            action="open_terminal",
            value="",
            match="prefix_tail",
            options={
                "cwd_source": "active_explorer",
                "host": "auto",
                "shell": "default",
                "draft_delivery": "clipboard",
            },
        )
        context = self.fresh_context(r"C:\Work\Mowik")
        directory = mowik.windows_actions.WorkingDirectoryResult(
            "active_explorer",
            Path(r"C:\Work\Mowik"),
        )
        handle = mowik.windows_actions.TerminalHandle(
            "windows_terminal",
            "default",
            Path(r"C:\Work\Mowik"),
            303,
            43.0,
        )
        launched = mowik.windows_actions.TerminalLaunchResult("launched", handle)
        copied = mowik.windows_actions.DraftDeliveryResult(
            "copied_only",
            clipboard_updated=True,
            reason="clipboard_mode",
        )

        with mock.patch.object(
            mowik.windows_actions,
            "resolve_working_directory",
            return_value=directory,
        ) as resolve, mock.patch.object(
            mowik.windows_actions,
            "launch_terminal",
            return_value=launched,
        ) as launch, mock.patch.object(
            mowik.windows_actions,
            "deliver_terminal_draft",
            return_value=copied,
        ) as deliver, mock.patch.object(
            mowik,
            "paste_text",
        ) as paste, mock.patch.object(
            mowik,
            "windows_type_unicode_text",
        ) as type_text:
            result = app._deliver_custom_command(
                "moja komenda git status",
                context,
            )

        self.assertTrue(result)
        resolve.assert_called_once()
        launch.assert_called_once_with(
            "auto",
            "default",
            Path(r"C:\Work\Mowik"),
        )
        deliver.assert_called_once_with(
            handle,
            "git status",
        )
        paste.assert_not_called()
        type_text.assert_not_called()

    def test_terminal_without_draft_never_invokes_clipboard_delivery(self) -> None:
        app = self.make_app(
            action="open_terminal",
            value="",
            match="exact",
            options={"cwd_source": "home"},
        )
        directory = mowik.windows_actions.WorkingDirectoryResult(
            "home",
            Path(r"C:\Users\User"),
        )
        handle = mowik.windows_actions.TerminalHandle(
            "console",
            "cmd",
            directory.path,
            303,
            43.0,
        )
        launched = mowik.windows_actions.TerminalLaunchResult("launched", handle)

        with mock.patch.object(
            mowik.windows_actions,
            "resolve_working_directory",
            return_value=directory,
        ), mock.patch.object(
            mowik.windows_actions,
            "launch_terminal",
            return_value=launched,
        ), mock.patch.object(
            mowik.windows_actions,
            "deliver_terminal_draft",
        ) as deliver, mock.patch.object(mowik, "paste_text") as paste:
            result = app._deliver_custom_command("moja komenda")

        self.assertTrue(result)
        deliver.assert_not_called()
        paste.assert_not_called()

    def test_shutdown_after_terminal_launch_prevents_draft_delivery(self) -> None:
        app = self.make_app(
            action="open_terminal",
            value="",
            match="prefix_tail",
            options={"cwd_source": "home"},
        )
        directory = mowik.windows_actions.WorkingDirectoryResult(
            "home",
            Path(r"C:\Users\User"),
        )
        handle = mowik.windows_actions.TerminalHandle(
            "console",
            "cmd",
            directory.path,
            303,
            43.0,
        )
        launched = mowik.windows_actions.TerminalLaunchResult("launched", handle)

        def launch_then_stop(*args, **kwargs):
            app.stop_event.set()
            return launched

        with mock.patch.object(
            mowik.windows_actions,
            "resolve_working_directory",
            return_value=directory,
        ), mock.patch.object(
            mowik.windows_actions,
            "launch_terminal",
            side_effect=launch_then_stop,
        ), mock.patch.object(
            mowik.windows_actions,
            "deliver_terminal_draft",
        ) as deliver:
            result = app._deliver_custom_command("moja komenda git status")

        self.assertFalse(result)
        deliver.assert_not_called()

    def test_terminal_here_fails_closed_without_captured_explorer_folder(self) -> None:
        app = self.make_app(
            action="open_terminal",
            value="",
            options={"cwd_source": "active_explorer"},
        )
        context = self.fresh_context()

        with mock.patch.object(mowik.windows_actions, "launch_terminal") as launch:
            result = app._deliver_custom_command("moja komenda", context)

        self.assertFalse(result)
        launch.assert_not_called()

    def test_open_and_terminal_actions_fail_closed_when_process_is_elevated(self) -> None:
        elevated = mowik.command_engine.ExecutionContext(
            101,
            202,
            r"C:\Work\Mowik",
            time.monotonic(),
            True,
        )
        for action, value, options in (
            ("open", r"C:\Windows\System32\notepad.exe", None),
            ("open_terminal", "", {"cwd_source": "home"}),
        ):
            with self.subTest(action=action):
                app = self.make_app(action=action, value=value, options=options)
                with mock.patch.object(
                    mowik, "open_custom_command_target"
                ) as opened, mock.patch.object(
                    mowik.windows_actions, "launch_terminal"
                ) as terminal:
                    result = app._deliver_custom_command("moja komenda", elevated)
                self.assertFalse(result)
                opened.assert_not_called()
                terminal.assert_not_called()

    def test_current_elevation_cannot_be_downgraded_by_captured_context(self) -> None:
        stale_non_elevated_context = mowik.command_engine.ExecutionContext(
            101,
            202,
            r"C:\Work\Mowik",
            time.monotonic(),
            False,
        )
        for action, value, options in (
            ("open", r"C:\Windows\System32\notepad.exe", None),
            ("open_terminal", "", {"cwd_source": "home"}),
        ):
            with self.subTest(action=action):
                app = self.make_app(action=action, value=value, options=options)
                app.process_elevated = True
                with mock.patch.object(
                    mowik, "open_custom_command_target"
                ) as opened, mock.patch.object(
                    mowik.windows_actions, "launch_terminal"
                ) as terminal:
                    result = app._deliver_custom_command(
                        "moja komenda",
                        stale_non_elevated_context,
                    )
                self.assertFalse(result)
                opened.assert_not_called()
                terminal.assert_not_called()

    def test_command_transcription_skips_voice_replacements_and_ollama(self) -> None:
        app = self.make_app()
        app.model = mock.Mock()
        segment = mock.Mock(text=" new paragraph. ")
        info = mock.Mock(language="en", language_probability=1.0)
        app.model.transcribe.return_value = ([segment], info)
        app.recorder = mock.Mock(sample_rate=mowik.SAMPLE_RATE)
        audio = np.tile(np.array([-0.2, 0.2], dtype=np.float32), 800)

        with mock.patch.object(mowik, "load_dictionary", return_value=[]), mock.patch.object(
            mowik, "apply_voice_commands"
        ) as voice_commands, mock.patch.object(
            mowik, "cleanup_with_ollama"
        ) as cleanup:
            result = app.transcribe(audio, mode="custom_command")

        self.assertEqual(result, "new paragraph.")
        voice_commands.assert_not_called()
        cleanup.assert_not_called()



class SettingsLifecycleTests(unittest.TestCase):
    def settings_args(self):
        return mowik.argparse.Namespace(
            create_config=False,
            settings=True,
            list_devices=False,
            download_model=False,
            test_ollama=False,
            console_log=False,
            restart_delay=0.0,
        )

    def test_restart_request_is_versioned_and_stale_requests_are_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "restart.request"
            with mock.patch.object(
                mowik, "RESTART_REQUEST_PATH", request_path
            ), mock.patch.object(mowik, "ensure_directories"), mock.patch.object(
                mowik.time, "time_ns", return_value=2_000
            ):
                self.assertEqual(mowik.request_app_restart(), 2_000)
                self.assertEqual(request_path.read_text(encoding="ascii"), "v1:2000\n")
                self.assertEqual(mowik.take_fresh_restart_request(1_500), "v1:2000")
                self.assertFalse(request_path.exists())

                request_path.write_text("v1:1499\n", encoding="ascii")
                self.assertIsNone(mowik.take_fresh_restart_request(1_500))
                self.assertFalse(request_path.exists())

                request_path.write_text("0.000001600\n", encoding="ascii")
                self.assertEqual(
                    mowik.take_fresh_restart_request(1_500),
                    "0.000001600",
                )

    def test_running_app_gets_request_but_standalone_settings_launches_cleanly(self) -> None:
        with mock.patch.object(
            mowik, "is_app_instance_running", return_value=True
        ), mock.patch.object(mowik, "request_app_restart") as request, mock.patch.object(
            mowik, "discard_pending_restart_request"
        ) as discard, mock.patch.object(mowik.subprocess, "Popen") as popen:
            result = mowik.restart_or_launch_app_after_settings()

        self.assertEqual(result, "restart_requested")
        request.assert_called_once_with()
        discard.assert_not_called()
        popen.assert_not_called()

        with mock.patch.object(
            mowik, "is_app_instance_running", return_value=False
        ), mock.patch.object(mowik, "request_app_restart") as request, mock.patch.object(
            mowik, "discard_pending_restart_request"
        ) as discard, mock.patch.object(
            mowik, "application_process_args", return_value=["python", "mowik.py"]
        ), mock.patch.object(mowik.subprocess, "Popen") as popen:
            result = mowik.restart_or_launch_app_after_settings()

        self.assertEqual(result, "app_started")
        request.assert_not_called()
        discard.assert_called_once_with()
        popen.assert_called_once_with(["python", "mowik.py"], cwd=str(mowik.APP_ROOT))

    def test_application_and_settings_args_preserve_source_and_frozen_modes(self) -> None:
        with mock.patch.object(mowik.sys, "frozen", False, create=True), mock.patch.object(
            mowik.sys, "executable", r"C:\Python\pythonw.exe"
        ):
            source = [
                r"C:\Python\pythonw.exe",
                str(Path(mowik.__file__).resolve()),
            ]
            self.assertEqual(mowik.application_process_args(), source)
            self.assertEqual(mowik.settings_process_args(), [*source, "--settings"])

        with mock.patch.object(mowik.sys, "frozen", True, create=True), mock.patch.object(
            mowik.sys, "executable", r"C:\Program Files\Mowik\Mowik.exe"
        ):
            frozen = [r"C:\Program Files\Mowik\Mowik.exe"]
            self.assertEqual(mowik.application_process_args(), frozen)
            self.assertEqual(mowik.settings_process_args(), [*frozen, "--settings"])

    def test_windows_app_mutex_probe_closes_opened_handle(self) -> None:
        kernel32 = mock.Mock()
        kernel32.OpenMutexW.return_value = 321
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik.ctypes, "WinDLL", return_value=kernel32, create=True
        ), mock.patch.object(mowik.ctypes, "set_last_error", create=True):
            self.assertTrue(mowik.is_app_instance_running())

        kernel32.CloseHandle.assert_called_once()

        missing_kernel32 = mock.Mock()
        missing_kernel32.OpenMutexW.return_value = 0
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik.ctypes, "WinDLL", return_value=missing_kernel32, create=True
        ), mock.patch.object(
            mowik.ctypes, "set_last_error", create=True
        ), mock.patch.object(
            mowik.ctypes, "get_last_error", return_value=2, create=True
        ):
            self.assertFalse(mowik.is_app_instance_running())

    def test_duplicate_settings_mutex_releases_handle_even_if_message_fails(self) -> None:
        with mock.patch.object(
            mowik,
            "_create_windows_named_mutex",
            return_value=(987, True),
        ), mock.patch.object(
            mowik,
            "show_settings_already_open",
            side_effect=RuntimeError("message failed"),
        ), mock.patch.object(mowik, "release_single_instance") as release:
            with self.assertRaisesRegex(RuntimeError, "message failed"):
                mowik.acquire_settings_instance(mowik.Translator("en"))

        release.assert_called_once_with(987)
        polish, polish_title = mowik.settings_already_open_message(
            mowik.Translator("pl")
        )
        english, english_title = mowik.settings_already_open_message(
            mowik.Translator("en")
        )
        self.assertIn("już otwarte", polish)
        self.assertIn("ustawienia", polish_title.lower())
        self.assertIn("already open", english)
        self.assertIn("settings", english_title.lower())

    def test_settings_main_sets_dpi_and_always_releases_mutex(self) -> None:
        events: list[str] = []

        def event(name: str, result=None):
            def callback(*args, **kwargs):
                events.append(name)
                if isinstance(result, BaseException):
                    raise result
                return result

            return callback

        with mock.patch.object(mowik, "parse_args", return_value=self.settings_args()), mock.patch.object(
            mowik, "setup_logging"
        ), mock.patch.object(mowik, "create_default_files"), mock.patch.object(
            mowik, "load_config", return_value=copy.deepcopy(mowik.DEFAULT_CONFIG)
        ), mock.patch.object(
            mowik.os, "name", "nt"
        ), mock.patch.object(
            mowik, "enable_windows_dpi_awareness", side_effect=event("dpi")
        ), mock.patch.object(
            mowik, "acquire_settings_instance", side_effect=event("acquire", 654)
        ), mock.patch.object(
            mowik,
            "run_settings_window",
            side_effect=event("run", RuntimeError("settings failed")),
        ), mock.patch.object(
            mowik, "release_single_instance", side_effect=event("release")
        ):
            with self.assertRaisesRegex(RuntimeError, "settings failed"):
                mowik.main()

        self.assertEqual(events, ["dpi", "acquire", "run", "release"])


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


class MicrophoneIntegrationTests(unittest.TestCase):
    @staticmethod
    def device(
        name: str,
        hostapi: int,
        *,
        inputs: int = 1,
        outputs: int = 0,
        sample_rate: float = 48_000.0,
    ) -> dict[str, object]:
        return {
            "name": name,
            "hostapi": hostapi,
            "max_input_channels": inputs,
            "max_output_channels": outputs,
            "default_samplerate": sample_rate,
        }

    def setUp(self) -> None:
        self.host_apis = [{"name": "MME"}, {"name": "Windows WASAPI"}]
        self.devices = [
            self.device("Speakers", 0, inputs=0, outputs=2, sample_rate=44_100),
            self.device("Secret Studio Microphone", 1, inputs=2, outputs=0),
        ]
        self.selector = mowik.audio_devices.build_microphone_selector(
            1,
            self.devices,
            self.host_apis,
        )

    def recorder_config(self, microphone) -> dict[str, object]:
        return {
            "microphone": copy.deepcopy(microphone),
            "pre_roll_ms": 300,
            "ui_language": "en",
        }

    def test_runtime_resolves_saved_selector_after_device_and_host_api_reorder(
        self,
    ) -> None:
        current_host_apis = [
            {"name": "Windows WASAPI"},
            {"name": "MME"},
        ]
        current_devices = [
            self.device("Secret Studio Microphone", 0, inputs=2, outputs=0),
            self.device("Speakers", 1, inputs=0, outputs=2, sample_rate=44_100),
        ]
        stream = mock.Mock()

        with mock.patch.object(
            mowik.sd,
            "query_devices",
            return_value=current_devices,
        ) as query_devices, mock.patch.object(
            mowik.sd,
            "query_hostapis",
            return_value=current_host_apis,
        ) as query_hostapis, mock.patch.object(
            mowik.sd,
            "InputStream",
            return_value=stream,
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(
                self.recorder_config(self.selector)
            )
            recorder.start()

        query_devices.assert_called_once_with()
        query_hostapis.assert_called_once_with()
        self.assertEqual(input_stream.call_args.kwargs["device"], 0)
        stream.start.assert_called_once_with()

    def test_runtime_reresolves_selector_before_retry_after_hotplug(self) -> None:
        reordered_devices = [
            copy.deepcopy(self.devices[1]),
            copy.deepcopy(self.devices[0]),
        ]
        first_stream = mock.Mock()
        first_stream.start.side_effect = RuntimeError("first attempt failed")
        second_stream = mock.Mock()

        with mock.patch.object(
            mowik.sd,
            "query_devices",
            side_effect=[self.devices, reordered_devices],
        ) as query_devices, mock.patch.object(
            mowik.sd,
            "query_hostapis",
            return_value=self.host_apis,
        ), mock.patch.object(
            mowik.sd,
            "InputStream",
            side_effect=[first_stream, second_stream],
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(
                self.recorder_config(self.selector)
            )
            recorder.start()

        self.assertEqual(query_devices.call_count, 2)
        self.assertEqual(
            [call.kwargs["device"] for call in input_stream.call_args_list],
            [1, 0],
        )
        first_stream.close.assert_called_once_with()
        second_stream.start.assert_called_once_with()

    def test_missing_ambiguous_and_malformed_selectors_never_open_a_stream(
        self,
    ) -> None:
        cases = {
            "missing": (
                self.selector,
                [self.device("Another Microphone", 1, inputs=2)],
            ),
            "ambiguous": (
                self.selector,
                [copy.deepcopy(self.devices[1]), copy.deepcopy(self.devices[1])],
            ),
            "malformed": (
                {"schema_version": 1, "name": "Secret Studio Microphone"},
                self.devices,
            ),
        }
        for name, (configured, current_devices) in cases.items():
            with self.subTest(name=name), mock.patch.object(
                mowik.sd,
                "query_devices",
                return_value=current_devices,
            ), mock.patch.object(
                mowik.sd,
                "query_hostapis",
                return_value=self.host_apis,
            ), mock.patch.object(mowik.sd, "InputStream") as input_stream:
                recorder = mowik.ContinuousRecorder(
                    self.recorder_config(configured)
                )
                with self.assertRaises(mowik.AppError) as raised:
                    recorder.start()

                input_stream.assert_not_called()
                self.assertNotIn("Secret Studio Microphone", str(raised.exception))

    def test_invalid_legacy_indices_never_open_a_stream(self) -> None:
        for legacy_index in (0, 99):
            with self.subTest(index=legacy_index), mock.patch.object(
                mowik.sd,
                "query_devices",
                return_value=self.devices,
            ), mock.patch.object(
                mowik.sd,
                "query_hostapis",
                return_value=self.host_apis,
            ), mock.patch.object(mowik.sd, "InputStream") as input_stream:
                recorder = mowik.ContinuousRecorder(
                    self.recorder_config(legacy_index)
                )
                with self.assertRaises(mowik.AppError):
                    recorder.start()

                input_stream.assert_not_called()

    def test_driver_enumeration_error_is_sanitized_before_stream_open(self) -> None:
        with mock.patch.object(
            mowik.sd,
            "query_devices",
            side_effect=RuntimeError("secret driver and device details"),
        ), mock.patch.object(mowik.sd, "query_hostapis") as query_hostapis, mock.patch.object(
            mowik.sd,
            "InputStream",
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(
                self.recorder_config(self.selector)
            )
            with self.assertRaises(mowik.AppError) as raised:
                recorder.start()

        query_hostapis.assert_not_called()
        input_stream.assert_not_called()
        self.assertNotIn("secret driver", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_settings_migrates_valid_legacy_index_to_schema_one_descriptor(
        self,
    ) -> None:
        translator = mowik.Translator("en")
        state = mowik.build_microphone_choice_state(
            1,
            self.devices,
            self.host_apis,
            translator,
        )

        self.assertIn("Windows WASAPI", state.selected_label)
        saved = mowik.microphone_config_value_for_choice(
            state,
            state.selected_label,
            translator,
        )
        self.assertIsInstance(saved, dict)
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["host_api_name"], "Windows WASAPI")
        self.assertNotIsInstance(saved, int)

    def test_settings_preserves_unresolved_values_and_requires_new_choice(
        self,
    ) -> None:
        translator = mowik.Translator("en")
        future_selector = {**self.selector, "schema_version": 2}

        for configured in (99, future_selector):
            with self.subTest(configured=configured):
                state = mowik.build_microphone_choice_state(
                    configured,
                    self.devices,
                    self.host_apis,
                    translator,
                )

                self.assertIsNotNone(state.unresolved_label)
                self.assertEqual(
                    state.values[state.unresolved_label],
                    configured,
                )
                with self.assertRaises(mowik.AppError):
                    mowik.microphone_config_value_for_choice(
                        state,
                        state.selected_label,
                        translator,
                    )

                default_label = next(
                    label for label, value in state.values.items() if value is None
                )
                self.assertIsNone(
                    mowik.microphone_config_value_for_choice(
                        state,
                        default_label,
                        translator,
                    )
                )

    def test_settings_preserves_selector_if_device_enumeration_is_unavailable(
        self,
    ) -> None:
        translator = mowik.Translator("en")
        state = mowik.build_unavailable_microphone_choice_state(
            self.selector,
            translator,
        )

        self.assertEqual(state.values[state.unresolved_label], self.selector)
        with self.assertRaises(mowik.AppError):
            mowik.microphone_config_value_for_choice(
                state,
                state.selected_label,
                translator,
            )

    def test_settings_never_saves_an_ambiguous_device_fingerprint(self) -> None:
        translator = mowik.Translator("en")
        duplicate_devices = [
            copy.deepcopy(self.devices[1]),
            copy.deepcopy(self.devices[1]),
        ]
        state = mowik.build_microphone_choice_state(
            None,
            duplicate_devices,
            self.host_apis,
            translator,
        )

        self.assertEqual(len(state.blocked_labels), 2)
        for label in state.blocked_labels:
            with self.subTest(label=label), self.assertRaises(mowik.AppError):
                mowik.microphone_config_value_for_choice(
                    state,
                    label,
                    translator,
                )

        legacy_state = mowik.build_microphone_choice_state(
            0,
            duplicate_devices,
            self.host_apis,
            translator,
        )
        self.assertEqual(
            legacy_state.error_code,
            mowik.audio_devices.ERROR_DEVICE_AMBIGUOUS,
        )
        self.assertEqual(
            legacy_state.values[legacy_state.unresolved_label],
            0,
        )


class RecorderTests(unittest.TestCase):
    def test_pre_roll_keeps_exact_number_of_samples(self) -> None:
        recorder = mowik.ContinuousRecorder({"pre_roll_ms": 300, "microphone": None})
        chunk = np.ones((1024, 1), dtype=np.float32)

        for _ in range(10):
            recorder._callback(chunk, len(chunk), None, 0)

        self.assertEqual(recorder.pre_roll_samples, 4_800)
        self.assertEqual(recorder._ring_samples, recorder.pre_roll_samples)
        self.assertEqual(sum(len(part) for part in recorder._ring), 4_800)

    def test_next_recording_uses_tail_of_previous_recording_as_pre_roll(
        self,
    ) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 300, "microphone": None}
        )
        old_chunk = np.ones((1024, 1), dtype=np.float32)
        new_chunk = np.full((1024, 1), 2.0, dtype=np.float32)
        for _ in range(5):
            recorder._callback(old_chunk, len(old_chunk), None, 0)

        recorder.begin()
        for _ in range(5):
            recorder._callback(new_chunk, len(new_chunk), None, 0)
        recorder.finish()

        recorder.begin()
        immediate_next_recording = recorder.finish()

        self.assertEqual(len(immediate_next_recording), 4_800)
        np.testing.assert_array_equal(
            immediate_next_recording,
            np.full(4_800, 2.0, dtype=np.float32),
        )
        self.assertEqual(recorder.last_recording_samples, 0)


if __name__ == "__main__":
    unittest.main()
