from __future__ import annotations

import argparse
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import wave
import weakref

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
    def test_version_probe_exits_before_loading_the_application_runtime(self) -> None:
        with mock.patch("builtins.print") as print_output, self.assertRaises(
            SystemExit
        ) as raised:
            mowik._run_early_read_only_probe(
                executed_as_main=True,
                argv=["--version"],
            )

        self.assertEqual(raised.exception.code, 0)
        print_output.assert_called_once_with(f"Mówik {mowik.APP_VERSION}")

    def test_gui_probe_exits_after_destroying_tk_without_app_runtime(self) -> None:
        root = mock.Mock()
        tkinter_module = mock.Mock()
        tkinter_module.Tk.return_value = root

        with mock.patch.dict(
            sys.modules,
            {"tkinter": tkinter_module},
        ), self.assertRaises(SystemExit) as raised:
            mowik._run_early_read_only_probe(
                executed_as_main=True,
                argv=["--runtime-gui-smoke-test"],
            )

        self.assertEqual(raised.exception.code, 0)
        root.withdraw.assert_called_once_with()
        root.update_idletasks.assert_called_once_with()
        root.destroy.assert_called_once_with()

    def test_elevated_main_is_blocked_before_native_runtime_import_boundary(
        self,
    ) -> None:
        windll = mock.Mock()
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik,
            "_early_windows_process_is_elevated",
            return_value=True,
        ), mock.patch.object(
            mowik.ctypes,
            "windll",
            windll,
            create=True,
        ), self.assertRaises(SystemExit) as raised:
            mowik._reject_elevated_runtime_before_native_imports(
                executed_as_main=True,
                argv=[],
            )

        self.assertEqual(raised.exception.code, 1)
        windll.user32.MessageBoxW.assert_called_once()

    def test_read_only_build_probes_are_exempt_from_early_elevation_block(
        self,
    ) -> None:
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik,
            "_early_windows_process_is_elevated",
        ) as is_elevated:
            for arguments in (["--version"], ["--runtime-gui-smoke-test"]):
                mowik._reject_elevated_runtime_before_native_imports(
                    executed_as_main=True,
                    argv=arguments,
                )

        is_elevated.assert_not_called()

    def test_runtime_gui_smoke_test_creates_and_destroys_tk_root(self) -> None:
        root = mock.Mock()
        tkinter_module = mock.Mock()
        tkinter_module.Tk.return_value = root

        with mock.patch.dict(sys.modules, {"tkinter": tkinter_module}):
            self.assertEqual(mowik.runtime_gui_smoke_test_command(), 0)

        tkinter_module.Tk.assert_called_once_with()
        root.withdraw.assert_called_once_with()
        root.update_idletasks.assert_called_once_with()
        root.destroy.assert_called_once_with()

    def test_runtime_gui_smoke_test_fails_without_showing_ui(self) -> None:
        tkinter_module = mock.Mock()
        tkinter_module.Tk.side_effect = RuntimeError("missing Tcl/Tk")

        with mock.patch.dict(sys.modules, {"tkinter": tkinter_module}), mock.patch(
            "builtins.print"
        ) as output:
            self.assertEqual(mowik.runtime_gui_smoke_test_command(), 1)

        self.assertIn("missing Tcl/Tk", output.call_args.args[0])

    def test_auto_cpu_threads_uses_physical_core_estimate(self) -> None:
        with mock.patch.object(mowik.os, "cpu_count", return_value=32):
            self.assertEqual(mowik.resolve_cpu_threads({"cpu_threads": 0}), 16)
        self.assertEqual(mowik.resolve_cpu_threads({"cpu_threads": 7}), 7)

    def test_model_download_uses_pinned_revision(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["model"] = "tiny"
        sentinel = object()

        with mock.patch.object(mowik, "get_cuda_count", return_value=0), mock.patch.object(
            mowik,
            "load_model_local_first",
            return_value=sentinel,
        ) as load:
            model, model_name, device = mowik.create_model(config)

        self.assertIs(model, sentinel)
        self.assertEqual((model_name, device), ("tiny", "cpu"))
        self.assertEqual(
            load.call_args.args[1]["revision"],
            mowik.MODEL_SOURCES["tiny"][1],
        )

    def test_forced_model_download_refreshes_pinned_snapshot(self) -> None:
        model = object()
        kwargs = {
            "device": "cpu",
            "compute_type": "int8",
            "download_root": str(mowik.MODEL_DIR),
            "revision": mowik.MODEL_SOURCES["small"][1],
        }

        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            for filename in (
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.json",
            ):
                (snapshot / filename).touch()
            with mock.patch.object(
                mowik.huggingface_hub,
                "snapshot_download",
                return_value=str(snapshot),
            ) as download, mock.patch.object(
                mowik,
                "WhisperModel",
                return_value=model,
            ) as whisper:
                result = mowik.load_model_local_first(
                    "small",
                    kwargs,
                    force_download=True,
                )

        self.assertIs(result, model)
        self.assertTrue(download.call_args.kwargs["force_download"])
        self.assertEqual(
            download.call_args.kwargs["revision"],
            mowik.MODEL_SOURCES["small"][1],
        )
        whisper.assert_called_once_with(
            str(snapshot),
            device="cpu",
            compute_type="int8",
        )

    def test_model_initialization_error_does_not_trigger_network_retry(self) -> None:
        kwargs = {
            "device": "cpu",
            "compute_type": "int8",
            "download_root": str(mowik.MODEL_DIR),
            "revision": mowik.MODEL_SOURCES["tiny"][1],
        }
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            for filename in (
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.json",
            ):
                (snapshot / filename).touch()
            with mock.patch.object(
                mowik.huggingface_hub,
                "snapshot_download",
                return_value=str(snapshot),
            ) as download, mock.patch.object(
                mowik,
                "WhisperModel",
                side_effect=RuntimeError("invalid runtime"),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid runtime"):
                    mowik.load_model_local_first("tiny", kwargs)

        download.assert_called_once()
        self.assertTrue(download.call_args.kwargs["local_files_only"])

    def test_missing_model_cache_is_downloaded_once(self) -> None:
        kwargs = {
            "device": "cpu",
            "compute_type": "int8",
            "download_root": str(mowik.MODEL_DIR),
            "revision": mowik.MODEL_SOURCES["tiny"][1],
        }
        model = object()
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory)
            for filename in (
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.json",
            ):
                (snapshot / filename).touch()
            with mock.patch.object(
                mowik.huggingface_hub,
                "snapshot_download",
                side_effect=[
                    mowik.LocalEntryNotFoundError("cache miss"),
                    str(snapshot),
                ],
            ) as download, mock.patch.object(
                mowik,
                "WhisperModel",
                return_value=model,
            ) as whisper:
                result = mowik.load_model_local_first("tiny", kwargs)

        self.assertIs(result, model)
        self.assertEqual(download.call_count, 2)
        self.assertTrue(download.call_args_list[0].kwargs["local_files_only"])
        self.assertFalse(download.call_args_list[1].kwargs["local_files_only"])
        whisper.assert_called_once_with(
            str(snapshot),
            device="cpu",
            compute_type="int8",
        )

    def test_cuda_model_is_released_before_cpu_fallback(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["model"] = "tiny"
        config["device"] = "auto"
        cpu_model = object()
        cuda_reference: dict[str, weakref.ReferenceType[object]] = {}
        load_count = 0

        class CudaModel:
            pass

        def load_model(*args, **kwargs):
            nonlocal load_count
            load_count += 1
            if load_count == 1:
                cuda_model = CudaModel()
                cuda_reference["model"] = weakref.ref(cuda_model)
                return cuda_model
            self.assertIsNone(cuda_reference["model"]())
            return cpu_model

        def fail_warmup(model, warmup_config):
            del warmup_config
            self.assertIsNotNone(model)
            raise RuntimeError("CUDA runtime failed")

        with mock.patch.object(mowik, "get_cuda_count", return_value=1), mock.patch.object(
            mowik,
            "load_model_local_first",
            side_effect=load_model,
        ), mock.patch.object(
            mowik,
            "warm_up_cuda_model",
            new=fail_warmup,
        ):
            model, model_name, device = mowik.create_model(config)

        self.assertIs(model, cpu_model)
        self.assertEqual((model_name, device), ("tiny", "cpu"))

    def test_unknown_model_is_rejected_before_network_access(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["model"] = "untrusted/example"

        with mock.patch.object(mowik, "get_cuda_count", return_value=0):
            with self.assertRaisesRegex(mowik.AppError, "Nieobsługiwany model"):
                mowik.resolve_model_plan(config, mowik.Translator("pl"))

    def test_removed_legacy_models_migrate_to_auto(self) -> None:
        for model_name in ("base", "medium"):
            with self.subTest(model=model_name), self.assertLogs(level="WARNING"):
                config = copy.deepcopy(mowik.DEFAULT_CONFIG)
                config["model"] = model_name
                mowik.migrate_legacy_config_values(config)

            self.assertEqual(config["model"], "auto")

    def test_loading_legacy_model_config_applies_safe_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text('{"model": "medium"}', encoding="utf-8")

            with mock.patch.object(mowik, "CONFIG_PATH", config_path), mock.patch.object(
                mowik, "create_default_files"
            ), self.assertLogs(level="WARNING"):
                config, _revision = mowik.load_config_with_revision()

        self.assertEqual(config["model"], "auto")

    def test_download_model_command_forces_a_refresh(self) -> None:
        model = object()
        with mock.patch.object(
            mowik,
            "create_model",
            return_value=(model, "small", "cpu"),
        ) as create, mock.patch("builtins.print"):
            self.assertEqual(
                mowik.download_model_command(copy.deepcopy(mowik.DEFAULT_CONFIG)),
                0,
            )

        self.assertTrue(create.call_args.kwargs["force_download"])

    def test_ensure_model_command_preserves_a_valid_cached_snapshot(self) -> None:
        model = object()
        with mock.patch.object(
            mowik,
            "create_model",
            return_value=(model, "small", "cpu"),
        ) as create, mock.patch("builtins.print"):
            self.assertEqual(
                mowik.download_model_command(
                    copy.deepcopy(mowik.DEFAULT_CONFIG),
                    force_download=False,
                ),
                0,
            )

        self.assertFalse(create.call_args.kwargs["force_download"])

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

    def test_shutdown_during_recorder_start_never_publishes_late_recorder(
        self,
    ) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        recorder = mock.Mock()
        recorder.start.side_effect = app.shutdown

        with mock.patch.object(
            mowik,
            "ContinuousRecorder",
            return_value=recorder,
        ), mock.patch.object(mowik, "create_model") as create_model, mock.patch.object(
            app, "set_status"
        ):
            app._load_runtime()

        self.assertIsNone(app.recorder)
        self.assertIsNone(app.model)
        self.assertFalse(app.model_ready.is_set())
        recorder.close.assert_called_once_with()
        create_model.assert_not_called()

    def test_shutdown_during_model_load_does_not_republish_or_double_close(
        self,
    ) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        recorder = mock.Mock()
        model = object()

        def shutdown_then_return(*_args):
            app.shutdown()
            return model, "small", "cpu"

        with mock.patch.object(
            mowik,
            "ContinuousRecorder",
            return_value=recorder,
        ), mock.patch.object(
            mowik,
            "create_model",
            side_effect=shutdown_then_return,
        ), mock.patch.object(app, "set_status"):
            app._load_runtime()

        self.assertIsNone(app.recorder)
        self.assertIsNone(app.model)
        self.assertFalse(app.model_ready.is_set())
        recorder.close.assert_called_once_with()

    def test_shutdown_flag_without_handover_still_closes_the_microphone(
        self,
    ) -> None:
        """Zamknięcie w trakcie ładowania nie może osierocić strumienia.

        Odtwarza wąskie okno, w którym shutdown() ustawił już flagę, ale nie
        zdążył przejąć recordera z pola. Wtedy zamknięcie należy do wątku
        ładującego — inaczej mikrofon zostaje otwarty do końca procesu.
        """

        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        recorder = mock.Mock()

        def flag_shutdown_without_handover(*_args):
            with app._shutdown_lock:
                app._shutdown_started = True
            return object(), "small", "cpu"

        with mock.patch.object(
            mowik,
            "ContinuousRecorder",
            return_value=recorder,
        ), mock.patch.object(
            mowik,
            "create_model",
            side_effect=flag_shutdown_without_handover,
        ), mock.patch.object(app, "set_status"):
            app._load_runtime()

        self.assertIsNone(app.recorder)
        self.assertIsNone(app.model)
        self.assertFalse(app.model_ready.is_set())
        recorder.close.assert_called_once_with()

    def test_model_ready_does_not_hide_disconnected_microphone_status(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        config["ui_language"] = "en"
        app = mowik.MowikApp(config)
        recorder = mock.Mock()

        def finish_model_load(*_args):
            with app._microphone_state_lock:
                app._microphone_unavailable = True
            return object(), "small", "cpu"

        with mock.patch.object(
            mowik,
            "ContinuousRecorder",
            return_value=recorder,
        ), mock.patch.object(
            mowik,
            "create_model",
            side_effect=finish_model_load,
        ), mock.patch.object(app, "set_status") as set_status:
            app._load_runtime()

        self.assertTrue(app.model_ready.is_set())
        self.assertEqual(set_status.call_args.kwargs["state"], "processing")
        self.assertIn("reconnecting", set_status.call_args.args[0])
        self.assertNotIn(
            "ready",
            [call.kwargs.get("state") for call in set_status.call_args_list],
        )


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


class NonElevatedProcessMixin:
    """Przypnij token procesu, zamiast dziedziczyć uprawnienia środowiska.

    Mówik świadomie odmawia zapisu ustawień i startu, gdy działa jako
    administrator. Runnery Windows w GitHub Actions są elewowane, więc bez
    tego testy ścieżki zwykłego użytkownika przewracały się tam, przechodząc
    jednocześnie na nieelewowanej maszynie dewelopera. Testy, które celowo
    sprawdzają odmowę, nadal nadpisują tę wartość własnym ``mock.patch``.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            mowik.windows_actions,
            "is_process_elevated",
            return_value=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class FeedbackConfigTests(NonElevatedProcessMixin, unittest.TestCase):
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

    def test_known_config_scalars_are_type_checked_before_runtime(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["pre_roll_ms"] = "300"

        with self.assertRaisesRegex(mowik.AppError, "pre_roll_ms"):
            mowik.validate_config_types(config)

    def test_config_rejects_disabled_paste_and_clipboard_outputs(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["paste"]["enabled"] = False
        config["paste"]["copy_to_clipboard"] = False

        with self.assertRaisesRegex(mowik.AppError, "paste.enabled"):
            mowik.validate_config_types(config)

    def test_config_rejects_minimum_recording_longer_than_maximum(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["minimum_recording_ms"] = 1_001
        config["maximum_recording_seconds"] = 1

        with self.assertRaisesRegex(mowik.AppError, "minimum_recording_ms"):
            mowik.validate_config_types(config)

    def test_config_validation_uses_selected_polish_language(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["ui_language"] = "pl"
        config["pre_roll_ms"] = "300"

        with self.assertRaisesRegex(mowik.AppError, "Nieprawidłowa wartość"):
            mowik.validate_config_types(config)

    def test_config_revision_conflict_preserves_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            initial = copy.deepcopy(mowik.DEFAULT_CONFIG)
            path.write_text(
                mowik.json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(mowik, "CONFIG_PATH", path):
                revision = mowik.config_file_revision()
                external = '{"external": true}\n'
                path.write_text(external, encoding="utf-8")

                with self.assertRaises(mowik.ConfigConflict):
                    mowik.save_config(initial, expected_revision=revision)

            self.assertEqual(path.read_text(encoding="utf-8"), external)

    def test_config_revision_errors_use_selected_language(self) -> None:
        config_path = mock.Mock()
        config_path.open.side_effect = OSError("locked")

        with mock.patch.object(mowik, "CONFIG_PATH", config_path):
            with self.assertRaisesRegex(mowik.AppError, "Nie udało się sprawdzić"):
                mowik.config_file_revision(mowik.Translator("pl"))

    def test_config_loader_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b"\xef\xbb\xbf" + b'{"ui_language":"en"}\n')

            with mock.patch.object(mowik, "CONFIG_PATH", path), mock.patch.object(
                mowik,
                "create_default_files",
            ):
                config, revision = mowik.load_config_with_revision()

        self.assertEqual(config["ui_language"], "en")
        self.assertEqual(
            revision,
            mowik.hashlib.sha256(
                b"\xef\xbb\xbf" + b'{"ui_language":"en"}\n'
            ).hexdigest(),
        )

    def test_oversized_config_is_rejected_at_the_read_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b"{" + b" " * mowik.MAX_CONFIG_FILE_BYTES)

            with mock.patch.object(mowik, "CONFIG_PATH", path), mock.patch.object(
                mowik,
                "create_default_files",
            ):
                with self.assertRaisesRegex(mowik.AppError, "zbyt duży|too large"):
                    mowik.load_config_with_revision()

    def test_write_ceiling_uses_pretty_payload_before_touching_config_or_wav(
        self,
    ) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        original_config = copy.deepcopy(config)
        compact = mowik.json.dumps(
            config,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        pretty = (
            mowik.json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        limit = (len(compact) + len(pretty)) // 2
        self.assertLessEqual(len(compact), limit)
        self.assertGreater(len(pretty), limit)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_bytes(b'{"preserved":true}\n')
            sounds = root / "sounds"
            sounds.mkdir()
            destination = sounds / "start.wav"
            destination.write_bytes(b"original wav")
            source = root / "replacement.wav"
            source.write_bytes(b"replacement wav")

            with mock.patch.object(
                mowik,
                "MAX_CONFIG_FILE_BYTES",
                limit,
            ), mock.patch.object(
                mowik,
                "CONFIG_PATH",
                config_path,
            ), mock.patch.object(
                mowik,
                "SOUNDS_DIR",
                sounds,
            ), mock.patch.object(
                mowik,
                "require_non_elevated_config_write",
            ), mock.patch.object(
                mowik,
                "ensure_directories",
            ) as ensure_directories, mock.patch.object(
                mowik,
                "config_write_guard",
            ) as write_guard, mock.patch.object(
                mowik,
                "import_custom_sound",
            ) as import_sound:
                with self.assertRaisesRegex(
                    mowik.AppError,
                    "zbyt duża|too large",
                ):
                    mowik.save_config(config)
                with self.assertRaisesRegex(
                    mowik.AppError,
                    "zbyt duża|too large",
                ):
                    mowik.save_config_with_custom_sounds(
                        config,
                        {"start": str(source)},
                    )

            ensure_directories.assert_not_called()
            write_guard.assert_not_called()
            import_sound.assert_not_called()
            self.assertEqual(config, original_config)
            self.assertEqual(config_path.read_bytes(), b'{"preserved":true}\n')
            self.assertEqual(destination.read_bytes(), b"original wav")
            self.assertEqual(source.read_bytes(), b"replacement wav")

    def test_config_conflict_uses_selected_language(self) -> None:
        with mock.patch.object(
            mowik,
            "config_file_revision",
            return_value="new-revision",
        ):
            with self.assertRaisesRegex(mowik.ConfigConflict, "innym oknie"):
                mowik._write_config_locked(
                    b"{}\n",
                    "old-revision",
                    mowik.Translator("pl"),
                )

    def test_elevated_process_cannot_write_user_config(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)

        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik.windows_actions,
            "is_process_elevated",
            return_value=True,
        ), mock.patch.object(mowik, "ensure_directories") as ensure_directories:
            with self.assertRaisesRegex(mowik.AppError, "administrator"):
                mowik.save_config(config)
            with self.assertRaisesRegex(mowik.AppError, "administrator"):
                mowik.save_config_with_custom_sounds(
                    config,
                    {"start": r"C:\untrusted\start.wav"},
                )

        ensure_directories.assert_not_called()

    def test_elevated_runtime_stops_before_user_data_initialization(self) -> None:
        args = argparse.Namespace(
            runtime_gui_smoke_test=False,
            console_log=False,
            download_model=False,
            ensure_model=False,
            list_devices=False,
            create_config=False,
            settings=False,
            test_ollama=False,
            restart_delay=0.0,
            restart_started_token=None,
        )

        with mock.patch.object(mowik, "parse_args", return_value=args), mock.patch.object(
            mowik.os,
            "name",
            "nt",
        ), mock.patch.object(
            mowik.windows_actions,
            "is_process_elevated",
            return_value=True,
        ), mock.patch.object(mowik, "setup_logging") as setup_logging, mock.patch.object(
            mowik,
            "create_default_files",
        ) as create_default_files:
            with self.assertRaisesRegex(mowik.AppError, "administrator"):
                mowik.main()

        setup_logging.assert_not_called()
        create_default_files.assert_not_called()

    def test_elevated_runtime_error_is_reported_without_log_or_config_io(self) -> None:
        error = mowik.ElevatedRuntimeError("do not run as administrator")

        with mock.patch.object(mowik, "main", side_effect=error), mock.patch.object(
            mowik,
            "setup_logging",
        ) as setup_logging, mock.patch.object(
            mowik,
            "load_config",
        ) as load_config, mock.patch.object(
            mowik,
            "show_fatal_error",
        ) as show_fatal_error:
            self.assertEqual(mowik.run_entrypoint(), 1)

        setup_logging.assert_not_called()
        load_config.assert_not_called()
        show_fatal_error.assert_called_once()
        self.assertEqual(show_fatal_error.call_args.args[0], str(error))
        self.assertFalse(show_fatal_error.call_args.kwargs["include_log_details"])
        self.assertFalse(show_fatal_error.call_args.kwargs["write_log"])

    def test_elevated_first_run_cannot_create_default_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "missing-config.json"
            dictionary_path = root / "missing-dictionary.txt"
            with mock.patch.object(mowik, "CONFIG_PATH", config_path), mock.patch.object(
                mowik,
                "DICTIONARY_PATH",
                dictionary_path,
            ), mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
                mowik.windows_actions,
                "is_process_elevated",
                return_value=True,
            ), mock.patch.object(mowik, "ensure_directories") as ensure_directories:
                with self.assertRaisesRegex(mowik.AppError, "administrator"):
                    mowik.create_default_files()

            ensure_directories.assert_not_called()
            self.assertFalse(config_path.exists())
            self.assertFalse(dictionary_path.exists())

    def test_elevated_load_with_existing_defaults_performs_no_mkdir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            dictionary_path = root / "dictionary.txt"
            config_path.write_text("{}", encoding="utf-8")
            dictionary_path.write_text("", encoding="utf-8")
            with mock.patch.object(mowik, "CONFIG_PATH", config_path), mock.patch.object(
                mowik,
                "DICTIONARY_PATH",
                dictionary_path,
            ), mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
                mowik.windows_actions,
                "is_process_elevated",
                return_value=True,
            ), mock.patch.object(mowik, "ensure_directories") as ensure_directories:
                mowik.create_default_files()

            ensure_directories.assert_not_called()

    def test_concurrent_first_run_creates_complete_default_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            dictionary_path = root / "dictionary.txt"
            errors: list[BaseException] = []

            def create_files() -> None:
                try:
                    mowik.create_default_files()
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(mowik, "CONFIG_PATH", config_path), mock.patch.object(
                mowik,
                "DICTIONARY_PATH",
                dictionary_path,
            ), mock.patch.object(
                mowik.windows_actions,
                "is_process_elevated",
                return_value=False,
            ), mock.patch.object(
                mowik,
                "ensure_directories",
            ):
                workers = [threading.Thread(target=create_files) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()

            self.assertEqual(errors, [])
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                mowik.DEFAULT_CONFIG,
            )
            self.assertEqual(
                dictionary_path.read_text(encoding="utf-8"),
                mowik.DICTIONARY_TEMPLATE,
            )

    def test_elevated_startup_logging_does_not_open_user_data_file(self) -> None:
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik.windows_actions,
            "is_process_elevated",
            return_value=True,
        ), mock.patch.object(mowik, "ensure_directories") as ensure_directories, mock.patch.object(
            mowik,
            "RotatingFileHandler",
        ) as rotating_handler, mock.patch.object(
            mowik.logging,
            "basicConfig",
        ) as basic_config:
            mowik.setup_logging()

        ensure_directories.assert_not_called()
        rotating_handler.assert_not_called()
        handlers = basic_config.call_args.kwargs["handlers"]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], mowik.logging.NullHandler)

    def test_unavailable_log_file_uses_a_safe_fallback_handler(self) -> None:
        with mock.patch.object(
            mowik.windows_actions,
            "is_process_elevated",
            return_value=False,
        ), mock.patch.object(
            mowik,
            "ensure_directories",
        ), mock.patch.object(
            mowik,
            "RotatingFileHandler",
            side_effect=PermissionError("log is locked"),
        ), mock.patch.object(
            mowik.logging,
            "basicConfig",
        ) as basic_config, mock.patch.object(
            mowik.logging,
            "warning",
        ) as warning:
            mowik.setup_logging()

        handlers = basic_config.call_args.kwargs["handlers"]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], mowik.logging.NullHandler)
        warning.assert_called_once()

    def test_failed_config_save_rolls_back_replaced_custom_sound(self) -> None:
        def write_wav(path: Path, sample: int) -> None:
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(np.full(160, sample, dtype=np.int16).tobytes())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sounds = root / "sounds"
            sounds.mkdir()
            destination = sounds / "start.wav"
            source = root / "new.wav"
            write_wav(destination, 100)
            original = destination.read_bytes()
            write_wav(source, 200)
            config = copy.deepcopy(mowik.DEFAULT_CONFIG)
            config["feedback"]["custom_sounds"]["start"] = "sounds/start.wav"

            with mock.patch.object(mowik, "SOUNDS_DIR", sounds), mock.patch.object(
                mowik,
                "_write_config_locked",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    mowik.save_config_with_custom_sounds(
                        config,
                        {"start": str(source)},
                    )

            self.assertEqual(destination.read_bytes(), original)

    def test_custom_sound_imports_are_serialized_with_config_write(self) -> None:
        first_import_started = threading.Event()
        release_first_import = threading.Event()
        results: list[str] = []

        def import_sound(*args) -> None:
            if not first_import_started.is_set():
                first_import_started.set()
                self.assertTrue(release_first_import.wait(2))

        def save_in_thread(label: str) -> None:
            config = copy.deepcopy(mowik.DEFAULT_CONFIG)
            try:
                mowik.save_config_with_custom_sounds(
                    config,
                    {"start": rf"C:\sources\{label}.wav"},
                )
            finally:
                results.append(label)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            mowik,
            "SOUNDS_DIR",
            Path(directory),
        ), mock.patch.object(
            mowik,
            "ensure_directories",
        ), mock.patch.object(
            mowik,
            "require_non_elevated_config_write",
        ), mock.patch.object(
            mowik,
            "validate_wave_file",
        ), mock.patch.object(
            mowik,
            "import_custom_sound",
            side_effect=import_sound,
        ) as imported, mock.patch.object(
            mowik,
            "_write_config_locked",
            return_value="revision",
        ):
            first = threading.Thread(target=save_in_thread, args=("first",))
            second = threading.Thread(target=save_in_thread, args=("second",))
            first.start()
            self.assertTrue(first_import_started.wait(2))
            second.start()
            time.sleep(0.05)
            self.assertEqual(imported.call_count, 1)
            release_first_import.set()
            first.join(2)
            second.join(2)

        self.assertCountEqual(results, ["first", "second"])
        self.assertEqual(imported.call_count, 2)

    def test_custom_sound_cross_references_use_the_original_source(self) -> None:
        def write_wav(path: Path, sample: int) -> None:
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(
                    np.full(160, sample, dtype=np.int16).tobytes()
                )

        def first_sample(path: Path) -> int:
            with wave.open(str(path), "rb") as wav_file:
                return int(
                    np.frombuffer(
                        wav_file.readframes(1),
                        dtype=np.int16,
                    )[0]
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sounds = root / "sounds"
            sounds.mkdir()
            start = sounds / "start.wav"
            stop = sounds / "stop.wav"
            replacement = root / "replacement.wav"
            write_wav(start, 100)
            write_wav(stop, 200)
            write_wav(replacement, 300)

            with mock.patch.object(mowik, "SOUNDS_DIR", sounds), mock.patch.object(
                mowik,
                "ensure_directories",
            ), mock.patch.object(
                mowik,
                "_write_config_locked",
                return_value="revision",
            ):
                mowik.save_config_with_custom_sounds(
                    copy.deepcopy(mowik.DEFAULT_CONFIG),
                    {
                        "start": str(replacement),
                        "stop": str(start),
                    },
                )

            self.assertEqual(first_sample(start), 300)
            self.assertEqual(first_sample(stop), 100)
            self.assertEqual(
                list(sounds.glob("*.staged.wav")),
                [],
            )

    def test_custom_sound_revision_conflict_is_checked_before_wav_changes(
        self,
    ) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)

        with mock.patch.object(mowik, "ensure_directories"), mock.patch.object(
            mowik,
            "require_non_elevated_config_write",
        ), mock.patch.object(
            mowik,
            "config_file_revision",
            return_value="new-revision",
        ), mock.patch.object(mowik, "import_custom_sound") as imported:
            with self.assertRaises(mowik.ConfigConflict):
                mowik.save_config_with_custom_sounds(
                    config,
                    {"start": r"C:\sources\start.wav"},
                    expected_revision="old-revision",
                )

        imported.assert_not_called()

    def test_ollama_cleanup_never_sends_transcript_to_remote_host(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["ollama_cleanup"].update(
            {"enabled": True, "model": "local", "url": "http://example.com:11434"}
        )

        with mock.patch.object(mowik.urllib.request, "urlopen") as urlopen:
            result = mowik.cleanup_with_ollama("private text", config, [])

        self.assertEqual(result, "private text")
        urlopen.assert_not_called()
        self.assertEqual(
            mowik.normalize_ollama_base_url("http://[::1]:11434/"),
            "http://[::1]:11434",
        )

    def test_ollama_loopback_transport_disables_proxies_and_redirects(self) -> None:
        request = mowik.urllib.request.Request("http://127.0.0.1:11434/api/chat")
        opener = mock.Mock()
        response = mock.Mock()
        opener.open.return_value = response

        with mock.patch.object(
            mowik.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            result = mowik._open_local_ollama_request(request, 12)

        self.assertIs(result, response)
        proxy_handler, redirect_handler = build_opener.call_args.args
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsInstance(redirect_handler, mowik._RejectOllamaRedirects)
        self.assertIsNone(
            redirect_handler.redirect_request(
                request,
                None,
                307,
                "redirect",
                {},
                "http://example.com/leak",
            )
        )
        opener.open.assert_called_once_with(request, timeout=12)

    def test_ollama_malformed_success_response_falls_back_to_transcript(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["ollama_cleanup"].update({"enabled": True, "model": "local"})

        for body in (
            [],
            {},
            {"message": None},
            {"message": {}},
            {"message": {"content": 123}},
        ):
            with self.subTest(body=body):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = (
                    mowik.json.dumps(body).encode("utf-8")
                )
                with mock.patch.object(
                    mowik,
                    "_open_local_ollama_request",
                    return_value=response,
                ):
                    result = mowik.cleanup_with_ollama(
                        "To jest oryginalny tekst.", config, []
                    )

                self.assertEqual(result, "To jest oryginalny tekst.")

    def test_ollama_runtime_timeout_is_defensively_clamped(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["ollama_cleanup"].update(
            {
                "enabled": True,
                "model": "local",
                "timeout_seconds": 999_999,
            }
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = mowik.json.dumps(
            {"message": {"content": "private text"}}
        ).encode("utf-8")

        with mock.patch.object(
            mowik,
            "_open_local_ollama_request",
            return_value=response,
        ) as open_request:
            result = mowik.cleanup_with_ollama("private text", config, [])

        self.assertEqual(result, "private text")
        self.assertEqual(
            open_request.call_args.args[1],
            mowik.MAX_OLLAMA_TIMEOUT_SECONDS,
        )

    def test_runtime_custom_sounds_stay_local_and_avoid_reparse_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            sounds = appdata / "sounds"
            sounds.mkdir()
            local_wav = sounds / "start.wav"
            local_wav.write_bytes(b"RIFF-local")
            outside_wav = appdata / "outside.wav"
            outside_wav.write_bytes(b"RIFF-outside")
            with mock.patch.object(mowik, "APPDATA_DIR", appdata), mock.patch.object(
                mowik,
                "SOUNDS_DIR",
                sounds,
            ):
                self.assertEqual(
                    mowik.runtime_sound_path("sounds/start.wav"),
                    local_wav.resolve(),
                )
                self.assertIsNone(mowik.runtime_sound_path(str(outside_wav)))
                self.assertIsNone(
                    mowik.runtime_sound_path(r"\\server\share\start.wav")
                )
                for rejected in (
                    r"\\?\C:\Windows\start.wav",
                    r"//?/C:/Windows/start.wav",
                    r"\??\C:\Windows\start.wav",
                ):
                    self.assertIsNone(mowik.runtime_sound_path(rejected))
                with mock.patch.object(
                    mowik,
                    "_path_is_reparse_point",
                    side_effect=lambda path: path == sounds,
                ):
                    self.assertIsNone(mowik.runtime_sound_path("sounds/start.wav"))

    def test_settings_reuses_managed_sound_after_successful_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdata = Path(directory)
            config = copy.deepcopy(mowik.DEFAULT_CONFIG)
            config["feedback"]["custom_sounds"]["start"] = "sounds/start.wav"

            with mock.patch.object(mowik, "APPDATA_DIR", appdata):
                sources = mowik.settings_sound_sources_from_config(config)

            self.assertEqual(sources["start"], str(appdata / "sounds" / "start.wav"))
            self.assertEqual(sources["stop"], "")


class DictionaryTests(unittest.TestCase):
    def tearDown(self) -> None:
        mowik._load_dictionary_snapshot.cache_clear()

    def test_zero_dictionary_limit_loads_no_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slownik.txt"
            path.write_text("one\ntwo\n", encoding="utf-8")
            config = copy.deepcopy(mowik.DEFAULT_CONFIG)
            config["dictionary"]["max_terms"] = 0

            with mock.patch.object(mowik, "DICTIONARY_PATH", path):
                self.assertEqual(mowik.load_dictionary(config), [])

    def test_invalid_utf8_dictionary_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slownik.txt"
            path.write_bytes(b"valid\n\xffbroken\n")

            with mock.patch.object(mowik, "DICTIONARY_PATH", path):
                self.assertEqual(
                    mowik.load_dictionary(copy.deepcopy(mowik.DEFAULT_CONFIG)),
                    [],
                )

    def test_utf8_bom_does_not_turn_first_comment_into_a_term(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slownik.txt"
            path.write_text("# comment\nMówik\n", encoding="utf-8-sig")

            with mock.patch.object(mowik, "DICTIONARY_PATH", path):
                self.assertEqual(
                    mowik.load_dictionary(copy.deepcopy(mowik.DEFAULT_CONFIG)),
                    ["Mówik"],
                )

    def test_oversized_dictionary_is_rejected_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slownik.txt"
            path.write_text("12345", encoding="utf-8")

            with mock.patch.object(mowik, "DICTIONARY_PATH", path), mock.patch.object(
                mowik,
                "MAX_DICTIONARY_FILE_BYTES",
                4,
            ), mock.patch.object(Path, "open", side_effect=AssertionError("must not read")):
                self.assertEqual(
                    mowik.load_dictionary(copy.deepcopy(mowik.DEFAULT_CONFIG)),
                    [],
                )


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

    def test_open_target_failure_is_localized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory).resolve() / "notes.txt"
            target.write_text("notes", encoding="utf-8")
            with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
                mowik.os,
                "startfile",
                create=True,
                side_effect=OSError("association unavailable"),
            ):
                with self.assertRaisesRegex(mowik.AppError, "Nie udało się otworzyć"):
                    mowik.open_custom_command_target(
                        str(target),
                        mowik.Translator("pl"),
                    )


class ClipboardReliabilityTests(unittest.TestCase):
    def test_clipboard_write_retries_transient_failures(self) -> None:
        error = mowik.pyperclip.PyperclipException("clipboard locked")
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik.pyperclip,
            "copy",
            side_effect=(error, error, None),
        ) as copy_text, mock.patch.object(mowik.time, "sleep") as sleep:
            mowik.windows_set_clipboard_text("tekst", mowik.Translator("pl"))

        self.assertEqual(copy_text.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            list(mowik.CLIPBOARD_WRITE_RETRY_DELAYS),
        )

    def test_clipboard_write_reports_error_after_retry_budget(self) -> None:
        error = mowik.pyperclip.PyperclipException("clipboard locked")
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik.pyperclip,
            "copy",
            side_effect=error,
        ) as copy_text, mock.patch.object(mowik.time, "sleep") as sleep:
            with self.assertRaisesRegex(mowik.AppError, "schowka"):
                mowik.windows_set_clipboard_text(
                    "tekst",
                    mowik.Translator("pl"),
                )

        self.assertEqual(copy_text.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


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

    def test_polish_and_directional_quote_pairs_are_removed(self) -> None:
        self.assertEqual(mowik.strip_llm_wrapping("„Gotowe.”"), "Gotowe.")
        self.assertEqual(mowik.strip_llm_wrapping("“Ready.”"), "Ready.")
        self.assertEqual(mowik.strip_llm_wrapping("«Prêt.»"), "Prêt.")

    def test_ctrl_v_path_always_verifies_clipboard_even_without_opt_in(self) -> None:
        config = self.paste_config()
        config["paste"]["delay_ms"] = 0
        with mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ), mock.patch.object(
            mowik,
            "windows_get_clipboard_text",
            return_value="substituted",
        ), mock.patch.object(mowik.keyboard, "Controller") as controller:
            with self.assertRaisesRegex(mowik.AppError, "clipboard changed"):
                mowik.paste_text("first\nsecond", config)

        controller.assert_not_called()

    def test_precise_target_compares_focused_control_not_only_top_level(self) -> None:
        current = mowik.windows_actions.ForegroundContext(hwnd=10, pid=20)
        expected = (10, 20, 30, 40, 0)
        with mock.patch.object(
            mowik.windows_actions,
            "capture_foreground_identity",
            return_value=current,
        ), mock.patch.object(
            mowik,
            "capture_paste_target_identity",
            return_value=(10, 20, 30, 41, 0),
        ):
            self.assertFalse(mowik.foreground_identity_matches(expected))

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

    def test_command_paste_without_clipboard_uses_unicode_typing(self) -> None:
        config = self.paste_config()
        config["paste"]["copy_to_clipboard"] = False

        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=True,
        ), mock.patch.object(
            mowik,
            "windows_type_unicode_text",
        ) as type_text, mock.patch.object(mowik.time, "sleep"):
            mowik.paste_text(
                "expected",
                config,
                append_space_override=False,
                expected_foreground=(101, 202),
                verify_clipboard_before_paste=True,
            )

        type_text.assert_called_once_with(
            "expected",
            mock.ANY,
            None,
            (101, 202),
        )

    def test_clipboard_only_delivery_does_not_depend_on_window_focus(self) -> None:
        config = self.paste_config()
        config["paste"]["enabled"] = False

        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
        ) as foreground_matches, mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text:
            mowik.paste_text(
                "expected",
                config,
                expected_foreground=(101, 202),
            )

        foreground_matches.assert_not_called()
        copy_text.assert_called_once_with("expected", mock.ANY)

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

    def test_single_line_custom_command_types_unicode_not_clipboard_contents(self) -> None:
        config = self.paste_config()
        cancel = threading.Event()
        with mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=True,
        ), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(
            mowik,
            "windows_get_clipboard_text",
            return_value="zażółć",
        ) as read_clipboard, mock.patch.object(
            mowik,
            "windows_type_unicode_text",
        ) as type_text, mock.patch.object(mowik.keyboard, "Controller") as controller:
            mowik.paste_text(
                "zażółć",
                config,
                append_space_override=False,
                expected_foreground=(101, 202),
                verify_clipboard_before_paste=True,
                cancel_event=cancel,
            )

        copy_text.assert_called_once_with("zażółć", mock.ANY)
        read_clipboard.assert_called_once_with(mock.ANY)
        controller.assert_not_called()
        type_text.assert_called_once_with(
            "zażółć",
            mock.ANY,
            cancel,
            (101, 202),
        )


class ShortcutCaptureTests(unittest.TestCase):
    def test_partial_listener_start_is_rolled_back(self) -> None:
        keyboard_listener = mock.Mock()
        mouse_listener = mock.Mock()
        mouse_listener.start.side_effect = RuntimeError("hook failed")

        with mock.patch.object(
            mowik.keyboard,
            "Listener",
            return_value=keyboard_listener,
        ), mock.patch.object(
            mowik.mouse,
            "Listener",
            return_value=mouse_listener,
        ):
            with self.assertRaisesRegex(RuntimeError, "hook failed"):
                mowik.start_shortcut_capture_listeners(mock.Mock(), mock.Mock())

        keyboard_listener.stop.assert_called_once_with()
        mouse_listener.stop.assert_called_once_with()


class StatusIndicatorTests(unittest.TestCase):
    def test_indicator_position_uses_monitor_work_area(self) -> None:
        expected_y = (
            1040
            - mowik.STATUS_INDICATOR_BOTTOM_MARGIN
            - mowik.STATUS_INDICATOR_HEIGHT
        )
        self.assertEqual(
            mowik.status_indicator_window_position((0, 0, 1920, 1040)),
            ((1920 - mowik.STATUS_INDICATOR_WIDTH) // 2, expected_y),
        )
        x, y = mowik.status_indicator_window_position(
            (-1920, 0, 0, 1040)
        )
        self.assertEqual(
            (x, y),
            (-1920 + (1920 - mowik.STATUS_INDICATOR_WIDTH) // 2, expected_y),
        )

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
        self.assertEqual(
            hidden.size,
            (mowik.STATUS_INDICATOR_WIDTH, mowik.STATUS_INDICATOR_HEIGHT),
        )
        self.assertIsNone(hidden.getbbox())

        for state in ("recording", "processing", "success", "error"):
            with self.subTest(state=state):
                frame = mowik.render_status_indicator_frame(state)
                self.assertEqual(frame.mode, "RGBA")
                self.assertIsNotNone(frame.getbbox())

    def test_recording_waveform_responds_to_microphone_level(self) -> None:
        quiet = mowik.render_status_indicator_frame(
            "recording",
            3,
            level=0.0,
            label="Listening",
        )
        loud = mowik.render_status_indicator_frame(
            "recording",
            3,
            level=1.0,
            label="Listening",
        )

        self.assertNotEqual(quiet.tobytes(), loud.tobytes())

    def test_transcript_preview_is_single_line_and_bounded(self) -> None:
        preview = mowik.status_indicator_preview_text(
            "  first line\n\nsecond line " + ("x" * 100),
            32,
        )

        self.assertNotIn("\n", preview)
        self.assertLessEqual(len(preview), 32)
        self.assertTrue(preview.endswith("…"))

    def test_transcript_preview_never_splits_joined_emoji_or_combining_mark(
        self,
    ) -> None:
        emoji = mowik.status_indicator_preview_text(
            "1234567👩\u200d💻tail",
            10,
        )
        combining = mowik.status_indicator_preview_text(
            "1234567e\u0301tail",
            10,
        )

        self.assertNotIn("\u200d…", emoji)
        self.assertNotIn("e…", combining)
        self.assertIn("e\u0301…", combining)

    def test_indicator_text_is_ellipsized_to_real_pixel_width(self) -> None:
        image = mowik.Image.new("RGBA", (400, 100))
        draw = mowik.ImageDraw.Draw(image)
        font = mowik._status_indicator_font(30)

        fitted = mowik._status_indicator_fit_text(
            draw,
            "WWWWWWWWWWWWWWWWWW",
            font,
            120,
        )

        self.assertTrue(fitted.endswith("…"))
        self.assertLessEqual(
            draw.textbbox((0, 0), fitted, font=font)[2],
            120,
        )

    def test_hidpi_indicator_scales_the_complete_visual(self) -> None:
        base = mowik.render_status_indicator_frame(
            "processing",
            4,
            label="Transcribing",
            detail="A longer live transcript preview",
        )
        large = mowik.render_status_indicator_frame(
            "processing",
            4,
            width=base.width * 2,
            height=base.height * 2,
            label="Transcribing",
            detail="A longer live transcript preview",
        )
        expected = base.resize(large.size, mowik.Image.Resampling.LANCZOS)
        difference = np.abs(
            np.asarray(large, dtype=np.int16)
            - np.asarray(expected, dtype=np.int16)
        )

        self.assertLess(float(difference.mean()), 3.0)

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

        state, queued_area, label, detail = indicator._commands.get_nowait()
        self.assertEqual((state, queued_area), ("recording", work_area))
        self.assertTrue(label)
        self.assertEqual(detail, "")
        indicator.close()


class DictationIndicatorFlowTests(unittest.TestCase):
    def make_app(self) -> mowik.MowikApp:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        return app

    def test_explorer_context_resolution_is_bounded(self) -> None:
        app = self.make_app()
        app._explorer_context_slots = mock.Mock()
        app._explorer_context_slots.acquire.return_value = False
        identity = mock.Mock(
            hwnd=101,
            pid=202,
            explorer_path=None,
            captured_at_monotonic=time.monotonic(),
        )

        with mock.patch.object(
            mowik.windows_actions,
            "capture_foreground_identity",
            return_value=identity,
        ), mock.patch.object(mowik.threading, "Thread") as thread_class:
            app._begin_command_context_capture()

        thread_class.assert_not_called()
        self.assertTrue(app._pending_command_context_ready.is_set())

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

    def test_post_roll_thread_start_failure_rolls_back_busy_capture(self) -> None:
        app = self.make_app()
        app.model_ready.set()
        app.recorder = mock.Mock()

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik.threading, "Thread"
        ) as thread_class:
            thread_class.return_value.start.side_effect = RuntimeError("no thread")
            app.begin_dictation()
            app.end_dictation()

        app.recorder.abort.assert_called_once_with()
        self.assertFalse(app.busy)
        self.assertFalse(app.capture_active)
        app.dictation_indicator.error.assert_called_once_with()

    def test_dictation_release_captures_delivery_window(self) -> None:
        app = self.make_app()
        app.model_ready.set()
        app.recorder = mock.Mock()
        target = (101, 202, 303, 404, 0)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik,
            "capture_paste_target_identity",
            return_value=target,
        ), mock.patch.object(mowik.threading, "Thread") as thread_class:
            app.begin_dictation()
            app.end_dictation()

        thread_class.assert_called_once()
        self.assertEqual(thread_class.call_args.kwargs["args"][2], target)

    def test_release_boundary_is_frozen_before_target_capture(self) -> None:
        app = self.make_app()
        app.model_ready.set()
        app.recorder = mock.Mock()
        order: list[str] = []
        app.recorder.mark_release.side_effect = lambda: order.append("release")

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik,
            "capture_paste_target_identity",
            side_effect=lambda: order.append("target") or (1, 2, 3, 4, 0),
        ), mock.patch.object(mowik.threading, "Thread"):
            app.begin_dictation()
            app.end_dictation()

        self.assertEqual(order, ["release", "target"])

    def test_changed_focus_aborts_delayed_dictation_without_clipboard_or_input(
        self,
    ) -> None:
        app = self.make_app()
        app.busy = True
        app.transcribe = mock.Mock(return_value="private transcript")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                delivery_foreground=(101, 202),
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik,
            "foreground_identity_matches",
            return_value=False,
        ), mock.patch.object(mowik, "windows_set_clipboard_text") as clipboard, mock.patch.object(
            mowik.keyboard,
            "Controller",
        ) as controller:
            app._job_worker()

        clipboard.assert_not_called()
        controller.assert_not_called()
        app.dictation_indicator.success.assert_not_called()
        app.dictation_indicator.error.assert_called_once_with()
        self.assertFalse(app.busy)

    def test_queued_dictation_preserves_release_window_until_delivery(self) -> None:
        app = self.make_app()
        app.busy = True
        app.transcribe = mock.Mock(return_value="Hello")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                delivery_foreground=(101, 202),
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik,
            "paste_text",
        ) as paste:
            app._job_worker()

        paste.assert_called_once_with(
            "Hello",
            app.config,
            cancel_event=app.stop_event,
            expected_foreground=(101, 202),
        )

    def test_queued_audio_keeps_capture_rate_after_stream_recovery(self) -> None:
        app = self.make_app()
        app.busy = True
        app.recorder = mock.Mock(sample_rate=16_000)
        app.transcribe = mock.Mock(return_value="")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(480, dtype=np.float32),
                sample_rate=48_000,
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"):
            app._job_worker()

        app.transcribe.assert_called_once_with(
            mock.ANY,
            mode="dictation",
            sample_rate=48_000,
        )

    def test_multiline_dictation_is_copied_for_manual_paste(self) -> None:
        app = self.make_app()
        app.busy = True
        app.set_status = mock.Mock()
        app.transcribe = mock.Mock(return_value="first\nsecond")
        app.jobs.put(
            mowik.SpeechJob(
                np.ones(160, dtype=np.float32),
                delivery_foreground=(101, 202),
            )
        )
        app.jobs.put(None)

        with mock.patch.object(app, "beep"), mock.patch.object(
            mowik,
            "windows_set_clipboard_text",
        ) as copy_text, mock.patch.object(mowik, "paste_text") as paste:
            app._job_worker()

        copy_text.assert_called_once_with("first\nsecond", app.translator)
        paste.assert_not_called()
        status_messages = [call.args[0] for call in app.set_status.call_args_list]
        self.assertTrue(any("Ctrl+V" in message for message in status_messages))

    def test_shutdown_during_recorder_begin_rolls_back_late_start(self) -> None:
        app = self.make_app()
        app.model_ready.set()
        begin_entered = threading.Event()
        allow_begin_to_return = threading.Event()
        recorder = mock.Mock()

        def blocking_begin() -> None:
            begin_entered.set()
            allow_begin_to_return.wait(2)

        recorder.begin.side_effect = blocking_begin
        app.recorder = recorder
        with mock.patch.object(app, "beep"):
            worker = threading.Thread(target=app.begin_dictation)
            worker.start()
            self.assertTrue(begin_entered.wait(1))
            app.shutdown()
            allow_begin_to_return.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(app.capture_active)
        self.assertIsNone(app.capture_mode)
        self.assertIsNone(app._capture_timer)
        self.assertFalse(app.busy)
        app.dictation_indicator.recording.assert_not_called()
        self.assertGreaterEqual(recorder.abort.call_count, 1)

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
        app.dictation_indicator.success.assert_called_once_with("Hello")
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

    def test_shutdown_continues_after_listener_cleanup_failure(self) -> None:
        app = self.make_app()
        app.keyboard_listener = mock.Mock()
        app.keyboard_listener.stop.side_effect = RuntimeError("hook stuck")
        app.mouse_listener = mock.Mock()
        mouse_listener = app.mouse_listener
        app.recorder = mock.Mock()
        recorder = app.recorder
        app.tray = mock.Mock()
        tray = app.tray

        app.shutdown()
        app.shutdown()

        mouse_listener.stop.assert_called_once_with()
        recorder.abort.assert_called_once_with()
        recorder.close.assert_called_once_with()
        tray.stop.assert_called_once_with()
        self.assertTrue(app._shutdown_complete.is_set())

    def test_parallel_shutdown_waits_for_owner_cleanup(self) -> None:
        app = self.make_app()
        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()
        waiter_entered = threading.Event()
        waiter_returned = threading.Event()

        def blocking_close() -> None:
            cleanup_entered.set()
            allow_cleanup.wait(2)

        app.dictation_indicator.close.side_effect = blocking_close
        owner = threading.Thread(target=app.shutdown)
        owner.start()
        self.assertTrue(cleanup_entered.wait(1))

        def wait_for_shutdown() -> None:
            waiter_entered.set()
            app.shutdown()
            waiter_returned.set()

        waiter = threading.Thread(target=wait_for_shutdown)
        waiter.start()
        self.assertTrue(waiter_entered.wait(1))
        self.assertFalse(waiter_returned.wait(0.05))
        allow_cleanup.set()
        owner.join(2)
        waiter.join(2)

        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertTrue(waiter_returned.is_set())
        app.dictation_indicator.close.assert_called_once_with()

    def test_shutdown_reentry_from_cleanup_owner_does_not_deadlock(self) -> None:
        app = self.make_app()
        app.dictation_indicator.close.side_effect = app.shutdown

        owner = threading.Thread(target=app.shutdown)
        owner.start()
        owner.join(2)

        self.assertFalse(owner.is_alive())
        self.assertTrue(app._shutdown_complete.is_set())
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
        opened.assert_called_once_with(
            r"C:\Windows\System32\notepad.exe",
            app.translator,
        )
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
        ) as deliver, mock.patch.object(
            mowik.windows_actions,
            "terminate_terminal",
            return_value=mock.Mock(status="terminated"),
        ) as terminate:
            result = app._deliver_custom_command("moja komenda git status")

        self.assertFalse(result)
        deliver.assert_not_called()
        terminate.assert_called_once_with(handle)

    def test_failed_terminal_draft_delivery_closes_new_terminal(self) -> None:
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
        failed = mowik.windows_actions.DraftDeliveryResult("failed")

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
            return_value=failed,
        ), mock.patch.object(
            mowik.windows_actions,
            "terminate_terminal",
            return_value=mock.Mock(status="terminated"),
        ) as terminate:
            with self.assertRaises(mowik.AppError):
                app._deliver_custom_command("moja komenda git status")

        terminate.assert_called_once_with(handle)

    def test_windows_terminal_handoff_is_not_treated_as_confirmed_cleanup(self) -> None:
        app = self.make_app(
            action="open_terminal",
            value="",
            match="prefix_tail",
            options={"cwd_source": "home", "host": "windows_terminal"},
        )
        directory = mowik.windows_actions.WorkingDirectoryResult(
            "home",
            Path(r"C:\Users\User"),
        )
        handle = mowik.windows_actions.TerminalHandle(
            "windows_terminal",
            "default",
            directory.path,
            303,
            43.0,
        )
        launched = mowik.windows_actions.TerminalLaunchResult("launched", handle)
        failed = mowik.windows_actions.DraftDeliveryResult("failed")

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
            return_value=failed,
        ), mock.patch.object(
            mowik.windows_actions,
            "terminate_terminal",
            return_value=mock.Mock(
                status="already_exited",
                reason="unmanaged_wt_handoff",
            ),
        ), mock.patch.object(mowik.logging, "warning") as warning:
            with self.assertRaises(mowik.AppError):
                app._deliver_custom_command("moja komenda git status")

        warning.assert_called_once()
        self.assertIn("zamknięcia terminala", warning.call_args.args[0])

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

    def test_paste_action_is_denied_when_mowik_is_elevated(self) -> None:
        app = self.make_app(action="paste_text", value="safe")
        elevated = mowik.command_engine.ExecutionContext(
            101,
            202,
            None,
            time.monotonic(),
            True,
        )

        with mock.patch.object(
            mowik,
            "paste_text",
        ) as paste, mock.patch.object(app, "beep"):
            result = app._deliver_custom_command("moja komenda", elevated)

        self.assertFalse(result)
        paste.assert_not_called()

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

    def test_resampled_transcription_uses_in_place_bounded_audio_buffers(self) -> None:
        app = self.make_app()
        app.model = mock.Mock()
        info = mock.Mock(language="en", language_probability=1.0)
        app.model.transcribe.return_value = ([], info)
        audio = np.tile(np.array([-0.25, 0.75], dtype=np.float32), 2_000)
        original_clip = np.clip
        original_multiply = np.multiply

        with mock.patch.object(
            mowik,
            "load_dictionary",
            return_value=[],
        ), mock.patch.object(
            mowik.np,
            "clip",
            wraps=original_clip,
        ) as clip, mock.patch.object(
            mowik.np,
            "multiply",
            wraps=original_multiply,
        ) as multiply:
            result = app.transcribe(
                audio,
                mode="custom_command",
                sample_rate=48_000,
            )

        self.assertEqual(result, "")
        self.assertAlmostEqual(float(np.mean(audio)), 0.0, places=6)
        clip.assert_called_once()
        self.assertTrue(np.shares_memory(clip.call_args.kwargs["out"], audio))
        multiply.assert_called_once()
        self.assertEqual(multiply.call_args.kwargs["out"].dtype, np.int16)
        wav_buffer = app.model.transcribe.call_args.args[0]
        with wave.open(wav_buffer, "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 48_000)
            self.assertEqual(wav_file.getnframes(), audio.size)



class SettingsLifecycleTests(NonElevatedProcessMixin, unittest.TestCase):
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

    def test_restart_ack_matches_only_the_current_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ack_path = Path(temporary) / "restart.ack"
            with mock.patch.object(mowik, "RESTART_ACK_PATH", ack_path), mock.patch.object(
                mowik, "ensure_directories"
            ):
                mowik.acknowledge_restart_request("v1:2000")
                self.assertTrue(mowik.wait_for_restart_ack(2_000, timeout=0))
                self.assertFalse(mowik.wait_for_restart_ack(1_999, timeout=0))
                self.assertFalse(mowik.wait_for_restart_ack(2_001, timeout=0))

    def test_restart_started_confirmation_is_versioned_and_request_scoped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started_path = Path(temporary) / "restart.started"
            with mock.patch.object(
                mowik,
                "RESTART_STARTED_PATH",
                started_path,
            ), mock.patch.object(mowik, "ensure_directories"):
                mowik.announce_restart_started(2_000)
                self.assertEqual(
                    started_path.read_text(encoding="ascii"),
                    "v2:started:2000\n",
                )
                self.assertTrue(mowik.wait_for_restart_started(2_000, timeout=0))
                self.assertFalse(mowik.wait_for_restart_started(1_999, timeout=0))
                self.assertFalse(mowik.wait_for_restart_started(2_001, timeout=0))

                started_path.write_text("v1:2000\n", encoding="ascii")
                self.assertFalse(mowik.wait_for_restart_started(2_000, timeout=0))

        self.assertEqual(mowik.parse_restart_started_cli_token("2000"), 2_000)
        for invalid in ("0", "02000", "+2000", "v1:2000", "2.0"):
            with self.subTest(invalid=invalid), self.assertRaises(
                mowik.argparse.ArgumentTypeError
            ):
                mowik.parse_restart_started_cli_token(invalid)

        self.assertGreater(
            mowik.RESTART_STARTED_TIMEOUT_SECONDS,
            0.2 + mowik.RESTART_MUTEX_WAIT_SECONDS,
        )

    def test_settings_launches_fresh_app_if_running_instance_exits_before_ack(
        self,
    ) -> None:
        with mock.patch.object(
            mowik, "is_app_instance_running", side_effect=[True, False]
        ), mock.patch.object(
            mowik, "request_app_restart", return_value=2_000
        ), mock.patch.object(
            mowik, "wait_for_restart_ack", return_value=False
        ), mock.patch.object(
            mowik, "wait_for_restart_started", return_value=True
        ) as wait_for_started, mock.patch.object(
            mowik, "discard_pending_restart_request"
        ) as discard, mock.patch.object(
            mowik, "application_process_args", return_value=["python", "mowik.py"]
        ), mock.patch.object(mowik.subprocess, "Popen") as popen:
            result = mowik.restart_or_launch_app_after_settings()

        self.assertEqual(result, "app_started")
        discard.assert_called_once_with()
        popen.assert_called_once_with(
            [
                "python",
                "mowik.py",
                "--restart-delay",
                "0.2",
                "--restart-started-token",
                "2000",
            ],
            cwd=str(mowik.APP_ROOT),
        )
        wait_for_started.assert_called_once_with(2_000)

    def test_restart_receipt_without_started_confirmation_is_an_error(self) -> None:
        with mock.patch.object(
            mowik, "is_app_instance_running", return_value=True
        ), mock.patch.object(
            mowik, "request_app_restart", return_value=2_000
        ), mock.patch.object(
            mowik, "wait_for_restart_ack", return_value=True
        ), mock.patch.object(
            mowik, "wait_for_restart_started", return_value=False
        ), mock.patch.object(mowik.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(
                mowik.AppError,
                "nie potwierdziła|did not confirm",
            ):
                mowik.restart_or_launch_app_after_settings(mowik.Translator("pl"))

        popen.assert_not_called()

    def test_direct_restart_popen_failure_is_reported(self) -> None:
        with mock.patch.object(
            mowik, "is_app_instance_running", return_value=False
        ), mock.patch.object(
            mowik, "request_app_restart", return_value=2_000
        ), mock.patch.object(
            mowik, "discard_pending_restart_request"
        ) as discard, mock.patch.object(
            mowik, "application_process_args", return_value=["python", "mowik.py"]
        ), mock.patch.object(
            mowik.subprocess,
            "Popen",
            side_effect=OSError("process creation failed"),
        ) as popen, mock.patch.object(
            mowik, "wait_for_restart_started"
        ) as wait_for_started:
            with self.assertRaisesRegex(
                mowik.AppError,
                "Nie udało się uruchomić|Could not start",
            ):
                mowik.restart_or_launch_app_after_settings(mowik.Translator("pl"))

        discard.assert_called_once_with()
        popen.assert_called_once()
        wait_for_started.assert_not_called()

    def test_running_app_gets_request_but_standalone_settings_launches_cleanly(self) -> None:
        with mock.patch.object(
            mowik, "is_app_instance_running", return_value=True
        ), mock.patch.object(
            mowik, "request_app_restart", return_value=2_000
        ) as request, mock.patch.object(
            mowik, "discard_pending_restart_request"
        ) as discard, mock.patch.object(
            mowik, "wait_for_restart_ack", return_value=True
        ) as wait_for_ack, mock.patch.object(
            mowik, "wait_for_restart_started", return_value=True
        ) as wait_for_started, mock.patch.object(mowik.subprocess, "Popen") as popen:
            result = mowik.restart_or_launch_app_after_settings()

        self.assertEqual(result, "restart_requested")
        request.assert_called_once_with()
        wait_for_ack.assert_called_once_with(request.return_value)
        wait_for_started.assert_called_once_with(request.return_value)
        discard.assert_not_called()
        popen.assert_not_called()

        with mock.patch.object(
            mowik, "is_app_instance_running", return_value=False
        ), mock.patch.object(
            mowik, "request_app_restart", return_value=2_000
        ) as request, mock.patch.object(
            mowik, "discard_pending_restart_request"
        ) as discard, mock.patch.object(
            mowik, "application_process_args", return_value=["python", "mowik.py"]
        ), mock.patch.object(mowik.subprocess, "Popen") as popen, mock.patch.object(
            mowik, "wait_for_restart_started", return_value=True
        ) as wait_for_started:
            result = mowik.restart_or_launch_app_after_settings()

        self.assertEqual(result, "app_started")
        request.assert_called_once_with()
        discard.assert_called_once_with()
        popen.assert_called_once_with(
            [
                "python",
                "mowik.py",
                "--restart-delay",
                "0.2",
                "--restart-started-token",
                "2000",
            ],
            cwd=str(mowik.APP_ROOT),
        )
        wait_for_started.assert_called_once_with(2_000)

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
            self.assertEqual(
                mowik.application_restart_process_args(2_000),
                [
                    *source,
                    "--restart-delay",
                    "0.2",
                    "--restart-started-token",
                    "2000",
                ],
            )

        with mock.patch.object(mowik.sys, "frozen", True, create=True), mock.patch.object(
            mowik.sys, "executable", r"C:\Program Files\Mowik\Mowik.exe"
        ):
            frozen = [r"C:\Program Files\Mowik\Mowik.exe"]
            self.assertEqual(mowik.application_process_args(), frozen)
            self.assertEqual(mowik.settings_process_args(), [*frozen, "--settings"])
            self.assertEqual(
                mowik.application_restart_process_args(),
                [*frozen, "--restart-delay", "0.2"],
            )

    def test_legacy_restart_request_is_forwarded_to_replacement_instance(
        self,
    ) -> None:
        app = mowik.MowikApp(copy.deepcopy(mowik.DEFAULT_CONFIG))
        events: list[str] = []
        app.shutdown = mock.Mock(side_effect=lambda *_args: events.append("shutdown"))

        def launch(*_args, **_kwargs):
            events.append("popen")
            return mock.Mock()

        def acknowledge(_request):
            events.append("ack")

        with mock.patch.object(
            mowik,
            "application_process_args",
            return_value=["python", "mowik.py"],
        ), mock.patch.object(
            mowik.subprocess,
            "Popen",
            side_effect=launch,
        ) as popen, mock.patch.object(
            mowik,
            "acknowledge_restart_request",
            side_effect=acknowledge,
        ):
            app.restart(restart_request="0.000002000")

        popen.assert_called_once_with(
            [
                "python",
                "mowik.py",
                "--restart-delay",
                "0.2",
                "--restart-started-token",
                "2000",
            ],
            cwd=str(mowik.APP_ROOT),
        )
        app.shutdown.assert_called_once_with(None, None)
        self.assertEqual(events, ["popen", "ack", "shutdown"])

    def test_failed_replacement_process_is_not_acknowledged(self) -> None:
        app = mowik.MowikApp(copy.deepcopy(mowik.DEFAULT_CONFIG))
        app.shutdown = mock.Mock()

        with mock.patch.object(
            mowik.subprocess,
            "Popen",
            side_effect=OSError("process creation failed"),
        ), mock.patch.object(
            mowik,
            "acknowledge_restart_request",
        ) as acknowledge, self.assertRaisesRegex(
            OSError,
            "process creation failed",
        ):
            app.restart(restart_request="v1:2000")

        acknowledge.assert_not_called()
        app.shutdown.assert_not_called()

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

    def test_single_instance_mutex_uses_the_error_resetting_helper(self) -> None:
        # CreateMutexW nie zeruje kodu błędu przy sukcesie, więc zalegające
        # ERROR_ALREADY_EXISTS z innego wywołania Win32 nie może zablokować
        # startu pierwszej instancji.
        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik,
            "_create_windows_named_mutex",
            return_value=(4242, False),
        ) as create_mutex, mock.patch.object(
            mowik, "release_single_instance"
        ) as release:
            handle = mowik.acquire_single_instance(mowik.Translator("en"))

        self.assertEqual(handle, 4242)
        create_mutex.assert_called_once_with(mowik.MUTEX_NAME)
        release.assert_not_called()

        with mock.patch.object(mowik.os, "name", "nt"), mock.patch.object(
            mowik,
            "_create_windows_named_mutex",
            return_value=(4242, True),
        ), mock.patch.object(mowik, "release_single_instance") as release:
            self.assertIsNone(
                mowik.acquire_single_instance(
                    mowik.Translator("en"),
                    notify_existing=False,
                )
            )

        release.assert_called_once_with(4242)

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
    def test_ready_status_shows_effective_recording_limit_when_clamped(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["maximum_recording_seconds"] = 300
        config["ui_language"] = "en"
        app = mowik.MowikApp(config)
        app.recorder = mock.Mock(maximum_recording_seconds=116.5)

        self.assertIn("recording limit: 116 s", app._ready_status())

    def test_tray_open_failure_is_reported_without_escaping_callback(self) -> None:
        app = mowik.MowikApp(copy.deepcopy(mowik.DEFAULT_CONFIG))
        app.set_status = mock.Mock()

        with mock.patch.object(
            mowik.os,
            "startfile",
            create=True,
            side_effect=OSError("association unavailable"),
        ) as startfile:
            app.open_log()

        startfile.assert_called_once_with(mowik.LOG_PATH)
        app.set_status.assert_called_once()
        self.assertEqual(app.set_status.call_args.kwargs["state"], "error")
        self.assertTrue(app.set_status.call_args.kwargs["error"])

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

    def test_restart_started_callback_runs_only_after_application_start(self) -> None:
        events: list[str] = []
        app = mowik.MowikApp(copy.deepcopy(mowik.DEFAULT_CONFIG))
        app.dictation_indicator = mock.Mock()
        app.dictation_indicator.start.return_value = False
        app.start = mock.Mock(side_effect=lambda: events.append("app-start"))
        tray = mock.Mock()

        def run_tray_backend(*, setup):
            events.append("tray-ready")
            setup(tray)

        tray.run.side_effect = run_tray_backend

        with mock.patch.object(
            mowik.pystray,
            "Icon",
            return_value=tray,
        ), mock.patch.object(app, "stop_feedback_sound"):
            app.run_tray(
                started_callback=lambda: events.append("restart-started")
            )

        self.assertEqual(
            events,
            ["app-start", "tray-ready", "restart-started"],
        )
        self.assertTrue(tray.visible)

    def test_restart_is_not_confirmed_when_standard_tray_backend_fails(self) -> None:
        app = mowik.MowikApp(copy.deepcopy(mowik.DEFAULT_CONFIG))
        app.dictation_indicator = mock.Mock()
        app.dictation_indicator.start.return_value = False
        app.start = mock.Mock()
        tray = mock.Mock()
        tray.run.side_effect = RuntimeError("tray backend failed")
        started_callback = mock.Mock()

        with mock.patch.object(
            mowik.pystray,
            "Icon",
            return_value=tray,
        ), mock.patch.object(app, "stop_feedback_sound"), self.assertRaisesRegex(
            RuntimeError,
            "tray backend failed",
        ):
            app.run_tray(started_callback=started_callback)

        started_callback.assert_not_called()

    def test_restart_is_not_confirmed_when_detached_tray_setup_fails(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = True
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        app.dictation_indicator.start.return_value = True
        app.start = mock.Mock()
        tray = mock.Mock()
        tray.run_detached.side_effect = RuntimeError("detached tray failed")
        started_callback = mock.Mock()

        with mock.patch.object(
            mowik.pystray,
            "Icon",
            return_value=tray,
        ), mock.patch.object(app, "stop_feedback_sound"), self.assertRaisesRegex(
            RuntimeError,
            "detached tray failed",
        ):
            app.run_tray(started_callback=started_callback)

        started_callback.assert_not_called()

    def test_partial_start_failure_still_cleans_up_every_loop(self) -> None:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        app.dictation_indicator.start.return_value = True
        app.start = mock.Mock(side_effect=RuntimeError("partial start"))
        tray = mock.Mock()
        started_callback = mock.Mock()

        with mock.patch.object(mowik.pystray, "Icon", return_value=tray), mock.patch.object(
            app,
            "stop_feedback_sound",
        ), self.assertRaisesRegex(RuntimeError, "partial start"):
            app.run_tray(started_callback=started_callback)

        self.assertTrue(app.stop_event.is_set())
        started_callback.assert_not_called()
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

        self.assertEqual(query_devices.call_count, 3)
        self.assertEqual(query_hostapis.call_count, 3)
        self.assertEqual(input_stream.call_args.kwargs["device"], 0)
        self.assertEqual(input_stream.call_args.kwargs["samplerate"], 48_000)
        self.assertEqual(input_stream.call_args.kwargs["latency"], "high")
        self.assertEqual(input_stream.call_args.kwargs["blocksize"], 0)
        self.assertEqual(
            input_stream.call_args.kwargs["finished_callback"],
            recorder._stream_finished_callback,
        )
        stream.start.assert_called_once_with()

    def test_default_microphone_prefers_windows_wasapi_endpoint(self) -> None:
        devices = [
            self.device("Default Microphone", 0, sample_rate=44_100),
            self.device("Default Microphone", 1, sample_rate=48_000),
        ]
        host_apis = [
            {"name": "MME", "default_input_device": 0},
            {"name": "Windows WASAPI", "default_input_device": 1},
        ]
        stream = mock.Mock()

        with mock.patch.object(
            mowik.sd, "query_devices", return_value=devices
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=host_apis
        ), mock.patch.object(
            mowik.sd, "InputStream", return_value=stream
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(self.recorder_config(None))
            recorder.start()

        self.assertEqual(input_stream.call_args.kwargs["device"], 1)
        self.assertEqual(input_stream.call_args.kwargs["samplerate"], 48_000)
        self.assertEqual(input_stream.call_args.kwargs["latency"], "high")

    def test_saved_mme_microphone_uses_unique_same_name_wasapi_endpoint(self) -> None:
        devices = [
            self.device("IN 1 (BEHRINGER UMC)", 0, sample_rate=44_100),
            self.device("IN 1 (BEHRINGER UMC)", 1, sample_rate=48_000),
        ]
        mme_selector = mowik.audio_devices.build_microphone_selector(
            0,
            devices,
            self.host_apis,
        )
        stream = mock.Mock()

        with mock.patch.object(
            mowik.sd, "query_devices", return_value=devices
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=self.host_apis
        ), mock.patch.object(
            mowik.sd, "InputStream", return_value=stream
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(self.recorder_config(mme_selector))
            recorder.start()

        self.assertEqual(input_stream.call_args.kwargs["device"], 1)
        self.assertEqual(input_stream.call_args.kwargs["samplerate"], 48_000)

    def test_failed_wasapi_fallback_is_reused_first_on_recovery(self) -> None:
        devices = [
            self.device("IN 1 (BEHRINGER UMC)", 0, sample_rate=44_100),
            self.device("IN 1 (BEHRINGER UMC)", 1, sample_rate=48_000),
        ]
        mme_selector = mowik.audio_devices.build_microphone_selector(
            0,
            devices,
            self.host_apis,
        )
        wasapi_high = mock.Mock()
        wasapi_high.start.side_effect = RuntimeError("WdmSyncIoctl")
        wasapi_low = mock.Mock()
        wasapi_low.start.side_effect = RuntimeError("WdmSyncIoctl")
        mme_high = mock.Mock()
        recovered_mme = mock.Mock(active=True)

        with mock.patch.object(
            mowik.sd, "query_devices", return_value=devices
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=self.host_apis
        ), mock.patch.object(
            mowik.sd,
            "InputStream",
            side_effect=[wasapi_high, wasapi_low, mme_high, recovered_mme],
        ) as input_stream, mock.patch.object(
            mowik.logging, "warning"
        ) as warning, mock.patch.object(mowik.logging, "info") as info:
            recorder = mowik.ContinuousRecorder(self.recorder_config(mme_selector))
            recorder.start()
            mme_high.active = False
            self.assertTrue(recorder.ensure_stream_alive())

        self.assertEqual(
            [call.kwargs["device"] for call in input_stream.call_args_list],
            [1, 1, 0, 0],
        )
        self.assertEqual(
            [call.kwargs["samplerate"] for call in input_stream.call_args_list],
            [48_000, 48_000, 44_100, 44_100],
        )
        self.assertEqual(
            [call.kwargs["latency"] for call in input_stream.call_args_list],
            ["high", "low", "high", "high"],
        )
        self.assertNotIn(
            mowik.SAMPLE_RATE,
            [call.kwargs["samplerate"] for call in input_stream.call_args_list],
        )
        self.assertEqual(
            recorder._last_working_stream_attempt,
            ("original", 44_100, "high"),
        )
        failed_attempt_logs = [
            call
            for call in info.call_args_list
            if call.args
            and call.args[0].startswith("Nie udało się otworzyć mikrofonu")
        ]
        self.assertEqual(
            [call.args[1] for call in failed_attempt_logs],
            ["preferred", "preferred"],
        )
        warning.assert_not_called()

    def test_ambiguous_optional_wasapi_uses_exact_saved_mme(self) -> None:
        devices = [
            self.device("IN 1 (BEHRINGER UMC)", 0, sample_rate=44_100),
            self.device("IN 1 (BEHRINGER UMC)", 1, sample_rate=48_000),
            self.device("IN 1 (BEHRINGER UMC)", 1, sample_rate=48_000),
        ]
        mme_selector = mowik.audio_devices.build_microphone_selector(
            0,
            devices,
            self.host_apis,
        )
        stream = mock.Mock()

        with mock.patch.object(
            mowik.sd, "query_devices", return_value=devices
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=self.host_apis
        ), mock.patch.object(
            mowik.sd, "InputStream", return_value=stream
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(self.recorder_config(mme_selector))
            recorder.start()

        input_stream.assert_called_once()
        self.assertEqual(input_stream.call_args.kwargs["device"], 0)
        self.assertEqual(input_stream.call_args.kwargs["samplerate"], 44_100)
        self.assertEqual(input_stream.call_args.kwargs["latency"], "high")

    def test_optional_wasapi_requires_the_same_duplex_channel_topology(self) -> None:
        devices = [
            self.device(
                "IN 1 (BEHRINGER UMC)",
                0,
                outputs=0,
                sample_rate=44_100,
            ),
            self.device(
                "IN 1 (BEHRINGER UMC)",
                1,
                outputs=2,
                sample_rate=48_000,
            ),
        ]
        mme_selector = mowik.audio_devices.build_microphone_selector(
            0,
            devices,
            self.host_apis,
        )
        stream = mock.Mock()

        with mock.patch.object(
            mowik.sd, "query_devices", return_value=devices
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=self.host_apis
        ), mock.patch.object(
            mowik.sd, "InputStream", return_value=stream
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(self.recorder_config(mme_selector))
            recorder.start()

        input_stream.assert_called_once()
        self.assertEqual(input_stream.call_args.kwargs["device"], 0)
        self.assertEqual(input_stream.call_args.kwargs["samplerate"], 44_100)

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
            side_effect=[
                self.devices,
                self.devices,
                self.devices,
                reordered_devices,
            ],
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

        self.assertEqual(query_devices.call_count, 4)
        self.assertEqual(
            [call.kwargs["device"] for call in input_stream.call_args_list],
            [1, 0],
        )
        first_stream.close.assert_called_once_with(ignore_errors=False)
        second_stream.start.assert_called_once_with()

    def test_legacy_index_is_pinned_before_hotplug_retry(self) -> None:
        replacement_devices = [
            copy.deepcopy(self.devices[0]),
            self.device("Replacement Microphone", 1, inputs=1),
        ]
        first_stream = mock.Mock()
        first_stream.start.side_effect = RuntimeError("first attempt failed")

        with mock.patch.object(
            mowik.sd,
            "query_devices",
            side_effect=[
                self.devices,
                self.devices,
                self.devices,
                replacement_devices,
            ],
        ) as query_devices, mock.patch.object(
            mowik.sd,
            "query_hostapis",
            return_value=self.host_apis,
        ), mock.patch.object(
            mowik.sd,
            "InputStream",
            return_value=first_stream,
        ) as input_stream:
            recorder = mowik.ContinuousRecorder(self.recorder_config(1))
            with self.assertRaises(mowik.AppError) as raised:
                recorder.start()

        self.assertEqual(query_devices.call_count, 4)
        input_stream.assert_called_once()
        self.assertEqual(input_stream.call_args.kwargs["device"], 1)
        self.assertEqual(recorder.device_selector, self.selector)
        self.assertNotIn("Replacement Microphone", str(raised.exception))

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

        self.assertIn("Secret Studio Microphone", state.selected_label)
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
    def test_recording_meter_tracks_voice_level_and_resets(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        recorder.begin()
        chunk = np.full((256, 1), 0.25, dtype=np.float32)

        recorder._callback(chunk, len(chunk), None, 0)

        self.assertGreater(recorder.current_level(), 0.0)
        self.assertLessEqual(recorder.current_level(), 1.0)
        recorder.finish()
        self.assertEqual(recorder.current_level(), 0.0)

    def test_recording_is_capped_to_configured_sample_limit(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {
                "pre_roll_ms": 0,
                "microphone": None,
                "maximum_recording_seconds": 1,
            }
        )
        recorder.begin()
        chunk = np.ones((4_000, 1), dtype=np.float32)

        for _ in range(10):
            recorder._callback(chunk, len(chunk), None, 0)

        self.assertTrue(recorder.recording_limit_reached.is_set())
        self.assertEqual(recorder._recording_samples, mowik.SAMPLE_RATE)
        self.assertEqual(len(recorder.finish()), mowik.SAMPLE_RATE)

    def test_audio_callback_defers_status_logging_until_finish(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        recorder.begin()
        chunk = np.ones((128, 1), dtype=np.float32)

        with mock.patch.object(mowik.logging, "warning") as warning:
            recorder._callback(chunk, len(chunk), None, "overflow")
            warning.assert_not_called()
            recorder.finish()

        warning.assert_called_once()

    def test_idle_audio_status_is_logged_outside_realtime_callback(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        chunk = np.ones((128, 1), dtype=np.float32)

        with mock.patch.object(mowik.logging, "warning") as warning:
            recorder._callback(chunk, len(chunk), None, "overflow")
            warning.assert_not_called()
            recorder.log_pending_audio_statuses()

        warning.assert_called_once()

    def test_dead_idle_stream_is_reopened_after_hotplug(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        dead_stream = mock.Mock(active=False)
        recorder._stream = dead_stream
        recorder._stream_monitoring_enabled = True

        with mock.patch.object(recorder, "_start_unlocked") as reopen:
            self.assertTrue(recorder.ensure_stream_alive())

        dead_stream.stop.assert_called_once_with(ignore_errors=False)
        dead_stream.close.assert_called_once_with(ignore_errors=False)
        reopen.assert_called_once_with()

    def test_recovery_cannot_reopen_stream_after_close_wins_the_lock(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        old_stream = mock.Mock(active=False)
        recorder._stream = old_stream
        recorder._stream_monitoring_enabled = True

        class CloseWinsLock:
            def __enter__(self):
                recorder._stream_monitoring_enabled = False
                recorder._stream = None

            def __exit__(self, exc_type, exc, traceback):
                return False

        recorder._stream_lock = CloseWinsLock()
        with mock.patch.object(recorder, "_start_unlocked") as reopen:
            self.assertTrue(recorder.ensure_stream_alive())

        reopen.assert_not_called()
        old_stream.stop.assert_not_called()
        old_stream.close.assert_not_called()

    def test_stream_shutdown_requests_portaudio_errors_for_diagnostics(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        stream = mock.Mock()
        stream.stop.side_effect = RuntimeError("stop failed")
        stream.close.side_effect = RuntimeError("close failed")

        with mock.patch.object(mowik.logging, "exception") as log_error:
            recorder._close_stream_unlocked(stream)

        stream.stop.assert_called_once_with(ignore_errors=False)
        stream.close.assert_called_once_with(ignore_errors=False)
        self.assertEqual(log_error.call_count, 2)

    def test_recovery_escalates_only_after_attempt_and_time_budget(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        recorder._stream = mock.Mock(active=False)
        recorder._stream_monitoring_enabled = True

        with mock.patch.object(
            recorder,
            "_start_unlocked",
            side_effect=RuntimeError("temporary driver reset"),
        ), mock.patch.object(
            mowik.time,
            "monotonic",
            side_effect=[100.0, 102.0, 104.0],
        ), mock.patch.object(
            mowik.logging, "warning"
        ) as warning, mock.patch.object(mowik.logging, "exception") as error:
            self.assertFalse(recorder.ensure_stream_alive())
            self.assertFalse(recorder.ensure_stream_alive())
            self.assertFalse(recorder.ensure_stream_alive())

        self.assertEqual(warning.call_count, 2)
        error.assert_called_once_with(
            "Nie udało się ponownie otworzyć mikrofonu po %d próbach",
            3,
        )

    def test_reopen_discards_previous_stream_pre_roll_and_meter(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 300, "microphone": None}
        )
        old_chunk = np.ones((1024, 1), dtype=np.float32)
        recorder._callback(old_chunk, len(old_chunk), None, 0)
        with recorder._lock:
            recorder._meter_level = 0.75
        dead_stream = mock.Mock(active=False)
        recorder._stream = dead_stream
        recorder._stream_monitoring_enabled = True
        new_stream = mock.Mock(active=True)

        with mock.patch.object(
            mowik,
            "preferred_default_input_device",
            return_value=(7, {"default_samplerate": 48_000.0}),
        ), mock.patch.object(
            recorder, "_open_stream", return_value=new_stream
        ):
            self.assertTrue(recorder.ensure_stream_alive())

        self.assertEqual(recorder.sample_rate, 48_000)
        self.assertEqual(recorder._ring_samples, 0)
        self.assertEqual(list(recorder._ring), [])
        self.assertEqual(recorder.current_level(), 0.0)

    def test_hour_limit_is_clamped_to_bounded_audio_memory(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {
                "pre_roll_ms": 0,
                "microphone": None,
                "maximum_recording_seconds": mowik.MAXIMUM_RECORDING_SECONDS,
            }
        )
        recorder._set_sample_rate(48_000)

        self.assertLess(
            recorder.maximum_recording_samples,
            48_000 * mowik.MAXIMUM_RECORDING_SECONDS,
        )
        self.assertLessEqual(
            recorder.maximum_recording_samples
            * np.dtype(np.float32).itemsize
            * mowik.MAX_RECORDING_PIPELINE_BUFFERS,
            mowik.MAX_RECORDING_BUFFER_BYTES,
        )

    def test_pre_roll_and_extreme_device_rate_share_the_memory_budget(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {
                "pre_roll_ms": 2_000,
                "microphone": None,
                "maximum_recording_seconds": mowik.MAXIMUM_RECORDING_SECONDS,
            }
        )

        recorder._set_sample_rate(10_000_000)

        self.assertEqual(recorder.sample_rate, mowik.MAX_CAPTURE_SAMPLE_RATE)
        total_pipeline_bytes = (
            recorder.maximum_recording_samples + recorder.pre_roll_samples
        ) * np.dtype(np.float32).itemsize * mowik.MAX_RECORDING_PIPELINE_BUFFERS
        self.assertLessEqual(total_pipeline_bytes, mowik.MAX_RECORDING_BUFFER_BYTES)

    def test_abort_discards_capture_without_concatenating(self) -> None:
        recorder = mowik.ContinuousRecorder(
            {"pre_roll_ms": 0, "microphone": None}
        )
        recorder.begin()
        chunk = np.ones((128, 1), dtype=np.float32)
        recorder._callback(chunk, len(chunk), None, 0)

        with mock.patch.object(mowik.np, "concatenate") as concatenate:
            recorder.abort()

        concatenate.assert_not_called()
        self.assertFalse(recorder._recording)
        self.assertEqual(recorder._recorded, [])

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


class SettingsMouseWheelTests(unittest.TestCase):
    """Przewijanie strony nie może po cichu zmieniać zapisywanych ustawień."""

    def _build_settings_window(self):
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover - Tk brakuje tylko w kiosku
            self.skipTest("Tkinter is unavailable")

        captured: dict[str, object] = {}

        def capture_mainloop(root) -> None:
            root.withdraw()
            captured["root"] = root

        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        with mock.patch.object(tk.Tk, "mainloop", capture_mainloop), mock.patch.object(
            mowik,
            "load_config_with_revision",
            return_value=(config, "revision"),
        ), mock.patch.object(mowik, "enable_windows_dpi_awareness"):
            try:
                mowik.run_settings_window()
            except tk.TclError as exc:  # pragma: no cover - brak sesji graficznej
                self.skipTest(f"Tk cannot create windows here: {exc}")

        root = captured.get("root")
        if root is None:  # pragma: no cover - mainloop zawsze jest wywoływany
            self.fail("The settings window never reached its main loop")
        self.addCleanup(root.destroy)
        return root

    def test_wheel_over_spinboxes_and_combos_never_changes_their_value(self) -> None:
        from tkinter import ttk

        root = self._build_settings_window()
        targets: list[object] = []

        def collect(widget) -> None:
            for child in widget.winfo_children():
                if isinstance(child, (ttk.Spinbox, ttk.Combobox)):
                    targets.append(child)
                collect(child)

        collect(root)
        self.assertGreaterEqual(len(targets), 10)

        checked = 0
        for widget in targets:
            if str(widget.cget("state")) == "disabled":
                continue
            before = widget.get()
            for delta in (-120, 120):
                widget.event_generate("<MouseWheel>", delta=delta, x=5, y=5)
            root.update()
            self.assertEqual(widget.get(), before)
            checked += 1
        self.assertGreaterEqual(checked, 10)


class ModelDownloadFeedbackTests(unittest.TestCase):
    def make_progress_class(self) -> tuple[type, list[str]]:
        statuses: list[str] = []
        progress_class = mowik.make_download_progress_tqdm(
            "large-v3",
            statuses.append,
            mowik.Translator("pl"),
        )
        return progress_class, statuses

    def test_progress_bar_reports_percent_without_console_streams(self) -> None:
        progress_class, statuses = self.make_progress_class()

        with mock.patch.object(mowik.sys, "stdout", None), mock.patch.object(
            mowik.sys, "stderr", None
        ):
            bar = progress_class(total=400, unit="B", unit_scale=True)
            bar.update(100)
            bar.update(300)
            bar.close()

        self.assertEqual(len(statuses), 2)
        self.assertIn("25%", statuses[0])
        self.assertIn("100%", statuses[1])

    def test_file_counter_is_hidden_once_byte_progress_is_known(self) -> None:
        progress_class, statuses = self.make_progress_class()

        byte_bar = progress_class(total=400, unit="B", unit_scale=True)
        byte_bar.update(200)
        file_bar = progress_class(total=4, unit="it")
        file_bar.update(1)

        self.assertEqual(len(statuses), 1)
        self.assertIn("50%", statuses[0])

    def test_repeated_percent_does_not_flood_the_status(self) -> None:
        progress_class, statuses = self.make_progress_class()

        bar = progress_class(total=1000, unit="B", unit_scale=True)
        for _ in range(5):
            bar.update(1)

        self.assertEqual(len(statuses), 1)
        self.assertIn("0%", statuses[0])

    def test_download_is_reported_through_the_application_progress_class(
        self,
    ) -> None:
        statuses: list[str] = []

        with mock.patch.object(
            mowik.huggingface_hub,
            "snapshot_download",
            side_effect=mowik.LocalEntryNotFoundError("brak cache"),
        ) as snapshot_download, self.assertRaises(mowik.AppError):
            mowik.load_model_local_first(
                "large-v3",
                {"device": "cpu"},
                statuses.append,
                mowik.Translator("pl"),
            )

        progress_class = snapshot_download.call_args.kwargs["tqdm_class"]
        self.assertTrue(issubclass(progress_class, mowik.base_tqdm))

    def test_failed_download_explains_itself_instead_of_pointing_at_the_log(
        self,
    ) -> None:
        statuses: list[str] = []
        attempts = {"count": 0}

        def snapshot_download(**kwargs):
            attempts["count"] += 1
            if kwargs.get("local_files_only"):
                raise mowik.LocalEntryNotFoundError("brak cache")
            raise OSError("brak sieci")

        with mock.patch.object(
            mowik.huggingface_hub, "snapshot_download", snapshot_download
        ), self.assertRaises(mowik.AppError) as raised:
            mowik.load_model_local_first(
                "large-v3",
                {"device": "cpu"},
                statuses.append,
                mowik.Translator("pl"),
            )

        self.assertEqual(attempts["count"], 2)
        self.assertIn("large-v3", str(raised.exception))
        self.assertIn("internet", str(raised.exception).lower())


class CudaRuntimeSelectionTests(unittest.TestCase):
    """Instalator nie wiezie bibliotek CUDA, więc karta musi wystarczyć."""

    def test_ready_runtime_is_usable(self) -> None:
        with mock.patch.object(mowik, "get_cuda_count", return_value=1):
            self.assertTrue(mowik.cuda_is_usable())

    def test_card_without_libraries_still_counts_as_usable(self) -> None:
        with mock.patch.object(
            mowik, "get_cuda_count", return_value=0
        ), mock.patch.object(
            mowik, "get_cuda_device_count", return_value=1
        ), mock.patch.object(
            mowik.cuda_runtime, "user_runtime_root", return_value=Path("C:/x")
        ):
            self.assertTrue(mowik.cuda_is_usable())

    def test_machine_without_a_card_is_not_usable(self) -> None:
        with mock.patch.object(
            mowik, "get_cuda_count", return_value=0
        ), mock.patch.object(mowik, "get_cuda_device_count", return_value=0):
            self.assertFalse(mowik.cuda_is_usable())

    def test_auto_picks_the_gpu_before_the_libraries_are_downloaded(self) -> None:
        with mock.patch.object(
            mowik, "get_cuda_count", return_value=0
        ), mock.patch.object(
            mowik, "get_cuda_device_count", return_value=1
        ), mock.patch.object(
            mowik.cuda_runtime, "user_runtime_root", return_value=Path("C:/x")
        ):
            _, device, compute_type = mowik.resolve_model_plan({"device": "auto"})

        self.assertEqual(device, "cuda")
        self.assertEqual(compute_type, "float16")

    def test_missing_libraries_are_downloaded_once(self) -> None:
        statuses: list[str] = []

        with mock.patch.object(mowik, "CUDA_DLL_SEARCH_PATHS", ()), mock.patch.object(
            mowik.cuda_runtime, "user_runtime_root", return_value=Path("C:/x")
        ), mock.patch.object(
            mowik.cuda_runtime, "is_runtime_complete", return_value=False
        ), mock.patch.object(
            mowik.cuda_runtime, "ensure_runtime"
        ) as ensure_runtime, mock.patch.object(
            mowik,
            "configure_cuda_dll_search_paths",
            return_value=(Path("C:/x/cublas/bin"),),
        ):
            self.assertTrue(
                mowik.ensure_cuda_runtime_available(statuses.append, mowik.Translator("pl"))
            )

        ensure_runtime.assert_called_once()
        self.assertTrue(any("GPU" in status for status in statuses), statuses)

    def test_failed_download_explains_itself_and_leaves_cpu_available(self) -> None:
        with mock.patch.object(mowik, "CUDA_DLL_SEARCH_PATHS", ()), mock.patch.object(
            mowik.cuda_runtime, "user_runtime_root", return_value=Path("C:/x")
        ), mock.patch.object(
            mowik.cuda_runtime, "is_runtime_complete", return_value=False
        ), mock.patch.object(
            mowik.cuda_runtime,
            "ensure_runtime",
            side_effect=mowik.cuda_runtime.CudaRuntimeError("brak sieci"),
        ):
            with self.assertRaises(mowik.AppError) as raised:
                mowik.ensure_cuda_runtime_available(None, mowik.Translator("pl"))

        message = str(raised.exception)
        self.assertIn("GPU", message)
        self.assertIn("procesorze", message)
        self.assertNotIn("brak sieci", message)

    def test_ready_paths_skip_the_download_entirely(self) -> None:
        with mock.patch.object(
            mowik, "CUDA_DLL_SEARCH_PATHS", (Path("C:/bundled"),)
        ), mock.patch.object(mowik.cuda_runtime, "ensure_runtime") as ensure_runtime:
            self.assertTrue(mowik.ensure_cuda_runtime_available())

        ensure_runtime.assert_not_called()


class MicrophonePickerTests(unittest.TestCase):
    """Skrócona lista ma być krótsza, ale nie może gubić żadnego wejścia."""

    HOST_APIS = [{"name": "MME"}, {"name": "Windows WASAPI"}]
    DEVICES = [
        {
            "name": "Microsoft Sound Mapper - Input",
            "hostapi": 0,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44_100.0,
        },
        {
            # MME ucina nazwy, więc ten sam mikrofon wygląda na dwa urządzenia.
            "name": "Studio Microphone (USB Audio De",
            "hostapi": 0,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44_100.0,
        },
        {
            "name": "Studio Microphone (USB Audio Device)",
            "hostapi": 1,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48_000.0,
        },
        {
            "name": "Laptop Array (Internal)",
            "hostapi": 1,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48_000.0,
        },
    ]

    def build(self, configured=None, **kwargs) -> mowik.MicrophoneChoiceState:
        return mowik.build_microphone_choice_state(
            configured,
            self.DEVICES,
            self.HOST_APIS,
            mowik.Translator("en"),
            **kwargs,
        )

    def test_variants_of_one_input_collapse_into_a_single_choice(self) -> None:
        short = self.build()
        full = self.build(show_all=True)

        self.assertEqual(len(short.values), 3)
        self.assertEqual(len(full.values), 5)

    def test_collapsed_choice_shows_the_name_mme_truncated(self) -> None:
        labels = list(self.build().values)

        self.assertTrue(
            any("Studio Microphone (USB Audio Device)" in label for label in labels),
            labels,
        )
        self.assertFalse(
            any(label.endswith("USB Audio De · 2 in") for label in labels),
            labels,
        )

    def test_collapsed_choice_saves_the_preferred_host_api(self) -> None:
        state = self.build()
        label = next(
            label for label in state.values if "Studio Microphone" in label
        )

        self.assertEqual(state.values[label]["host_api_name"], "Windows WASAPI")

    def test_system_alias_is_replaced_by_the_default_entry(self) -> None:
        short = list(self.build().values)
        full = list(self.build(show_all=True).values)

        self.assertFalse(any("Sound Mapper" in label for label in short), short)
        self.assertTrue(any("Sound Mapper" in label for label in full), full)

    def test_default_windows_input_is_marked(self) -> None:
        labels = list(self.build(default_input_index=3).values)

        marked = [label for label in labels if label.startswith("★")]
        self.assertEqual(len(marked), 1)
        self.assertIn("Laptop Array", marked[0])

    def test_saved_selector_is_never_rewritten_by_the_shorter_list(self) -> None:
        saved = mowik.audio_devices.build_microphone_selector(
            1,
            self.DEVICES,
            self.HOST_APIS,
        )

        state = self.build(saved)

        self.assertEqual(state.values[state.selected_label], saved)
        self.assertEqual(
            mowik.microphone_config_value_for_choice(
                state,
                state.selected_label,
                mowik.Translator("en"),
            ),
            saved,
        )

    def test_same_name_devices_on_one_host_api_stay_separate(self) -> None:
        devices = [
            {
                "name": "USB Microphone",
                "hostapi": 1,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48_000.0,
            },
            {
                "name": "USB Microphone",
                "hostapi": 1,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48_000.0,
            },
        ]

        state = mowik.build_microphone_choice_state(
            None,
            devices,
            self.HOST_APIS,
            mowik.Translator("en"),
        )

        self.assertEqual(len(state.values), 3)
        self.assertEqual(len(state.blocked_labels), 2)


class SettingsMicrophoneUiTests(unittest.TestCase):
    """Okno ustawień musi wstać i faktycznie przełączać widok listy."""

    _window = None

    def setUp(self) -> None:
        # Drugi interpreter Tk w jednym procesie potrafi nie znaleźć tk.tcl,
        # więc oba testy dzielą jedno okno zamiast budować własne.
        if type(self)._window is None:
            type(self)._window = self.build_window()
        self.root = type(self)._window

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._window is not None:
            try:
                cls._window.destroy()
            finally:
                cls._window = None

    def build_window(self):
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover - Tk brakuje tylko w kiosku
            self.skipTest("Tkinter is unavailable")

        captured: dict[str, object] = {}

        def capture_mainloop(root) -> None:
            root.withdraw()
            captured["root"] = root

        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["ui_language"] = "en"
        with mock.patch.object(tk.Tk, "mainloop", capture_mainloop), mock.patch.object(
            mowik,
            "load_config_with_revision",
            return_value=(config, "revision"),
        ), mock.patch.object(
            mowik, "enable_windows_dpi_awareness"
        ), mock.patch.object(
            mowik.sd, "query_devices", return_value=MicrophonePickerTests.DEVICES
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=MicrophonePickerTests.HOST_APIS
        ):
            try:
                mowik.run_settings_window()
            except tk.TclError as exc:  # pragma: no cover - brak sesji graficznej
                self.skipTest(f"Tk cannot create windows here: {exc}")

        root = captured.get("root")
        if root is None:  # pragma: no cover - mainloop zawsze jest wywoływany
            self.fail("The settings window never reached its main loop")
        return root

    @staticmethod
    def walk(widget):
        for child in widget.winfo_children():
            yield child
            yield from SettingsMicrophoneUiTests.walk(child)

    def microphone_combo(self, root):
        from tkinter import ttk

        for widget in self.walk(root):
            if isinstance(widget, ttk.Combobox) and any(
                "Studio Microphone" in str(value) for value in widget["values"]
            ):
                return widget
        self.fail("The microphone picker is missing from the settings window")

    def variants_checkbox(self, root):
        from tkinter import ttk

        for widget in self.walk(root):
            if isinstance(widget, ttk.Checkbutton) and "variant" in str(
                widget.cget("text")
            ):
                return widget
        self.fail("The variants switch is missing from the settings window")

    def test_checkbox_switches_between_the_short_and_the_full_list(self) -> None:
        root = self.root
        combo = self.microphone_combo(root)
        short = list(combo["values"])

        with mock.patch.object(
            mowik.sd, "query_devices", return_value=MicrophonePickerTests.DEVICES
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=MicrophonePickerTests.HOST_APIS
        ):
            self.variants_checkbox(root).invoke()
            root.update_idletasks()
        full = list(combo["values"])

        self.assertEqual(len(short), 3)
        self.assertEqual(len(full), 5)
        self.assertTrue(any("Windows WASAPI" in label for label in full), full)

    def test_level_preview_reports_a_busy_input_instead_of_failing(self) -> None:
        from tkinter import ttk

        root = self.root
        buttons = [
            widget
            for widget in self.walk(root)
            if isinstance(widget, ttk.Button) and str(widget.cget("text")) == "Test"
        ]
        self.assertEqual(len(buttons), 1)

        with mock.patch.object(
            mowik.sd, "InputStream", side_effect=RuntimeError("device busy")
        ), mock.patch.object(
            mowik.sd, "query_devices", return_value=MicrophonePickerTests.DEVICES
        ), mock.patch.object(
            mowik.sd, "query_hostapis", return_value=MicrophonePickerTests.HOST_APIS
        ):
            buttons[0].invoke()
            root.update_idletasks()

        hints = [
            str(widget.cget("text"))
            for widget in self.walk(root)
            if isinstance(widget, ttk.Label) and "another program" in str(
                widget.cget("text")
            )
        ]
        self.assertEqual(len(hints), 1)
        self.assertEqual(str(buttons[0].cget("text")), "Test")


class MicrophoneLevelMonitorTests(unittest.TestCase):
    def test_level_scale_spans_silence_to_a_loud_voice(self) -> None:
        self.assertEqual(mowik.microphone_level_percent(0.0), 0)
        self.assertEqual(mowik.microphone_level_percent(float("nan")), 0)
        self.assertEqual(mowik.microphone_level_percent(1.0), 100)
        quiet = mowik.microphone_level_percent(0.005)
        speech = mowik.microphone_level_percent(0.05)
        self.assertLess(quiet, speech)
        self.assertTrue(0 < quiet < 100)

    def test_monitor_opens_stops_and_releases_the_input(self) -> None:
        monitor = mowik.MicrophoneLevelMonitor()
        stream = mock.Mock()

        with mock.patch.object(mowik.sd, "InputStream", return_value=stream):
            monitor.start(3)
            self.assertTrue(monitor.active)
            monitor._callback(np.full((512, 1), 0.05, dtype=np.float32), 512, None, 0)
            level = monitor.level_percent()
            monitor.stop()

        self.assertGreater(level, 0)
        self.assertFalse(monitor.active)
        stream.close.assert_called_once_with(ignore_errors=True)
        self.assertEqual(monitor.level_percent(), 0)

    def test_failed_open_leaves_no_stream_behind(self) -> None:
        monitor = mowik.MicrophoneLevelMonitor()
        stream = mock.Mock()
        stream.start.side_effect = RuntimeError("device busy")

        with mock.patch.object(mowik.sd, "InputStream", return_value=stream):
            with self.assertRaises(RuntimeError):
                monitor.start(None)

        self.assertFalse(monitor.active)
        stream.close.assert_called_once_with(ignore_errors=True)


class FailureMessageTests(unittest.TestCase):
    def make_app(self) -> mowik.MowikApp:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        return app

    def finish_with(self, error: Exception) -> mock.Mock:
        app = self.make_app()
        with mock.patch.object(
            app, "_finish_dictation_after_tail", side_effect=error
        ), mock.patch.object(app, "beep"), mock.patch.object(
            app, "_release_busy"
        ), mock.patch.object(
            app, "set_status"
        ) as set_status:
            app._finish_dictation_safely()
        return set_status

    def test_known_reason_replaces_the_generic_recording_status(self) -> None:
        set_status = self.finish_with(
            mowik.AppError("Mikrofon jest zajęty przez inną aplikację.")
        )

        self.assertEqual(
            set_status.call_args.args[0],
            "Mikrofon jest zajęty przez inną aplikację.",
        )
        self.assertIn("zajęty", set_status.call_args.kwargs["notify"])

    def test_unknown_error_names_its_type_without_leaking_driver_text(self) -> None:
        set_status = self.finish_with(RuntimeError("secret driver and device details"))

        notification = set_status.call_args.kwargs["notify"]
        self.assertNotIn("secret driver", notification)
        self.assertIn("RuntimeError", notification)
        self.assertEqual(set_status.call_args.args[0], "Błąd nagrywania")

    def test_notification_still_points_at_the_log_for_details(self) -> None:
        set_status = self.finish_with(mowik.AppError("Brak miejsca na dysku."))

        self.assertIn(str(mowik.LOG_PATH), set_status.call_args.kwargs["notify"])


class StartupFailureVisibilityTests(unittest.TestCase):
    def make_app(self) -> mowik.MowikApp:
        config = copy.deepcopy(mowik.DEFAULT_CONFIG)
        config["feedback"]["floating_indicator"] = False
        app = mowik.MowikApp(config)
        app.dictation_indicator = mock.Mock()
        return app

    def test_shortcut_notifies_why_dictation_cannot_start(self) -> None:
        app = self.make_app()
        app._remember_startup_failure("Nie udało się pobrać modelu large-v3.")

        with mock.patch.object(app, "beep"), mock.patch.object(
            app, "set_status"
        ) as set_status:
            app.begin_dictation()

        set_status.assert_called_once()
        self.assertIn("large-v3", set_status.call_args.args[0])
        notification = set_status.call_args.kwargs["notify"]
        self.assertIn("large-v3", notification)
        self.assertTrue(set_status.call_args.kwargs["error"])

    def test_repeated_shortcut_keeps_status_but_stops_notifying(self) -> None:
        app = self.make_app()
        app._remember_startup_failure("Nie udało się pobrać modelu large-v3.")

        with mock.patch.object(app, "beep"), mock.patch.object(
            app, "set_status"
        ) as set_status:
            app.begin_dictation()
            app.begin_dictation()

        self.assertEqual(set_status.call_count, 2)
        self.assertIsNone(set_status.call_args.kwargs["notify"])
        self.assertIn("large-v3", set_status.call_args.args[0])

    def test_loading_model_is_not_reported_as_a_failure(self) -> None:
        app = self.make_app()

        with mock.patch.object(app, "beep"), mock.patch.object(
            app, "set_status"
        ) as set_status:
            app.begin_dictation()

        self.assertEqual(set_status.call_args.kwargs["state"], "idle")
        self.assertNotIn("error", set_status.call_args.kwargs)

    def test_successful_load_clears_the_remembered_failure(self) -> None:
        app = self.make_app()
        app._remember_startup_failure("Nie udało się pobrać modelu large-v3.")

        app._remember_startup_failure(None)

        self.assertEqual(app._startup_failure_message(), (None, False))


if __name__ == "__main__":
    unittest.main()
