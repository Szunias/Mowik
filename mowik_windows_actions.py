"""Bezpieczne, opcjonalne adaptery akcji specyficznych dla Windows.

Moduł nie importuje pywin32 podczas startu. Integracja COM z Eksploratorem jest
ładowana dopiero wtedy, gdy aktywne okno rzeczywiście należy do explorer.exe.
Każda niejednoznaczność kończy się bezpiecznie: brakiem katalogu lub samym
skopiowaniem szkicu do schowka, nigdy wysłaniem go do przypadkowego okna.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import ntpath
import os
from pathlib import Path
import subprocess
import time
from typing import Literal, Optional
import unicodedata


WorkingDirectorySource = Literal["active_explorer", "fixed", "home"]
LaunchStatus = Literal["launched", "failed"]
DraftDeliveryStatus = Literal["copied_only", "rejected", "failed"]

MAX_DRAFT_LENGTH = 8_000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_CLASS = 20
_CREATE_NEW_CONSOLE = 0x00000010
_CSIDL_LOCAL_APPDATA = 0x001C
_CSIDL_PROGRAM_FILES = 0x0026
_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 5, 6})  # removable, fixed, CD-ROM, RAM disk


@dataclass(frozen=True)
class ForegroundContext:
    """Niezmienny kontekst okna aktywnego w chwili rozpoczęcia nagrania."""

    hwnd: int
    pid: int
    process_path: Optional[Path] = None
    explorer_path: Optional[Path] = None
    error: Optional[str] = None
    captured_at_monotonic: float = 0.0

    @property
    def is_valid(self) -> bool:
        return self.hwnd > 0 and self.pid > 0


@dataclass(frozen=True)
class WorkingDirectoryResult:
    source: str
    path: Optional[Path]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.path is not None and self.error is None


@dataclass(frozen=True)
class TerminalHandle:
    """Minimalny, nieuprzywilejowany opis uruchomionego terminala."""

    host: str
    shell: str
    cwd: Path
    launcher_pid: int
    launched_at_monotonic: float


@dataclass(frozen=True)
class TerminalLaunchResult:
    status: LaunchStatus
    handle: Optional[TerminalHandle] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "launched" and self.handle is not None


@dataclass(frozen=True)
class DraftDeliveryResult:
    status: DraftDeliveryStatus
    clipboard_updated: bool = False
    reason: Optional[str] = None


def is_process_elevated() -> bool:
    """Sprawdź token procesu; na Windows każda niepewność jest fail-closed."""

    if os.name != "nt":
        return False

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    token = wintypes.HANDLE()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            return True
        elevation = TOKEN_ELEVATION()
        returned = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_ELEVATION_CLASS,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            return True
        return bool(elevation.TokenIsElevated)
    except (AttributeError, OSError, ValueError):
        return True
    finally:
        if token:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(token)
            except (AttributeError, OSError):
                pass


def _query_process_path(pid: int) -> Optional[Path]:
    if os.name != "nt" or pid <= 0:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query_image.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        process = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return None
        try:
            size = wintypes.DWORD(32_768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not query_image(process, 0, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value)
        finally:
            close_handle(process)
    except (AttributeError, OSError, ValueError):
        return None


def is_local_filesystem_path(value: os.PathLike[str] | str) -> bool:
    """Return true only for a rooted path on a non-network Windows drive."""

    if os.name != "nt":
        return False
    try:
        raw = os.fspath(value)
        if not raw or raw.startswith(("\\\\", "//")):
            return False
        candidate = Path(raw)
        if not candidate.is_absolute() or not candidate.drive:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        drive_root = candidate.drive.rstrip("\\/") + "\\"
        return int(kernel32.GetDriveTypeW(drive_root)) in _LOCAL_DRIVE_TYPES
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return False


def _canonical_local_directory(value: object) -> Optional[Path]:
    """Zwróć istniejący katalog dyskowy; odrzuć URI, UNC i miejsca wirtualne."""

    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.path.expandvars(os.fspath(value))
    if (
        not raw
        or raw != raw.strip()
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in raw
        )
    ):
        return None
    lowered = raw.casefold()
    if (
        raw.startswith(("\\\\", "//"))
        or lowered.startswith(("shell:", "search-ms:", "::{", "file:"))
    ):
        return None
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute() or not candidate.drive:
            return None
        resolved = candidate.resolve(strict=True)
        if (
            str(resolved).startswith(("\\\\", "//"))
            or not is_local_filesystem_path(resolved)
            or not resolved.is_dir()
        ):
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _explorer_path_from_com(hwnd: int) -> Optional[Path]:
    """Odczytaj ścieżkę przez Shell.Application, jeśli pywin32 jest dostępne."""

    initialized = False
    try:
        # Importy są celowo lokalne: typowa ścieżka startowa Mówika ich nie ładuje.
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        initialized = True
        shell = win32com.client.Dispatch("Shell.Application")
        matches: set[Path] = set()
        for window in shell.Windows():
            try:
                if int(window.HWND) != hwnd:
                    continue
                path = _canonical_local_directory(window.Document.Folder.Self.Path)
                if path is not None:
                    matches.add(path)
            except Exception:
                continue
        # Kilka kart Explorera może współdzielić HWND. Nie zgadujemy aktywnej karty.
        return next(iter(matches)) if len(matches) == 1 else None
    except Exception:
        return None
    finally:
        if initialized:
            try:
                pythoncom.CoUninitialize()  # type: ignore[name-defined]
            except Exception:
                pass


def capture_foreground_identity() -> ForegroundContext:
    """Przechwyć HWND/PID bez uruchamiania wolniejszej integracji COM."""
    captured_at = time.monotonic()
    if os.name != "nt":
        return ForegroundContext(0, 0, error="unsupported_platform", captured_at_monotonic=captured_at)
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_foreground = user32.GetForegroundWindow
        get_foreground.argtypes = []
        get_foreground.restype = wintypes.HWND
        get_pid = user32.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD

        hwnd = int(get_foreground() or 0)
        pid_value = wintypes.DWORD(0)
        if hwnd:
            get_pid(hwnd, ctypes.byref(pid_value))
        pid = int(pid_value.value)
        if not hwnd or not pid:
            return ForegroundContext(0, 0, error="foreground_unavailable", captured_at_monotonic=captured_at)

        process_path = _query_process_path(pid)
        return ForegroundContext(
            hwnd=hwnd,
            pid=pid,
            process_path=process_path,
            captured_at_monotonic=captured_at,
        )
    except (AttributeError, OSError, ValueError):
        return ForegroundContext(0, 0, error="foreground_unavailable", captured_at_monotonic=captured_at)


def resolve_explorer_context(context: ForegroundContext) -> ForegroundContext:
    """Uzupełnij wcześniej zamrożoną tożsamość okna o katalog Explorera."""

    if not context.is_valid or context.process_path is None:
        return context
    if ntpath.basename(str(context.process_path)).casefold() != "explorer.exe":
        return context
    path = _explorer_path_from_com(context.hwnd)
    return replace(
        context,
        explorer_path=path,
        error=None if path is not None else "active_explorer_path_unavailable",
    )


def capture_foreground_context() -> ForegroundContext:
    """Przechwyć HWND/PID oraz jednoznaczną ścieżkę aktywnego Explorera."""

    return resolve_explorer_context(capture_foreground_identity())


def resolve_working_directory(
    source: str,
    fixed_path: Optional[os.PathLike[str] | str] = None,
    context: Optional[ForegroundContext] = None,
) -> WorkingDirectoryResult:
    """Rozwiąż katalog bez niejawnego fallbacku dla aktywnego Explorera."""

    normalized = str(source).strip().casefold()
    if normalized == "active_explorer":
        path = _canonical_local_directory(context.explorer_path) if context else None
        if path is None:
            return WorkingDirectoryResult(normalized, None, "active_explorer_path_unavailable")
        return WorkingDirectoryResult(normalized, path)
    if normalized == "fixed":
        path = _canonical_local_directory(fixed_path)
        if path is None:
            return WorkingDirectoryResult(normalized, None, "invalid_fixed_path")
        return WorkingDirectoryResult(normalized, path)
    if normalized == "home":
        path = _canonical_local_directory(Path.home())
        if path is None:
            return WorkingDirectoryResult(normalized, None, "home_path_unavailable")
        return WorkingDirectoryResult(normalized, path)
    return WorkingDirectoryResult(normalized, None, "unsupported_source")


def _is_app_execution_alias(candidate: Path) -> bool:
    """Recognize the AppExecLink reparse point created by Windows packages."""

    if os.name != "nt":
        return False
    try:
        stat = os.lstat(candidate)
    except (OSError, TypeError, ValueError):
        return False
    return (
        getattr(stat, "st_reparse_tag", 0) == _IO_REPARSE_TAG_APPEXECLINK
        and stat.st_size == 0
    )


def _existing_absolute_executable(
    value: os.PathLike[str] | str,
    *,
    allow_app_execution_alias: bool = False,
) -> Optional[str]:
    """Return an existing absolute executable without consulting PATH or cwd."""

    try:
        candidate = Path(value)
        if not candidate.is_absolute() or not candidate.is_file():
            return None
        if allow_app_execution_alias:
            alias_tail = tuple(part.casefold() for part in candidate.parts[-3:])
            if alias_tail != (
                "microsoft",
                "windowsapps",
                "wt.exe",
            ) or not _is_app_execution_alias(candidate):
                return None
            # Launch the package-owned alias itself.  Following a user-created
            # symlink from this writable directory would defeat trusted-shell
            # resolution; a real AppExecLink is activated by CreateProcess.
            return str(candidate)
        try:
            return str(candidate.resolve(strict=True))
        except OSError:
            return None
    except (OSError, RuntimeError, ValueError):
        return None


def _windows_directory() -> Optional[Path]:
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        kernel32.GetWindowsDirectoryW.restype = wintypes.UINT
        buffer = ctypes.create_unicode_buffer(32_768)
        length = int(kernel32.GetWindowsDirectoryW(buffer, len(buffer)))
        if length <= 0 or length >= len(buffer):
            return None
        candidate = Path(buffer.value)
        return candidate if candidate.is_absolute() else None
    except (AttributeError, OSError, ValueError):
        return None


def _known_folder_path(csidl: int) -> Optional[Path]:
    """Resolve a Windows known folder without trusting mutable environment variables."""

    if os.name != "nt":
        return None
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetFolderPathW.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
        ]
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(32_768)
        result = int(shell32.SHGetFolderPathW(None, csidl, None, 0, buffer))
        if result != 0:
            return None
        candidate = Path(buffer.value)
        return candidate if candidate.is_absolute() else None
    except (AttributeError, OSError, ValueError):
        return None


def _find_executable(name: str) -> Optional[str]:
    """Resolve only known terminal executables from trusted absolute locations.

    Shell resolution intentionally never uses ``PATH``.  This prevents a local
    ``cmd.exe``, ``powershell.exe`` or ``wt.exe`` from being selected merely
    because it appears before the real Windows executable.
    """

    normalized = ntpath.basename(str(name)).casefold()
    windows_directory = _windows_directory()
    if normalized == "cmd.exe" and windows_directory is not None:
        return _existing_absolute_executable(
            windows_directory / "System32" / "cmd.exe"
        )
    if normalized == "powershell.exe" and windows_directory is not None:
        return _existing_absolute_executable(
            windows_directory
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
    if normalized == "pwsh.exe":
        program_files = _known_folder_path(_CSIDL_PROGRAM_FILES)
        if program_files is None:
            return None
        return _existing_absolute_executable(
            program_files / "PowerShell" / "7" / "pwsh.exe"
        )
    if normalized == "wt.exe":
        local_app_data = _known_folder_path(_CSIDL_LOCAL_APPDATA)
        if local_app_data is None:
            return None
        return _existing_absolute_executable(
            local_app_data / "Microsoft" / "WindowsApps" / "wt.exe",
            allow_app_execution_alias=True,
        )
    return None


def _resolve_shell(shell: str) -> tuple[Optional[str], Optional[str]]:
    normalized = shell.strip().casefold().replace("windows_powershell", "powershell")
    if normalized in {"", "auto"}:
        for kind, executable in (("pwsh", "pwsh.exe"), ("powershell", "powershell.exe"), ("cmd", "cmd.exe")):
            path = _find_executable(executable)
            if path:
                return kind, path
        return None, None
    executable = {"pwsh": "pwsh.exe", "powershell": "powershell.exe", "cmd": "cmd.exe"}.get(normalized)
    return (normalized, _find_executable(executable)) if executable else (None, None)


def _shell_arguments(kind: str, executable: str, *, embedded: bool) -> list[str]:
    if kind == "cmd":
        return [executable, "/d", "/q"] if embedded else [executable, "/d", "/q"]
    return [executable, "-NoLogo", "-NoProfile"]


def _classic_arguments(kind: str, executable: str) -> list[str]:
    if kind == "cmd":
        return [executable, "/d", "/q"]
    return [executable, "-NoLogo", "-NoProfile", "-NoExit"]


def launch_terminal(
    host: str,
    shell: str,
    cwd: os.PathLike[str] | str,
) -> TerminalLaunchResult:
    """Uruchom widoczny terminal; katalog przekazuj wyłącznie jako cwd/argument."""

    if os.name != "nt":
        return TerminalLaunchResult("failed", error="unsupported_platform")
    directory = _canonical_local_directory(cwd)
    if directory is None:
        return TerminalLaunchResult("failed", error="invalid_working_directory")

    requested_host = host.strip().casefold().replace("classic", "console")
    requested_shell = shell.strip().casefold()
    if requested_host in {"powershell", "pwsh", "cmd"}:
        requested_shell, requested_host = requested_host, "console"
    if requested_host not in {"auto", "windows_terminal", "wt", "console"}:
        return TerminalLaunchResult("failed", error="unsupported_terminal_host")

    shell_kind: Optional[str]
    shell_path: Optional[str]
    if requested_shell in {"", "default"} and requested_host in {
        "auto",
        "windows_terminal",
        "wt",
    }:
        shell_kind, shell_path = "default", None
    else:
        shell_kind, shell_path = _resolve_shell(
            "auto" if requested_shell in {"", "default"} else requested_shell
        )
        if shell_kind is None:
            return TerminalLaunchResult("failed", error="requested_shell_unavailable")

    use_wt = requested_host in {"auto", "windows_terminal", "wt"}
    wt_path = _find_executable("wt.exe") if use_wt else None
    if requested_host in {"windows_terminal", "wt"} and not wt_path:
        return TerminalLaunchResult("failed", error="windows_terminal_unavailable")

    if wt_path:
        arguments = [
            wt_path,
            "-w",
            "new",
            "new-tab",
            "--startingDirectory",
            str(directory),
        ]
        if shell_kind != "default" and shell_path:
            arguments.extend(_shell_arguments(shell_kind or "", shell_path, embedded=True))
        try:
            process = subprocess.Popen(
                arguments,
                cwd=str(directory),
                close_fds=True,
                shell=False,
            )
        except OSError:
            if requested_host != "auto":
                return TerminalLaunchResult("failed", error="terminal_launch_failed")
        else:
            handle = TerminalHandle(
                host="windows_terminal",
                shell=shell_kind or "default",
                cwd=directory,
                launcher_pid=process.pid,
                launched_at_monotonic=time.monotonic(),
            )
            return TerminalLaunchResult("launched", handle)

    # Auto fallback: klasyczna, widoczna konsola z PowerShell/pwsh/cmd.
    if shell_kind in {None, "default"} or not shell_path:
        shell_kind, shell_path = _resolve_shell("auto")
    if shell_kind is None or shell_path is None:
        return TerminalLaunchResult("failed", error="terminal_unavailable")
    arguments = _classic_arguments(shell_kind, shell_path)
    try:
        process = subprocess.Popen(
            arguments,
            cwd=str(directory),
            creationflags=_CREATE_NEW_CONSOLE,
            close_fds=True,
            shell=False,
        )
    except OSError:
        return TerminalLaunchResult("failed", error="terminal_launch_failed")
    handle = TerminalHandle(
        host="console",
        shell=shell_kind,
        cwd=directory,
        launcher_pid=process.pid,
        launched_at_monotonic=time.monotonic(),
    )
    return TerminalLaunchResult("launched", handle)


def _validate_draft(text: object) -> Optional[str]:
    if not isinstance(text, str) or not text or len(text) > MAX_DRAFT_LENGTH:
        return "invalid_length"
    if any(ch in "\r\n" or unicodedata.category(ch).startswith("C") for ch in text):
        return "control_character_rejected"
    if any(ch in "\u2028\u2029" for ch in text):
        return "multiline_draft_rejected"
    return None


def _copy_to_windows_clipboard(text: str) -> bool:
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        payload = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, payload, len(payload))
        kernel32.GlobalUnlock(handle)
        opened = False
        for _ in range(6):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            kernel32.GlobalFree(handle)
            return False
        transferred = False
        try:
            if not user32.EmptyClipboard():
                return False
            transferred = bool(user32.SetClipboardData(13, handle))  # CF_UNICODETEXT
            return transferred
        finally:
            user32.CloseClipboard()
            if not transferred:
                kernel32.GlobalFree(handle)
    except (AttributeError, OSError, ValueError):
        return False


def _clipboard_fallback(reason: str, text: str) -> DraftDeliveryResult:
    if _copy_to_windows_clipboard(text):
        return DraftDeliveryResult("copied_only", clipboard_updated=True, reason=reason)
    return DraftDeliveryResult("failed", reason=f"{reason}:clipboard_unavailable")


def deliver_terminal_draft(
    handle: TerminalHandle,
    text: str,
) -> DraftDeliveryResult:
    """Copy one safe line to the clipboard; never type or submit it."""

    validation_error = _validate_draft(text)
    if validation_error:
        return DraftDeliveryResult("rejected", reason=validation_error)
    if os.name != "nt" or not isinstance(handle, TerminalHandle):
        return DraftDeliveryResult("failed", reason="unsupported_target")
    return _clipboard_fallback("clipboard_mode", text)


__all__ = [
    "DraftDeliveryResult",
    "ForegroundContext",
    "TerminalHandle",
    "TerminalLaunchResult",
    "WorkingDirectoryResult",
    "capture_foreground_context",
    "capture_foreground_identity",
    "deliver_terminal_draft",
    "launch_terminal",
    "is_process_elevated",
    "is_local_filesystem_path",
    "resolve_explorer_context",
    "resolve_working_directory",
]
