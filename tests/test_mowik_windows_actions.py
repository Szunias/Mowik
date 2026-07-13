from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mowik_windows_actions as windows_actions


def terminal_handle() -> windows_actions.TerminalHandle:
    return windows_actions.TerminalHandle(
        host="windows_terminal",
        shell="default",
        cwd=Path(r"C:\Work\Mowik"),
        launcher_pid=303,
        launched_at_monotonic=42.0,
    )


class WorkingDirectoryResolutionTests(unittest.TestCase):
    def test_active_explorer_uses_only_the_captured_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            expected = Path(temporary_directory).resolve()
            context = windows_actions.ForegroundContext(
                hwnd=10,
                pid=20,
                process_path=Path(r"C:\Windows\explorer.exe"),
                explorer_path=expected,
            )

            result = windows_actions.resolve_working_directory(
                "active_explorer",
                context=context,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.source, "active_explorer")
        self.assertEqual(result.path, expected)
        self.assertIsNone(result.error)

    def test_active_explorer_without_a_captured_path_does_not_fallback(self) -> None:
        context = windows_actions.ForegroundContext(
            hwnd=10,
            pid=20,
            process_path=Path(r"C:\Windows\explorer.exe"),
        )
        with mock.patch.object(
            windows_actions.Path,
            "home",
            side_effect=AssertionError("home fallback must not be used"),
        ):
            result = windows_actions.resolve_working_directory(
                "active_explorer",
                context=context,
            )

        self.assertFalse(result.ok)
        self.assertIsNone(result.path)
        self.assertEqual(result.error, "active_explorer_path_unavailable")

    def test_fixed_directory_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            expected = Path(temporary_directory).resolve()
            valid = windows_actions.resolve_working_directory("fixed", expected)
            missing = windows_actions.resolve_working_directory(
                "fixed",
                expected / "missing",
            )

        self.assertTrue(valid.ok)
        self.assertEqual(valid.path, expected)
        self.assertFalse(missing.ok)
        self.assertIsNone(missing.path)
        self.assertEqual(missing.error, "invalid_fixed_path")

    def test_home_is_resolved_explicitly_and_fails_closed_when_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            expected = Path(temporary_directory).resolve()
            with mock.patch.object(windows_actions.Path, "home", return_value=expected):
                valid = windows_actions.resolve_working_directory("home")
            with mock.patch.object(
                windows_actions.Path,
                "home",
                return_value=Path("relative-home"),
            ):
                invalid = windows_actions.resolve_working_directory("home")

        self.assertTrue(valid.ok)
        self.assertEqual(valid.path, expected)
        self.assertFalse(invalid.ok)
        self.assertIsNone(invalid.path)
        self.assertEqual(invalid.error, "home_path_unavailable")

    def test_virtual_unc_and_relative_directories_are_rejected(self) -> None:
        unsafe_paths = (
            r"shell:Downloads",
            r"search-ms:query=Mowik",
            r"::{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
            r"file:///C:/Windows",
            r"\\server\share",
            r"//server/share",
            "tests",
        )

        for unsafe_path in unsafe_paths:
            with self.subTest(path=unsafe_path):
                result = windows_actions.resolve_working_directory(
                    "fixed",
                    unsafe_path,
                )
                self.assertFalse(result.ok)
                self.assertIsNone(result.path)
                self.assertEqual(result.error, "invalid_fixed_path")

    def test_unknown_source_never_falls_back_to_home(self) -> None:
        with mock.patch.object(
            windows_actions.Path,
            "home",
            side_effect=AssertionError("home fallback must not be used"),
        ):
            result = windows_actions.resolve_working_directory("automatic")

        self.assertFalse(result.ok)
        self.assertIsNone(result.path)
        self.assertEqual(result.error, "unsupported_source")


class ExecutableAndPrivilegeSafetyTests(unittest.TestCase):
    def test_shell_resolution_never_falls_back_to_path(self) -> None:
        with (
            mock.patch.object(windows_actions, "_windows_directory", return_value=None),
            mock.patch.object(windows_actions, "_known_folder_path", return_value=None),
            mock.patch.dict(
                windows_actions.os.environ,
                {
                    "PATH": r"C:\Untrusted",
                    "ProgramW6432": r"C:\Untrusted",
                    "ProgramFiles": r"C:\Untrusted",
                    "LOCALAPPDATA": r"C:\Untrusted",
                },
                clear=False,
            ),
        ):
            self.assertIsNone(windows_actions._find_executable("cmd.exe"))
            self.assertIsNone(windows_actions._find_executable("powershell.exe"))
            self.assertIsNone(windows_actions._find_executable("pwsh.exe"))
            self.assertIsNone(windows_actions._find_executable("wt.exe"))
            self.assertIsNone(windows_actions._find_executable("unknown.exe"))

    def test_windows_store_alias_is_accepted_only_when_explicitly_allowed(self) -> None:
        alias = Path(r"C:\Users\Test\AppData\Local\Microsoft\WindowsApps\wt.exe")
        with (
            mock.patch.object(windows_actions.Path, "is_file", return_value=True),
            mock.patch.object(
                windows_actions.Path,
                "resolve",
                side_effect=OSError("app execution alias"),
            ),
        ):
            self.assertIsNone(windows_actions._existing_absolute_executable(alias))
            self.assertEqual(
                windows_actions._existing_absolute_executable(
                    alias,
                    allow_app_execution_alias=True,
                ),
                str(alias),
            )
            self.assertIsNone(
                windows_actions._existing_absolute_executable(
                    Path(r"C:\Users\Test\AppData\Local\WindowsApps\evil.exe"),
                    allow_app_execution_alias=True,
                )
            )

    def test_pwsh_and_terminal_roots_come_from_windows_known_folders(self) -> None:
        program_files = Path(r"C:\Program Files")
        local_app_data = Path(r"C:\Users\Test\AppData\Local")

        def known_folder(csidl: int):
            return {
                windows_actions._CSIDL_PROGRAM_FILES: program_files,
                windows_actions._CSIDL_LOCAL_APPDATA: local_app_data,
            }.get(csidl)

        with mock.patch.object(
            windows_actions,
            "_known_folder_path",
            side_effect=known_folder,
        ), mock.patch.object(
            windows_actions,
            "_existing_absolute_executable",
            side_effect=lambda path, **kwargs: str(path),
        ) as existing:
            pwsh = windows_actions._find_executable("pwsh.exe")
            terminal = windows_actions._find_executable("wt.exe")

        self.assertEqual(
            pwsh,
            str(program_files / "PowerShell" / "7" / "pwsh.exe"),
        )
        self.assertEqual(
            terminal,
            str(local_app_data / "Microsoft" / "WindowsApps" / "wt.exe"),
        )
        self.assertEqual(existing.call_count, 2)
        self.assertFalse(existing.call_args_list[0].kwargs)
        self.assertTrue(existing.call_args_list[1].kwargs["allow_app_execution_alias"])

    def test_windows_elevation_probe_failure_is_fail_closed(self) -> None:
        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(
                windows_actions.ctypes,
                "WinDLL",
                side_effect=OSError("token query unavailable"),
            ),
        ):
            self.assertTrue(windows_actions.is_process_elevated())


class TerminalLaunchTests(unittest.TestCase):
    def test_terminal_argument_templates_never_execute_or_hide_commands(self) -> None:
        executables = {
            "cmd": r"C:\Windows\System32\cmd.exe",
            "powershell": (
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            ),
            "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
        }
        for kind, executable in executables.items():
            for embedded in (False, True):
                with self.subTest(kind=kind, embedded=embedded):
                    arguments = (
                        windows_actions._shell_arguments(
                            kind,
                            executable,
                            embedded=embedded,
                        )
                        if embedded
                        else windows_actions._classic_arguments(kind, executable)
                    )
                    self.assertEqual(arguments[0], executable)
                    normalized = " ".join(arguments[1:]).casefold()
                    for forbidden in (
                        "/c",
                        "/k",
                        "-command",
                        "-encodedcommand",
                        "-executionpolicy",
                        "-windowstyle",
                    ):
                        self.assertNotIn(forbidden, normalized)

    def test_windows_terminal_uses_argument_list_cwd_and_shell_false(self) -> None:
        directory = Path(r"C:\Work\Mowik")
        wt_path = r"C:\Program Files\WindowsApps\wt.exe"
        process = mock.Mock(pid=4321)

        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(
                windows_actions,
                "_canonical_local_directory",
                return_value=directory,
            ),
            mock.patch.object(
                windows_actions,
                "_find_executable",
                side_effect=lambda name: wt_path if name == "wt.exe" else None,
            ),
            mock.patch.object(
                windows_actions,
                "_resolve_shell",
            ) as resolve_shell,
            mock.patch.object(
                windows_actions.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
        ):
            result = windows_actions.launch_terminal(
                "windows_terminal",
                "default",
                directory,
            )

        self.assertTrue(result.ok)
        resolve_shell.assert_not_called()
        popen.assert_called_once()
        positional, keyword = popen.call_args
        self.assertEqual(
            positional[0],
            [
                wt_path,
                "-w",
                "new",
                "new-tab",
                "--startingDirectory",
                str(directory),
            ],
        )
        self.assertIsInstance(positional[0], list)
        self.assertEqual(keyword["cwd"], str(directory))
        self.assertIs(keyword["shell"], False)
        self.assertTrue(keyword["close_fds"])
        assert result.handle is not None
        self.assertEqual(result.handle.host, "windows_terminal")
        self.assertEqual(result.handle.shell, "default")

    def test_classic_host_with_default_shell_maps_to_a_visible_console(self) -> None:
        directory = Path(r"C:\Work\Mowik")
        cmd_path = r"C:\Windows\System32\cmd.exe"
        process = mock.Mock(pid=8765)

        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(
                windows_actions,
                "_canonical_local_directory",
                return_value=directory,
            ),
            mock.patch.object(
                windows_actions,
                "_resolve_shell",
                return_value=("cmd", cmd_path),
            ) as resolve_shell,
            mock.patch.object(
                windows_actions,
                "_find_executable",
                side_effect=AssertionError("classic host must not probe wt.exe"),
            ),
            mock.patch.object(
                windows_actions.subprocess,
                "Popen",
                return_value=process,
            ) as popen,
        ):
            result = windows_actions.launch_terminal(
                "classic",
                "default",
                directory,
            )

        self.assertTrue(result.ok)
        resolve_shell.assert_called_once_with("auto")
        positional, keyword = popen.call_args
        self.assertEqual(
            positional[0],
            [cmd_path, "/d", "/q"],
        )
        self.assertIsInstance(positional[0], list)
        self.assertEqual(keyword["cwd"], str(directory))
        self.assertIs(keyword["shell"], False)
        self.assertEqual(keyword["creationflags"], windows_actions._CREATE_NEW_CONSOLE)
        self.assertTrue(keyword["close_fds"])
        assert result.handle is not None
        self.assertEqual(result.handle.host, "console")
        self.assertEqual(result.handle.shell, "cmd")

    def test_invalid_directory_is_rejected_before_process_creation(self) -> None:
        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(
                windows_actions,
                "_canonical_local_directory",
                return_value=None,
            ),
            mock.patch.object(windows_actions.subprocess, "Popen") as popen,
        ):
            result = windows_actions.launch_terminal(
                "auto",
                "default",
                r"C:\missing",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_working_directory")
        popen.assert_not_called()


class TerminalDraftDeliveryTests(unittest.TestCase):
    def test_default_delivery_is_clipboard_only(self) -> None:
        handle = terminal_handle()
        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(
                windows_actions,
                "_copy_to_windows_clipboard",
                return_value=True,
            ) as copy_text,
        ):
            result = windows_actions.deliver_terminal_draft(handle, "git status")

        self.assertEqual(result.status, "copied_only")
        self.assertTrue(result.clipboard_updated)
        self.assertEqual(result.reason, "clipboard_mode")
        copy_text.assert_called_once_with("git status")

    def test_multiline_and_control_characters_are_rejected_before_any_io(self) -> None:
        unsafe_drafts = (
            "echo first\rwhoami",
            "echo first\nwhoami",
            "echo\x00hidden",
            "echo\tvalue",
            "echo\x1b[31m",
            "echo first\u2028whoami",
            "echo first\u2029whoami",
        )
        handle = terminal_handle()

        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(windows_actions, "_copy_to_windows_clipboard") as copy_text,
        ):
            for unsafe_draft in unsafe_drafts:
                with self.subTest(draft=repr(unsafe_draft)):
                    result = windows_actions.deliver_terminal_draft(
                        handle,
                        unsafe_draft,
                    )
                    self.assertEqual(result.status, "rejected")

        copy_text.assert_not_called()

    def test_draft_length_boundaries_and_clipboard_failure_fail_closed(self) -> None:
        handle = terminal_handle()
        with (
            mock.patch.object(windows_actions.os, "name", "nt"),
            mock.patch.object(
                windows_actions,
                "_copy_to_windows_clipboard",
                side_effect=(True, False),
            ) as copy_text,
        ):
            accepted = windows_actions.deliver_terminal_draft(handle, "x" * 8_000)
            failed = windows_actions.deliver_terminal_draft(handle, "safe")
            oversized = windows_actions.deliver_terminal_draft(handle, "x" * 8_001)
            empty = windows_actions.deliver_terminal_draft(handle, "")
            invalid_handle = windows_actions.deliver_terminal_draft(object(), "safe")

        self.assertEqual(accepted.status, "copied_only")
        self.assertTrue(accepted.clipboard_updated)
        self.assertEqual(failed.status, "failed")
        self.assertFalse(failed.clipboard_updated)
        self.assertEqual(oversized.status, "rejected")
        self.assertEqual(empty.status, "rejected")
        self.assertEqual(invalid_handle.status, "failed")
        self.assertEqual(copy_text.call_count, 2)

    def test_terminal_adapter_has_no_targeted_input_or_window_focus_helpers(self) -> None:
        for name in (
            "_send_unicode_text",
            "_focus_verified_window",
            "_find_terminal_window",
            "_enumerate_windows",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(windows_actions, name))


class ExplorerIntegrationFailClosedTests(unittest.TestCase):
    def test_missing_pywin32_returns_no_explorer_path(self) -> None:
        with mock.patch.dict(
            sys.modules,
            {
                "pythoncom": None,
                "win32com": None,
                "win32com.client": None,
            },
        ):
            result = windows_actions._explorer_path_from_com(123)

        self.assertIsNone(result)

    def test_missing_pywin32_marks_explorer_context_unavailable(self) -> None:
        context = windows_actions.ForegroundContext(
            hwnd=123,
            pid=456,
            process_path=Path(r"C:\Windows\explorer.exe"),
        )
        with mock.patch.dict(
            sys.modules,
            {
                "pythoncom": None,
                "win32com": None,
                "win32com.client": None,
            },
        ):
            resolved = windows_actions.resolve_explorer_context(context)

        self.assertIsNone(resolved.explorer_path)
        self.assertEqual(resolved.error, "active_explorer_path_unavailable")
        self.assertEqual(resolved.hwnd, context.hwnd)
        self.assertEqual(resolved.pid, context.pid)


if __name__ == "__main__":
    unittest.main()
