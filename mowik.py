"""Mówik — lokalne dyktowanie push-to-talk dla Windows.

Przytrzymaj skonfigurowany klawisz, mów, puść. Nagranie jest lokalnie
transkrybowane przez faster-whisper i wklejane do aktywnego pola tekstowego.
Opcjonalne czyszczenie lokalnym LLM przez Ollama jest domyślnie wyłączone.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import io
import ctypes
from ctypes import wintypes
import difflib
import gc
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Mapping, Optional, Sequence


APP_NAME = "Mowik"
APP_DISPLAY_NAME = "Mówik"
APP_VERSION = "2.8.0"


def _run_early_read_only_probe(
    *,
    executed_as_main: Optional[bool] = None,
    argv: Optional[list[str]] = None,
) -> None:
    """Handle build probes without importing the application dependency stack."""

    if executed_as_main is None:
        executed_as_main = __name__ == "__main__"
    if not executed_as_main:
        return
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--version"]:
        print(f"{APP_DISPLAY_NAME} {APP_VERSION}")
        raise SystemExit(0)
    if arguments != ["--runtime-gui-smoke-test"]:
        return
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
    except Exception as exc:
        print(f"Tk GUI smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(0)


def _early_windows_process_is_elevated() -> bool:
    """Query elevation using only the standard library; uncertainty denies."""

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
            0x0008,  # TOKEN_QUERY
            ctypes.byref(token),
        ):
            return True
        elevation = TOKEN_ELEVATION()
        returned = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(
            token,
            20,  # TokenElevation
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


def _reject_elevated_runtime_before_native_imports(
    *,
    executed_as_main: Optional[bool] = None,
    argv: Optional[list[str]] = None,
) -> None:
    """Stop the elevated app before user-writable native packages are loaded."""

    if executed_as_main is None:
        executed_as_main = __name__ == "__main__"
    if not executed_as_main or os.name != "nt":
        return
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--version"], ["--runtime-gui-smoke-test"]):
        return
    if not _early_windows_process_is_elevated():
        return
    message = (
        "Ze względów bezpieczeństwa Mówika nie można uruchamiać z "
        "uprawnieniami administratora. Uruchom program normalnie.\n\n"
        "For security reasons, Mówik cannot run with administrator "
        "privileges. Start the application normally."
    )
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Mówik — uruchomienie zablokowane / startup blocked",
            0x10,
        )
    except (AttributeError, OSError):
        print(message, file=sys.stderr)
    raise SystemExit(1)


_run_early_read_only_probe()
_reject_elevated_runtime_before_native_imports()

import mowik_commands as command_engine
import mowik_audio_devices as audio_devices
import mowik_cuda as cuda_runtime
from mowik_i18n import Translator, resolve_ui_language
import mowik_windows_actions as windows_actions


_CUDA_DLL_DIRECTORY_HANDLES: list[Any] = []
_CUDA_DLL_HANDLES: list[Any] = []


def enable_windows_dpi_awareness() -> None:
    """Włącz ostre renderowanie interfejsu na monitorach HiDPI.

    Tk 8.6.12 nie skaluje niezawodnie układu po przeniesieniu okna między
    monitorami, dlatego używamy stabilnego trybu SystemAware. Wywołanie musi
    nastąpić przed utworzeniem pierwszego okna Tk; manifest ustawia ten sam
    tryb w wersji spakowanej, a fallback obejmuje uruchamianie ze źródeł.
    """
    if os.name != "nt":
        return

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        if set_context(ctypes.c_void_p(-2)):  # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE
            return
        # Manifest ustawia świadomość DPI przed startem Pythona. Windows
        # zwraca wtedy ERROR_ACCESS_DENIED i starszych API nie należy wywoływać.
        if ctypes.get_last_error() == 5:
            return
        logging.debug(
            "Windows odrzucił ustawienie SystemAware (kod %s)",
            ctypes.get_last_error(),
        )
    except (AttributeError, OSError):
        logging.debug("Nie udało się włączyć obsługi DPI", exc_info=True)


def configure_cuda_dll_search_paths() -> tuple[Path, ...]:
    """Udostępnij CTranslate2 biblioteki CUDA dołączone przez pakiety NVIDIA.

    Python 3.8+ na Windowsie nie przeszukuje automatycznie katalogów DLL
    z pakietów ``nvidia-*``. Zachowujemy uchwyty zwrócone przez
    ``os.add_dll_directory`` przez cały czas życia procesu; ich zwolnienie
    natychmiast usunęłoby katalog z wyszukiwania.
    """
    if os.name != "nt":
        return ()

    roots: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root) / "nvidia")
    executable_root = Path(sys.executable).resolve().parent
    roots.extend(
        (
            executable_root / "_internal" / "nvidia",
            executable_root / "nvidia",
            Path(sys.prefix) / "Lib" / "site-packages" / "nvidia",
        )
    )
    # Instalator nie zawiera bibliotek CUDA; wydania pobierają je na żądanie
    # do profilu użytkownika.
    downloaded_root = cuda_runtime.user_runtime_root()
    if downloaded_root is not None:
        roots.append(downloaded_root)

    # Wybieramy dokładnie jednego dostawcę cuBLAS. AddDllDirectory nie
    # gwarantuje kolejności między wieloma katalogami, a zmieszanie wersji
    # cublas/cublasLt kończy się losowymi błędami podczas inferencji.
    provider: Optional[Path] = None
    provider_root: Optional[Path] = None
    for root in roots:
        candidate = root / "cublas" / "bin"
        if all((candidate / name).is_file() for name in ("cublasLt64_12.dll", "cublas64_12.dll")):
            provider = candidate
            provider_root = root
            break
    if provider is None:
        cuda_path = os.environ.get("CUDA_PATH")
        candidate = Path(cuda_path) / "bin" if cuda_path else None
        if candidate is not None and all(
            (candidate / name).is_file()
            for name in ("cublasLt64_12.dll", "cublas64_12.dll")
        ):
            provider = candidate
    if provider is None:
        return ()

    candidates = [provider]
    if provider_root is not None:
        candidates.extend(
            provider_root / package / "bin"
            for package in ("cuda_nvrtc", "cuda_runtime")
        )
    added: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        try:
            handle = os.add_dll_directory(str(resolved))
        except (AttributeError, OSError):
            continue
        _CUDA_DLL_DIRECTORY_HANDLES.append(handle)
        added.append(resolved)

    try:
        # Jawne pełne ścieżki i kolejność Lt -> BLAS blokują przypadkowe
        # podchwycenie starszej biblioteki z PATH producenta laptopa.
        for dll_name in ("cublasLt64_12.dll", "cublas64_12.dll"):
            _CUDA_DLL_HANDLES.append(ctypes.WinDLL(str(provider / dll_name)))
    except OSError:
        _CUDA_DLL_HANDLES.clear()

    if added:
        current_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(
            [*(str(path) for path in added), current_path]
        )
    return tuple(added)


CUDA_DLL_SEARCH_PATHS = configure_cuda_dll_search_paths()


def console_streams_available() -> bool:
    """W trybie okienkowym stdout i stderr są puste, więc tqdm nie ma gdzie pisać."""

    return all(
        getattr(stream, "write", None) is not None
        for stream in (sys.stdout, sys.stderr)
    )


if not console_streams_available():
    # Pasek postępu Hugging Face pisze do stdout/stderr.  Bez konsoli pobieranie
    # brakujących plików modelu kończyło się AttributeError, więc aplikacja
    # startowała bez modelu i skrót dyktowania nic nie robił.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
import huggingface_hub
from huggingface_hub.errors import LocalEntryNotFoundError
from huggingface_hub.utils import disable_progress_bars
from tqdm import tqdm as base_tqdm

if not console_streams_available():
    # Zmienna środowiskowa działa tylko przy pierwszym imporcie biblioteki, więc
    # dokładamy jawne wyłączenie dla builda, który zaimportował ją wcześniej.
    disable_progress_bars()

import ctranslate2
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pynput import keyboard, mouse
import pystray
import pyperclip


MUTEX_NAME = r"Local\MowikLocalDictation"
SETTINGS_MUTEX_NAME = r"Local\MowikLocalDictation.Settings"
CONFIG_WRITE_MUTEX_NAME = r"Local\MowikLocalDictation.ConfigWrite"
PROCESS_STARTED_AT_NS = time.time_ns()
SAMPLE_RATE = 16_000
# Podpis ('MOWI') wstawiany w `dwExtraInfo` każdego zdarzenia klawiatury, które
# sami wstrzykujemy przez SendInput. Globalny hook WH_KEYBOARD_LL widzi także
# nasze własne znaki, a Windows blokuje SendInput do powrotu procedury hooka —
# przez co wpisywanie kosztowało 2,5 raza więcej niż powinno (2,61 ms na znak
# wobec 1,04 ms bez hooka). Podpis pozwala hookowi odrzucić własne zdarzenia
# zanim dotkną Pythona. Filtrujemy po własnym podpisie, a nie po fladze
# LLKHF_INJECTED, bo tamtą ustawiają też RDP, KVM i klawiatura ekranowa —
# odrzucanie ich zablokowałoby skrót użytkownika.
MOWIK_INJECTED_INPUT_SIGNATURE = 0x4D4F5749
DEFAULT_MAXIMUM_RECORDING_SECONDS = 300
MAXIMUM_RECORDING_SECONDS = 3_600
MAX_RECORDING_BUFFER_BYTES = 256 * 1024 * 1024
MAX_RECORDING_PIPELINE_BUFFERS = 3
MAX_CAPTURE_SAMPLE_RATE = 384_000
MAX_CONFIG_FILE_BYTES = 16 * 1024 * 1024
MAX_DICTIONARY_FILE_BYTES = 8 * 1024 * 1024
MAX_PENDING_EXPLORER_CONTEXTS = 2
MAX_OLLAMA_TIMEOUT_SECONDS = 600
RESTART_ACK_TIMEOUT_SECONDS = 2.0
RESTART_MUTEX_WAIT_SECONDS = 15.0
RESTART_STARTED_TIMEOUT_SECONDS = 25.0
CLIPBOARD_WRITE_RETRY_DELAYS = (0.05, 0.10)
STARTUP_FAILURE_NOTIFY_INTERVAL = 15.0
# WASAPI podaje pełne nazwy i natywną częstotliwość; MME tnie nazwy i dokłada
# opóźnienie, więc na skróconej liście jest ostatnim wyborem.
MICROPHONE_HOST_API_PREFERENCE = ("wasapi", "directsound", "mme", "wdm-ks")
# Windows powiela domyślne wejście pod własnymi nazwami. Na skróconej liście
# zastępuje je pozycja „Domyślny mikrofon Windows”; w pełnej nadal są widoczne.
SYSTEM_MICROPHONE_ALIASES = frozenset(
    {
        "microsoft sound mapper - input",
        "mapowanie dźwięku microsoft - input",
        "primary sound capture driver",
        "podstawowy sterownik przechwytywania dźwięku",
    }
)
MODEL_ALLOW_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)
CUSTOM_COMMAND_ACTIONS = {
    "paste_text",
    "open",
    "open_terminal",
}
MAX_CUSTOM_COMMANDS = 200
MAX_CUSTOM_COMMAND_PHRASE_LENGTH = 120
MAX_CUSTOM_COMMAND_VALUE_LENGTH = 50_000
MAX_CUSTOM_COMMAND_LINE_LENGTH = 8_000
MAX_CUSTOM_OPEN_TARGET_LENGTH = command_engine.MAX_OPEN_TARGET_LENGTH
MAX_CUSTOM_COMMAND_CONTEXT_AGE_SECONDS = 120.0
BLOCKED_CUSTOM_OPEN_SUFFIXES = command_engine.BLOCKED_OPEN_SUFFIXES
MODEL_SOURCES: dict[str, tuple[str, str]] = {
    "tiny": (
        "Systran/faster-whisper-tiny",
        "d90ca5fe260221311c53c58e660288d3deb8d356",
    ),
    "small": (
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
    ),
    "large-v3-turbo": (
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    ),
    "large-v3": (
        "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
    ),
}
LEGACY_REMOVED_MODELS = frozenset({"base", "medium"})

APP_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", APP_ROOT))

APPDATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
LOCALDATA_DIR = Path(os.environ.get("LOCALAPPDATA", APPDATA_DIR)) / APP_NAME
CONFIG_PATH = APPDATA_DIR / "config.json"
DICTIONARY_PATH = APPDATA_DIR / "slownik.txt"
SOUNDS_DIR = APPDATA_DIR / "sounds"
MODEL_DIR = LOCALDATA_DIR / "models"
LOG_PATH = LOCALDATA_DIR / "mowik.log"
CONTROL_DIR = LOCALDATA_DIR / "control"
RESTART_REQUEST_PATH = CONTROL_DIR / "restart.request"
RESTART_ACK_PATH = CONTROL_DIR / "restart.ack"
RESTART_STARTED_PATH = CONTROL_DIR / "restart.started"

DEFAULT_CONFIG: dict[str, Any] = {
    "trigger": "keyboard:f8",
    "ui_language": "auto",
    "language": "auto",
    "model": "auto",
    "device": "auto",
    "cpu_threads": 0,
    "beam_size": 2,
    "pre_roll_ms": 300,
    "post_roll_ms": 120,
    "minimum_recording_ms": 250,
    "maximum_recording_seconds": DEFAULT_MAXIMUM_RECORDING_SECONDS,
    "minimum_rms": 0.0015,
    "microphone": None,
    "vad": {
        "enabled": True,
        "threshold": 0.45,
        "min_speech_duration_ms": 120,
        "min_silence_duration_ms": 250,
        # Enkoder Whispera zawsze pracuje na oknie 30 s, więc dla dyktowania
        # krótszego niż ~29 s dłuższy pad nie kosztuje ani milisekundy — mel
        # i tak jest dopychany zerami. Dawne 180 ms to raptem 5-6 okien Silero
        # wstecz i ucinało ciche polskie nagłosy („Wyślij" → „ślij").
        "speech_pad_ms": 300,
    },
    "dictionary": {
        "enabled": True,
        "max_terms": 120,
    },
    "paste": {
        "enabled": True,
        "copy_to_clipboard": True,
        "append_space": True,
        "delay_ms": 25,
    },
    "feedback": {
        "sounds": True,
        "notifications": True,
        "floating_indicator": True,
        "loop_recording_sound": False,
        "custom_sounds": {
            "start": "",
            "stop": "",
            "done": "",
            "error": "",
        },
    },
    "voice_commands": {
        "enabled": False,
    },
    "custom_commands": {
        "schema_version": command_engine.CUSTOM_COMMANDS_SCHEMA_VERSION,
        "enabled": False,
        "trigger": "keyboard:f7",
        "items": [],
    },
    "ollama_cleanup": {
        "enabled": False,
        "url": "http://127.0.0.1:11434",
        "model": "",
        "timeout_seconds": 45,
    },
}

QUICK_PROFILES: dict[str, dict[str, Any]] = {
    "light": {
        "display": {
            "pl": {
                "label": "Szybki",
                "description": "small, beam 1 — najmniejsze obciążenie",
            },
            "en": {
                "label": "Fast",
                "description": "small, beam 1 — lowest resource use",
            },
        },
        "changes": {
            "model": "small",
            "device": "auto",
            "beam_size": 1,
        },
    },
    "balanced": {
        "display": {
            "pl": {
                "label": "Zalecany",
                "description": "large-v3-turbo, beam 2 — zalecany",
            },
            "en": {
                "label": "Recommended",
                "description": "large-v3-turbo, beam 2 — recommended",
            },
        },
        "changes": {
            "model": "large-v3-turbo",
            "device": "auto",
            "beam_size": 2,
        },
    },
    "accurate": {
        "display": {
            "pl": {
                "label": "Najdokładniejszy",
                "description": "large-v3, beam 5 — najwyższa jakość",
            },
            "en": {
                "label": "Most accurate",
                "description": "large-v3, beam 5 — highest accuracy",
            },
        },
        "changes": {
            "model": "large-v3",
            "device": "auto",
            "beam_size": 5,
        },
    },
}


def matching_quick_profile(
    model: Any,
    device: Any,
    beam_size: Any,
) -> Optional[str]:
    """Zwróć profil odpowiadający ustawieniom runtime.

    ``model=auto`` oznacza obecnie rekomendowany ``large-v3-turbo``. Ta
    normalizacja służy wyłącznie prezentacji i nie zmienia zapisanej
    konfiguracji użytkownika.
    """
    normalized_model = str(model).strip()
    if normalized_model == "auto":
        normalized_model = "large-v3-turbo"
    normalized_device = str(device).strip()
    try:
        normalized_beam = int(beam_size)
    except (TypeError, ValueError):
        return None

    for profile_name, profile in QUICK_PROFILES.items():
        changes = profile["changes"]
        if (
            normalized_model == changes["model"]
            and normalized_device == changes["device"]
            and normalized_beam == changes["beam_size"]
        ):
            return profile_name
    return None

DICTIONARY_TEMPLATE = """# One name or phrase per line. / Jedna nazwa lub fraza w każdym wierszu.
# Lines beginning with # are ignored. / Linie zaczynające się od # są ignorowane.
# Add names, brands, projects, places, and specialist terms.
# Dopisz nazwiska, marki, projekty, miejscowości i fachowe terminy.
OpenAI
ChatGPT
Mówik
"""

VOICE_COMMAND_REPLACEMENTS = {
    "pl": (
        (re.compile(r"\bnowy akapit\b[,.]?", re.IGNORECASE), "\n\n"),
        (re.compile(r"\bnowa linia\b[,.]?", re.IGNORECASE), "\n"),
    ),
    "en": (
        (re.compile(r"\bnew paragraph\b[,.]?", re.IGNORECASE), "\n\n"),
        (re.compile(r"\bnew line\b[,.]?", re.IGNORECASE), "\n"),
    ),
}


class AppError(RuntimeError):
    pass


class ElevatedRuntimeError(AppError):
    """The per-user application was started with unsafe elevated rights."""


class ConfigConflict(AppError):
    """The on-disk configuration changed after an editor loaded it."""


class OperationCancelled(AppError):
    """Internal control-flow signal for work cancelled during shutdown."""


class RecordingDeadlineTimer(threading.Thread):
    """A cancellable daemon deadline independent of patched Thread factories."""

    def __init__(self, delay: float, callback, args: tuple[Any, ...]) -> None:
        super().__init__(name="RecordingLimit", daemon=True)
        self._delay = max(0.0, float(delay))
        self._callback = callback
        self._args = args
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        if not self._cancelled.wait(self._delay):
            self._callback(*self._args)


@dataclass(frozen=True)
class MicrophoneChoiceState:
    """One fail-closed Settings snapshot of the available microphones."""

    values: dict[str, Any]
    selected_label: str
    unresolved_label: Optional[str] = None
    error_code: Optional[str] = None
    blocked_labels: frozenset[str] = frozenset()


def microphone_selection_app_error(
    error_code: str,
    translator: Translator,
) -> AppError:
    """Translate stable selector errors without exposing device metadata."""

    if error_code == audio_devices.ERROR_DEVICE_MISSING:
        message = translator.t(
            "Zapisany mikrofon nie jest obecnie dostępny. Wybierz go ponownie "
            "w ustawieniach.",
            "The saved microphone is not currently available. Choose it again "
            "in Settings.",
        )
    elif error_code == audio_devices.ERROR_DEVICE_AMBIGUOUS:
        message = translator.t(
            "Nie można jednoznacznie rozpoznać zapisanego mikrofonu. Wybierz "
            "go ponownie w ustawieniach.",
            "The saved microphone cannot be identified uniquely. Choose it "
            "again in Settings.",
        )
    elif error_code in {
        audio_devices.ERROR_SELECTOR_MALFORMED,
        audio_devices.ERROR_SELECTOR_SCHEMA_UNSUPPORTED,
    }:
        message = translator.t(
            "Zapisane ustawienie mikrofonu jest nieprawidłowe lub pochodzi z "
            "nieobsługiwanej wersji. Wybierz mikrofon ponownie w ustawieniach.",
            "The saved microphone setting is invalid or comes from an "
            "unsupported version. Choose the microphone again in Settings.",
        )
    elif error_code in {
        audio_devices.ERROR_LEGACY_INDEX_INVALID,
        audio_devices.ERROR_DEVICE_NOT_INPUT,
    }:
        message = translator.t(
            "Starsze ustawienie mikrofonu nie wskazuje już prawidłowego wejścia. "
            "Wybierz mikrofon ponownie w ustawieniach.",
            "The legacy microphone setting no longer points to a valid input. "
            "Choose the microphone again in Settings.",
        )
    else:
        message = translator.t(
            "Nie można bezpiecznie odczytać listy mikrofonów. Sprawdź urządzenia "
            "audio i spróbuj ponownie.",
            "The microphone list could not be read safely. Check the audio "
            "devices and try again.",
        )
    return AppError(message)


def _microphone_default_label(translator: Translator) -> str:
    return translator.t(
        "Domyślny mikrofon Windows",
        "Default Windows microphone",
    )


def _microphone_unresolved_label(translator: Translator) -> str:
    return translator.t(
        "Zapisany mikrofon niedostępny — wybierz ponownie",
        "Saved microphone unavailable — choose again",
    )


def _microphone_device_label(
    selector: audio_devices.MicrophoneSelector,
    device_index: int,
    translator: Translator,
    *,
    ambiguous: bool = False,
) -> str:
    input_channels = translator.t(
        "{count} wej.",
        "{count} in",
        count=selector.max_input_channels,
    )
    label = (
        f"{selector.name} — {selector.host_api_name} · {input_channels} · "
        f"{selector.default_samplerate_hz} Hz · #{device_index}"
    )
    if ambiguous:
        label += translator.t(
            " · nie można rozróżnić",
            " · cannot distinguish",
        )
    return label


def _microphone_group_key(
    selector: audio_devices.MicrophoneSelector,
) -> tuple[str, int]:
    """Zepnij warianty jednego wejścia; MME tnie nazwy po 31 znakach."""

    name = " ".join(selector.name.split()).lower()
    head = name.split("(", 1)[0].strip()
    return head or name, selector.max_input_channels


def _microphone_host_api_rank(host_api_name: str) -> int:
    name = host_api_name.lower()
    for rank, marker in enumerate(MICROPHONE_HOST_API_PREFERENCE):
        if marker in name:
            return rank
    return len(MICROPHONE_HOST_API_PREFERENCE)


def _microphone_group_hides_separate_devices(
    indexes: Sequence[int],
    selectors_by_index: Mapping[int, audio_devices.MicrophoneSelector],
) -> bool:
    """Dwa fizyczne wejścia o tej samej nazwie muszą pozostać rozróżnialne."""

    seen: set[str] = set()
    for index in indexes:
        host_api = selectors_by_index[index].host_api_name.strip().lower()
        if host_api in seen:
            return True
        seen.add(host_api)
    return False


def _microphone_grouped_label(
    display_name: str,
    selector: audio_devices.MicrophoneSelector,
    translator: Translator,
    *,
    is_default: bool,
    ambiguous: bool,
) -> str:
    input_channels = translator.t(
        "{count} wej.",
        "{count} in",
        count=selector.max_input_channels,
    )
    label = f"{display_name} · {input_channels}"
    if is_default:
        label = "★ " + label + translator.t(
            " — używany teraz przez Windows",
            " — currently used by Windows",
        )
    if ambiguous:
        label += translator.t(
            " · nie można rozróżnić",
            " · cannot distinguish",
        )
    return label


def _microphone_display_name(
    indexes: Sequence[int],
    selectors_by_index: Mapping[int, audio_devices.MicrophoneSelector],
    representative: int,
) -> str:
    """Uzupełnij nazwę uciętą przez MME, nie podmieniając jej na cudzy wariant."""

    name = selectors_by_index[representative].name
    for index in indexes:
        candidate = selectors_by_index[index].name
        if len(candidate) > len(name) and candidate.startswith(name):
            name = candidate
    return name


def _unique_microphone_label(label: str, taken: Mapping[str, Any], index: int) -> str:
    """Etykieta jest kluczem wyboru, więc kolizja skasowałaby cudzy wpis."""

    if label not in taken:
        return label
    return f"{label} · #{index}"


def build_microphone_choice_state(
    configured_value: Any,
    devices: Any,
    host_apis: Any,
    translator: Translator,
    *,
    show_all: bool = False,
    default_input_index: Optional[int] = None,
) -> MicrophoneChoiceState:
    """Build Settings choices and migrate a valid legacy index in memory."""

    device_snapshot = tuple(devices)
    host_api_snapshot = tuple(host_apis)
    default_label = _microphone_default_label(translator)
    values: dict[str, Any] = {default_label: None}
    labels_by_index: dict[int, str] = {}
    descriptors_by_index: dict[int, dict[str, Any]] = {}
    selectors_by_index: dict[int, audio_devices.MicrophoneSelector] = {}

    for index in range(len(device_snapshot)):
        try:
            descriptor = audio_devices.build_microphone_selector(
                index,
                device_snapshot,
                host_api_snapshot,
            )
        except audio_devices.MicrophoneSelectionError as exc:
            if exc.code == audio_devices.ERROR_DEVICE_NOT_INPUT:
                continue
            raise
        descriptors_by_index[index] = descriptor
        selectors_by_index[index] = audio_devices.parse_microphone_selector(
            descriptor
        )

    selector_counts: dict[tuple[Any, ...], int] = {}
    for selector in selectors_by_index.values():
        identity = audio_devices.microphone_identity_key(selector)
        selector_counts[identity] = selector_counts.get(identity, 0) + 1

    blocked_labels: set[str] = set()

    def is_ambiguous(index: int) -> bool:
        identity = audio_devices.microphone_identity_key(selectors_by_index[index])
        return selector_counts[identity] > 1

    def add_detailed_choice(index: int) -> None:
        ambiguous = is_ambiguous(index)
        label = _microphone_device_label(
            selectors_by_index[index],
            index,
            translator,
            ambiguous=ambiguous,
        )
        values[label] = descriptors_by_index[index]
        labels_by_index[index] = label
        if ambiguous:
            blocked_labels.add(label)

    if show_all:
        for index in selectors_by_index:
            add_detailed_choice(index)
    else:
        groups: dict[tuple[str, int], list[int]] = {}
        for index, selector in selectors_by_index.items():
            groups.setdefault(_microphone_group_key(selector), []).append(index)

        def add_grouped_choice(indexes: Sequence[int]) -> None:
            representative = min(
                indexes,
                key=lambda index: (
                    _microphone_host_api_rank(
                        selectors_by_index[index].host_api_name
                    ),
                    index,
                ),
            )
            ambiguous = any(is_ambiguous(index) for index in indexes)
            label = _unique_microphone_label(
                _microphone_grouped_label(
                    _microphone_display_name(
                        indexes,
                        selectors_by_index,
                        representative,
                    ),
                    selectors_by_index[representative],
                    translator,
                    is_default=default_input_index in indexes,
                    ambiguous=ambiguous,
                ),
                values,
                representative,
            )
            values[label] = descriptors_by_index[representative]
            for index in indexes:
                labels_by_index[index] = label
            if ambiguous:
                blocked_labels.add(label)

        for (group_name, _channels), indexes in groups.items():
            if group_name in SYSTEM_MICROPHONE_ALIASES:
                continue
            if not _microphone_group_hides_separate_devices(
                indexes, selectors_by_index
            ):
                add_grouped_choice(indexes)
                continue
            # Wspólny początek nazwy okazał się kryć różne wejścia, więc
            # rozdzielamy je po pełnej nazwie zamiast scalać w jedną pozycję.
            subgroups: dict[str, list[int]] = {}
            for index in indexes:
                full_name = " ".join(selectors_by_index[index].name.split()).lower()
                subgroups.setdefault(full_name, []).append(index)
            for subgroup in subgroups.values():
                if _microphone_group_hides_separate_devices(
                    subgroup, selectors_by_index
                ):
                    for index in subgroup:
                        add_detailed_choice(index)
                else:
                    add_grouped_choice(subgroup)

    if configured_value is None:
        return MicrophoneChoiceState(
            values,
            default_label,
            blocked_labels=frozenset(blocked_labels),
        )

    try:
        selected_index = audio_devices.resolve_microphone_device(
            configured_value,
            device_snapshot,
            host_api_snapshot,
        )
        selected_label = labels_by_index[selected_index]
        if selected_label in blocked_labels:
            raise audio_devices.MicrophoneSelectionError(
                audio_devices.ERROR_DEVICE_AMBIGUOUS
            )
    except (audio_devices.MicrophoneSelectionError, KeyError) as exc:
        error_code = (
            exc.code
            if isinstance(exc, audio_devices.MicrophoneSelectionError)
            else audio_devices.ERROR_SNAPSHOT_MALFORMED
        )
        unresolved_label = _microphone_unresolved_label(translator)
        values[unresolved_label] = copy.deepcopy(configured_value)
        return MicrophoneChoiceState(
            values,
            unresolved_label,
            unresolved_label=unresolved_label,
            error_code=error_code,
            blocked_labels=frozenset(blocked_labels),
        )

    if isinstance(configured_value, dict):
        # Zapisany wybór zostaje dokładnie taki, jaki jest. Skrócona lista
        # proponuje inne API tego samego wejścia, ale zapis bez świadomej
        # zmiany nie może po cichu podmienić ustawienia.
        values[selected_label] = copy.deepcopy(configured_value)

    return MicrophoneChoiceState(
        values,
        selected_label,
        blocked_labels=frozenset(blocked_labels),
    )


def microphone_level_percent(rms: float) -> int:
    """Przelicz RMS na skalę, w której zwykła mowa wypełnia środek paska."""

    if not math.isfinite(rms) or rms <= 0.0:
        return 0
    decibels = 20.0 * math.log10(rms)
    ratio = (decibels + 60.0) / 54.0
    return max(0, min(100, int(round(ratio * 100))))


class MicrophoneLevelMonitor:
    """Podsłuch wybranego wejścia w ustawieniach, niezależny od dyktowania."""

    _DECAY = 0.82

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._level = 0.0
        self._stream: Optional[Any] = None

    def _callback(self, indata, frames, time_info, status) -> None:
        del frames, time_info, status
        block = np.asarray(indata, dtype=np.float32)
        level = (
            float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        )
        if not math.isfinite(level):
            level = 0.0
        with self._lock:
            # Szybki wzrost i wolniejszy spadek czytają się jak zwykły wskaźnik.
            self._level = max(level, self._level * self._DECAY)

    def start(self, device: Optional[int]) -> None:
        self.stop()
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            device=device,
            blocksize=0,
            latency="high",
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception:
            try:
                stream.close(ignore_errors=True)
            except Exception:
                logging.debug("Nie udało się zamknąć podglądu", exc_info=True)
            raise
        with self._lock:
            self._stream = stream
            self._level = 0.0

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._level = 0.0
        if stream is None:
            return
        try:
            stream.stop(ignore_errors=True)
            stream.close(ignore_errors=True)
        except Exception:
            logging.debug("Nie udało się zamknąć podglądu", exc_info=True)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._stream is not None

    def level_percent(self) -> int:
        with self._lock:
            return microphone_level_percent(self._level)


def build_unavailable_microphone_choice_state(
    configured_value: Any,
    translator: Translator,
    error_code: str = audio_devices.ERROR_SNAPSHOT_UNAVAILABLE,
) -> MicrophoneChoiceState:
    """Preserve an explicit selector when PortAudio enumeration is unavailable."""

    default_label = _microphone_default_label(translator)
    values: dict[str, Any] = {default_label: None}
    if configured_value is None:
        return MicrophoneChoiceState(
            values,
            default_label,
            error_code=error_code,
        )
    unresolved_label = _microphone_unresolved_label(translator)
    values[unresolved_label] = copy.deepcopy(configured_value)
    return MicrophoneChoiceState(
        values,
        unresolved_label,
        unresolved_label=unresolved_label,
        error_code=error_code,
    )


def microphone_config_value_for_choice(
    state: MicrophoneChoiceState,
    selected_label: str,
    translator: Translator,
) -> Any:
    """Return a JSON-ready choice, rejecting unresolved opaque selectors."""

    if (
        selected_label not in state.values
        or selected_label == state.unresolved_label
        or selected_label in state.blocked_labels
    ):
        raise AppError(
            translator.t(
                "Wybierz dostępny mikrofon albo mikrofon domyślny. Zapisanego "
                "niedostępnego urządzenia nie można użyć automatycznie.",
                "Choose an available microphone or the default microphone. "
                "The saved unavailable device cannot be used automatically.",
            )
        )
    return copy.deepcopy(state.values[selected_label])


def resolve_runtime_microphone(
    configured_value: Any,
    translator: Translator,
    device_source: Any = None,
    host_api_source: Any = None,
) -> tuple[Optional[int], Optional[Any]]:
    """Resolve one explicit selector against a single fresh PortAudio snapshot."""

    device_index, device_info, _ = _resolve_runtime_microphone_with_identity(
        configured_value,
        translator,
        device_source,
        host_api_source,
    )
    return device_index, device_info


def _prefer_wasapi_equivalent(
    device_index: int,
    devices: tuple[Any, ...],
    host_apis: tuple[Any, ...],
) -> int:
    """Map a legacy MME endpoint to one unambiguous same-name WASAPI input."""

    selected = audio_devices.parse_microphone_selector(
        audio_devices.build_microphone_selector(device_index, devices, host_apis)
    )
    if "wasapi" in selected.host_api_name.casefold():
        return device_index
    normalized_name = " ".join(selected.name.split()).casefold()
    candidates: list[int] = []
    for index in range(len(devices)):
        try:
            candidate = audio_devices.parse_microphone_selector(
                audio_devices.build_microphone_selector(index, devices, host_apis)
            )
        except audio_devices.MicrophoneSelectionError as exc:
            if exc.code == audio_devices.ERROR_DEVICE_NOT_INPUT:
                continue
            raise
        if (
            "wasapi" in candidate.host_api_name.casefold()
            and " ".join(candidate.name.split()).casefold() == normalized_name
            and candidate.max_input_channels == selected.max_input_channels
            and candidate.max_output_channels == selected.max_output_channels
        ):
            candidates.append(index)
    if len(candidates) > 1:
        # WASAPI is only an optional optimization for a device that was
        # already resolved unambiguously.  Multiple same-name alternatives
        # must not make the exact endpoint saved by the user unusable.
        return device_index
    return candidates[0] if candidates else device_index


def _resolve_runtime_microphone_with_identity(
    configured_value: Any,
    translator: Translator,
    device_source: Any = None,
    host_api_source: Any = None,
    *,
    prefer_wasapi: bool = True,
) -> tuple[Optional[int], Optional[Any], Optional[dict[str, Any]]]:
    """Resolve a microphone and capture its identity from the same snapshot."""

    if configured_value is None:
        return None, None, None
    if device_source is None:
        device_source = sd.query_devices
    if host_api_source is None:
        host_api_source = sd.query_hostapis
    error_code: Optional[str] = None
    device_index: Optional[int] = None
    device_info: Optional[Any] = None
    device_identity: Optional[dict[str, Any]] = None
    try:
        device_snapshot = tuple(device_source())
        host_api_snapshot = tuple(host_api_source())
        device_index = audio_devices.resolve_microphone_device(
            configured_value,
            device_snapshot,
            host_api_snapshot,
        )
        if device_index is None:
            return None, None, None
        if prefer_wasapi:
            device_index = _prefer_wasapi_equivalent(
                device_index,
                device_snapshot,
                host_api_snapshot,
            )
        device_info = device_snapshot[device_index]
        device_identity = audio_devices.build_microphone_selector(
            device_index,
            device_snapshot,
            host_api_snapshot,
        )
    except audio_devices.MicrophoneSelectionError as exc:
        error_code = exc.code
    except Exception:
        error_code = audio_devices.ERROR_SNAPSHOT_UNAVAILABLE
    if error_code is not None:
        # Wyjście z bloku except usuwa niejawny kontekst, który mógłby zawierać
        # nazwę urządzenia albo szczegóły błędu sterownika.
        raise microphone_selection_app_error(error_code, translator)
    return device_index, device_info, device_identity


class CustomOpenTargetError(ValueError):
    """Raised without echoing a potentially sensitive configured target."""


def ensure_directories() -> None:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    LOCALDATA_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(console: bool = False) -> None:
    elevated = os.name == "nt" and windows_actions.is_process_elevated()
    file_handler_error: Optional[OSError] = None
    if elevated:
        # Logging is initialized before first-run config creation.  Do not let
        # an elevated process create/rotate a file through user-controlled
        # LOCALAPPDATA before the elevated-write guard aborts startup.
        handlers: list[logging.Handler] = (
            [logging.StreamHandler(sys.stdout)]
            if console
            else [logging.NullHandler()]
        )
    else:
        try:
            ensure_directories()
            handlers = [
                RotatingFileHandler(
                    LOG_PATH,
                    maxBytes=2_000_000,
                    backupCount=3,
                    encoding="utf-8",
                )
            ]
        except OSError as exc:
            # A locked, read-only, or unavailable log file must not make both
            # normal startup and the top-level fatal-error handler crash.
            file_handler_error = exc
            handlers = (
                [logging.StreamHandler(sys.stdout)]
                if console
                else [logging.NullHandler()]
            )
        else:
            if console:
                handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    if file_handler_error is not None:
        logging.warning(
            "Nie udało się otworzyć pliku logu; używam wyjścia awaryjnego: %s",
            file_handler_error,
        )


def deep_merge(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, default_value in defaults.items():
        if key not in loaded:
            # Konfiguracja jest dalej modyfikowana przez panel ustawień. Nie
            # wolno więc zwracać referencji do zagnieżdżonych DEFAULT_CONFIG,
            # bo zapis starszego config.json zmieniałby fabryczne wartości w
            # tym samym procesie (włącznie z listą własnych komend).
            result[key] = copy.deepcopy(default_value)
        elif isinstance(default_value, dict):
            if key == "custom_commands" and not (
                command_engine.custom_commands_schema_supported(loaded[key])
            ):
                # Nowszy lub nieznany schemat jest nieprzezroczysty dla tej
                # wersji. Nie wolno do niego wstrzykiwać pól schema-1 ani
                # zmieniać go przy zapisie niezwiązanych ustawień.
                result[key] = copy.deepcopy(loaded[key])
            elif isinstance(loaded[key], dict):
                result[key] = deep_merge(default_value, loaded[key])
            else:
                # Zły typ w ręcznie edytowanym config.json nie może
                # wywrócić programu w trakcie dyktowania.
                result[key] = copy.deepcopy(default_value)
        else:
            result[key] = copy.deepcopy(loaded[key])
    for key, value in loaded.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
    return result


def migrate_legacy_config_values(config: dict[str, Any]) -> None:
    """Keep configurations from older releases on a supported local model."""

    configured_model = str(config.get("model", "")).strip().lower()
    if configured_model in LEGACY_REMOVED_MODELS:
        logging.warning(
            "Zmieniono wycofany model %s na auto (large-v3-turbo)",
            configured_model,
        )
        config["model"] = "auto"


def validate_config_types(config: dict[str, Any]) -> None:
    """Reject malformed known settings before they reach audio or OS code."""

    translator = Translator.from_config(config)

    def invalid(path: str) -> AppError:
        return AppError(
            translator.t(
                "Nieprawidłowa wartość w config.json: {path}",
                "Invalid value in config.json: {path}",
                path=path,
            )
        )

    def require_string(path: str, value: Any, *, nonempty: bool = False) -> None:
        if not isinstance(value, str) or (nonempty and not value.strip()):
            raise invalid(path)

    def require_bool(path: str, value: Any) -> None:
        if type(value) is not bool:
            raise invalid(path)

    def require_int(path: str, value: Any, minimum: int, maximum: int) -> None:
        if type(value) is not int or not minimum <= value <= maximum:
            raise invalid(path)

    def require_number(path: str, value: Any, minimum: float, maximum: float) -> None:
        if type(value) not in (int, float):
            raise invalid(path)
        numeric = float(value)
        if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
            raise invalid(path)

    for key in ("trigger", "ui_language", "language", "model", "device"):
        require_string(key, config.get(key), nonempty=True)
    require_int("cpu_threads", config.get("cpu_threads"), 0, 256)
    require_int("beam_size", config.get("beam_size"), 1, 10)
    require_int("pre_roll_ms", config.get("pre_roll_ms"), 0, 2_000)
    require_int("post_roll_ms", config.get("post_roll_ms"), 0, 2_000)
    require_int(
        "minimum_recording_ms",
        config.get("minimum_recording_ms"),
        0,
        10_000,
    )
    require_int(
        "maximum_recording_seconds",
        config.get("maximum_recording_seconds"),
        1,
        MAXIMUM_RECORDING_SECONDS,
    )
    if config["minimum_recording_ms"] > config["maximum_recording_seconds"] * 1_000:
        raise invalid("minimum_recording_ms <= maximum_recording_seconds * 1000")
    require_number("minimum_rms", config.get("minimum_rms"), 0.0, 1.0)

    section_specs = {
        "vad": (
            ("enabled", "bool", 0, 0),
            ("threshold", "number", 0.0, 1.0),
            ("min_speech_duration_ms", "int", 0, 10_000),
            ("min_silence_duration_ms", "int", 0, 10_000),
            ("speech_pad_ms", "int", 0, 3_000),
        ),
        "dictionary": (
            ("enabled", "bool", 0, 0),
            ("max_terms", "int", 0, 5_000),
        ),
        "paste": (
            ("enabled", "bool", 0, 0),
            ("copy_to_clipboard", "bool", 0, 0),
            ("append_space", "bool", 0, 0),
            ("delay_ms", "int", 0, 5_000),
        ),
        "feedback": (
            ("sounds", "bool", 0, 0),
            ("notifications", "bool", 0, 0),
            ("floating_indicator", "bool", 0, 0),
            ("loop_recording_sound", "bool", 0, 0),
        ),
        "voice_commands": (("enabled", "bool", 0, 0),),
        "ollama_cleanup": (
            ("enabled", "bool", 0, 0),
            ("url", "string", 0, 0),
            ("model", "string", 0, 0),
            (
                "timeout_seconds",
                "int",
                1,
                MAX_OLLAMA_TIMEOUT_SECONDS,
            ),
        ),
    }
    for section_name, specs in section_specs.items():
        section = config.get(section_name)
        if not isinstance(section, dict):
            raise invalid(section_name)
        for field, kind, minimum, maximum in specs:
            path = f"{section_name}.{field}"
            value = section.get(field)
            if kind == "bool":
                require_bool(path, value)
            elif kind == "int":
                require_int(path, value, int(minimum), int(maximum))
            elif kind == "number":
                require_number(path, value, float(minimum), float(maximum))
            else:
                require_string(path, value)

    paste = config["paste"]
    if not paste["enabled"] and not paste["copy_to_clipboard"]:
        raise invalid("paste.enabled + paste.copy_to_clipboard")

    custom_sounds = config["feedback"].get("custom_sounds")
    if not isinstance(custom_sounds, dict):
        raise invalid("feedback.custom_sounds")
    for kind in ("start", "stop", "done", "error"):
        require_string(f"feedback.custom_sounds.{kind}", custom_sounds.get(kind))


def create_default_files() -> None:
    config_missing = not CONFIG_PATH.exists()
    dictionary_missing = not DICTIONARY_PATH.exists()
    if config_missing or dictionary_missing:
        # ``load_config`` also runs during an elevated startup.  Never let that
        # first-run path create files through a lower-integrity APPDATA reparse
        # point before the normal settings-write guard has a chance to run.
        require_non_elevated_config_write(DEFAULT_CONFIG)
    if os.name == "nt" and windows_actions.is_process_elevated():
        # Existing files may be read by an elevated diagnostic run, but avoid
        # even idempotent mkdir calls in user-controlled data directories.
        return
    ensure_directories()
    with config_write_guard():
        _create_text_file_exclusive(
            CONFIG_PATH,
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
        )
        _create_text_file_exclusive(DICTIONARY_PATH, DICTIONARY_TEMPLATE)


def _create_text_file_exclusive(path: Path, payload: str) -> bool:
    """Publish a complete first-run file without overwriting another writer."""

    temporary_path = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    descriptor = os.open(temporary_path, flags, 0o600)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return False
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except OSError:
            pass


def _read_config_payload(
    translator: Optional[Translator] = None,
    *,
    missing_ok: bool = False,
) -> Optional[bytes]:
    """Read config through a hard byte ceiling before JSON decoding."""

    translator = translator or Translator("auto")
    try:
        with CONFIG_PATH.open("rb") as config_file:
            payload = config_file.read(MAX_CONFIG_FILE_BYTES + 1)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise AppError(
            translator.t(
                "Nie można odczytać {path}: {error}",
                "Could not read {path}: {error}",
                path=CONFIG_PATH,
                error=exc,
            )
        ) from exc
    if len(payload) > MAX_CONFIG_FILE_BYTES:
        raise AppError(
            translator.t(
                "Plik config.json jest zbyt duży (maksymalnie {limit} MiB).",
                "config.json is too large (maximum {limit} MiB).",
                limit=MAX_CONFIG_FILE_BYTES // (1024 * 1024),
            )
        )
    return payload


def load_config_with_revision() -> tuple[dict[str, Any], str]:
    create_default_files()
    fallback_translator = Translator("auto")
    try:
        payload = _read_config_payload(fallback_translator)
        assert payload is not None
        loaded = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppError(
            fallback_translator.t(
                "Nie można odczytać {path}: {error}",
                "Could not read {path}: {error}",
                path=CONFIG_PATH,
                error=exc,
            )
        ) from exc
    if not isinstance(loaded, dict):
        raise AppError(
            fallback_translator.t(
                "Plik config.json musi zawierać obiekt JSON.",
                "config.json must contain a JSON object.",
            )
        )
    config = deep_merge(DEFAULT_CONFIG, loaded)
    migrate_legacy_config_values(config)
    validate_config_types(config)
    return config, hashlib.sha256(payload).hexdigest()


def load_config() -> dict[str, Any]:
    return load_config_with_revision()[0]


_CONFIG_REVISION_UNSET = object()
_CONFIG_WRITE_LOCK = threading.Lock()


def require_non_elevated_config_write(config: dict[str, Any]) -> None:
    """Do not let a lower-integrity user directory redirect elevated writes."""

    if os.name != "nt" or not windows_actions.is_process_elevated():
        return
    raise AppError(
        Translator.from_config(config).t(
            "Nie zapisuję ustawień z uprawnieniami administratora. "
            "Uruchom Mówika normalnie i spróbuj ponownie.",
            "Settings are not saved while Mówik is running as administrator. "
            "Start Mówik normally and try again.",
        )
    )


def require_non_elevated_runtime(
    translator: Optional[Translator] = None,
) -> None:
    """Reject an elevated per-user runtime before it touches user data."""

    if os.name != "nt" or not windows_actions.is_process_elevated():
        return
    translator = translator or Translator("auto")
    raise ElevatedRuntimeError(
        translator.t(
            "Ze względów bezpieczeństwa Mówika nie można uruchamiać z "
            "uprawnieniami administratora. Zamknij tę instancję i uruchom "
            "program normalnie.",
            "For security reasons, Mówik cannot run with administrator "
            "privileges. Close this instance and start the application "
            "normally.",
        )
    )


def config_file_revision(
    translator: Optional[Translator] = None,
) -> Optional[str]:
    translator = translator or Translator("auto")
    try:
        payload = _read_config_payload(translator, missing_ok=True)
    except AppError as exc:
        if not isinstance(exc.__cause__, OSError):
            raise
        raise AppError(
            translator.t(
                "Nie udało się sprawdzić pliku config.json: {error}",
                "Could not inspect config.json: {error}",
                error=exc.__cause__,
            )
        ) from exc
    if payload is None:
        return None
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def config_write_guard():
    """Serialize config replacement in this process and across Windows processes."""

    with _CONFIG_WRITE_LOCK:
        handle: Optional[int] = None
        kernel32 = None
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_bool,
                ctypes.c_wchar_p,
            ]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.ReleaseMutex.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            raw_handle = kernel32.CreateMutexW(None, False, CONFIG_WRITE_MUTEX_NAME)
            if not raw_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            handle = int(raw_handle)
            wait_result = int(
                kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF)
            )
            if wait_result not in {0, 0x80}:  # WAIT_OBJECT_0 / WAIT_ABANDONED
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if handle is not None and kernel32 is not None:
                kernel32.ReleaseMutex(ctypes.c_void_p(handle))
                kernel32.CloseHandle(ctypes.c_void_p(handle))


def _serialize_config_payload(
    config: dict[str, Any],
    translator: Optional[Translator] = None,
) -> bytes:
    """Serialize exactly what will be written and enforce the read ceiling."""

    translator = translator or Translator.from_config(config)
    payload_bytes = (
        json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if len(payload_bytes) > MAX_CONFIG_FILE_BYTES:
        raise AppError(
            translator.t(
                "Konfiguracja jest zbyt duża do zapisania (maksymalnie {limit} MiB).",
                "The configuration is too large to save (maximum {limit} MiB).",
                limit=MAX_CONFIG_FILE_BYTES // (1024 * 1024),
            )
        )
    return payload_bytes


def save_config(
    config: dict[str, Any],
    *,
    expected_revision: Any = _CONFIG_REVISION_UNSET,
) -> str:
    """Zapisz konfigurację atomowo, aby nie pozostawić uciętego JSON-a."""
    validate_config_types(config)
    require_non_elevated_config_write(config)
    translator = Translator.from_config(config)
    payload_bytes = _serialize_config_payload(config, translator)
    ensure_directories()
    with config_write_guard():
        return _write_config_locked(payload_bytes, expected_revision, translator)


def _write_config_locked(
    payload_bytes: bytes,
    expected_revision: Any,
    translator: Optional[Translator] = None,
) -> str:
    """Write config while the caller owns ``config_write_guard``."""

    translator = translator or Translator("auto")
    if (
        expected_revision is not _CONFIG_REVISION_UNSET
        and config_file_revision(translator) != expected_revision
    ):
        raise ConfigConflict(
            translator.t(
                "Plik config.json został zmieniony w innym oknie. "
                "Otwórz Ustawienia ponownie i spróbuj jeszcze raz.",
                "config.json changed in another window. "
                "Reopen Settings and try again.",
            )
        )
    temp_path = CONFIG_PATH.with_name(
        f".{CONFIG_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor: Optional[int] = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        descriptor = os.open(temp_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as temp_file:
            descriptor = None
            temp_file.write(payload_bytes)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, CONFIG_PATH)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return hashlib.sha256(payload_bytes).hexdigest()


def apply_quick_profile(config: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profile = QUICK_PROFILES.get(profile_name)
    if profile is None:
        raise AppError(
            Translator.from_config(config).t(
                "Nieznany profil: {profile}",
                "Unknown profile: {profile}",
                profile=profile_name,
            )
        )
    result = deep_merge(DEFAULT_CONFIG, config)
    for key, value in profile["changes"].items():
        result[key] = value
    return result


def request_app_restart() -> int:
    """Poproś działającą instancję o restart i zwróć znacznik żądania."""
    ensure_directories()
    requested_at_ns = time.time_ns()
    temp_path = RESTART_REQUEST_PATH.with_name(
        f"{RESTART_REQUEST_PATH.name}.{os.getpid()}.tmp"
    )
    temp_path.write_text(f"v1:{requested_at_ns}\n", encoding="ascii")
    os.replace(temp_path, RESTART_REQUEST_PATH)
    return requested_at_ns


def acknowledge_restart_request(request_text: str) -> None:
    """Publish an atomic acknowledgement for the Settings process."""

    ensure_directories()
    temp_path = RESTART_ACK_PATH.with_name(
        f"{RESTART_ACK_PATH.name}.{os.getpid()}.tmp"
    )
    temp_path.write_text(f"{str(request_text).strip()}\n", encoding="ascii")
    os.replace(temp_path, RESTART_ACK_PATH)


def wait_for_restart_ack(
    requested_at_ns: int,
    timeout: float = RESTART_ACK_TIMEOUT_SECONDS,
) -> bool:
    """Wait briefly until the running instance claims this restart request."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            acknowledged = RESTART_ACK_PATH.read_text(encoding="ascii")
        except (FileNotFoundError, OSError, UnicodeError):
            acknowledged = ""
        timestamp_ns = parse_restart_request_timestamp_ns(acknowledged)
        if timestamp_ns is not None and timestamp_ns == int(requested_at_ns):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def announce_restart_started(requested_at_ns: int) -> None:
    """Atomically confirm that the replacement instance actually started."""

    if isinstance(requested_at_ns, bool) or not isinstance(requested_at_ns, int):
        raise ValueError("restart timestamp must be an integer")
    if requested_at_ns <= 0:
        raise ValueError("restart timestamp must be positive")
    ensure_directories()
    temp_path = RESTART_STARTED_PATH.with_name(
        f"{RESTART_STARTED_PATH.name}.{os.getpid()}.tmp"
    )
    temp_path.write_text(
        f"v2:started:{requested_at_ns}\n",
        encoding="ascii",
    )
    os.replace(temp_path, RESTART_STARTED_PATH)


def parse_restart_started_timestamp_ns(value: str) -> Optional[int]:
    """Parse only the versioned confirmation emitted by a new instance."""

    raw = str(value).strip()
    prefix = "v2:started:"
    if not raw.startswith(prefix):
        return None
    timestamp_text = raw[len(prefix) :]
    if not timestamp_text.isascii() or not timestamp_text.isdecimal():
        return None
    try:
        timestamp_ns = int(timestamp_text)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp_ns if timestamp_ns > 0 else None


def parse_restart_started_cli_token(value: str) -> int:
    """Require the canonical positive integer passed by the restart parent."""

    raw = str(value)
    if not raw.isascii() or not raw.isdecimal():
        raise argparse.ArgumentTypeError("restart token must be a positive integer")
    try:
        timestamp_ns = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            "restart token must be a positive integer"
        ) from exc
    if timestamp_ns <= 0 or raw != str(timestamp_ns):
        raise argparse.ArgumentTypeError("restart token must be a positive integer")
    return timestamp_ns


def wait_for_restart_started(
    requested_at_ns: int,
    timeout: float = RESTART_STARTED_TIMEOUT_SECONDS,
) -> bool:
    """Wait for a matching v2 start confirmation, never for receipt alone."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            confirmation = RESTART_STARTED_PATH.read_text(encoding="ascii")
        except (FileNotFoundError, OSError, UnicodeError):
            confirmation = ""
        timestamp_ns = parse_restart_started_timestamp_ns(confirmation)
        if timestamp_ns is not None and timestamp_ns == int(requested_at_ns):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def parse_restart_request_timestamp_ns(value: str) -> Optional[int]:
    """Odczytaj bieżący format v1 oraz znacznik sekundowy ze starszych wersji."""

    raw = str(value).strip()
    try:
        if raw.startswith("v1:"):
            timestamp_ns = int(raw[3:])
        else:
            legacy_seconds = float(raw)
            if not math.isfinite(legacy_seconds):
                return None
            timestamp_ns = int(legacy_seconds * 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp_ns if timestamp_ns > 0 else None


def take_fresh_restart_request(not_before_ns: int) -> Optional[str]:
    """Atomowo przejmij request i odrzuć plik pochodzący ze starego procesu."""

    # Ta funkcja jest odpytywana prawie trzy razy na sekundę przez cały czas
    # życia procesu, a plik żądania pojawia się raz na kilka minut w najlepszym
    # razie. Tani test istnienia oszczędza os.replace i uuid4 na każdą próbę;
    # atomowość przejęcia zapewnia i tak dopiero os.replace poniżej.
    if not RESTART_REQUEST_PATH.exists():
        return None
    claimed_path = RESTART_REQUEST_PATH.with_name(
        f"{RESTART_REQUEST_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.claimed"
    )
    try:
        os.replace(RESTART_REQUEST_PATH, claimed_path)
    except FileNotFoundError:
        return None
    try:
        request_text = claimed_path.read_text(encoding="ascii").strip()
    finally:
        claimed_path.unlink(missing_ok=True)
    timestamp_ns = parse_restart_request_timestamp_ns(request_text)
    if timestamp_ns is None or timestamp_ns < max(0, int(not_before_ns)):
        logging.info("Pominięto nieaktualną prośbę o restart ustawień")
        return None
    return request_text


def discard_pending_restart_request() -> None:
    """Usuń request bez odbiorcy przed uruchomieniem nowej instancji."""

    RESTART_REQUEST_PATH.unlink(missing_ok=True)


@lru_cache(maxsize=4)
def _load_dictionary_snapshot(
    modified_ns: int, file_size: int, limit: int
) -> tuple[str, ...]:
    # modified_ns i file_size są częścią klucza cache; sam odczyt zawsze dotyczy
    # stałej ścieżki prywatnego słownika.
    del modified_ns
    if limit <= 0:
        return ()
    if file_size > MAX_DICTIONARY_FILE_BYTES:
        logging.warning(
            "Pominięto słownik większy niż %d MiB: %s",
            MAX_DICTIONARY_FILE_BYTES // (1024 * 1024),
            DICTIONARY_PATH,
        )
        return ()
    terms: list[str] = []
    seen: set[str] = set()
    try:
        with DICTIONARY_PATH.open("r", encoding="utf-8-sig") as dictionary_file:
            for line in dictionary_file:
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                normalized = value.casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                terms.append(value)
                if len(terms) >= limit:
                    break
    except (OSError, UnicodeError) as exc:
        logging.warning("Pominięto nieczytelny słownik %s: %s", DICTIONARY_PATH, exc)
        return ()
    return tuple(terms)


def load_dictionary(config: dict[str, Any]) -> list[str]:
    settings = config.get("dictionary", {})
    if not settings.get("enabled", True):
        return []
    limit = max(0, int(settings.get("max_terms", 120)))
    try:
        stat = DICTIONARY_PATH.stat()
    except OSError:
        return []
    return list(_load_dictionary_snapshot(stat.st_mtime_ns, stat.st_size, limit))


def windows_cuda_runtime_present() -> bool:
    if os.name != "nt":
        return True
    # CTranslate2 >= 4.6.3 ma własną implementację konwolucji Whispera na
    # CUDA, więc cuDNN nie jest już wymagane. cuBLAS (wraz z cublasLt) nadal jest.
    required_dlls = ("cublas64_12.dll",)
    for dll_name in required_dlls:
        try:
            ctypes.WinDLL(dll_name)
        except OSError:
            logging.info("Brak biblioteki GPU w PATH: %s", dll_name)
            return False
    if CUDA_DLL_SEARCH_PATHS:
        logging.info(
            "Biblioteki CUDA znalezione w: %s",
            ", ".join(str(path) for path in CUDA_DLL_SEARCH_PATHS),
        )
    return True


def get_cuda_device_count() -> int:
    """Policz karty widziane przez sterownik, nie zaglądając do cuBLAS."""

    try:
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        logging.exception("Nie udało się sprawdzić urządzeń CUDA")
        return 0


def get_cuda_count() -> int:
    count = get_cuda_device_count()
    if count > 0 and not windows_cuda_runtime_present():
        return 0
    return count


def cuda_is_usable() -> bool:
    """Sama karta wystarczy: brakujące biblioteki Mówik dociąga przed startem."""

    if get_cuda_count() > 0:
        return True
    return (
        get_cuda_device_count() > 0
        and cuda_runtime.user_runtime_root() is not None
    )


def resolve_model_plan(
    config: dict[str, Any],
    translator: Optional[Translator] = None,
) -> tuple[str, str, str]:
    translator = translator or Translator("pl")
    requested_device = str(config.get("device", "auto")).lower().strip()
    requested_model = str(config.get("model", "auto")).strip()
    cuda_available = cuda_is_usable()

    if requested_device == "auto":
        device = "cuda" if cuda_available else "cpu"
    elif requested_device in {"cuda", "cpu"}:
        device = requested_device
    else:
        raise AppError(
            translator.t(
                "device musi mieć wartość: auto, cuda albo cpu.",
                "device must be one of: auto, cuda, or cpu.",
            )
        )

    if requested_model.lower() == "auto":
        # Turbo zachowuje jakość rodziny large-v3, a na krótkich dyktowaniach
        # daje znacznie niższe opóźnienie. Pełny large-v3 pozostaje w profilu
        # „Najdokładniejszy” dla osób świadomie wybierających jakość ponad szybkość.
        model_name = "large-v3-turbo"
    else:
        model_name = requested_model
    if model_name not in MODEL_SOURCES:
        raise AppError(
            translator.t(
                "Nieobsługiwany model: {model_name}.",
                "Unsupported model: {model_name}.",
                model_name=model_name,
            )
        )

    compute_type = "float16" if device == "cuda" else "int8"
    return model_name, device, compute_type


def resolve_cpu_threads(config: dict[str, Any]) -> int:
    """Dobierz sensowną liczbę wątków zamiast domyślnych 4 CTranslate2."""
    configured = max(0, int(config.get("cpu_threads", 0) or 0))
    if configured:
        return configured
    logical = max(1, int(os.cpu_count() or 4))
    # Na typowym CPU z SMT połowa wątków logicznych odpowiada rdzeniom
    # fizycznym. Dla mniejszych układów nie obniżamy dostępnej liczby.
    estimated_physical = logical // 2 if logical >= 8 else logical
    return max(1, min(16, estimated_physical))


class SilentProgressStream:
    """tqdm musi mieć dokąd pisać, a w trybie okienkowym stdout nie istnieje."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def format_download_size(value: float) -> str:
    megabytes = max(0.0, float(value)) / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes:.0f} MB"


def make_download_progress_tqdm(
    model_name: str,
    status_callback,
    translator: Translator,
):
    """Pokaż postęp pobierania modelu w aplikacji zamiast w nieistniejącej konsoli."""

    if status_callback is None:
        return None

    lock = threading.Lock()
    state: dict[str, Any] = {"percent": -1, "seen_bytes": False}

    def report(unit: str, done: float, total: Optional[float]) -> None:
        if not total or total <= 0:
            return
        in_bytes = str(unit).strip().upper().startswith("B")
        percent = min(100, max(0, int(done * 100 / total)))
        with lock:
            if in_bytes:
                state["seen_bytes"] = True
            elif state["seen_bytes"]:
                # Licznik plików jest mniej czytelny niż postęp w bajtach.
                return
            if percent == state["percent"]:
                return
            state["percent"] = percent
        if in_bytes:
            status_callback(
                translator.t(
                    "Pobieram model {model_name}: {percent}% ({done} z {total})",
                    "Downloading model {model_name}: {percent}% ({done} of {total})",
                    model_name=model_name,
                    percent=percent,
                    done=format_download_size(done),
                    total=format_download_size(total),
                )
            )
        else:
            status_callback(
                translator.t(
                    "Pobieram pliki modelu {model_name}: {done} z {total}",
                    "Downloading model files for {model_name}: {done} of {total}",
                    model_name=model_name,
                    done=int(done),
                    total=int(total),
                )
            )

    class DownloadProgressTqdm(base_tqdm):
        def __init__(self, *args, **kwargs):
            # Globalnie wyłączone paski nie mogą uciszyć raportowania postępu,
            # a własny strumień zastępuje brakujące stdout w trybie okienkowym.
            kwargs.pop("name", None)
            kwargs["file"] = SilentProgressStream()
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            updated = super().update(n)
            try:
                report(self.unit, self.n, self.total)
            except Exception:
                logging.debug("Nie udało się pokazać postępu", exc_info=True)
            return updated

    return DownloadProgressTqdm


def load_model_local_first(
    model_name: str,
    kwargs: dict[str, Any],
    status_callback=None,
    translator: Optional[Translator] = None,
    *,
    force_download: bool = False,
) -> WhisperModel:
    """Załaduj cache bez odpytywania Hugging Face; sieć tylko przy braku plików."""
    translator = translator or Translator("pl")
    repo_id, revision = MODEL_SOURCES[model_name]
    local_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key not in {"download_root", "revision"}
    }

    def snapshot_complete(snapshot_path: str | os.PathLike[str]) -> bool:
        path = Path(snapshot_path)
        required = ("config.json", "model.bin", "tokenizer.json")
        return all((path / name).is_file() for name in required) and any(
            path.glob("vocabulary.*")
        )

    if force_download:
        if status_callback:
            status_callback(
                translator.t(
                    "Pobieram ponownie model {model_name}…",
                    "Downloading model {model_name} again…",
                    model_name=model_name,
                )
            )
        model_path = huggingface_hub.snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=str(MODEL_DIR),
            force_download=True,
            allow_patterns=MODEL_ALLOW_PATTERNS,
            tqdm_class=make_download_progress_tqdm(
                model_name, status_callback, translator
            ),
        )
        if not snapshot_complete(model_path):
            raise AppError(
                translator.t(
                    "Pobrany model {model_name} jest niekompletny.",
                    "Downloaded model {model_name} is incomplete.",
                    model_name=model_name,
                )
            )
        return WhisperModel(model_path, **local_kwargs)

    try:
        model_path = huggingface_hub.snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=str(MODEL_DIR),
            local_files_only=True,
            allow_patterns=MODEL_ALLOW_PATTERNS,
        )
        if not snapshot_complete(model_path):
            raise LocalEntryNotFoundError(
                f"Incomplete cached snapshot for {model_name}"
            )
    except LocalEntryNotFoundError as local_error:
        logging.info(
            "Brak kompletnego modelu %s w lokalnym cache (%s); pobieram pliki.",
            model_name,
            local_error,
        )
        if status_callback:
            status_callback(
                translator.t(
                    "Sprawdzam pliki modelu {model_name}…",
                    "Checking model files for {model_name}…",
                    model_name=model_name,
                )
            )
        try:
            model_path = huggingface_hub.snapshot_download(
                repo_id=repo_id,
                revision=revision,
                cache_dir=str(MODEL_DIR),
                local_files_only=False,
                allow_patterns=MODEL_ALLOW_PATTERNS,
                tqdm_class=make_download_progress_tqdm(
                    model_name, status_callback, translator
                ),
            )
        except Exception as download_error:
            logging.exception("Nie udało się pobrać modelu %s", model_name)
            raise AppError(
                translator.t(
                    "Nie udało się pobrać modelu {model_name}. "
                    "Sprawdź połączenie z internetem i spróbuj ponownie.",
                    "Could not download model {model_name}. "
                    "Check your internet connection and try again.",
                    model_name=model_name,
                )
            ) from download_error
        if not snapshot_complete(model_path):
            # Chybienie w cache zostało już zalogowane wyżej i nie jest
            # przyczyną niekompletnego świeżego pobrania.
            raise AppError(
                translator.t(
                    "Model {model_name} jest niekompletny.",
                    "Model {model_name} is incomplete.",
                    model_name=model_name,
                )
            ) from None
    return WhisperModel(model_path, **local_kwargs)


def ensure_cuda_runtime_available(
    status_callback=None,
    translator: Optional[Translator] = None,
) -> bool:
    """Dociągnij biblioteki CUDA, których instalator świadomie nie zawiera."""

    global CUDA_DLL_SEARCH_PATHS

    if os.name != "nt":
        return False
    if CUDA_DLL_SEARCH_PATHS:
        return True

    translator = translator or Translator("pl")
    root = cuda_runtime.user_runtime_root()
    if root is None:
        logging.warning("Brak profilu użytkownika na biblioteki GPU")
        return False

    if not cuda_runtime.is_runtime_complete(root):
        if status_callback:
            status_callback(
                translator.t(
                    "Przygotowuję biblioteki GPU…",
                    "Preparing GPU libraries…",
                )
            )
        state: dict[str, Any] = {"project": None, "percent": -1, "index": 0}

        def report(package, done: int, total: Optional[int]) -> None:
            if status_callback is None or not total:
                return
            if state["project"] != package.project:
                state["project"] = package.project
                state["percent"] = -1
                state["index"] += 1
            percent = min(100, max(0, int(done * 100 / total)))
            if percent == state["percent"]:
                return
            state["percent"] = percent
            status_callback(
                translator.t(
                    "Pobieram biblioteki GPU ({index} z {count}): {percent}%"
                    " ({done} z {total})",
                    "Downloading GPU libraries ({index} of {count}): {percent}%"
                    " ({done} of {total})",
                    index=state["index"],
                    count=len(cuda_runtime.CUDA_PACKAGES),
                    percent=percent,
                    done=format_download_size(done),
                    total=format_download_size(total),
                )
            )

        try:
            cuda_runtime.ensure_runtime(root, status_callback=report)
        except cuda_runtime.CudaRuntimeError as exc:
            logging.error("Nie udało się przygotować bibliotek GPU: %s", exc)
            raise AppError(
                translator.t(
                    "Nie udało się pobrać bibliotek GPU. Sprawdź połączenie "
                    "z internetem — dyktowanie zadziała na procesorze.",
                    "Could not download the GPU libraries. Check your internet "
                    "connection — dictation will run on the processor.",
                )
            ) from exc

    CUDA_DLL_SEARCH_PATHS = configure_cuda_dll_search_paths()
    if not CUDA_DLL_SEARCH_PATHS:
        logging.warning("Pobrane biblioteki GPU nie dały się załadować")
    return bool(CUDA_DLL_SEARCH_PATHS)


def warm_up_cuda_model(model: WhisperModel, config: dict[str, Any]) -> None:
    """Rozgrzej encoder i sprawdź CUDA bez uruchamiania dekodera na ciszy."""
    del config  # zachowany parametr ułatwia testowanie tej samej ścieżki
    features = model.feature_extractor(
        np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
    )
    model.encode(pad_or_trim(features))


def warm_up_voice_activity_detector() -> None:
    """Zbuduj sesję ONNX Silero, zanim użytkownik pierwszy raz naciśnie skrót.

    faster-whisper tworzy ją leniwie przy pierwszym ``transcribe``, więc koszt
    (zmierzone 0,32 s) płaciło pierwsze dyktowanie w sesji — już po tym, jak
    użytkownik puścił klawisz i czeka na tekst. Robimy to na etapie ładowania,
    gdzie i tak jest widoczny postęp. Dotyczy tak samo CPU jak GPU, bo VAD nie
    korzysta z karty; dotąd użytkownicy CPU nie mieli żadnej rozgrzewki.
    """

    try:
        from faster_whisper.vad import get_vad_model

        get_vad_model()
    except Exception:
        # Rozgrzewka jest wyłącznie optymalizacją — jeśli zawiedzie, VAD i tak
        # załaduje się leniwie przy pierwszym dyktowaniu.
        logging.debug("Nie udało się rozgrzać VAD", exc_info=True)


def create_model(
    config: dict[str, Any], status_callback=None, *, force_download: bool = False
) -> tuple[WhisperModel, str, str]:
    translator = Translator.from_config(config)
    model_name, device, compute_type = resolve_model_plan(config, translator)
    if status_callback:
        status_callback(
            translator.t(
                "Ładowanie modelu {model_name} ({device})…",
                "Loading model {model_name} ({device})…",
                model_name=model_name,
                device=device,
            )
        )

    kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "download_root": str(MODEL_DIR),
        "revision": MODEL_SOURCES[model_name][1],
    }
    cpu_threads = resolve_cpu_threads(config)
    if device == "cpu":
        kwargs["cpu_threads"] = cpu_threads

    logging.info(
        "Ładowanie modelu: model=%s device=%s compute_type=%s cpu_threads=%s",
        model_name,
        device,
        compute_type,
        cpu_threads if device == "cpu" else "n/d",
    )
    model: Optional[WhisperModel] = None
    try:
        if device == "cuda" and not ensure_cuda_runtime_available(
            status_callback, translator
        ):
            raise AppError(
                translator.t(
                    "Biblioteki GPU są niedostępne.",
                    "The GPU libraries are unavailable.",
                )
            )
        model = load_model_local_first(
            model_name,
            kwargs,
            status_callback,
            translator,
            force_download=force_download,
        )
        if bool(config.get("vad", {}).get("enabled", True)):
            warm_up_voice_activity_detector()
        if device == "cuda":
            if status_callback:
                status_callback(
                    translator.t(
                        "Optymalizuję model na GPU…",
                        "Optimizing model for GPU…",
                    )
                )
            warm_started = time.perf_counter()
            warm_up_cuda_model(model, config)
            logging.info(
                "Model GPU rozgrzany w %.3f s", time.perf_counter() - warm_started
            )
        return model, model_name, device
    except Exception as exc:
        logging.exception("Nie udało się uruchomić modelu na %s", device)
        if device != "cuda":
            raise

        error_summary = f"{type(exc).__name__}: {exc}"
        # Traceback rozgrzewki przechowuje argument ``model`` w swojej ramce.
        # Samo nadpisanie zmiennej lokalnej nie zwolniłoby wtedy pamięci GPU.
        failed_model = model
        model = None
        current_frame = sys._getframe()
        traceback_cursor = exc.__traceback__
        while traceback_cursor is not None:
            if traceback_cursor.tb_frame is not current_frame:
                try:
                    traceback_cursor.tb_frame.clear()
                except RuntimeError:
                    pass
            traceback_cursor = traceback_cursor.tb_next
        exc.__traceback__ = None
        del failed_model
        gc.collect()

        # Automatyczny bezpiecznik: jeżeli CUDA jest wykryta, ale brakuje bibliotek
        # lub sterownik jest niezgodny, program nadal ma działać na CPU.
        # Używamy już pobranego modelu, aby po błędzie CUDA nie ściągać
        # od razu drugiego, wielogigabajtowego wariantu.
        requested_model = str(config.get("model", "auto")).strip().lower()
        fallback_model = (
            "large-v3-turbo" if requested_model == "auto" else model_name
        )
        if status_callback:
            status_callback(
                translator.t(
                    "CUDA nie ruszyła — przełączam na CPU. "
                    "Szczegóły zapisano w logu.",
                    "CUDA failed to start — switching to CPU. "
                    "Details were saved to the log.",
                )
            )
        logging.warning(
            "Fallback CPU po błędzie CUDA (%s). Model: %s",
            error_summary,
            fallback_model,
        )
        model = load_model_local_first(
            fallback_model,
            {
                "device": "cpu",
                "compute_type": "int8",
                "download_root": str(MODEL_DIR),
                "revision": MODEL_SOURCES[fallback_model][1],
                "cpu_threads": cpu_threads,
            },
            status_callback,
            translator,
        )
        return model, fallback_model, "cpu"


def resolve_transcription_language(config: Mapping[str, Any]) -> Optional[str]:
    """Ustal j\u0119zyk przekazywany do Whispera; ``None`` oznacza autodetekcj\u0119.

    Autodetekcja kosztuje osobny przebieg enkodera \u2014 przy kr\u00f3tkiej komendzie
    tyle samo co sama transkrypcja, czyli podwaja czas oczekiwania. Pomiar na
    24 nagraniach po ok. 1,6 s: 873 ms na nagranie przy autodetekcji wobec
    464 ms przy podanym j\u0119zyku, przy identycznych transkrypcjach. Dlatego
    domy\u015blne ``auto`` idzie teraz za j\u0119zykiem interfejsu, a kto faktycznie
    dyktuje w kilku j\u0119zykach, wybiera ``detect``.
    """

    configured = str(config.get("language", "auto")).strip().lower()
    if configured == "detect":
        return None
    if configured and configured != "auto":
        return configured
    return resolve_ui_language(str(config.get("ui_language", "auto")))


# Whisper przycina prompt kontekstowy do ``max_length // 2 - 1`` = 223 token\u00f3w
# (faster_whisper/transcribe.py). Polskie s\u0142owa z diakrytykami tokenizuj\u0105 si\u0119
# po ~3-4 tokeny, wi\u0119c lista 120 termin\u00f3w (~1450 znak\u00f3w) traci\u0142a po drodze
# dwie trzecie pozycji \u2014 i ucina\u0142a ostatni\u0105 w po\u0142owie. Kikut w promptcie jest
# gorszy ni\u017c jego brak: zach\u0119ca model do halucynowania podobnych s\u0142\u00f3w.
# Tniemy wi\u0119c sami, po granicy terminu, z zapasem na zdanie kieruj\u0105ce.
TRANSCRIPTION_PROMPT_TOKEN_BUDGET = 223
TRANSCRIPTION_PROMPT_CHARS_PER_TOKEN = 4


def build_transcription_prompt(terms: Sequence[str], language: Optional[str]) -> str:
    """Zbuduj prompt kontekstowy dla Whispera z listy termin\u00f3w s\u0142ownika.

    ``hotwords`` w faster-whisper nie jest osobnym mechanizmem biasowania \u2014
    trafia dok\u0142adnie w ten sam slot ``<|startofprev|>`` co ``initial_prompt``,
    czyli jest zwyk\u0142ym promptem kontekstowym. Dlatego jego *styl* przecieka do
    wyj\u015bcia: go\u0142a lista rozdzielona przecinkami, bez kropki i bez wielkiej
    litery, statystycznie ci\u0105gnie dekoder ku tekstowi bez interpunkcji.
    Opakowujemy j\u0105 wi\u0119c w zdanie, kt\u00f3re modeluje po\u017c\u0105dany wynik.
    """

    lead = {
        "pl": "Transkrypcja po polsku, z pe\u0142n\u0105 interpunkcj\u0105 i wielkimi literami.",
        "en": "Transcript in English, fully punctuated and capitalised.",
    }.get(language or "", "")
    if not terms:
        return lead

    budget = TRANSCRIPTION_PROMPT_TOKEN_BUDGET * TRANSCRIPTION_PROMPT_CHARS_PER_TOKEN
    intro = f"{lead} W tek\u015bcie mog\u0105 wyst\u0105pi\u0107 nazwy: " if lead else ""
    if language and language != "pl" and lead:
        intro = f"{lead} Names that may appear: "
    remaining = budget - len(intro) - 1
    kept: list[str] = []
    for term in terms:
        addition = len(term) + (2 if kept else 0)
        if remaining - addition < 0:
            break
        kept.append(term)
        remaining -= addition
    if not kept:
        return lead
    if len(kept) < len(terms):
        logging.info(
            "S\u0142ownik: do promptu trafi\u0142o %d z %d termin\u00f3w (limit %d token\u00f3w).",
            len(kept),
            len(terms),
            TRANSCRIPTION_PROMPT_TOKEN_BUDGET,
        )
    return f"{intro}{', '.join(kept)}."


# Whisper wyuczył się tych fraz z napisów do filmów i wypluwa je na ciszy albo
# szumie. Domyślne progi faster-whisper ich nie łapią: pominięcie ciszy jest
# kasowane, gdy ``avg_logprob`` jest wysokie, a akurat te frazy model zna na
# pamięć, więc mają wysoki logprob mimo ``no_speech_prob`` bliskiego jedynki.
#
# Celowo NIE filtrujemy tu podziękowań ani pożegnań („Dziękuję", „Do widzenia").
# Owszem, Whisper też je halucynuje, ale to są zdania, które użytkownik ma
# pełne prawo naprawdę podyktować — zjedzenie prawdziwego tekstu byłoby gorsze
# niż przepuszczenie halucynacji.
WHISPER_SUBTITLE_HALLUCINATIONS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"napisy\s+(?:stworzone\s+)?przez\s+społeczność\s+amara\.org",
        r"napisy\s+(?:zrobione|wykonane|tworzone)\s+przez\s+\S+",
        r"subtitles\s+by\s+the\s+amara\.org\s+community",
        r"zapraszam(?:y)?\s+do\s+subskrypcji(?:\s+kanału)?",
        r"do\s+zobaczenia\s+w\s+(?:następnym|kolejnym)\s+odcinku",
        r"dziękuję\s+za\s+(?:obejrzenie|uwagę)\s+(?:filmu|materiału|odcinka)",
    )
)


def strip_whisper_hallucinations(text: str) -> str:
    """Usuń frazy z napisów, które Whisper wypluwa na ciszy i szumie.

    Pusty wynik jest już poprawnie obsłużony wyżej jako „brak mowy" — nic nie
    zostaje wklejone, a kapsuła pokazuje błąd zamiast udawać sukces.
    """

    cleaned = text
    for pattern in WHISPER_SUBTITLE_HALLUCINATIONS:
        cleaned = pattern.sub(" ", cleaned)
    if cleaned == text:
        return text
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?…-")
    logging.info(
        "Odfiltrowano halucynację Whispera (%d → %d znaków).",
        len(text),
        len(cleaned),
    )
    return cleaned


def normalize_transcript(text: str) -> str:
    text = text.replace("\u00a0", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text


def normalize_custom_command_phrase(value: Any) -> str:
    """Utwórz klucz używany przez wydzielony silnik własnych komend."""
    return command_engine.normalize_command_phrase(value)


def custom_command_definition_to_dict(
    definition: command_engine.CommandDefinition,
) -> dict[str, Any]:
    """Zamień bezpieczny model runtime na format edytowalny przez UI."""
    item: dict[str, Any] = {
        "id": definition.id,
        "phrase": definition.phrase,
        "match": definition.match_mode,
        "action": definition.action,
        "value": definition.value,
        "confirm": definition.confirm,
    }
    options = definition.terminal_options
    if options is not None:
        item["options"] = {
            "cwd_source": options.cwd_source,
            "host": options.host,
            "shell": options.shell,
            "draft_delivery": options.draft_delivery,
        }
        if options.fixed_cwd is not None:
            item["options"]["fixed_cwd"] = options.fixed_cwd
    return item


def configured_custom_commands(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Zwróć poprawne i jednoznaczne komendy z potencjalnie ręcznego JSON-a."""
    registry = command_engine.CommandRegistry.from_config(config)
    return [
        custom_command_definition_to_dict(definition)
        for definition in registry.definitions
    ]


def partition_custom_command_items(
    config: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    list[Any],
]:
    """Rozdziel wpisy edytowalne przez UI od wpisów wymagających zachowania.

    Nieobsługiwane rekordy mogą pochodzić z ręcznej edycji albo nowszej wersji
    programu. Panel ich nie wykonuje i nie pokazuje jako aktywnych, ale nie może
    ich po cichu usuwać podczas zapisu zupełnie innego ustawienia.
    """
    settings = config.get("custom_commands", {})
    if not command_engine.custom_commands_schema_supported(settings):
        raw_items = settings.get("items", []) if isinstance(settings, dict) else []
        unmanaged = (
            [copy.deepcopy(item) for item in raw_items]
            if isinstance(raw_items, list)
            else []
        )
        return [], {}, unmanaged

    valid_items = configured_custom_commands(config)
    valid_keys = {
        (
            str(item.get("match", command_engine.MATCH_EXACT)),
            normalize_custom_command_phrase(item["phrase"]),
        )
        for item in valid_items
    }
    raw_items = settings.get("items", []) if isinstance(settings, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []

    original_by_key: dict[str, dict[str, Any]] = {}
    seen_valid_keys: set[tuple[str, str]] = set()
    unmanaged: list[Any] = []
    for raw_item in raw_items:
        single_config = {
            "custom_commands": {
                "items": [raw_item],
            }
        }
        single = configured_custom_commands(single_config)
        normalized = (
            normalize_custom_command_phrase(single[0]["phrase"])
            if single
            else ""
        )
        match_key = (
            str(single[0].get("match", command_engine.MATCH_EXACT))
            if single
            else ""
        )
        valid_key = (match_key, normalized)
        if (
            valid_key in valid_keys
            and valid_key not in seen_valid_keys
            and isinstance(raw_item, dict)
        ):
            original_copy = copy.deepcopy(raw_item)
            original_by_key.setdefault(normalized, original_copy)
            command_id = single[0].get("id") if single else None
            if isinstance(command_id, str) and command_id:
                original_by_key[f"id:{command_id}"] = copy.deepcopy(raw_item)
            seen_valid_keys.add(valid_key)
        else:
            unmanaged.append(copy.deepcopy(raw_item))
    return valid_items, original_by_key, unmanaged


def custom_commands_settings_for_save(
    original: Any,
    *,
    enabled: bool,
    trigger: str,
    items: list[Any],
) -> Any:
    """Update only a schema understood by this build; preserve foreign data."""

    if not command_engine.custom_commands_schema_supported(original):
        return copy.deepcopy(original)
    updated = copy.deepcopy(dict(original))
    updated.update(
        {
            "schema_version": command_engine.CUSTOM_COMMANDS_SCHEMA_VERSION,
            "enabled": enabled is True,
            "trigger": trigger,
            "items": copy.deepcopy(items),
        }
    )
    return updated


def custom_command_context_denial(
    context: command_engine.ExecutionContext,
    *,
    now: Optional[float] = None,
    require_foreground: bool = False,
) -> Optional[str]:
    """Return a stable denial code for stale or incomplete command context."""

    captured_at = context.captured_at
    if (
        type(captured_at) not in (int, float)
        or not math.isfinite(captured_at)
        or captured_at <= 0
    ):
        return "invalid_command_context"
    current = time.monotonic() if now is None else now
    if (
        type(current) not in (int, float)
        or not math.isfinite(current)
        or current < captured_at
        or current - captured_at > MAX_CUSTOM_COMMAND_CONTEXT_AGE_SECONDS
    ):
        return "stale_command_context"
    if require_foreground and (
        type(context.foreground_hwnd) is not int
        or type(context.foreground_pid) is not int
        or context.foreground_hwnd <= 0
        or context.foreground_pid <= 0
    ):
        return "command_target_unavailable"
    return None


def custom_command_lookup(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        normalize_custom_command_phrase(item["phrase"]): item
        for item in configured_custom_commands(config)
        if item.get("match", command_engine.MATCH_EXACT)
        == command_engine.MATCH_EXACT
    }


def match_custom_command(
    transcript: str,
    config: dict[str, Any],
) -> Optional[dict[str, Any]]:
    match = command_engine.CommandRegistry.from_config(config).match(transcript)
    if match is None:
        return None
    item = custom_command_definition_to_dict(match.definition)
    if match.spoken_tail:
        item["spoken_tail"] = match.spoken_tail
    return item


def format_custom_command_confirmation_preview(
    action: str,
    value: str,
    translator: Optional[Translator] = None,
) -> str:
    """Render exact boundaries and line endings without echoing them to logs."""

    if action != "paste_text":
        return value
    translator = translator or Translator("pl")
    visible = value.replace("\r", "␍").replace("\n", "␊\n")
    return translator.t(
        "Granice treści: ⟦{visible}⟧\n␍ oznacza CR, a ␊ oznacza LF/Enter.",
        "Content boundaries: ⟦{visible}⟧\n␍ means CR; ␊ means LF/Enter.",
        visible=visible,
    )


def confirm_custom_command_action(
    action: str,
    value: str,
    translator: Optional[Translator] = None,
) -> bool:
    """Poproś o zgodę natywnym oknem Windows przed akcją systemową."""
    translator = translator or Translator("pl")
    if os.name != "nt":
        return False
    action_label = translator.t(
        "otwarcie programu, pliku lub strony",
        "opening an app, file, or website",
    )
    if action == "paste_text":
        action_label = translator.t(
            "wklejenie wielowierszowego tekstu",
            "inserting multi-line text",
        )
    preview = format_custom_command_confirmation_preview(
        action,
        value,
        translator,
    )
    message = translator.t(
        "Mówik rozpoznał komendę proszącą o {action}.\n\n"
        "Czy na pewno chcesz kontynuować?\n\n{preview}",
        "Mówik recognized a command requesting {action}.\n\n"
        "Do you want to continue?\n\n{preview}",
        action=action_label,
        preview=preview,
    )
    try:
        # MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST
        result = ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None,
            message,
            translator.t(
                "Mówik — potwierdź akcję",
                "Mówik — Confirm action",
            ),
            0x00000001 | 0x00000030 | 0x00000100 | 0x00040000,
        )
    except Exception:
        logging.exception("Nie udało się wyświetlić potwierdzenia komendy")
        return False
    return result == 1  # IDOK


def resolve_custom_command_open_target(value: str) -> str:
    """Resolve an HTTPS URL or an existing safe local path, never PATH lookup."""

    if not isinstance(value, str):
        raise CustomOpenTargetError("target_not_string")
    if value != value.strip():
        raise CustomOpenTargetError("target_outer_whitespace")
    target = os.path.expandvars(os.path.expanduser(value))
    try:
        target = command_engine.validate_open_target_syntax(
            target,
            maximum=MAX_CUSTOM_OPEN_TARGET_LENGTH,
        )
    except command_engine.CommandValidationError as exc:
        raise CustomOpenTargetError("target_invalid") from exc

    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme.casefold() == "https":
        return target

    try:
        candidate = Path(target)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CustomOpenTargetError("target_missing") from exc
    if str(resolved).startswith(("\\\\", "//")):
        raise CustomOpenTargetError("network_path_denied")
    if not windows_actions.is_local_filesystem_path(resolved):
        raise CustomOpenTargetError("network_or_unknown_drive_denied")
    if not (resolved.is_file() or resolved.is_dir()):
        raise CustomOpenTargetError("target_not_file_or_directory")
    try:
        command_engine.validate_open_target_syntax(
            str(resolved),
            maximum=MAX_CUSTOM_OPEN_TARGET_LENGTH,
        )
    except command_engine.CommandValidationError as exc:
        raise CustomOpenTargetError("resolved_target_invalid") from exc
    return str(resolved)


def open_custom_command_target(
    value: str,
    translator: Optional[Translator] = None,
) -> None:
    translator = translator or Translator("auto")
    if os.name != "nt":
        raise AppError(
            translator.t(
                "Otwieranie celów własnych komend jest obsługiwane na Windowsie.",
                "Opening custom-command targets is supported on Windows.",
            )
        )
    try:
        target = resolve_custom_command_open_target(value)
    except CustomOpenTargetError as exc:
        raise AppError(
            translator.t(
                "Skonfigurowany cel otwierania jest niedostępny lub niebezpieczny.",
                "The configured open target is unavailable or unsafe.",
            )
        ) from exc
    try:
        os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        raise AppError(
            translator.t(
                "Nie udało się otworzyć skonfigurowanego celu: {error}",
                "Could not open the configured target: {error}",
                error=exc,
            )
        ) from exc


# Sygnał zwrotny własnej komendy: status końcowy jest już ustawiony i
# wywołujący nie powinien go nadpisywać domyślnym "Gotowy".
COMMAND_STATUS_HANDLED = "handled"


@dataclass(frozen=True)
class SpeechJob:
    audio: np.ndarray
    mode: str = "dictation"
    released_at: Optional[float] = None
    execution_context: Optional[command_engine.ExecutionContext] = None
    delivery_foreground: Optional[tuple[int, int]] = None
    sample_rate: Optional[int] = None


def apply_voice_commands(text: str, config: dict[str, Any]) -> str:
    if not config.get("voice_commands", {}).get("enabled", False):
        return text
    language = str(config.get("language", "auto")).strip().lower()
    if language in VOICE_COMMAND_REPLACEMENTS:
        replacements = VOICE_COMMAND_REPLACEMENTS[language]
    else:
        replacements = (
            *VOICE_COMMAND_REPLACEMENTS["pl"],
            *VOICE_COMMAND_REPLACEMENTS["en"],
        )
    result = text
    for pattern, replacement in replacements:
        result = pattern.sub(replacement, result)
    return normalize_transcript(result)


def strip_llm_wrapping(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
        if value.lower().startswith("text\n"):
            value = value[5:]
    value = re.sub(
        (
            r"^(poprawiony tekst|wynik|transkrypcja|corrected text|result|"
            r"transcription)\s*:\s*"
        ),
        "",
        value,
        flags=re.IGNORECASE,
    )
    quote_pairs = {
        ('"', '"'),
        ("'", "'"),
        ("„", "”"),
        ("“", "”"),
        ("«", "»"),
        ("‹", "›"),
    }
    if len(value) >= 2 and (value[0], value[-1]) in quote_pairs:
        value = value[1:-1].strip()
    return value


def llm_result_is_safe(original: str, corrected: str) -> bool:
    if not corrected:
        return False
    ratio = len(corrected) / max(1, len(original))
    if ratio < 0.60 or ratio > 1.40:
        return False
    similarity = difflib.SequenceMatcher(
        None, original.casefold(), corrected.casefold()
    ).ratio()
    if similarity < 0.55:
        return False
    if re.findall(r"\d+(?:[.,]\d+)?", original) != re.findall(
        r"\d+(?:[.,]\d+)?", corrected
    ):
        return False
    negation_pattern = (
        r"\b(?:"
        r"nie|nigdy|żaden|żadna|żadne|bez|"
        r"not|no|never|without|cannot|"
        r"nicht|niemals|kein(?:e|en|er|es|em)?|ohne|"
        r"ne|pas|jamais|sans|aucun(?:e)?|"
        r"nunca|jamás|sin|ningún|ninguna|"
        r"не|ні|ніколи|без"
        r")\b|\b\w+n['’]t\b|\bn['’]"
    )
    original_negations = sorted(
        match.group(0).casefold().replace("’", "'")
        for match in re.finditer(negation_pattern, original, flags=re.IGNORECASE)
    )
    corrected_negations = sorted(
        match.group(0).casefold().replace("’", "'")
        for match in re.finditer(negation_pattern, corrected, flags=re.IGNORECASE)
    )
    return original_negations == corrected_negations


class _RejectOllamaRedirects(urllib.request.HTTPRedirectHandler):
    """Keep a validated loopback request from being redirected off-host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_local_ollama_request(
    request: urllib.request.Request,
    timeout: int,
):
    # Do not inherit HTTP_PROXY/registry proxy settings: the transcript must
    # travel directly to the already validated loopback endpoint.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectOllamaRedirects(),
    )
    return opener.open(request, timeout=timeout)


def normalize_ollama_base_url(
    value: Any,
    translator: Optional[Translator] = None,
) -> str:
    """Allow transcripts to be sent only to an explicit local Ollama endpoint."""

    translator = translator or Translator("auto")
    raw = str(value).strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise AppError(
            translator.t(
                "Adres Ollamy jest nieprawidłowy.",
                "The Ollama address is invalid.",
            )
        ) from exc
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    local_host = hostname == "localhost"
    if hostname and not local_host:
        try:
            local_host = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local_host = False
    if (
        parsed.scheme.casefold() != "http"
        or not local_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is None and parsed.netloc.endswith(":"))
    ):
        raise AppError(
            translator.t(
                "Ollama musi używać lokalnego adresu HTTP (localhost, 127.0.0.1 lub ::1).",
                "Ollama must use a local HTTP address (localhost, 127.0.0.1, or ::1).",
            )
        )
    return raw


def cleanup_with_ollama(
    text: str, config: dict[str, Any], dictionary_terms: list[str]
) -> str:
    settings = config.get("ollama_cleanup", {})
    if not settings.get("enabled", False):
        return text
    model = str(settings.get("model", "")).strip()
    if not model:
        logging.warning("Korekta Ollama włączona, ale nie podano nazwy modelu")
        return text

    try:
        base_url = normalize_ollama_base_url(
            settings.get("url", "http://127.0.0.1:11434")
        )
    except AppError:
        logging.warning("Zablokowano nielokalny lub nieprawidłowy adres Ollamy")
        return text
    timeout = min(
        MAX_OLLAMA_TIMEOUT_SECONDS,
        max(1, int(settings.get("timeout_seconds", 45))),
    )
    # Przy dawnym odczycie wprost z configu polski użytkownik z domyślnym
    # „auto” dostawał angielski prompt do polskiego tekstu, przez co małe
    # modele lokalne chętnie anglicyzowały interpunkcję.
    transcription_language = resolve_transcription_language(config) or ""
    glossary = ", ".join(dictionary_terms[:80])
    if transcription_language == "pl":
        glossary = glossary or "brak"
        system_prompt = (
            "Jesteś bardzo zachowawczym korektorem polskiej transkrypcji mowy. "
            "Zwróć wyłącznie poprawiony tekst, bez komentarza i bez cudzysłowu. "
            "Wolno poprawić interpunkcję, wielkie litery, oczywiste literówki i "
            "jednoznaczne błędy rozpoznania dźwięku. Nie parafrazuj, nie skracaj, "
            "nie dodawaj informacji. Nigdy nie zmieniaj liczb, nazw własnych, negacji "
            "ani znaczenia. Gdy nie masz pewności, pozostaw fragment bez zmian."
        )
        user_prompt = (
            f"Słownik preferowanych zapisów: {glossary}\n\nTekst:\n{text}"
        )
    else:
        glossary = glossary or "none"
        system_prompt = (
            "You are an extremely conservative proofreader of a speech "
            "transcription. Preserve the language of the input. Return only the "
            "corrected text, without commentary or quotation marks. You may fix "
            "punctuation, capitalization, obvious spelling mistakes, and "
            "unambiguous speech-recognition errors. Do not paraphrase, shorten, or "
            "add information. Never change numbers, proper names, negations, or "
            "meaning. When unsure, leave the passage unchanged."
        )
        user_prompt = (
            f"Preferred spellings: {glossary}\n\nTranscription:\n{text}"
        )
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0, "seed": 0},
    }
    request = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _open_local_ollama_request(request, timeout) as response:
            response_bytes = response.read(1_048_577)
            if len(response_bytes) > 1_048_576:
                raise ValueError("Ollama response exceeds the safe size limit")
            body = json.loads(response_bytes.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("Ollama response must be a JSON object")
        message = body.get("message")
        if not isinstance(message, dict):
            raise ValueError("Ollama response is missing the message object")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("Ollama response message content must be text")
        corrected = strip_llm_wrapping(content)
    except (
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        urllib.error.URLError,
    ) as exc:
        logging.warning("Korekta Ollama niedostępna: %s", exc)
        return text

    corrected = normalize_transcript(corrected)
    if not llm_result_is_safe(text, corrected):
        logging.warning("Odrzucono zbyt agresywną korektę LLM")
        return text
    return corrected


CUSTOM_SOUND_KINDS = {"start", "stop", "done", "error"}
BUILTIN_SOUND_NOTES: dict[str, tuple[tuple[float, int, int], ...]] = {
    # częstotliwość Hz, czas tonu ms, cisza po tonie ms
    "start": ((523.25, 72, 0),),
    "stop": ((392.00, 68, 0),),
    "done": ((523.25, 55, 10), (659.25, 74, 0)),
    "error": ((329.63, 75, 8), (277.18, 95, 0)),
}
BUILTIN_SOUND_FALLBACK_TONES = {
    "start": (520, 45),
    "stop": (390, 40),
    "done": (660, 55),
    "error": (280, 100),
}


@lru_cache(maxsize=len(BUILTIN_SOUND_NOTES))
def builtin_sound_wav(kind: str) -> bytes:
    """Zbuduj cichy PCM WAV z łagodnym atakiem i wybrzmieniem, bez kliknięć."""
    notes = BUILTIN_SOUND_NOTES.get(kind)
    if notes is None:
        raise AppError(
            Translator("auto").t(
                "Nieznany wbudowany dźwięk: {kind}",
                "Unknown built-in sound: {kind}",
                kind=kind,
            )
        )

    sample_rate = 44_100
    pieces: list[np.ndarray] = []
    for frequency, duration_ms, gap_ms in notes:
        sample_count = max(1, int(round(sample_rate * duration_ms / 1000)))
        timeline = np.arange(sample_count, dtype=np.float64) / sample_rate
        tone = np.sin(2 * np.pi * frequency * timeline)
        tone += 0.14 * np.sin(4 * np.pi * frequency * timeline + 0.35)

        envelope = np.ones(sample_count, dtype=np.float64)
        attack = min(sample_count // 2, max(1, int(sample_rate * 0.008)))
        release = min(sample_count - attack, max(1, int(sample_rate * 0.032)))
        envelope[:attack] = np.sin(
            np.linspace(0.0, np.pi / 2, attack, endpoint=False)
        ) ** 2
        if release:
            envelope[-release:] = np.cos(
                np.linspace(0.0, np.pi / 2, release, endpoint=True)
            ) ** 2
        pieces.append(0.075 * tone * envelope)
        if gap_ms:
            pieces.append(
                np.zeros(int(round(sample_rate * gap_ms / 1000)), dtype=np.float64)
            )

    waveform = np.concatenate(pieces) if pieces else np.zeros(1, dtype=np.float64)
    waveform[0] = 0.0
    waveform[-1] = 0.0
    pcm = np.asarray(np.clip(waveform, -1.0, 1.0) * 32767, dtype=np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def play_builtin_sound(kind: str) -> None:
    """Odtwórz wbudowany sygnał w bieżącym wątku; wywołujący nie blokuje UI."""
    try:
        import winsound

        winsound.PlaySound(
            builtin_sound_wav(kind),
            winsound.SND_MEMORY | winsound.SND_NODEFAULT,
        )
        return
    except Exception:
        logging.debug("Nie udało się odtworzyć łagodnego WAV", exc_info=True)

    try:
        import winsound

        frequency, duration = BUILTIN_SOUND_FALLBACK_TONES.get(kind, (520, 45))
        winsound.Beep(frequency, duration)
    except Exception:
        logging.debug("Nie udało się odtworzyć sygnału awaryjnego", exc_info=True)


def play_builtin_sound_async(kind: str, thread_name: Optional[str] = None) -> None:
    threading.Thread(
        target=play_builtin_sound,
        args=(kind,),
        name=thread_name or f"Feedback-{kind}",
        daemon=True,
    ).start()


def resolve_sound_path(value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = APPDATA_DIR / path
    return path


def _path_is_reparse_point(path: Path) -> bool:
    """Treat symlinks and Windows reparse points as untrusted at runtime."""

    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & 0x0400)


def runtime_sound_path(value: Any) -> Optional[Path]:
    """Resolve only a local WAV stored below Mówik's private sounds folder."""

    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("/", "\\")
    # ``\\`` obejmuje UNC oraz prefiksy ``\\?\`` i ``\\.\``; ``\??\`` to
    # nazwa z menedżera obiektów NT i zaczyna się od pojedynczego ukośnika.
    if normalized.startswith(("\\\\", "\\??\\")):
        return None
    configured = Path(raw).expanduser()
    candidate = configured if configured.is_absolute() else APPDATA_DIR / configured
    try:
        lexical_root = Path(os.path.abspath(SOUNDS_DIR))
        lexical_candidate = Path(os.path.abspath(candidate))
        relative_parts = lexical_candidate.relative_to(lexical_root).parts
    except (OSError, ValueError):
        return None
    if _path_is_reparse_point(APPDATA_DIR) or _path_is_reparse_point(SOUNDS_DIR):
        return None
    current = lexical_root
    for part in relative_parts:
        current /= part
        if _path_is_reparse_point(current):
            return None
    try:
        root = SOUNDS_DIR.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if _path_is_reparse_point(root):
        return None
    if resolved.suffix.casefold() != ".wav" or not resolved.is_file():
        return None
    return resolved


def validate_wave_file(
    path: Path,
    translator: Optional[Translator] = None,
) -> None:
    translator = translator or Translator("auto")
    if path.suffix.lower() != ".wav":
        raise AppError(
            translator.t(
                "Własny dźwięk musi być plikiem WAV.",
                "A custom sound must be a WAV file.",
            )
        )
    if not path.is_file():
        raise AppError(
            translator.t(
                "Nie znaleziono pliku dźwięku: {path}",
                "Sound file not found: {path}",
                path=path,
            )
        )
    if path.stat().st_size > 50 * 1024 * 1024:
        raise AppError(
            translator.t(
                "Plik WAV jest większy niż 50 MB.",
                "The WAV file is larger than 50 MB.",
            )
        )
    try:
        with wave.open(str(path), "rb") as wav_file:
            if wav_file.getnframes() <= 0:
                raise AppError(
                    translator.t(
                        "Plik WAV nie zawiera dźwięku: {path}",
                        "The WAV file contains no audio: {path}",
                        path=path,
                    )
                )
            if wav_file.getcomptype() != "NONE":
                raise AppError(
                    translator.t(
                        "Plik WAV musi używać nieskompresowanego formatu PCM.",
                        "The WAV file must use uncompressed PCM audio.",
                    )
                )
    except (OSError, wave.Error) as exc:
        raise AppError(
            translator.t(
                "Nieprawidłowy plik WAV „{name}”: {error}",
                "Invalid WAV file “{name}”: {error}",
                name=path.name,
                error=exc,
            )
        ) from exc


def import_custom_sound(
    kind: str,
    source_value: Any,
    translator: Optional[Translator] = None,
) -> str:
    """Skopiuj wskazany WAV do prywatnego folderu Mówika."""
    translator = translator or Translator("auto")
    if kind not in CUSTOM_SOUND_KINDS:
        raise AppError(
            translator.t(
                "Nieznany rodzaj dźwięku: {kind}",
                "Unknown sound type: {kind}",
                kind=kind,
            )
        )
    source = resolve_sound_path(source_value)
    if source is None:
        return ""
    source = source.resolve()
    validate_wave_file(source, translator)
    ensure_directories()
    destination = (SOUNDS_DIR / f"{kind}.wav").resolve()
    if source != destination:
        shutil.copy2(source, destination)
    validate_wave_file(destination, translator)
    return str(Path("sounds") / destination.name)


def save_config_with_custom_sounds(
    config: dict[str, Any],
    sound_sources: dict[str, Any],
    translator: Optional[Translator] = None,
    *,
    expected_revision: Any = _CONFIG_REVISION_UNSET,
) -> str:
    """Replace custom WAVs and config as one rollback-capable operation."""

    translator = translator or Translator("auto")
    validate_config_types(config)
    require_non_elevated_config_write(config)
    payload_bytes = _serialize_config_payload(config, translator)
    ensure_directories()
    # The revision check, sound replacements, config replacement, and any
    # rollback must share one cross-process critical section.  Otherwise a
    # losing Settings window could restore its backup over the winner's WAV.
    with config_write_guard():
        backups: dict[Path, Optional[Path]] = {}
        staged_sources: dict[Path, Path] = {}
        try:
            if (
                expected_revision is not _CONFIG_REVISION_UNSET
                and config_file_revision(translator) != expected_revision
            ):
                raise ConfigConflict(
                    translator.t(
                        "Plik config.json został zmieniony w innym oknie. "
                        "Otwórz Ustawienia ponownie i spróbuj jeszcze raz.",
                        "config.json changed in another window. "
                        "Reopen Settings and try again.",
                    )
                )

            prepared: dict[str, tuple[Path, Path]] = {}
            for kind in sorted(CUSTOM_SOUND_KINDS):
                source = resolve_sound_path(sound_sources.get(kind, ""))
                if source is None:
                    continue
                source = source.resolve()
                destination = (SOUNDS_DIR / f"{kind}.wav").resolve()
                validate_wave_file(source, translator)
                prepared[kind] = (source, destination)

            # A user may select another managed sound as a source (for
            # example, copy the current start.wav to the stop cue). Snapshot
            # those cross-references before replacing any destination, or the
            # outcome would depend on alphabetical processing order.
            managed_destinations = {
                (SOUNDS_DIR / f"{kind}.wav").resolve()
                for kind in CUSTOM_SOUND_KINDS
            }
            for source, destination in prepared.values():
                if source == destination or source not in managed_destinations:
                    continue
                if source in staged_sources:
                    continue
                staged = SOUNDS_DIR / (
                    f".{source.name}.{os.getpid()}.{uuid.uuid4().hex}.staged.wav"
                )
                staged_sources[source] = staged
                shutil.copy2(source, staged)
                validate_wave_file(staged, translator)

            for kind in sorted(prepared):
                source, destination = prepared[kind]
                if source == destination:
                    continue
                backup: Optional[Path] = None
                if destination.exists():
                    backup = destination.with_name(
                        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.bak"
                    )
                    shutil.copy2(destination, backup)
                backups[destination] = backup
                import_custom_sound(
                    kind,
                    str(staged_sources.get(source, source)),
                    translator,
                )
            # The revision was checked before touching any WAV.  Passing the
            # sentinel avoids a redundant read while the same guard is held.
            return _write_config_locked(
                payload_bytes,
                _CONFIG_REVISION_UNSET,
                translator,
            )
        except Exception:
            for destination, backup in reversed(tuple(backups.items())):
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    elif backup.exists():
                        os.replace(backup, destination)
                except OSError:
                    logging.exception("Nie udało się wycofać importu dźwięku")
            raise
        finally:
            for backup in backups.values():
                if backup is not None:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError:
                        logging.warning(
                            "Nie udało się usunąć kopii zapasowej dźwięku",
                            exc_info=True,
                        )
            for staged in staged_sources.values():
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    logging.warning(
                        "Nie udało się usunąć tymczasowej kopii dźwięku",
                        exc_info=True,
                    )


def configured_sound_path(config: dict[str, Any], kind: str) -> Optional[Path]:
    feedback = config.get("feedback", {})
    custom = feedback.get("custom_sounds", {})
    if not isinstance(custom, dict):
        return None
    path = runtime_sound_path(custom.get(kind, ""))
    if path is None:
        return None
    return path


def settings_sound_sources_from_config(config: dict[str, Any]) -> dict[str, str]:
    """Return managed sound paths suitable for the next Settings save."""

    custom = config.get("feedback", {}).get("custom_sounds", {})
    if not isinstance(custom, dict):
        custom = {}
    result: dict[str, str] = {}
    for kind in ("start", "stop", "done", "error"):
        managed_path = resolve_sound_path(custom.get(kind, ""))
        result[kind] = str(managed_path) if managed_path is not None else ""
    return result


def windows_set_clipboard_text(
    text: str,
    translator: Optional[Translator] = None,
) -> None:
    translator = translator or Translator("auto")
    if os.name != "nt":
        raise AppError(
            translator.t(
                "Wklejanie jest obsługiwane wyłącznie na Windowsie.",
                "Pasting is supported only on Windows.",
            )
        )
    last_error: Optional[pyperclip.PyperclipException] = None
    for retry_delay in (*CLIPBOARD_WRITE_RETRY_DELAYS, None):
        try:
            pyperclip.copy(text)
            return
        except pyperclip.PyperclipException as exc:
            last_error = exc
            if retry_delay is not None:
                time.sleep(retry_delay)
    assert last_error is not None
    raise AppError(
        translator.t(
            "Nie udało się zapisać tekstu do schowka: {error}",
            "Could not copy text to the clipboard: {error}",
            error=last_error,
        )
    ) from last_error


def windows_get_clipboard_text(
    translator: Optional[Translator] = None,
) -> str:
    """Read clipboard text for a last-moment integrity check before Ctrl+V."""

    translator = translator or Translator("auto")
    if os.name != "nt":
        raise AppError(
            translator.t(
                "Schowek jest obsługiwany wyłącznie na Windowsie.",
                "The clipboard is supported only on Windows.",
            )
        )
    try:
        value = pyperclip.paste()
    except pyperclip.PyperclipException as exc:
        raise AppError(
            translator.t(
                "Nie udało się sprawdzić zawartości schowka.",
                "The clipboard contents could not be verified.",
            )
        ) from exc
    return value if isinstance(value, str) else str(value)


def capture_paste_target_identity(
    foreground: Optional[windows_actions.ForegroundContext] = None,
) -> tuple[int, int, int, int, int]:
    """Capture the top-level window and its focused GUI control atomically enough.

    The focused/control handles catch tab, pane, and edit-control changes that
    leave the same process and top-level HWND active.  Zeroes are deliberately
    returned on any ambiguity so delayed input fails closed.
    """

    invalid = (0, 0, 0, 0, 0)
    try:
        foreground = foreground or windows_actions.capture_foreground_identity()
        if not foreground.is_valid or os.name != "nt":
            return invalid

        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(GUITHREADINFO),
        ]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL

        process_id = wintypes.DWORD()
        thread_id = int(
            user32.GetWindowThreadProcessId(
                wintypes.HWND(foreground.hwnd),
                ctypes.byref(process_id),
            )
        )
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if (
            thread_id <= 0
            or int(process_id.value) != foreground.pid
            or not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info))
        ):
            return invalid
        active_hwnd = int(info.hwndActive or 0)
        focus_hwnd = int(info.hwndFocus or 0)
        caret_hwnd = int(info.hwndCaret or 0)
        if active_hwnd != foreground.hwnd or focus_hwnd <= 0:
            return invalid
        return (
            foreground.hwnd,
            foreground.pid,
            thread_id,
            focus_hwnd,
            caret_hwnd,
        )
    except Exception:
        return invalid


def foreground_identity_matches(expected: tuple[int, ...]) -> bool:
    """Fail closed unless the exact captured window/control is still focused."""

    if (
        not isinstance(expected, tuple)
        or len(expected) not in {2, 5}
        or any(type(value) is not int for value in expected)
        or any(value <= 0 for value in expected[:2])
    ):
        return False
    current = windows_actions.capture_foreground_identity()
    top_level_matches = (
        current.is_valid
        and current.hwnd == expected[0]
        and current.pid == expected[1]
    )
    if not top_level_matches:
        return False
    if len(expected) == 2:
        # Compatibility for API callers predating focused-control pinning.
        return True
    if any(value <= 0 for value in expected[2:4]) or expected[4] < 0:
        return False
    return capture_paste_target_identity(current) == expected


def require_foreground_identity(
    expected: tuple[int, ...],
    translator: Optional[Translator] = None,
) -> None:
    if foreground_identity_matches(expected):
        return
    translator = translator or Translator("auto")
    raise AppError(
        translator.t(
            "Aktywne okno zmieniło się — tekst nie został wklejony.",
            "The active window changed, so the text was not pasted.",
        )
    )


def windows_type_unicode_text(
    text: str,
    translator: Optional[Translator] = None,
    cancel_event: Optional[threading.Event] = None,
    expected_foreground: Optional[tuple[int, ...]] = None,
) -> None:
    """Wpisz tekst przez Win32 SendInput bez używania schowka."""
    translator = translator or Translator("auto")
    if os.name != "nt":
        raise AppError(
            translator.t(
                "Wpisywanie tekstu jest obsługiwane wyłącznie na Windowsie.",
                "Typing text is supported only on Windows.",
            )
        )
    if not text:
        return

    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004


    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    ]
    user32.SendInput.restype = wintypes.UINT

    encoded = text.encode("utf-16-le")
    code_units = [
        int.from_bytes(encoded[index : index + 2], "little")
        for index in range(0, len(encoded), 2)
    ]
    for offset in range(0, len(code_units), 256):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled()
        if expected_foreground is not None:
            require_foreground_identity(expected_foreground, translator)
        inputs: list[INPUT] = []
        for code_unit in code_units[offset : offset + 256]:
            press = INPUT()
            press.type = INPUT_KEYBOARD
            press.ki = KEYBDINPUT(
                0,
                code_unit,
                KEYEVENTF_UNICODE,
                0,
                MOWIK_INJECTED_INPUT_SIGNATURE,
            )
            release = INPUT()
            release.type = INPUT_KEYBOARD
            release.ki = KEYBDINPUT(
                0,
                code_unit,
                KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                0,
                MOWIK_INJECTED_INPUT_SIGNATURE,
            )
            inputs.extend((press, release))
        array_type = INPUT * len(inputs)
        array = array_type(*inputs)
        sent = int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))
        if sent != len(inputs):
            raise AppError(
                translator.t(
                    "Windows wysłał tylko {sent} z {total} zdarzeń klawiatury.",
                    "Windows sent only {sent} of {total} keyboard events.",
                    sent=sent,
                    total=len(inputs),
                )
            )
    if expected_foreground is not None:
        require_foreground_identity(expected_foreground, translator)


def paste_text(
    text: str,
    config: dict[str, Any],
    *,
    append_space_override: Optional[bool] = None,
    expected_foreground: Optional[tuple[int, ...]] = None,
    verify_clipboard_before_paste: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    translator = Translator.from_config(config)
    settings = config.get("paste", {})
    paste_enabled = bool(settings.get("enabled", True))
    copy_to_clipboard = bool(settings.get("copy_to_clipboard", True))
    append_space = (
        bool(settings.get("append_space", True))
        if append_space_override is None
        else bool(append_space_override)
    )

    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled()

    if not paste_enabled and not copy_to_clipboard:
        raise AppError(
            translator.t(
                "Włącz automatyczne wklejanie lub kopiowanie tekstu do schowka.",
                "Enable automatic pasting or copying text to the clipboard.",
            )
        )

    if not paste_enabled:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled()
        if copy_to_clipboard:
            windows_set_clipboard_text(text, translator)
        return

    if expected_foreground is not None:
        require_foreground_identity(expected_foreground, translator)

    delay = max(0, int(settings.get("delay_ms", 25))) / 1000
    if delay:
        time.sleep(delay)
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled()
    if expected_foreground is not None:
        require_foreground_identity(expected_foreground, translator)

    if copy_to_clipboard:
        # Ustaw tekst dopiero po opóźnieniu. Następnie sprawdź dokładną
        # zawartość i fokus bezpośrednio przed pojedynczym Ctrl+V.
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled()
        windows_set_clipboard_text(text, translator)
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled()
        if expected_foreground is not None:
            require_foreground_identity(expected_foreground, translator)
        # Every Ctrl+V-capable path verifies the exact clipboard payload.  The
        # argument remains for source compatibility but can no longer disable
        # this last-moment integrity check.
        if windows_get_clipboard_text(translator) != text:
            raise AppError(
                translator.t(
                    "Schowek zmienił się — tekst nie został wklejony.",
                    "The clipboard changed, so the text was not pasted.",
                )
            )
        if expected_foreground is not None:
            require_foreground_identity(expected_foreground, translator)
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled()
        if expected_foreground is not None and "\r" not in text and "\n" not in text:
            payload = text
            if append_space and text and not text[-1].isspace():
                payload += " "
            windows_type_unicode_text(
                payload,
                translator,
                cancel_event,
                expected_foreground,
            )
            return
        controller = keyboard.Controller()
        with controller.pressed(keyboard.Key.ctrl):
            controller.press("v")
            controller.release("v")
        if append_space and text and not text[-1].isspace():
            time.sleep(0.02)
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled()
            if expected_foreground is not None:
                require_foreground_identity(expected_foreground, translator)
            controller.press(keyboard.Key.space)
            controller.release(keyboard.Key.space)
    else:
        payload = text
        if append_space and text and not text[-1].isspace():
            payload += " "
        windows_type_unicode_text(
            payload,
            translator,
            cancel_event,
            expected_foreground,
        )


def key_name(key: keyboard.Key | keyboard.KeyCode) -> str:
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            return f"vk{key.vk}"
        return ""
    return key.name or str(key).replace("Key.", "")


def mouse_name(button: mouse.Button) -> str:
    value = str(button).replace("Button.", "")
    aliases = {"x1": "x1", "x2": "x2", "left": "left", "right": "right", "middle": "middle"}
    return aliases.get(value, value)


def start_shortcut_capture_listeners(on_key, on_mouse) -> dict[str, Any]:
    """Start suppressing shortcut detectors with rollback on partial failure."""

    listeners: dict[str, Any] = {
        "keyboard": keyboard.Listener(on_press=on_key, suppress=True),
        "mouse": mouse.Listener(on_click=on_mouse, suppress=True),
    }
    try:
        listeners["keyboard"].start()
        listeners["mouse"].start()
    except Exception:
        for listener in listeners.values():
            try:
                listener.stop()
            except Exception:
                logging.debug("Nie udało się wycofać hooka skrótu", exc_info=True)
        raise
    return listeners


def split_trigger(
    trigger: str,
    translator: Optional[Translator] = None,
) -> tuple[str, str]:
    translator = translator or Translator("pl")
    parts = trigger.strip().lower().split(":", 1)
    if len(parts) != 2 or parts[0] not in {"keyboard", "mouse"} or not parts[1]:
        raise AppError(
            translator.t(
                "trigger musi wyglądać np. tak: keyboard:f8 albo mouse:x2.",
                "trigger must look like keyboard:f8 or mouse:x2.",
            )
        )
    return parts[0], parts[1]


def trigger_display_name(
    trigger: str,
    translator: Optional[Translator] = None,
) -> str:
    translator = translator or Translator("pl")
    trigger_type, name = split_trigger(trigger, translator)
    if trigger_type == "mouse":
        mouse_labels = {
            "left": translator.t("lewy przycisk", "left button"),
            "right": translator.t("prawy przycisk", "right button"),
            "middle": translator.t("środkowy przycisk", "middle button"),
            "x1": translator.t("boczny przycisk X1", "side button X1"),
            "x2": translator.t("boczny przycisk X2", "side button X2"),
        }
        return translator.t(
            "Mysz: {label}",
            "Mouse: {label}",
            label=mouse_labels.get(name, name.upper()),
        )

    key_labels = {
        "pause": "Pause/Break",
        "scroll_lock": "Scroll Lock",
        "caps_lock": "Caps Lock",
        "space": translator.t("Spacja", "Space"),
        "tab": "Tab",
        "insert": "Insert",
        "delete": "Delete",
        "home": "Home",
        "end": "End",
        "page_up": "Page Up",
        "page_down": "Page Down",
        "ctrl": "Ctrl",
        "ctrl_l": translator.t("Lewy Ctrl", "Left Ctrl"),
        "ctrl_r": translator.t("Prawy Ctrl", "Right Ctrl"),
        "alt": "Alt",
        "alt_l": translator.t("Lewy Alt", "Left Alt"),
        "alt_r": translator.t("Prawy Alt", "Right Alt"),
        "shift": "Shift",
        "shift_l": translator.t("Lewy Shift", "Left Shift"),
        "shift_r": translator.t("Prawy Shift", "Right Shift"),
        "cmd": "Windows",
        "cmd_l": translator.t("Lewy Windows", "Left Windows"),
        "cmd_r": translator.t("Prawy Windows", "Right Windows"),
    }
    if name in key_labels:
        label = key_labels[name]
    elif len(name) == 1 or re.fullmatch(r"f\d{1,2}", name):
        label = name.upper()
    elif name.startswith("vk") and name[2:].isdigit():
        label = translator.t(
            "klawisz VK {number}",
            "VK key {number}",
            number=name[2:],
        )
    else:
        label = name.replace("_", " ").title()
    return translator.t(
        "Klawiatura: {label}",
        "Keyboard: {label}",
        label=label,
    )


def tray_state_for_status(status: str, error: bool = False) -> str:
    """Mapuj komunikat aplikacji na niewielki zestaw stanów ikony zasobnika."""
    normalized = status.casefold()
    if error or "błąd" in normalized or "error" in normalized:
        return "error"
    if "nagrywanie" in normalized or "recording" in normalized:
        return "recording"
    if normalized.startswith(
        (
            "gotowy",
            "ready",
            "nagranie było",
            "recording was",
            "nie wykryłem",
            "no clear speech",
        )
    ):
        return "ready"
    if normalized.startswith(
        (
            "cuda nie ruszyła",
            "cuda failed",
            "kończę",
            "finishing",
            "kopiuję",
            "copying",
            "ładowanie",
            "loading",
            "przełączam",
            "switching",
            "przygotowuję",
            "preparing",
            "rozpoznaję",
            "transcribing",
            "stosuję",
            "applying",
            "wklejam",
            "pasting",
            "włączam profil",
            "activating profile",
        )
    ):
        return "processing"
    return "idle"


@lru_cache(maxsize=6)
def make_tray_image(state: str = "idle") -> Image.Image:
    """Utwórz ikonę mikrofonu; wariant ``brand`` nie pokazuje stanu."""
    state_colors = {
        "brand": (59, 130, 246, 255),
        "idle": (100, 116, 139, 255),
        "ready": (34, 197, 94, 255),
        "recording": (244, 63, 94, 255),
        "processing": (59, 130, 246, 255),
        "error": (239, 68, 68, 255),
    }
    if state not in state_colors:
        state = "idle"

    accent = state_colors[state]
    surface = (20, 28, 44, 255)
    foreground = (248, 250, 252, 255)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((3, 3, 61, 61), radius=17, fill=surface)
    draw.rounded_rectangle((5, 5, 59, 59), radius=15, outline=accent, width=4)

    # Prosty, kontrastowy mikrofon pozostaje czytelny po skalowaniu do 16–24 px.
    draw.rounded_rectangle((23, 10, 41, 36), radius=9, fill=foreground)
    draw.arc((17, 21, 47, 47), start=0, end=180, fill=foreground, width=4)
    draw.line((32, 45, 32, 52), fill=foreground, width=4)
    draw.rounded_rectangle((23, 51, 41, 55), radius=2, fill=foreground)

    if state == "brand":
        return image

    # Plakietka rozróżnia stany także kształtem, nie tylko kolorem.
    draw.ellipse((43, 42, 62, 61), fill=surface)
    draw.ellipse((45, 44, 60, 59), fill=accent)
    if state == "idle":
        draw.line((49, 52, 56, 52), fill=foreground, width=2)
    elif state == "ready":
        draw.line((49, 52, 52, 55, 57, 49), fill=foreground, width=2)
    elif state == "recording":
        draw.ellipse((50, 49, 55, 54), fill=foreground)
    elif state == "processing":
        draw.arc((49, 48, 57, 56), start=205, end=80, fill=foreground, width=2)
    else:
        draw.line((53, 48, 53, 53), fill=foreground, width=2)
        draw.ellipse((52, 55, 54, 57), fill=foreground)
    return image


STATUS_INDICATOR_STATES = frozenset(
    {
        "hidden",
        "recording",
        "processing",
        "success",
        "error",
        "command_recording",
        "command_processing",
        "command_success",
    }
)
# Okno jest większe niż sama kapsuła: nadmiarowy margines mieści rozmyty cień
# i akcentową poświatę, które przy warstwowym oknie są naprawdę przezroczyste.
STATUS_INDICATOR_WIDTH = 360
STATUS_INDICATOR_HEIGHT = 112
STATUS_INDICATOR_MARGIN_X = 22
STATUS_INDICATOR_MARGIN_TOP = 20
STATUS_INDICATOR_MARGIN_BOTTOM = 24
# Zachowany alias dla prostych integracji i starszych testów renderera.
STATUS_INDICATOR_SIZE = STATUS_INDICATOR_HEIGHT
# Odstęp mierzony do krawędzi okna; wizualnie kapsuła siedzi o margines wyżej.
STATUS_INDICATOR_BOTTOM_MARGIN = 12
# Każdy stan dostaje parę barw; gradient między nimi biegnie przez wizualizator
# i pierścień ikony, dzięki czemu kapsuła czyta się jako jeden obiekt.
STATUS_INDICATOR_ACCENTS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "recording": ((56, 189, 248), (129, 140, 248)),
    "command": ((192, 132, 252), (244, 114, 182)),
    "processing": ((45, 212, 191), (56, 189, 248)),
    "success": ((52, 211, 153), (110, 231, 183)),
    "error": ((248, 113, 113), (251, 146, 60)),
}
STATUS_INDICATOR_SURFACE_TOP = (26, 32, 52)
STATUS_INDICATOR_SURFACE_BOTTOM = (12, 15, 27)
STATUS_INDICATOR_MIN_PROCESSING_SECONDS = 0.25
STATUS_INDICATOR_SUCCESS_SECONDS = 1.8
STATUS_INDICATOR_ERROR_SECONDS = 1.35


def status_indicator_window_position(
    work_area: tuple[int, int, int, int],
    size: int | tuple[int, int] = (
        STATUS_INDICATOR_WIDTH,
        STATUS_INDICATOR_HEIGHT,
    ),
    bottom_margin: int = STATUS_INDICATOR_BOTTOM_MARGIN,
) -> tuple[int, int]:
    """Wycentruj wskaźnik nad dolną krawędzią obszaru roboczego monitora."""
    left, top, right, bottom = (int(value) for value in work_area)
    if right <= left or bottom <= top:
        raise ValueError("The monitor work area must have a positive size")
    if isinstance(size, tuple):
        width, height = size
    else:
        width = height = size
    width = max(1, int(width))
    height = max(1, int(height))
    bottom_margin = max(0, int(bottom_margin))
    max_x = max(left, right - width)
    max_y = max(top, bottom - height)
    x = left + (right - left - width) // 2
    y = bottom - bottom_margin - height
    return min(max(x, left), max_x), min(max(y, top), max_y)


@lru_cache(maxsize=24)
def _status_indicator_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """Załaduj systemowy krój kapsuły z bezpiecznym fallbackiem Pillow."""

    candidates = (
        "segoeuib.ttf" if bold else "segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, max(8, int(size)))
        except OSError:
            continue
    return ImageFont.load_default()


def _status_indicator_graphemes(text: str) -> list[str]:
    """Split enough Unicode grapheme structure to avoid broken preview tails."""

    clusters: list[str] = []
    regional_run = 0
    for character in text:
        codepoint = ord(character)
        combining = bool(unicodedata.combining(character)) or unicodedata.category(
            character
        ).startswith("M")
        variation = 0xFE00 <= codepoint <= 0xFE0F
        emoji_modifier = 0x1F3FB <= codepoint <= 0x1F3FF
        regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        joins_previous = bool(
            clusters
            and (
                combining
                or variation
                or emoji_modifier
                or character == "\u200d"
                or clusters[-1].endswith("\u200d")
                or (regional and regional_run % 2 == 1)
            )
        )
        if joins_previous:
            clusters[-1] += character
        else:
            clusters.append(character)
        regional_run = regional_run + 1 if regional else 0
    return clusters


def status_indicator_preview_text(text: Any, max_characters: int = 72) -> str:
    """Zwiń poufny podgląd do jednego krótkiego wiersza bez łamania grafemów."""

    normalized = " ".join(str(text or "").split())
    maximum = max(8, int(max_characters))
    if len(normalized) <= maximum:
        return normalized
    result: list[str] = []
    length = 0
    for cluster in _status_indicator_graphemes(normalized):
        if length + len(cluster) + 1 > maximum:
            break
        result.append(cluster)
        length += len(cluster)
    return "".join(result).rstrip() + "…"


def _status_indicator_fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    maximum_width: int,
) -> str:
    """Ellipsize one normalized line to its real rendered pixel width."""

    value = status_indicator_preview_text(text, 256)
    maximum_width = max(1, int(maximum_width))
    if draw.textbbox((0, 0), value, font=font)[2] <= maximum_width:
        return value
    ellipsis = "…"
    if draw.textbbox((0, 0), ellipsis, font=font)[2] > maximum_width:
        return ""
    clusters = _status_indicator_graphemes(value.removesuffix(ellipsis))
    low = 0
    high = len(clusters)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = "".join(clusters[:middle]).rstrip() + ellipsis
        if draw.textbbox((0, 0), candidate, font=font)[2] <= maximum_width:
            low = middle
        else:
            high = middle - 1
    return "".join(clusters[:low]).rstrip() + ellipsis


def _indicator_mix(
    first: tuple[int, int, int],
    second: tuple[int, int, int],
    position: float,
) -> tuple[int, int, int]:
    """Zmieszaj dwie barwy akcentu w podanym punkcie gradientu."""

    position = min(1.0, max(0.0, float(position)))
    return tuple(  # type: ignore[return-value]
        int(round(first[index] + (second[index] - first[index]) * position))
        for index in range(3)
    )


@lru_cache(maxsize=8)
def _indicator_capsule_body(
    size: tuple[int, int],
    radius: int,
) -> Image.Image:
    """Korpus kapsuły z pionowym gradientem zamiast sztucznego połysku."""

    body_width, body_height = size
    column = Image.new("RGB", (1, body_height))
    pixels = column.load()
    for row in range(body_height):
        pixels[0, row] = _indicator_mix(
            STATUS_INDICATOR_SURFACE_TOP,
            STATUS_INDICATOR_SURFACE_BOTTOM,
            row / max(1, body_height - 1),
        )
    body = column.resize(size, Image.Resampling.BILINEAR).convert("RGBA")
    mask = Image.new("L", (body_width * 2, body_height * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, body_width * 2 - 1, body_height * 2 - 1),
        radius=radius * 2,
        fill=252,
    )
    body.putalpha(mask.resize(size, Image.Resampling.LANCZOS))
    return body


@lru_cache(maxsize=6)
def _indicator_chrome(
    size: tuple[int, int],
    box: tuple[int, int, int, int],
    radius: int,
    accent: tuple[int, int, int],
    scale: int,
) -> Image.Image:
    """Nieruchome tło kapsuły: cień, poświata, korpus i obwódka.

    Nic tutaj nie zależy od numeru klatki ani od poziomu mikrofonu, więc cała
    warstwa jest liczona raz na stan i tylko kopiowana przy 20 kl./s. Cień i
    poświata powstają w rozdzielczości docelowej — rozmycie i tak zjada
    schodki, a rozmywanie płótna 3× było najdroższą operacją klatki.
    """

    width, height = size
    left, top, right, bottom = box
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (left, top + 5, right, bottom + 5),
        radius=radius,
        fill=(2, 6, 23, 170),
    )
    chrome = shadow.filter(ImageFilter.GaussianBlur(7))
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        box,
        radius=radius,
        fill=(*accent, 70),
    )
    chrome.alpha_composite(glow.filter(ImageFilter.GaussianBlur(9)))
    chrome = chrome.resize(
        (width * scale, height * scale),
        Image.Resampling.BILINEAR,
    )
    chrome.alpha_composite(
        _indicator_capsule_body(
            ((right - left) * scale, (bottom - top) * scale),
            radius * scale,
        ),
        (left * scale, top * scale),
    )
    ImageDraw.Draw(chrome).rounded_rectangle(
        (left * scale, top * scale, right * scale, bottom * scale),
        radius=radius * scale,
        outline=(*_indicator_mix(accent, (148, 163, 184), 0.45), 125),
        width=scale,
    )
    return chrome


def _indicator_microphone(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    center_y: float,
    size: float,
    color: tuple[int, int, int],
) -> None:
    """Sylwetka mikrofonu — czytelniejsza niż abstrakcyjna kropka."""

    body_width = size * 0.52
    body_height = size * 0.78
    draw.rounded_rectangle(
        (
            center_x - body_width / 2,
            center_y - body_height / 2 - size * 0.08,
            center_x + body_width / 2,
            center_y + body_height * 0.16,
        ),
        radius=body_width / 2,
        fill=(*color, 255),
    )
    arc_radius = size * 0.46
    stroke = max(1, int(size * 0.13))
    draw.arc(
        (
            center_x - arc_radius,
            center_y - arc_radius * 0.72,
            center_x + arc_radius,
            center_y + arc_radius,
        ),
        start=15,
        end=165,
        fill=(*color, 255),
        width=stroke,
    )
    draw.line(
        (center_x, center_y + size * 0.42, center_x, center_y + size * 0.62),
        fill=(*color, 255),
        width=stroke,
    )


def render_status_indicator_frame(
    state: str,
    frame: int = 0,
    width: int = STATUS_INDICATOR_WIDTH,
    height: int = STATUS_INDICATOR_HEIGHT,
    *,
    level: float = 0.0,
    label: str = "",
    detail: str = "",
) -> Image.Image:
    """Renderuj antyaliasowaną klatkę nowoczesnej kapsuły dyktowania."""
    if state not in STATUS_INDICATOR_STATES:
        raise ValueError(f"Unknown status indicator state: {state}")
    output_width = max(160, int(width))
    output_height = max(48, int(height))
    # The whole canonical layout is scaled as one image. Previously only the
    # outer canvas grew on HiDPI displays while icons, text and offsets stayed
    # at 100%, leaving tiny content in the upper-left of an oversized capsule.
    width = STATUS_INDICATOR_WIDTH
    height = STATUS_INDICATOR_HEIGHT
    level = min(1.0, max(0.0, float(level)))
    scale = 3
    canvas_width = width * scale
    canvas_height = height * scale
    if state == "hidden":
        return Image.new(
            "RGBA",
            (output_width, output_height),
            (0, 0, 0, 0),
        )
    command_mode = state.startswith("command_")
    base_state = state.removeprefix("command_")

    if base_state == "error":
        accent, accent_end = STATUS_INDICATOR_ACCENTS["error"]
    elif command_mode:
        accent, accent_end = STATUS_INDICATOR_ACCENTS["command"]
    elif base_state == "success":
        accent, accent_end = STATUS_INDICATOR_ACCENTS["success"]
    elif base_state == "processing":
        accent, accent_end = STATUS_INDICATOR_ACCENTS["processing"]
    else:
        accent, accent_end = STATUS_INDICATOR_ACCENTS["recording"]
    soft_accent = (*_indicator_mix(accent_end, (255, 255, 255), 0.28), 255)

    capsule_left = STATUS_INDICATOR_MARGIN_X
    capsule_top = STATUS_INDICATOR_MARGIN_TOP
    capsule_right = width - STATUS_INDICATOR_MARGIN_X
    capsule_bottom = height - STATUS_INDICATOR_MARGIN_BOTTOM
    capsule_height = capsule_bottom - capsule_top
    capsule_radius = capsule_height // 2

    image = _indicator_chrome(
        (width, height),
        (capsule_left, capsule_top, capsule_right, capsule_bottom),
        capsule_radius,
        accent,
        scale,
    ).copy()
    draw = ImageDraw.Draw(image)

    icon_x = (capsule_left + 24) * scale
    icon_y = (capsule_top + capsule_height // 2) * scale
    icon_radius = 17 * scale

    if base_state == "recording":
        # Oddech ikony niesie informację o poziomie mikrofonu również wtedy,
        # gdy wizualizator zasłoni długa etykieta.
        pulse = (math.sin(int(frame) * 0.28) + 1.0) / 2.0
        halo_radius = icon_radius * (0.95 + 0.28 * pulse + 0.12 * level)
        # Rozmycie liczone na kwadraciku wokół ikony, a nie na całym płótnie —
        # różnica to rząd wielkości w koszcie klatki.
        halo_blur = 4 * scale
        halo_span = int(halo_radius + halo_blur * 3)
        halo = Image.new("RGBA", (halo_span * 2, halo_span * 2), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse(
            (
                halo_span - halo_radius,
                halo_span - halo_radius,
                halo_span + halo_radius,
                halo_span + halo_radius,
            ),
            fill=(*accent, 90),
        )
        image.alpha_composite(
            halo.filter(ImageFilter.GaussianBlur(halo_blur)),
            (int(icon_x) - halo_span, int(icon_y) - halo_span),
        )
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (
                icon_x - icon_radius,
                icon_y - icon_radius,
                icon_x + icon_radius,
                icon_y + icon_radius,
            ),
            fill=(*_indicator_mix(accent, (10, 14, 26), 0.72), 255),
            outline=(*accent, 190),
            width=scale,
        )
        _indicator_microphone(draw, icon_x, icon_y, icon_radius * 1.15, accent_end)
    elif base_state == "processing":
        spinner_radius = int(icon_radius * 0.92)
        spinner_box = (
            icon_x - spinner_radius,
            icon_y - spinner_radius,
            icon_x + spinner_radius,
            icon_y + spinner_radius,
        )
        stroke = 3 * scale
        draw.ellipse(spinner_box, outline=(51, 65, 85, 200), width=stroke)
        start = (int(frame) * 14 - 90) % 360
        segments = 12
        for index in range(segments):
            segment_start = start + index * (250 / segments)
            draw.arc(
                spinner_box,
                start=segment_start,
                end=segment_start + (250 / segments) + 1,
                fill=(
                    *_indicator_mix(
                        accent,
                        accent_end,
                        index / (segments - 1),
                    ),
                    255,
                ),
                width=stroke,
            )
    else:
        draw.ellipse(
            (
                icon_x - icon_radius,
                icon_y - icon_radius,
                icon_x + icon_radius,
                icon_y + icon_radius,
            ),
            fill=(*accent, 255),
        )
        glyph = icon_radius * 1.5
        stroke = max(scale, int(glyph * 0.22))
        if base_state == "success":
            draw.line(
                (
                    (icon_x - glyph * 0.42, icon_y + glyph * 0.02),
                    (icon_x - glyph * 0.10, icon_y + glyph * 0.34),
                    (icon_x + glyph * 0.45, icon_y - glyph * 0.34),
                ),
                fill=(255, 255, 255, 255),
                width=stroke,
                joint="curve",
            )
        else:
            offset = glyph * 0.32
            draw.line(
                (
                    icon_x - offset,
                    icon_y - offset,
                    icon_x + offset,
                    icon_y + offset,
                ),
                fill=(255, 255, 255, 255),
                width=stroke,
            )
            draw.line(
                (
                    icon_x + offset,
                    icon_y - offset,
                    icon_x - offset,
                    icon_y + offset,
                ),
                fill=(255, 255, 255, 255),
                width=stroke,
            )

    # Wszystko żyje w jednej, pionowo wyśrodkowanej linii — bez mikroskopijnej
    # etykiety w rogu i pustki pod nią, która psuła rytm poprzedniej wersji.
    label = status_indicator_preview_text(label, 128)
    detail = status_indicator_preview_text(detail, 256)
    text_x = icon_x + icon_radius + 14 * scale
    text_right = canvas_width - (
        STATUS_INDICATOR_MARGIN_X + (46 if base_state == "processing" else 16)
    ) * scale
    available_text_width = max(scale, text_right - text_x)

    if base_state == "recording":
        label_font = _status_indicator_font(13 * scale, bold=True)
        label = _status_indicator_fit_text(
            draw,
            label,
            label_font,
            available_text_width,
        )
        draw.text(
            (text_x, icon_y),
            label,
            font=label_font,
            fill=(226, 232, 240, 255),
            anchor="lm",
        )
        wave_left = text_x + draw.textbbox((0, 0), label, font=label_font)[2]
        wave_left += 14 * scale
        wave_right = canvas_width - (STATUS_INDICATOR_MARGIN_X + 16) * scale
        bar_count = 16
        if wave_right - wave_left >= 16 * scale:
            slot = (wave_right - wave_left) / bar_count
            bar_width = slot * 0.58
            half_height = 15 * scale
            for index in range(bar_count):
                position = index / (bar_count - 1)
                phase = int(frame) * 0.30 + index * 0.55
                wave = 0.45 + 0.55 * abs(math.sin(phase))
                envelope = (
                    math.sin(math.pi * (index + 0.5) / bar_count) ** 0.65
                )
                # Minimum jest ułamkiem pełnej wysokości, a nie stałą równą
                # szerokości słupka — cicha mowa przestaje wyglądać jak rząd
                # identycznych kropek udających pasek ładowania.
                amplitude = 0.14 + 0.86 * level * wave * envelope
                bar_height = max(bar_width, half_height * 2 * amplitude)
                x = wave_left + index * slot + (slot - bar_width) / 2
                draw.rounded_rectangle(
                    (
                        x,
                        icon_y - bar_height / 2,
                        x + bar_width,
                        icon_y + bar_height / 2,
                    ),
                    radius=bar_width / 2,
                    fill=(*_indicator_mix(accent, accent_end, position), 255),
                )
    else:
        text = detail if base_state == "success" and detail else label
        text_font = _status_indicator_font(14 * scale, bold=base_state != "success")
        draw.text(
            (text_x, icon_y),
            _status_indicator_fit_text(
                draw,
                text,
                text_font,
                available_text_width,
            ),
            font=text_font,
            fill=(241, 245, 249, 255),
            anchor="lm",
        )

    if base_state == "processing":
        dot_start = canvas_width - (STATUS_INDICATOR_MARGIN_X + 38) * scale
        active_dot = (int(frame) // 3) % 3
        for index in range(3):
            dot_radius = (3 if index == active_dot else 2) * scale
            x = dot_start + index * 10 * scale
            draw.ellipse(
                (
                    x - dot_radius,
                    icon_y - dot_radius,
                    x + dot_radius,
                    icon_y + dot_radius,
                ),
                fill=(
                    soft_accent if index == active_dot else (71, 85, 105, 255)
                ),
            )

    rendered = image.resize((width, height), Image.Resampling.LANCZOS)
    if (output_width, output_height) != (width, height):
        rendered = rendered.resize(
            (output_width, output_height),
            Image.Resampling.LANCZOS,
        )
    return rendered


class _IndicatorPoint(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))


class _IndicatorSize(ctypes.Structure):
    _fields_ = (("cx", ctypes.c_long), ("cy", ctypes.c_long))


class _IndicatorBlendFunction(ctypes.Structure):
    _fields_ = (
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    )


class _IndicatorBitmapInfoHeader(ctypes.Structure):
    _fields_ = (
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    )


def _premultiplied_bgra(image: Image.Image) -> bytes:
    """Przetłumacz klatkę RGBA na format oczekiwany przez ``AC_SRC_ALPHA``.

    GDI kompozytuje warstwowe okna zakładając kanały już przemnożone przez
    alfę i ułożone jako BGRA. Bez tego półprzezroczysty cień rozjaśniałby się
    do białej mgły zamiast przyciemniać pulpit.
    """

    buffer = np.asarray(image.convert("RGBA"), dtype=np.uint16)
    alpha = buffer[:, :, 3]
    premultiplied = (buffer[:, :, :3] * alpha[:, :, None] + 127) // 255
    payload = np.empty(buffer.shape, dtype=np.uint8)
    payload[:, :, 0] = premultiplied[:, :, 2]
    payload[:, :, 1] = premultiplied[:, :, 1]
    payload[:, :, 2] = premultiplied[:, :, 0]
    payload[:, :, 3] = alpha.astype(np.uint8)
    return np.ascontiguousarray(payload).tobytes()


def blit_layered_window(hwnd: int, image: Image.Image, x: int, y: int) -> None:
    """Wyślij klatkę na ekran z pełną przezroczystością każdego piksela.

    ``UpdateLayeredWindow`` zastępuje sztuczkę z kolorem kluczowym: dopiero
    ono pozwala na rozmyty cień i poświatę, bo półprzezroczyste piksele są
    mieszane z pulpitem, a nie z tłem okna.
    """

    width, height = image.size
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    user32.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_IndicatorPoint),
        ctypes.POINTER(_IndicatorSize),
        ctypes.c_void_p,
        ctypes.POINTER(_IndicatorPoint),
        ctypes.c_uint32,
        ctypes.POINTER(_IndicatorBlendFunction),
        ctypes.c_uint32,
    ]
    user32.UpdateLayeredWindow.restype = ctypes.c_bool

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise ctypes.WinError(ctypes.get_last_error())
    memory_dc = None
    bitmap = None
    previous_bitmap = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise ctypes.WinError(ctypes.get_last_error())
        header = _IndicatorBitmapInfoHeader()
        header.biSize = ctypes.sizeof(_IndicatorBitmapInfoHeader)
        header.biWidth = width
        header.biHeight = -height  # ujemna wysokość = układ od góry w dół
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            memory_dc,
            ctypes.byref(header),
            0,  # DIB_RGB_COLORS
            ctypes.byref(bits),
            None,
            0,
        )
        if not bitmap or not bits:
            raise ctypes.WinError(ctypes.get_last_error())
        payload = _premultiplied_bgra(image)
        ctypes.memmove(bits, payload, len(payload))
        previous_bitmap = gdi32.SelectObject(memory_dc, bitmap)
        blend = _IndicatorBlendFunction(0, 0, 255, 1)  # AC_SRC_OVER/AC_SRC_ALPHA
        destination = _IndicatorPoint(int(x), int(y))
        span = _IndicatorSize(width, height)
        source = _IndicatorPoint(0, 0)
        ctypes.set_last_error(0)
        updated = user32.UpdateLayeredWindow(
            ctypes.c_void_p(hwnd),
            screen_dc,
            ctypes.byref(destination),
            ctypes.byref(span),
            memory_dc,
            ctypes.byref(source),
            0,
            ctypes.byref(blend),
            2,  # ULW_ALPHA
        )
        if not updated:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if memory_dc:
            if previous_bitmap:
                gdi32.SelectObject(memory_dc, previous_bitmap)
            gdi32.DeleteDC(memory_dc)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        user32.ReleaseDC(None, screen_dc)


class _IndicatorRect(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


class _IndicatorMonitorInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _IndicatorRect),
        ("rcWork", _IndicatorRect),
        ("dwFlags", ctypes.c_ulong),
    )


def active_monitor_work_area() -> Optional[tuple[int, int, int, int]]:
    """Zwróć obszar roboczy monitora z aktualnie aktywnym oknem."""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_IndicatorMonitorInfo),
        ]
        user32.GetMonitorInfoW.restype = ctypes.c_bool
        window = user32.GetForegroundWindow()
        monitor = user32.MonitorFromWindow(window, 2)  # nearest monitor
        if not monitor:
            return None
        info = _IndicatorMonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return work.left, work.top, work.right, work.bottom
    except (AttributeError, OSError):
        logging.debug(
            "Nie udało się ustalić monitora dla wskaźnika",
            exc_info=True,
        )
        return None


class FloatingStatusIndicator:
    """Nieaktywujące okno statusu sterowane bezpiecznie z dowolnego wątku."""

    def __init__(
        self,
        enabled: bool = True,
        translator: Optional[Translator] = None,
    ) -> None:
        self.enabled = bool(enabled and os.name == "nt")
        self._commands: queue.Queue[
            Optional[
                tuple[
                    str,
                    Optional[tuple[int, int, int, int]],
                    str,
                    str,
                ]
            ]
        ] = queue.Queue()
        self.translator = translator or Translator("auto")
        self._root = None
        self._command_lock = threading.Lock()
        self._level_source = None
        self._closed = threading.Event()
        self._failed = threading.Event()

    def start(self) -> bool:
        """Przygotuj Tk w głównym wątku przed uruchomieniem pętli zasobnika."""
        if self._root is not None:
            return True
        if not self.enabled or self._closed.is_set() or self._failed.is_set():
            return False
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("The status indicator must start on the main thread")
        return self._prepare_window()

    def set_level_source(self, source) -> None:
        """Podłącz lekki, tylko-odczytowy miernik mikrofonu."""

        with self._command_lock:
            self._level_source = source if callable(source) else None

    def _read_level(self) -> float:
        with self._command_lock:
            source = self._level_source
        if source is None:
            return 0.0
        try:
            return min(1.0, max(0.0, float(source())))
        except Exception:
            return 0.0

    def show(self, state: str, label: str = "", detail: str = "") -> None:
        if state not in STATUS_INDICATOR_STATES - {"hidden"}:
            raise ValueError(f"Unknown status indicator state: {state}")
        with self._command_lock:
            if not self.enabled or self._closed.is_set() or self._failed.is_set():
                return
            self._commands.put(
                (
                    state,
                    active_monitor_work_area(),
                    status_indicator_preview_text(label, 128),
                    status_indicator_preview_text(detail, 256),
                )
            )

    def recording(self, command: bool = False) -> None:
        self.show(
            "command_recording" if command else "recording",
            self.translator.t(
                "Komenda głosowa" if command else "Słucham",
                "Voice command" if command else "Listening",
            ),
        )

    def processing(self, command: bool = False) -> None:
        self.show(
            "command_processing" if command else "processing",
            self.translator.t(
                "Rozpoznaję komendę" if command else "Rozpoznaję mowę",
                "Recognizing command" if command else "Transcribing speech",
            ),
            self.translator.t(
                "Chwila…" if command else "Porządkuję tekst…",
                "One moment…" if command else "Preparing your text…",
            ),
        )

    def success(self, text: str = "", command: bool = False) -> None:
        self.show(
            "command_success" if command else "success",
            self.translator.t(
                "Komenda wykonana" if command else "Gotowe",
                "Command completed" if command else "Ready",
            ),
            status_indicator_preview_text(text, 256)
            or self.translator.t(
                "Wykonano" if command else "Tekst został wklejony",
                "Completed" if command else "Text inserted",
            ),
        )

    def error(self) -> None:
        self.show(
            "error",
            self.translator.t("Nie udało się", "Something went wrong"),
            self.translator.t("Spróbuj ponownie", "Please try again"),
        )

    def hide(self) -> None:
        with self._command_lock:
            if not self.enabled or self._closed.is_set() or self._failed.is_set():
                return
            self._commands.put(("hidden", None, "", ""))

    def close(self) -> None:
        with self._command_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._commands.put(None)

    def run(self) -> None:
        """Uruchom pętlę wskaźnika w głównym wątku procesu."""
        if self._root is None and not self.start():
            return
        root = self._root
        if root is None:
            return
        try:
            root.mainloop()
        except Exception:
            self._failed.set()
            logging.exception("Błąd pętli wskaźnika dyktowania")
        finally:
            self._root = None
            try:
                root.destroy()
            except Exception:
                pass

    @staticmethod
    def _window_handle(root) -> int:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        widget = ctypes.c_void_p(int(root.winfo_id()))
        return int(user32.GetAncestor(widget, 2) or widget.value or 0)

    @classmethod
    def _configure_no_activate(cls, root, layered: bool = False) -> int:
        hwnd = cls._window_handle(root)
        if not hwnd:
            raise ctypes.WinError()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        long_ptr = (
            ctypes.c_longlong
            if ctypes.sizeof(ctypes.c_void_p) == 8
            else ctypes.c_long
        )
        get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = long_ptr
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, long_ptr]
        set_style.restype = long_ptr
        ex_style = int(get_style(ctypes.c_void_p(hwnd), -20))
        ex_style |= 0x00000020  # WS_EX_TRANSPARENT: clicks pass through
        ex_style |= 0x00000080  # WS_EX_TOOLWINDOW: no taskbar / Alt+Tab
        ex_style |= 0x08000000  # WS_EX_NOACTIVATE: preserve paste target
        if layered:
            ex_style |= 0x00080000  # WS_EX_LAYERED: per-pixel alpha via GDI
        ctypes.set_last_error(0)
        previous = set_style(ctypes.c_void_p(hwnd), -20, long_ptr(ex_style))
        if previous == 0 and ctypes.get_last_error() != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    @staticmethod
    def _show_no_activate(
        root,
        hwnd: int,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        root.deiconify()
        root.update_idletasks()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.ShowWindow(ctypes.c_void_p(hwnd), 4)  # SW_SHOWNOACTIVATE
        positioned = user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(-1),  # HWND_TOPMOST
            x,
            y,
            width,
            height,
            0x0010 | 0x0040,  # SWP_NOACTIVATE | SWP_SHOWWINDOW
        )
        if not positioned:
            raise ctypes.WinError(ctypes.get_last_error())

    def _prepare_window(self) -> bool:
        root = None
        try:
            import tkinter as tk
            from PIL import ImageTk

            transparent = "#010203"
            root = tk.Tk(className=APP_NAME)
            root.withdraw()
            root.title(f"{APP_DISPLAY_NAME} status")
            root.overrideredirect(True)
            root.configure(background=transparent)
            root.attributes("-topmost", True)

            ui_scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
            window_width = max(
                220,
                int(round(STATUS_INDICATOR_WIDTH * ui_scale)),
            )
            window_height = max(
                52,
                int(round(STATUS_INDICATOR_HEIGHT * ui_scale)),
            )
            bottom_margin = max(
                0,
                int(round(STATUS_INDICATOR_BOTTOM_MARGIN * ui_scale)),
            )
            root.geometry(f"{window_width}x{window_height}+0+0")
            canvas = tk.Canvas(
                root,
                width=window_width,
                height=window_height,
                background=transparent,
                borderwidth=0,
                highlightthickness=0,
            )
            canvas.pack(fill="both", expand=True)
            root.update_idletasks()
            hwnd = self._configure_no_activate(root, layered=True)

            # Warstwowe okno GDI daje prawdziwy miękki cień, ale wymaga
            # sprawnej kompozycji pulpitu. Gdy pierwsza próba zawiedzie,
            # wracamy do koloru kluczowego — kapsuła straci cień, lecz
            # wskaźnik pozostanie użyteczny.
            layered = True
            try:
                blit_layered_window(
                    hwnd,
                    Image.new(
                        "RGBA",
                        (window_width, window_height),
                        (0, 0, 0, 0),
                    ),
                    0,
                    0,
                )
            except Exception:
                layered = False
                logging.warning(
                    "Warstwowe okno wskaźnika niedostępne; używam koloru "
                    "kluczowego",
                    exc_info=True,
                )
                self._configure_no_activate(root, layered=False)
                root.wm_attributes("-transparentcolor", transparent)

            current_state = "hidden"
            current_frame = 0
            current_label = ""
            current_detail = ""
            current_x = 0
            current_y = 0
            meter_level = 0.0
            generation = 0
            processing_started = 0.0
            current_photo = None
            poll_pending = False

            def report_callback_exception(exc_type, exc, traceback) -> None:
                self._failed.set()
                logging.error(
                    "Błąd obsługi wskaźnika dyktowania",
                    exc_info=(exc_type, exc, traceback),
                )
                try:
                    root.withdraw()
                    schedule_poll(100)
                except Exception:
                    logging.exception(
                        "Nie udało się bezpiecznie wyłączyć wskaźnika"
                    )
                    root.quit()

            root.report_callback_exception = report_callback_exception

            def fallback_work_area() -> tuple[int, int, int, int]:
                return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())

            def place_window(
                work_area: Optional[tuple[int, int, int, int]],
            ) -> tuple[int, int]:
                nonlocal current_x, current_y
                current_x, current_y = status_indicator_window_position(
                    work_area or fallback_work_area(),
                    (window_width, window_height),
                    bottom_margin,
                )
                root.geometry(
                    f"{window_width}x{window_height}"
                    f"{current_x:+d}{current_y:+d}"
                )
                return current_x, current_y

            def render_current() -> None:
                nonlocal current_photo, meter_level
                if current_state == "hidden":
                    return
                target_level = (
                    self._read_level()
                    if current_state in {"recording", "command_recording"}
                    else 0.0
                )
                response = 0.62 if target_level >= meter_level else 0.20
                meter_level += (target_level - meter_level) * response
                frame_image = render_status_indicator_frame(
                    current_state,
                    current_frame,
                    window_width,
                    window_height,
                    level=meter_level,
                    label=current_label,
                    detail=current_detail,
                )
                if layered:
                    blit_layered_window(
                        hwnd,
                        frame_image,
                        current_x,
                        current_y,
                    )
                    return
                current_photo = ImageTk.PhotoImage(frame_image, master=root)
                canvas.delete("all")
                canvas.create_image(
                    window_width // 2,
                    window_height // 2,
                    image=current_photo,
                )

            def hide_if_current(token: int) -> None:
                nonlocal current_state
                if token != generation or self._failed.is_set():
                    return
                current_state = "hidden"
                root.withdraw()

            def show_terminal(
                state: str,
                work_area: Optional[tuple[int, int, int, int]],
                label: str,
                detail: str,
                token: int,
            ) -> None:
                nonlocal current_state, current_frame
                nonlocal current_label, current_detail, meter_level
                if token != generation or self._failed.is_set():
                    return
                current_state = state
                current_frame = 0
                current_label = label
                current_detail = detail
                meter_level = 0.0
                x, y = place_window(work_area)
                render_current()
                self._show_no_activate(
                    root,
                    hwnd,
                    x,
                    y,
                    window_width,
                    window_height,
                )
                visible_seconds = (
                    STATUS_INDICATOR_SUCCESS_SECONDS
                    if state.endswith("success")
                    else STATUS_INDICATOR_ERROR_SECONDS
                )
                root.after(
                    int(visible_seconds * 1000),
                    lambda: hide_if_current(token),
                )

            def animate_if_current(token: int) -> None:
                nonlocal current_frame
                if (
                    token != generation
                    or self._failed.is_set()
                    or current_state
                    not in {
                        "recording",
                        "processing",
                        "command_recording",
                        "command_processing",
                    }
                ):
                    return
                current_frame += 1
                render_current()
                root.after(50, lambda: animate_if_current(token))

            def apply_command(
                state: str,
                work_area: Optional[tuple[int, int, int, int]],
                label: str,
                detail: str,
            ) -> None:
                nonlocal current_state, current_frame
                nonlocal current_label, current_detail, meter_level
                nonlocal generation, processing_started
                generation += 1
                token = generation
                if state == "hidden":
                    current_state = "hidden"
                    root.withdraw()
                    return
                terminal_states = {"success", "error", "command_success"}
                processing_states = {"processing", "command_processing"}
                if state in terminal_states and current_state in processing_states:
                    elapsed = time.monotonic() - processing_started
                    remaining = STATUS_INDICATOR_MIN_PROCESSING_SECONDS - elapsed
                    if remaining > 0:
                        root.after(
                            max(1, int(remaining * 1000)),
                            lambda: show_terminal(
                                state,
                                work_area,
                                label,
                                detail,
                                token,
                            ),
                        )
                        root.after(50, lambda: animate_if_current(token))
                        return
                if state in terminal_states:
                    show_terminal(state, work_area, label, detail, token)
                    return
                current_state = state
                current_frame = 0
                current_label = label
                current_detail = detail
                meter_level = 0.0
                if state in processing_states:
                    processing_started = time.monotonic()
                x, y = place_window(work_area)
                render_current()
                self._show_no_activate(
                    root,
                    hwnd,
                    x,
                    y,
                    window_width,
                    window_height,
                )
                root.after(50, lambda: animate_if_current(token))

            def schedule_poll(delay: int) -> None:
                nonlocal poll_pending
                if poll_pending:
                    return
                poll_pending = True
                root.after(delay, poll_commands)

            def poll_commands() -> None:
                nonlocal poll_pending
                poll_pending = False
                try:
                    while True:
                        command = self._commands.get_nowait()
                        if command is None:
                            root.quit()
                            return
                        if not self._failed.is_set():
                            apply_command(*command)
                except queue.Empty:
                    pass
                if self._failed.is_set():
                    schedule_poll(100)
                else:
                    delay = 25 if current_state != "hidden" else 60
                    schedule_poll(delay)

            schedule_poll(0)
            self._root = root
            return True
        except Exception:
            self._failed.set()
            logging.exception("Nie udało się uruchomić wskaźnika dyktowania")
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            return False


def application_process_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def settings_process_args() -> list[str]:
    return [*application_process_args(), "--settings"]


def application_restart_process_args(
    requested_at_ns: Optional[int] = None,
) -> list[str]:
    args = [*application_process_args(), "--restart-delay", "0.2"]
    if requested_at_ns is None:
        return args
    if isinstance(requested_at_ns, bool) or not isinstance(requested_at_ns, int):
        raise ValueError("restart timestamp must be an integer")
    if requested_at_ns <= 0:
        raise ValueError("restart timestamp must be positive")
    return [
        *args,
        "--restart-started-token",
        str(requested_at_ns),
    ]


def is_app_instance_running() -> bool:
    """Sprawdź mutex głównej aplikacji bez tworzenia nowej instancji."""

    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    SYNCHRONIZE = 0x00100000
    ctypes.set_last_error(0)
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    error = ctypes.get_last_error()
    if error == 2:  # ERROR_FILE_NOT_FOUND
        return False
    if error == 5:  # ERROR_ACCESS_DENIED oznacza, że obiekt jednak istnieje.
        return True
    raise ctypes.WinError(error)


def restart_or_launch_app_after_settings(
    translator: Optional[Translator] = None,
) -> str:
    """Apply settings only after a matching replacement process confirms start."""

    translator = translator or Translator("auto")
    requested_at_ns = request_app_restart()
    if is_app_instance_running():
        if wait_for_restart_ack(requested_at_ns):
            if wait_for_restart_started(requested_at_ns):
                return "restart_requested"
            raise AppError(
                translator.t(
                    "Mówik odebrał prośbę o restart, ale nowa instancja nie "
                    "potwierdziła uruchomienia. Panel ustawień pozostaje otwarty.",
                    "Mówik received the restart request, but the replacement "
                    "instance did not confirm startup. Settings will remain open.",
                )
            )
        # The process may have exited after the mutex probe but before it
        # could claim the request.  Launch a fresh process in that case.
        if is_app_instance_running():
            raise AppError(
                translator.t(
                    "Działający Mówik nie potwierdził odbioru prośby o restart. "
                    "Panel ustawień pozostaje otwarty.",
                    "The running Mówik instance did not acknowledge the restart "
                    "request. Settings will remain open.",
                )
            )
    discard_pending_restart_request()
    try:
        subprocess.Popen(
            application_restart_process_args(requested_at_ns),
            cwd=str(APP_ROOT),
        )
    except OSError as exc:
        raise AppError(
            translator.t(
                "Nie udało się uruchomić Mówika: {error}",
                "Could not start Mówik: {error}",
                error=exc,
            )
        ) from exc
    if not wait_for_restart_started(requested_at_ns):
        raise AppError(
            translator.t(
                "Uruchomiony proces Mówika nie potwierdził startu. "
                "Panel ustawień pozostaje otwarty.",
                "The launched Mówik process did not confirm startup. "
                "Settings will remain open.",
            )
        )
    return "app_started"


def run_settings_window() -> int:
    """Uruchom osobny panel ustawień oparty na wbudowanym Tkinterze."""
    config, config_revision = load_config_with_revision()
    translator = Translator.from_config(config)
    t = translator.t
    enable_windows_dpi_awareness()

    try:
        import tkinter as tk
        from tkinter import filedialog, font as tkfont, messagebox, ttk
    except ImportError as exc:
        raise AppError(
            t(
                "Brakuje składnika Tkinter. Zainstaluj standardowy 64-bitowy "
                "Python 3.11–3.12 z python.org albo uruchom "
                "NAPRAW_INSTALACJE.cmd.",
                "Tkinter is missing. Install standard 64-bit Python 3.11–3.12 "
                "from python.org or run NAPRAW_INSTALACJE.cmd.",
            )
        ) from exc

    root = tk.Tk()
    root.title(
        t(
            "{app} — centrum ustawień",
            "{app} — Settings Center",
            app=APP_DISPLAY_NAME,
        )
    )
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    ui_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    if os.name == "nt":
        try:
            get_dpi_for_window = ctypes.WinDLL("user32").GetDpiForWindow
            get_dpi_for_window.argtypes = [ctypes.c_void_p]
            get_dpi_for_window.restype = ctypes.c_uint
            window_dpi = int(get_dpi_for_window(root.winfo_id()))
            if window_dpi > 0:
                ui_scale = max(1.0, window_dpi / 96.0)
        except (AttributeError, OSError):
            pass

    def px(value: int | float) -> int:
        return max(1, int(round(float(value) * ui_scale)))

    window_width = min(px(1120), max(px(480), screen_width - px(64)))
    window_height = min(px(780), max(px(420), screen_height - px(96)))
    window_x = max(0, (screen_width - window_width) // 2)
    window_y = max(0, (screen_height - window_height) // 2)
    root.geometry(
        f"{window_width}x{window_height}+{window_x}+{window_y}"
    )
    root.minsize(min(px(960), window_width), min(px(660), window_height))
    try:
        root.iconbitmap(default=str(RESOURCE_ROOT / "assets" / "Mowik.ico"))
    except tk.TclError:
        logging.debug("Nie udało się ustawić ikony okna", exc_info=True)

    colors = {
        "canvas": "#090E18",
        "surface": "#111827",
        "surface_alt": "#182235",
        "surface_hover": "#233047",
        "control": "#0B1220",
        "control_disabled": "#202B3D",
        "sidebar": "#080D17",
        "sidebar_active": "#18385F",
        "sidebar_hover": "#111F33",
        "sidebar_success": "#0E342B",
        "sidebar_success_text": "#86EFAC",
        "sidebar_text": "#CBD5E1",
        "sidebar_muted": "#94A3B8",
        "text": "#F1F5F9",
        "muted": "#AAB5C6",
        "disabled_text": "#7F8DA3",
        "border": "#2D3A4F",
        "control_border": "#65738A",
        "button_pressed": "#2C3A50",
        "button_hover_border": "#52637B",
        "primary": "#60A5FA",
        "primary_button": "#2563EB",
        "primary_hover": "#1D4ED8",
        "primary_pressed": "#1E40AF",
        "primary_disabled": "#274365",
        "primary_soft": "#14243D",
        "primary_soft_hover": "#193454",
        "primary_border": "#2E5F92",
        "success": "#6EE7B7",
        "success_soft": "#0F2D25",
        "success_border": "#28614E",
        "warning": "#FBBF24",
        "warning_soft": "#33260E",
        "warning_border": "#765514",
        "danger": "#FB7185",
        "selection": "#1D4ED8",
        "scrollbar_thumb": "#475569",
        "scrollbar_thumb_hover": "#64748B",
        "white": "#F8FAFC",
    }

    try:
        root.tk_setPalette(
            background=colors["surface"],
            foreground=colors["text"],
            activeBackground=colors["surface_hover"],
            activeForeground=colors["text"],
            selectBackground=colors["selection"],
            selectForeground=colors["white"],
            highlightColor=colors["primary"],
            disabledForeground=colors["disabled_text"],
        )
    except tk.TclError:
        logging.debug("Nie udało się ustawić ciemnej palety widgetów Tk")
    root.configure(background=colors["canvas"])

    def apply_dark_window_chrome(window) -> None:
        """Ustaw ciemne tło i natywną ciemną belkę okna, gdy Windows ją wspiera."""

        try:
            window.configure(background=colors["surface"])
        except tk.TclError:
            return
        if os.name != "nt":
            return
        try:
            window.update_idletasks()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.GetParent.restype = ctypes.c_void_p
            hwnd = int(window.winfo_id())
            parent_hwnd = user32.GetParent(ctypes.c_void_p(hwnd))
            if parent_hwnd:
                hwnd = int(parent_hwnd)

            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            dwmapi.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_uint,
            ]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                result = dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    attribute,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )
                if result == 0:
                    break
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            logging.debug(
                "Natywna ciemna belka okna jest niedostępna",
                exc_info=True,
            )

    apply_dark_window_chrome(root)
    root.configure(background=colors["canvas"])

    font_families = {
        family.casefold(): family for family in tkfont.families(root)
    }
    ui_font_family = font_families.get(
        "segoe ui variable text",
        font_families.get("segoe ui", "Segoe UI"),
    )
    display_font_family = font_families.get(
        "segoe ui variable display",
        ui_font_family,
    )
    named_font_settings = {
        "TkDefaultFont": (10, "normal"),
        "TkTextFont": (10, "normal"),
        "TkMenuFont": (10, "normal"),
        "TkHeadingFont": (11, "bold"),
        "TkCaptionFont": (10, "normal"),
        "TkSmallCaptionFont": (9, "normal"),
    }
    for font_name, (size, weight) in named_font_settings.items():
        try:
            tkfont.nametofont(font_name, root=root).configure(
                family=ui_font_family,
                size=size,
                weight=weight,
            )
        except tk.TclError:
            logging.debug("Nie udało się ustawić czcionki %s", font_name)

    classic_widget_options = {
        "*Menu.background": colors["surface_alt"],
        "*Menu.foreground": colors["text"],
        "*Menu.activeBackground": colors["surface_hover"],
        "*Menu.activeForeground": colors["text"],
        "*Menu.disabledForeground": colors["disabled_text"],
        "*Menu.selectColor": colors["primary"],
        "*Menu.borderWidth": 1,
        "*Menu.activeBorderWidth": 0,
        "*Listbox.background": colors["control"],
        "*Listbox.foreground": colors["text"],
        "*Listbox.selectBackground": colors["selection"],
        "*Listbox.selectForeground": colors["white"],
        "*Listbox.disabledForeground": colors["disabled_text"],
        "*Listbox.highlightBackground": colors["control_border"],
        "*Listbox.highlightColor": colors["primary"],
        "*TCombobox*Listbox.background": colors["control"],
        "*TCombobox*Listbox.foreground": colors["text"],
        "*TCombobox*Listbox.selectBackground": colors["selection"],
        "*TCombobox*Listbox.selectForeground": colors["white"],
        "*TCombobox*Listbox.disabledForeground": colors["disabled_text"],
        "*TCombobox*Listbox.highlightBackground": colors["control_border"],
        "*TCombobox*Listbox.highlightColor": colors["primary"],
        "*TCombobox*Listbox.highlightThickness": 1,
        "*Text.background": colors["control"],
        "*Text.foreground": colors["text"],
        "*Text.insertBackground": colors["text"],
        "*Text.selectBackground": colors["selection"],
        "*Text.selectForeground": colors["white"],
        "*Text.disabledForeground": colors["disabled_text"],
        "*Text.highlightBackground": colors["control_border"],
        "*Text.highlightColor": colors["primary"],
    }
    for option_pattern, option_value in classic_widget_options.items():
        root.option_add(option_pattern, option_value, "interactive")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    base_font = (ui_font_family, 10)
    style.configure(
        ".",
        background=colors["surface"],
        foreground=colors["text"],
        fieldbackground=colors["control"],
        troughcolor=colors["canvas"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        selectbackground=colors["selection"],
        selectforeground=colors["white"],
        insertcolor=colors["text"],
        font=base_font,
    )
    style.configure("TFrame", background=colors["surface"])
    style.configure("App.TFrame", background=colors["canvas"])
    style.configure("Surface.TFrame", background=colors["surface"])
    style.configure("Alt.TFrame", background=colors["surface_alt"])
    style.configure("Sidebar.TFrame", background=colors["sidebar"])
    style.configure("TLabel", background=colors["surface"], foreground=colors["text"])
    style.map("TLabel", foreground=[("disabled", colors["disabled_text"])])
    style.configure(
        "Title.TLabel",
        font=(display_font_family, 21, "bold"),
        foreground=colors["text"],
    )
    style.configure(
        "Subtitle.TLabel",
        font=(ui_font_family, 10),
        foreground=colors["muted"],
    )
    style.configure(
        "Section.TLabel",
        font=(ui_font_family, 11, "bold"),
        foreground=colors["text"],
    )
    style.configure(
        "Muted.TLabel",
        foreground=colors["muted"],
        font=(ui_font_family, 9),
    )
    style.configure(
        "Field.TLabel",
        foreground=colors["text"],
        font=(ui_font_family, 9, "bold"),
    )
    style.configure(
        "SidebarBrand.TLabel",
        background=colors["sidebar"],
        foreground=colors["white"],
        font=(display_font_family, 17, "bold"),
    )
    style.configure(
        "SidebarMeta.TLabel",
        background=colors["sidebar"],
        foreground=colors["sidebar_text"],
        font=(ui_font_family, 9),
    )
    style.configure(
        "SidebarSection.TLabel",
        background=colors["sidebar"],
        foreground=colors["sidebar_muted"],
        font=(ui_font_family, 9, "bold"),
    )
    style.configure(
        "TButton",
        background=colors["surface_alt"],
        foreground=colors["text"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        relief="flat",
        padding=(px(14), px(9)),
    )
    style.map(
        "TButton",
        background=[
            ("disabled", colors["control_disabled"]),
            ("pressed", colors["button_pressed"]),
            ("active", colors["surface_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[
            ("disabled", colors["border"]),
            ("focus", colors["primary"]),
            ("active", colors["button_hover_border"]),
        ],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "Primary.TButton",
        background=colors["primary_button"],
        foreground=colors["white"],
        bordercolor=colors["primary_button"],
        lightcolor=colors["primary_button"],
        darkcolor=colors["primary_button"],
        font=(ui_font_family, 10, "bold"),
        padding=(px(17), px(10)),
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", colors["primary_disabled"]),
            ("pressed", colors["primary_pressed"]),
            ("active", colors["primary_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[
            ("disabled", colors["primary_disabled"]),
            ("focus", colors["primary"]),
        ],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "Accent.TButton",
        background=colors["primary_soft"],
        foreground=colors["primary"],
        bordercolor=colors["primary_border"],
        lightcolor=colors["primary_border"],
        darkcolor=colors["primary_border"],
        font=(ui_font_family, 10, "bold"),
        padding=(px(14), px(9)),
    )
    style.map(
        "Accent.TButton",
        background=[
            ("disabled", colors["control_disabled"]),
            ("pressed", colors["button_pressed"]),
            ("active", colors["primary_soft_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[
            ("disabled", colors["border"]),
            ("focus", colors["primary"]),
            ("active", colors["primary"]),
        ],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "Nav.TButton",
        background=colors["sidebar"],
        foreground=colors["sidebar_text"],
        bordercolor=colors["sidebar"],
        lightcolor=colors["sidebar"],
        darkcolor=colors["sidebar"],
        anchor="w",
        padding=(px(18), px(11)),
        font=(ui_font_family, 10),
    )
    style.map(
        "Nav.TButton",
        background=[
            ("disabled", colors["sidebar"]),
            ("active", colors["sidebar_hover"]),
        ],
        foreground=[
            ("disabled", colors["disabled_text"]),
            ("active", colors["white"]),
        ],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    # Przycisk "Usuń" niesie skutek nieodwracalny, więc nie może wyglądać
    # dokładnie tak samo jak "Edytuj" obok niego.
    style.configure(
        "Danger.TButton",
        background=colors["surface_alt"],
        foreground=colors["danger"],
        bordercolor=colors["danger"],
        lightcolor=colors["surface_alt"],
        darkcolor=colors["surface_alt"],
        padding=(px(14), px(9)),
        font=(ui_font_family, 10),
    )
    style.map(
        "Danger.TButton",
        background=[
            ("disabled", colors["control_disabled"]),
            ("pressed", colors["danger"]),
            ("active", colors["danger"]),
        ],
        foreground=[
            ("disabled", colors["disabled_text"]),
            ("pressed", colors["white"]),
            ("active", colors["white"]),
        ],
        bordercolor=[("disabled", colors["border"])],
    )
    style.configure(
        "NavActive.TButton",
        background=colors["sidebar_active"],
        foreground=colors["white"],
        bordercolor=colors["sidebar_active"],
        lightcolor=colors["sidebar_active"],
        darkcolor=colors["sidebar_active"],
        anchor="w",
        padding=(px(18), px(11)),
        font=(ui_font_family, 10, "bold"),
    )
    style.map(
        "NavActive.TButton",
        background=[
            ("disabled", colors["sidebar_active"]),
            ("active", colors["sidebar_active"]),
        ],
        foreground=[
            ("disabled", colors["disabled_text"]),
            ("active", colors["white"]),
        ],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "Profile.TButton",
        anchor="w",
        padding=(px(14), px(12)),
        font=(ui_font_family, 9),
    )
    style.configure(
        "SelectedProfile.TButton",
        background=colors["primary_soft"],
        foreground=colors["primary"],
        bordercolor=colors["primary_border"],
        lightcolor=colors["primary_border"],
        darkcolor=colors["primary_border"],
        anchor="w",
        padding=(px(14), px(12)),
        font=(ui_font_family, 9, "bold"),
    )
    style.map(
        "SelectedProfile.TButton",
        background=[
            ("disabled", colors["control_disabled"]),
            ("active", colors["primary_soft_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "Disclosure.TButton",
        background=colors["surface_alt"],
        foreground=colors["text"],
        bordercolor=colors["surface_alt"],
        lightcolor=colors["surface_alt"],
        darkcolor=colors["surface_alt"],
        anchor="w",
        padding=(px(14), px(11)),
        font=(ui_font_family, 10, "bold"),
    )
    style.map(
        "Disclosure.TButton",
        background=[
            ("disabled", colors["control_disabled"]),
            ("active", colors["surface_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "DisclosureOpen.TButton",
        background=colors["primary_soft"],
        foreground=colors["primary"],
        bordercolor=colors["primary_soft"],
        lightcolor=colors["primary_soft"],
        darkcolor=colors["primary_soft"],
        anchor="w",
        padding=(px(14), px(11)),
        font=(ui_font_family, 10, "bold"),
    )
    style.map(
        "DisclosureOpen.TButton",
        background=[
            ("disabled", colors["control_disabled"]),
            ("active", colors["primary_soft_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TEntry",
        fieldbackground=colors["control"],
        foreground=colors["text"],
        bordercolor=colors["control_border"],
        lightcolor=colors["control_border"],
        darkcolor=colors["control_border"],
        insertcolor=colors["text"],
        selectbackground=colors["selection"],
        selectforeground=colors["white"],
        padding=px(9),
    )
    style.map(
        "TEntry",
        fieldbackground=[
            ("disabled", colors["control_disabled"]),
            ("readonly", colors["control"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TCombobox",
        fieldbackground=colors["control"],
        foreground=colors["text"],
        background=colors["surface_alt"],
        bordercolor=colors["control_border"],
        lightcolor=colors["control_border"],
        darkcolor=colors["control_border"],
        arrowcolor=colors["muted"],
        padding=px(7),
    )
    style.map(
        "TCombobox",
        fieldbackground=[
            ("disabled", colors["control_disabled"]),
            ("readonly", colors["control"]),
        ],
        background=[
            ("disabled", colors["control_disabled"]),
            ("active", colors["surface_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        arrowcolor=[
            ("disabled", colors["disabled_text"]),
            ("active", colors["text"]),
        ],
        selectbackground=[
            ("disabled", colors["control_disabled"]),
            ("readonly", colors["control"]),
        ],
        selectforeground=[
            ("disabled", colors["disabled_text"]),
            ("readonly", colors["text"]),
        ],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TSpinbox",
        fieldbackground=colors["control"],
        foreground=colors["text"],
        background=colors["surface_alt"],
        bordercolor=colors["control_border"],
        lightcolor=colors["control_border"],
        darkcolor=colors["control_border"],
        arrowcolor=colors["muted"],
        padding=px(7),
    )
    style.map(
        "TSpinbox",
        fieldbackground=[
            ("disabled", colors["control_disabled"]),
            ("readonly", colors["control"]),
        ],
        background=[
            ("disabled", colors["control_disabled"]),
            ("active", colors["surface_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        arrowcolor=[
            ("disabled", colors["disabled_text"]),
            ("active", colors["text"]),
        ],
        bordercolor=[("focus", colors["primary"])],
        lightcolor=[("focus", colors["primary"])],
        darkcolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TCheckbutton",
        background=colors["surface"],
        foreground=colors["text"],
        focuscolor=colors["primary"],
        indicatorbackground=colors["control"],
        indicatorforeground=colors["white"],
        bordercolor=colors["control_border"],
        lightcolor=colors["control_border"],
        darkcolor=colors["control_border"],
        padding=(0, px(4)),
    )
    style.map(
        "TCheckbutton",
        background=[("active", colors["surface_hover"])],
        foreground=[("disabled", colors["disabled_text"])],
        indicatorbackground=[
            ("disabled", colors["control_disabled"]),
            ("selected", colors["primary_button"]),
            ("active", colors["surface_hover"]),
        ],
        indicatorforeground=[
            ("disabled", colors["disabled_text"]),
            ("selected", colors["white"]),
        ],
        bordercolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TRadiobutton",
        background=colors["surface"],
        foreground=colors["text"],
        focuscolor=colors["primary"],
        indicatorbackground=colors["control"],
        indicatorforeground=colors["white"],
        bordercolor=colors["control_border"],
        lightcolor=colors["control_border"],
        darkcolor=colors["control_border"],
        padding=(0, px(4)),
    )
    style.map(
        "TRadiobutton",
        background=[("active", colors["surface_hover"])],
        foreground=[("disabled", colors["disabled_text"])],
        indicatorbackground=[
            ("disabled", colors["control_disabled"]),
            ("selected", colors["primary_button"]),
            ("active", colors["surface_hover"]),
        ],
        indicatorforeground=[
            ("disabled", colors["disabled_text"]),
            ("selected", colors["white"]),
        ],
        bordercolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TLabelframe",
        background=colors["surface"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "TLabelframe.Label",
        background=colors["surface"],
        foreground=colors["text"],
        font=(ui_font_family, 11, "bold"),
        padding=(px(4), 0, px(8), 0),
    )
    style.configure(
        "Vertical.TScrollbar",
        background=colors["scrollbar_thumb"],
        troughcolor=colors["surface"],
        bordercolor=colors["surface"],
        arrowcolor=colors["muted"],
        gripcount=0,
        width=px(10),
    )
    style.map(
        "Vertical.TScrollbar",
        background=[
            ("disabled", colors["control_disabled"]),
            ("pressed", colors["primary_button"]),
            ("active", colors["scrollbar_thumb_hover"]),
        ],
        arrowcolor=[
            ("disabled", colors["disabled_text"]),
            ("active", colors["text"]),
        ],
    )
    style.configure(
        "Horizontal.TScrollbar",
        background=colors["scrollbar_thumb"],
        troughcolor=colors["surface"],
        bordercolor=colors["surface"],
        arrowcolor=colors["muted"],
        gripcount=0,
        width=px(10),
    )
    style.map(
        "Horizontal.TScrollbar",
        background=[
            ("disabled", colors["control_disabled"]),
            ("pressed", colors["primary_button"]),
            ("active", colors["scrollbar_thumb_hover"]),
        ],
        arrowcolor=[
            ("disabled", colors["disabled_text"]),
            ("active", colors["text"]),
        ],
    )
    try:
        style.layout(
            "Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            (
                                "Vertical.Scrollbar.thumb",
                                {"expand": "1", "sticky": "nswe"},
                            )
                        ],
                    },
                )
            ],
        )
    except tk.TclError:
        logging.debug("Uproszczony scrollbar jest niedostępny")
    style.configure("TSeparator", background=colors["border"])
    style.configure(
        "Treeview",
        background=colors["surface_alt"],
        fieldbackground=colors["surface_alt"],
        foreground=colors["text"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        rowheight=px(28),
    )
    style.map(
        "Treeview",
        background=[("selected", colors["selection"])],
        foreground=[
            ("disabled", colors["disabled_text"]),
            ("selected", colors["white"]),
        ],
    )
    style.configure(
        "Treeview.Heading",
        background=colors["surface_hover"],
        foreground=colors["text"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        relief="flat",
        font=(ui_font_family, 9, "bold"),
        padding=(px(8), px(7)),
    )
    style.map(
        "Treeview.Heading",
        background=[
            ("pressed", colors["button_pressed"]),
            ("active", colors["surface_hover"]),
        ],
        foreground=[("disabled", colors["disabled_text"])],
        bordercolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TNotebook",
        background=colors["surface"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        tabmargins=0,
    )
    style.configure(
        "TNotebook.Tab",
        background=colors["surface_alt"],
        foreground=colors["muted"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        padding=(px(12), px(8)),
    )
    style.map(
        "TNotebook.Tab",
        background=[
            ("selected", colors["surface"]),
            ("active", colors["surface_hover"]),
        ],
        foreground=[
            ("disabled", colors["disabled_text"]),
            ("selected", colors["text"]),
            ("active", colors["text"]),
        ],
        bordercolor=[("focus", colors["primary"])],
    )
    style.configure(
        "Horizontal.TProgressbar",
        background=colors["primary_button"],
        troughcolor=colors["control_disabled"],
        bordercolor=colors["border"],
        lightcolor=colors["primary_button"],
        darkcolor=colors["primary_button"],
    )
    style.configure(
        "TScale",
        background=colors["surface"],
        troughcolor=colors["control_disabled"],
        slidercolor=colors["scrollbar_thumb"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
    )
    style.map(
        "TScale",
        background=[("active", colors["surface_hover"])],
        slidercolor=[
            ("disabled", colors["control_disabled"]),
            ("active", colors["primary"]),
        ],
    )

    try:
        from PIL import ImageTk

        root._mowik_icon = ImageTk.PhotoImage(  # type: ignore[attr-defined]
            make_tray_image("brand")
        )
        root.iconphoto(True, root._mowik_icon)  # type: ignore[attr-defined]
    except Exception:
        logging.debug("Nie udało się ustawić ikony okna ustawień", exc_info=True)

    trigger_values: dict[str, Any] = {
        "F6": "keyboard:f6",
        "F7": "keyboard:f7",
        t("F8 (domyślnie)", "F8 (default)"): "keyboard:f8",
        "F9": "keyboard:f9",
        "F10": "keyboard:f10",
        "F11": "keyboard:f11",
        "F12": "keyboard:f12",
        "Pause/Break": "keyboard:pause",
        "Scroll Lock": "keyboard:scroll_lock",
        t(
            "Boczny przycisk myszy 1 (X1)",
            "Side mouse button 1 (X1)",
        ): "mouse:x1",
        t(
            "Boczny przycisk myszy 2 (X2)",
            "Side mouse button 2 (X2)",
        ): "mouse:x2",
    }
    model_values: dict[str, Any] = {
        t("Automatycznie", "Automatic"): "auto",
        t("tiny — najmniejszy", "tiny — smallest"): "tiny",
        t("small — lekki (~0,5 GB)", "small — lightweight (~0.5 GB)"): "small",
        t(
            "large-v3-turbo — zalecany (~1,6 GB)",
            "large-v3-turbo — recommended (~1.6 GB)",
        ): "large-v3-turbo",
        t(
            "large-v3 — najdokładniejszy (~3,1 GB)",
            "large-v3 — most accurate (~3.1 GB)",
        ): "large-v3",
    }
    device_values: dict[str, Any] = {
        t("Automatycznie (zalecane)", "Automatic (recommended)"): "auto",
        t("Procesor (CPU)", "Processor (CPU)"): "cpu",
        t("Karta NVIDIA (CUDA)", "NVIDIA GPU (CUDA)"): "cuda",
    }
    language_values: dict[str, Any] = {
        t("Jak interfejs (zalecane)", "Same as interface (recommended)"): "auto",
        t("Wykryj z nagrania", "Detect from recording"): "detect",
        t("Polski", "Polish"): "pl",
        t("Angielski", "English"): "en",
        t("Niemiecki", "German"): "de",
        t("Francuski", "French"): "fr",
        t("Hiszpański", "Spanish"): "es",
        t("Ukraiński", "Ukrainian"): "uk",
    }
    ui_language_values: dict[str, Any] = {
        t("Automatycznie (Windows)", "Automatic (Windows)"): "auto",
        "Polski": "pl",
        "English": "en",
    }
    microphone_values: dict[str, Any] = {}
    microphone_choice_state: dict[str, Optional[MicrophoneChoiceState]] = {
        "current": None
    }
    microphone_refresh_unset = object()
    microphone_level_monitor = MicrophoneLevelMonitor()

    def default_input_device_index() -> Optional[int]:
        try:
            device = sd.default.device
            index = device[0] if isinstance(device, (list, tuple)) else device
            return int(index) if int(index) >= 0 else None
        except Exception:
            # Brak domyślnego wejścia zabiera tylko gwiazdkę przy pozycji.
            return None

    def display_for_value(
        mapping: dict[str, Any],
        value: Any,
        custom_prefix: Optional[str] = None,
    ) -> str:
        for label, stored in mapping.items():
            if stored == value:
                return label
        custom_prefix = custom_prefix or t("Niestandardowe", "Custom")
        label = f"{custom_prefix}: {value}"
        mapping[label] = value
        return label

    def localized_trigger_display_name(trigger: str, compact: bool = False) -> str:
        trigger_type, name = split_trigger(trigger)
        if trigger_type == "mouse":
            if compact and name in {"x1", "x2"}:
                return t("Mysz {name}", "Mouse {name}", name=name.upper())
            mouse_labels = {
                "left": t("lewy przycisk", "left button"),
                "right": t("prawy przycisk", "right button"),
                "middle": t("środkowy przycisk", "middle button"),
                "x1": t("boczny przycisk X1", "side button X1"),
                "x2": t("boczny przycisk X2", "side button X2"),
            }
            label = mouse_labels.get(name, name.upper())
            return t("Mysz: {name}", "Mouse: {name}", name=label)

        key_labels = {
            "pause": "Pause/Break",
            "scroll_lock": "Scroll Lock",
            "caps_lock": "Caps Lock",
            "space": t("Spacja", "Space"),
            "tab": "Tab",
            "insert": "Insert",
            "delete": "Delete",
            "home": "Home",
            "end": "End",
            "page_up": "Page Up",
            "page_down": "Page Down",
            "ctrl": "Ctrl",
            "ctrl_l": t("Lewy Ctrl", "Left Ctrl"),
            "ctrl_r": t("Prawy Ctrl", "Right Ctrl"),
            "alt": "Alt",
            "alt_l": t("Lewy Alt", "Left Alt"),
            "alt_r": t("Prawy Alt", "Right Alt"),
            "shift": "Shift",
            "shift_l": t("Lewy Shift", "Left Shift"),
            "shift_r": t("Prawy Shift", "Right Shift"),
            "cmd": "Windows",
            "cmd_l": t("Lewy Windows", "Left Windows"),
            "cmd_r": t("Prawy Windows", "Right Windows"),
        }
        if name in key_labels:
            label = key_labels[name]
        elif len(name) == 1 or re.fullmatch(r"f\d{1,2}", name):
            label = name.upper()
        elif name.startswith("vk") and name[2:].isdigit():
            label = t(
                "klawisz VK {number}",
                "VK key {number}",
                number=name[2:],
            )
        else:
            label = name.replace("_", " ").title()
        if compact:
            return label
        return t("Klawiatura: {name}", "Keyboard: {name}", name=label)

    def ensure_trigger_display(value: Any) -> str:
        trigger = str(value or "keyboard:f8").strip().lower()
        for label, stored in trigger_values.items():
            if stored == trigger:
                return label
        try:
            label = localized_trigger_display_name(trigger)
        except AppError:
            label = t(
                "Niestandardowe: {trigger}",
                "Custom: {trigger}",
                trigger=trigger,
            )
        trigger_values[label] = trigger
        return label

    trigger_var = tk.StringVar(
        value=ensure_trigger_display(config.get("trigger", "keyboard:f8"))
    )
    custom_commands_source = config.get("custom_commands", {})
    custom_commands_schema_is_supported = (
        command_engine.custom_commands_schema_supported(custom_commands_source)
    )
    custom_commands_config = custom_commands_source
    if not isinstance(custom_commands_config, dict):
        custom_commands_config = {}
    custom_commands_enabled_var = tk.BooleanVar(
        value=(
            custom_commands_schema_is_supported
            and custom_commands_config.get("enabled", False) is True
        )
    )
    custom_commands_trigger_var = tk.StringVar(
        value=ensure_trigger_display(
            custom_commands_config.get("trigger", "keyboard:f7")
        )
    )
    (
        custom_command_items,
        original_custom_command_items_by_key,
        unmanaged_custom_command_items,
    ) = partition_custom_command_items(config)
    preserve_original_custom_commands_enabled = bool(
        custom_commands_schema_is_supported
        and custom_commands_config.get("enabled", False) is True
        and not custom_command_items
        and unmanaged_custom_command_items
    )
    custom_commands_enabled_touched = {"value": False}
    if not custom_command_items:
        custom_commands_enabled_var.set(False)
    custom_commands_revision_var = tk.StringVar(
        value=json.dumps(
            {
                "items": custom_command_items,
                "unmanaged": unmanaged_custom_command_items,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    model_var = tk.StringVar(
        value=display_for_value(model_values, config.get("model", "auto"))
    )
    device_var = tk.StringVar(
        value=display_for_value(device_values, config.get("device", "auto"))
    )
    language_var = tk.StringVar(
        value=display_for_value(language_values, config.get("language", "auto"))
    )
    ui_language_var = tk.StringVar(
        value=display_for_value(
            ui_language_values,
            config.get("ui_language", "auto"),
        )
    )
    cpu_threads_var = tk.StringVar(value=str(config.get("cpu_threads", 0)))
    beam_size_var = tk.StringVar(value=str(config.get("beam_size", 2)))
    microphone_var = tk.StringVar()
    microphone_show_all_var = tk.BooleanVar(value=False)

    pre_roll_var = tk.StringVar(value=str(config.get("pre_roll_ms", 300)))
    post_roll_var = tk.StringVar(value=str(config.get("post_roll_ms", 120)))
    minimum_recording_var = tk.StringVar(
        value=str(config.get("minimum_recording_ms", 250))
    )
    maximum_recording_var = tk.StringVar(
        value=str(
            config.get(
                "maximum_recording_seconds",
                DEFAULT_MAXIMUM_RECORDING_SECONDS,
            )
        )
    )
    minimum_rms_var = tk.StringVar(value=str(config.get("minimum_rms", 0.0015)))

    vad = config.get("vad", {})
    vad_enabled_var = tk.BooleanVar(value=bool(vad.get("enabled", True)))
    vad_threshold_var = tk.StringVar(value=str(vad.get("threshold", 0.45)))
    vad_min_speech_var = tk.StringVar(
        value=str(vad.get("min_speech_duration_ms", 120))
    )
    vad_min_silence_var = tk.StringVar(
        value=str(vad.get("min_silence_duration_ms", 250))
    )
    vad_speech_pad_var = tk.StringVar(value=str(vad.get("speech_pad_ms", 180)))

    dictionary = config.get("dictionary", {})
    dictionary_enabled_var = tk.BooleanVar(
        value=bool(dictionary.get("enabled", True))
    )
    dictionary_max_terms_var = tk.StringVar(
        value=str(dictionary.get("max_terms", 120))
    )

    paste = config.get("paste", {})
    paste_enabled_var = tk.BooleanVar(value=bool(paste.get("enabled", True)))
    copy_to_clipboard_var = tk.BooleanVar(
        value=bool(paste.get("copy_to_clipboard", True))
    )
    append_space_var = tk.BooleanVar(value=bool(paste.get("append_space", True)))
    paste_delay_var = tk.StringVar(value=str(paste.get("delay_ms", 25)))

    feedback = config.get("feedback", {})
    sounds_var = tk.BooleanVar(value=bool(feedback.get("sounds", True)))
    notifications_var = tk.BooleanVar(
        value=bool(feedback.get("notifications", True))
    )
    floating_indicator_var = tk.BooleanVar(
        value=bool(feedback.get("floating_indicator", True))
    )
    loop_recording_sound_var = tk.BooleanVar(
        value=bool(feedback.get("loop_recording_sound", False))
    )
    custom_sounds = feedback.get("custom_sounds", {})
    if not isinstance(custom_sounds, dict):
        custom_sounds = {}
    sound_path_vars: dict[str, tk.StringVar] = {}
    for sound_kind in sorted(CUSTOM_SOUND_KINDS):
        sound_path = resolve_sound_path(custom_sounds.get(sound_kind, ""))
        sound_path_vars[sound_kind] = tk.StringVar(
            value=str(sound_path) if sound_path is not None else ""
        )
    voice_commands_var = tk.BooleanVar(
        value=bool(config.get("voice_commands", {}).get("enabled", False))
    )

    ollama = config.get("ollama_cleanup", {})
    ollama_enabled_var = tk.BooleanVar(value=bool(ollama.get("enabled", False)))
    ollama_url_var = tk.StringVar(
        value=str(ollama.get("url", "http://127.0.0.1:11434"))
    )
    ollama_model_var = tk.StringVar(value=str(ollama.get("model", "")))
    ollama_timeout_var = tk.StringVar(
        value=str(ollama.get("timeout_seconds", 45))
    )

    status_var = tk.StringVar(
        value=t(
            "Wszystko gotowe — ustawienia są zapisane.",
            "Everything is ready — settings are saved.",
        )
    )
    status_level = {"value": "success"}
    shortcut_summary_var = tk.StringVar()
    custom_command_summary_var = tk.StringVar()
    quality_profile_var = tk.StringVar()
    profile_labels = {
        "light": t("Szybki", "Fast"),
        "balanced": t("Zalecany", "Recommended"),
        "accurate": t("Najdokładniejszy", "Most accurate"),
    }

    def set_status(message: str, level: str = "success") -> None:
        status_level["value"] = level
        status_var.set(message)

    def refresh_shortcut_summary(*args) -> None:
        trigger = str(
            trigger_values.get(
                trigger_var.get(),
                config.get("trigger", "keyboard:f8"),
            )
        )
        try:
            summary = localized_trigger_display_name(trigger, compact=True)
        except AppError:
            summary = trigger_var.get()
        shortcut_summary_var.set(summary)

    trigger_var.trace_add("write", refresh_shortcut_summary)
    refresh_shortcut_summary()

    def localized_command_count(count: int) -> str:
        count = max(0, int(count))
        if translator.language != "pl":
            noun = "command" if count == 1 else "commands"
            return f"{count} {noun}"
        if count == 1:
            noun = "komenda"
        elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
            noun = "komendy"
        else:
            noun = "komend"
        return f"{count} {noun}"

    def refresh_custom_command_summary(*args) -> None:
        if not custom_commands_enabled_var.get() or not custom_command_items:
            custom_command_summary_var.set(t("Wyłączone", "Disabled"))
            return
        trigger = str(
            trigger_values.get(
                custom_commands_trigger_var.get(),
                custom_commands_config.get("trigger", "keyboard:f7"),
            )
        )
        try:
            trigger_label = localized_trigger_display_name(trigger, compact=True)
        except AppError:
            trigger_label = custom_commands_trigger_var.get()
        custom_command_summary_var.set(
            t(
                "{trigger} · {count}",
                "{trigger} · {count}",
                trigger=trigger_label,
                count=localized_command_count(len(custom_command_items)),
            )
        )

    for command_summary_variable in (
        custom_commands_enabled_var,
        custom_commands_trigger_var,
        custom_commands_revision_var,
    ):
        command_summary_variable.trace_add(
            "write", refresh_custom_command_summary
        )
    refresh_custom_command_summary()

    def selected_profile_key() -> Optional[str]:
        selected_model = str(model_values.get(model_var.get(), model_var.get()))
        selected_device = str(
            device_values.get(device_var.get(), device_var.get())
        )
        return matching_quick_profile(
            selected_model,
            selected_device,
            beam_size_var.get(),
        )

    def refresh_quality_summary(*args) -> None:
        profile_name = selected_profile_key()
        quality_profile_var.set(
            profile_labels[profile_name]
            if profile_name is not None
            else t("Niestandardowy", "Custom")
        )

    for profile_variable in (model_var, device_var, beam_size_var):
        profile_variable.trace_add("write", refresh_quality_summary)
    refresh_quality_summary()
    page_title_var = tk.StringVar(value=t("Start", "Home"))
    page_subtitle_var = tk.StringVar(
        value=t(
            "Najważniejsze informacje i szybki dostęp do ustawień dyktowania.",
            "Key information and quick access to dictation settings.",
        )
    )

    shell = ttk.Frame(root, style="App.TFrame")
    shell.grid(row=0, column=0, sticky="nsew")
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)
    shell.rowconfigure(0, weight=1)
    shell.columnconfigure(1, weight=1)

    sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=px(216))
    sidebar.grid(row=0, column=0, sticky="ns")
    sidebar.grid_propagate(False)
    sidebar.columnconfigure(0, weight=1)
    sidebar.rowconfigure(20, weight=1)

    brand = ttk.Frame(
        sidebar,
        style="Sidebar.TFrame",
        padding=(px(18), px(24), px(18), px(18)),
    )
    brand.grid(row=0, column=0, sticky="ew")
    brand.columnconfigure(1, weight=1)
    try:
        from PIL import ImageTk

        brand_image = make_tray_image("brand").resize((px(38), px(38)))
        root._mowik_brand_icon = ImageTk.PhotoImage(brand_image)  # type: ignore[attr-defined]
        ttk.Label(
            brand,
            image=root._mowik_brand_icon,  # type: ignore[attr-defined]
            style="SidebarMeta.TLabel",
        ).grid(row=0, column=0, rowspan=2, padx=(0, px(11)))
    except Exception:
        pass
    ttk.Label(brand, text="Mówik", style="SidebarBrand.TLabel").grid(
        row=0, column=1, sticky="sw"
    )
    ttk.Label(
        brand,
        text=t("Centrum ustawień", "Settings Center"),
        style="SidebarMeta.TLabel",
    ).grid(row=1, column=1, sticky="nw")

    privacy_badge = tk.Label(
        sidebar,
        text=t("●  DZIAŁA LOKALNIE", "●  RUNS LOCALLY"),
        background=colors["sidebar_success"],
        foreground=colors["sidebar_success_text"],
        font=(ui_font_family, 9, "bold"),
        padx=px(12),
        pady=px(7),
        anchor="w",
    )
    privacy_badge.grid(
        row=1,
        column=0,
        sticky="ew",
        padx=px(18),
        pady=(0, px(20)),
    )

    main = ttk.Frame(shell, style="Surface.TFrame")
    main.grid(row=0, column=1, sticky="nsew")
    main.rowconfigure(2, weight=1)
    main.columnconfigure(0, weight=1)

    header = ttk.Frame(
        main,
        style="Surface.TFrame",
        padding=(px(32), px(24), px(32), px(18)),
    )
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(0, weight=1)
    ttk.Label(header, textvariable=page_title_var, style="Title.TLabel").grid(
        row=0, column=0, sticky="w"
    )
    ttk.Label(
        header,
        textvariable=page_subtitle_var,
        style="Subtitle.TLabel",
        wraplength=px(650),
    ).grid(row=1, column=0, sticky="w", pady=(px(3), 0))
    local_chip = tk.Label(
        header,
        text=t("Prywatnie i offline", "Private and offline"),
        background=colors["success_soft"],
        foreground=colors["success"],
        font=(ui_font_family, 9, "bold"),
        padx=px(11),
        pady=px(6),
    )
    local_chip.grid(
        row=0,
        column=1,
        rowspan=2,
        sticky="e",
        padx=(px(18), 0),
    )
    ttk.Separator(main).grid(row=1, column=0, sticky="ew")

    page_host = ttk.Frame(
        main,
        style="Surface.TFrame",
        padding=(px(32), 0, px(22), 0),
    )
    page_host.grid(row=2, column=0, sticky="nsew")
    page_host.rowconfigure(0, weight=1)
    page_host.columnconfigure(0, weight=1)

    def create_scrollable_page(parent) -> tuple[ttk.Frame, ttk.Frame, tk.Canvas]:
        wrapper = ttk.Frame(parent, style="Surface.TFrame")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        canvas = tk.Canvas(
            wrapper,
            background=colors["surface"],
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(
            wrapper,
            orient="vertical",
            command=canvas.yview,
            style="Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(px(8), 0))
        content = ttk.Frame(
            canvas,
            style="Surface.TFrame",
            padding=(0, px(16), px(8), px(28)),
        )
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas._mowik_window_id = window_id  # type: ignore[attr-defined]

        def update_scroll_region(event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_content(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        content.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", fit_content)
        return wrapper, content, canvas

    start_page, start_tab, start_canvas = create_scrollable_page(page_host)
    general_page, general_tab, general_canvas = create_scrollable_page(page_host)
    audio_page, audio_tab, audio_canvas = create_scrollable_page(page_host)
    text_page, text_tab, text_canvas = create_scrollable_page(page_host)
    commands_page, commands_tab, commands_canvas = create_scrollable_page(
        page_host
    )
    sounds_page, sounds_tab, sounds_canvas = create_scrollable_page(page_host)
    ollama_page, ollama_tab, ollama_canvas = create_scrollable_page(page_host)
    files_page, files_tab, files_canvas = create_scrollable_page(page_host)

    page_frames = {
        "start": start_page,
        "dictation": general_page,
        "audio": audio_page,
        "text": text_page,
        "commands": commands_page,
        "sounds": sounds_page,
        "integrations": ollama_page,
        "help": files_page,
    }
    page_canvases = {
        "start": start_canvas,
        "dictation": general_canvas,
        "audio": audio_canvas,
        "text": text_canvas,
        "commands": commands_canvas,
        "sounds": sounds_canvas,
        "integrations": ollama_canvas,
        "help": files_canvas,
    }
    page_contents = {
        "start": start_tab,
        "dictation": general_tab,
        "audio": audio_tab,
        "text": text_tab,
        "commands": commands_tab,
        "sounds": sounds_tab,
        "integrations": ollama_tab,
        "help": files_tab,
    }
    page_meta = {
        "start": (
            t("Start", "Home"),
            t(
                "Najważniejsze informacje i szybki dostęp do ustawień dyktowania.",
                "Key information and quick access to dictation settings.",
            ),
        ),
        "dictation": (
            t("Dyktowanie", "Dictation"),
            t(
                "Skrót, mikrofon, język oraz profil szybkości i dokładności.",
                "Choose the shortcut, microphone, language, speed, and accuracy.",
            ),
        ),
        "audio": (
            t("Mikrofon i wykrywanie mowy", "Microphone and speech detection"),
            t(
                "Dopasuj czułość i bufory tylko wtedy, gdy nagrania są ucinane.",
                "Adjust sensitivity and buffers only if recordings are clipped.",
            ),
        ),
        "text": (
            t("Tekst i słownik", "Text and dictionary"),
            t(
                "Zdecyduj, gdzie trafia transkrypcja i podpowiedz Mówikowi własne nazwy.",
                "Choose where transcripts go and teach Mówik your preferred names.",
            ),
        ),
        "commands": (
            t("Własne komendy", "Custom commands"),
            t(
                "Powiedz krótką frazę, aby wkleić tekst albo uruchomić wybraną akcję.",
                "Say a short phrase to insert text or run a chosen action.",
            ),
        ),
        "sounds": (
            t("Dźwięki i powiadomienia", "Sounds and notifications"),
            t(
                "Wybierz dyskretną informację zwrotną na każdym etapie dyktowania.",
                "Choose subtle feedback for each stage of dictation.",
            ),
        ),
        "integrations": (
            t("Integracje", "Integrations"),
            t(
                "Opcjonalna, lokalna korekta tekstu przez Ollamę.",
                "Optional local text correction with Ollama.",
            ),
        ),
        "help": (
            t("Pomoc i diagnostyka", "Help and diagnostics"),
            t(
                "Szybki dostęp do konfiguracji, danych aplikacji i logów.",
                "Quick access to configuration, application data, and logs.",
            ),
        ),
    }
    for page in page_frames.values():
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_remove()

    nav_buttons: dict[str, ttk.Button] = {}
    nav_markers: dict[str, tk.Frame] = {}
    active_page_key = {"value": "start"}

    def widget_is_inside(widget, ancestor) -> bool:
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    def keep_focused_control_visible(widget) -> None:
        page_key = active_page_key["value"]
        canvas = page_canvases[page_key]
        content = page_contents[page_key]
        try:
            if (
                not widget.winfo_exists()
                or root.focus_get() != widget
                or not widget_is_inside(widget, content)
            ):
                return
            canvas.update_idletasks()
            scroll_region = canvas.bbox("all")
            if scroll_region is None:
                return
            content_top = content.winfo_rooty()
            widget_top = widget.winfo_rooty() - content_top
            widget_bottom = widget_top + widget.winfo_height()
            viewport_top = canvas.canvasy(0)
            viewport_height = max(1, canvas.winfo_height())
            viewport_bottom = viewport_top + viewport_height
            margin = px(14)
            total_height = max(1, scroll_region[3] - scroll_region[1])
            if widget_top < viewport_top + margin:
                target = max(0, widget_top - margin)
                canvas.yview_moveto(target / total_height)
            elif widget_bottom > viewport_bottom - margin:
                target = max(0, widget_bottom + margin - viewport_height)
                canvas.yview_moveto(min(1, target / total_height))
        except tk.TclError:
            return

    def handle_focus_in(event) -> None:
        root.after_idle(
            lambda target=event.widget: keep_focused_control_visible(target)
        )

    root.bind_all("<FocusIn>", handle_focus_in, add="+")

    def scroll_active_page(event) -> None:
        if not event.delta:
            return
        try:
            # Modalne okna (edytor komend, wykrywanie skrótu) mają własne
            # przewijanie; tło nie może się pod nimi przesuwać.
            if event.widget.winfo_toplevel() is not root:
                return
        except (AttributeError, tk.TclError):
            return
        page_canvases[active_page_key["value"]].yview_scroll(
            int(-event.delta / 120), "units"
        )

    def scroll_page_without_changing_value(event) -> str:
        # Tk domyślnie zmienia wartość Spinboxa i Combobox kółkiem myszy.
        # W panelu ustawień zwykłe przewinięcie strony po najechaniu na takie
        # pole po cichu podmieniłoby model, mikrofon albo skrót dyktowania.
        scroll_active_page(event)
        return "break"

    root.bind_all("<MouseWheel>", scroll_active_page)
    for wheel_sensitive_class in ("TSpinbox", "TCombobox"):
        root.bind_class(
            wheel_sensitive_class,
            "<MouseWheel>",
            scroll_page_without_changing_value,
        )

    def show_page(page_key: str) -> None:
        title, subtitle = page_meta[page_key]
        active_page_key["value"] = page_key
        page_title_var.set(title)
        page_subtitle_var.set(subtitle)
        for key, page in page_frames.items():
            if key == page_key:
                page.grid()
                page.tkraise()
            else:
                page.grid_remove()
        for key, button in nav_buttons.items():
            button.configure(
                style="NavActive.TButton" if key == page_key else "Nav.TButton"
            )
        for key, marker in nav_markers.items():
            marker.configure(
                background=(
                    colors["primary"] if key == page_key else colors["sidebar"]
                )
            )
        def finish_page_layout() -> None:
            canvas = page_canvases[page_key]
            content = page_contents[page_key]
            canvas.update_idletasks()
            canvas.itemconfigure(
                canvas._mowik_window_id,  # type: ignore[attr-defined]
                width=max(1, canvas.winfo_width()),
            )
            content.update_idletasks()
            scroll_region = canvas.bbox("all")
            if scroll_region is not None:
                canvas.configure(scrollregion=scroll_region)
            canvas.yview_moveto(0)

        root.after_idle(finish_page_layout)

    nav_row = 2
    for section, items in (
        (t("CENTRUM", "CENTER"), (("start", t("Start", "Home")),)),
        (
            t("USTAWIENIA", "SETTINGS"),
            (
                ("dictation", t("Dyktowanie", "Dictation")),
                ("audio", t("Mikrofon i mowa", "Microphone and speech")),
                ("text", t("Tekst i słownik", "Text and dictionary")),
                ("commands", t("Własne komendy", "Custom commands")),
                ("sounds", t("Dźwięki", "Sounds")),
            ),
        ),
        (
            t("WIĘCEJ", "MORE"),
            (
                ("integrations", t("Integracje", "Integrations")),
                ("help", t("Pomoc i diagnostyka", "Help and diagnostics")),
            ),
        ),
    ):
        ttk.Label(sidebar, text=section, style="SidebarSection.TLabel").grid(
            row=nav_row,
            column=0,
            sticky="w",
            padx=px(19),
            pady=(px(7) if nav_row == 2 else px(18), px(6)),
        )
        nav_row += 1
        for page_key, label in items:
            # Wiersz nawigacji to pasek akcentu + przycisk. Sama plama tła nie
            # wystarczała, by na pierwszy rzut oka wskazać otwartą stronę.
            row = tk.Frame(sidebar, background=colors["sidebar"])
            row.grid(
                row=nav_row,
                column=0,
                sticky="ew",
                padx=px(10),
                pady=px(1),
            )
            row.columnconfigure(1, weight=1)
            marker = tk.Frame(
                row,
                background=colors["sidebar"],
                width=px(3),
            )
            marker.grid(row=0, column=0, sticky="ns")
            marker.grid_propagate(False)
            button = ttk.Button(
                row,
                text=label,
                style="Nav.TButton",
                cursor="hand2",
                command=lambda selected=page_key: show_page(selected),
            )
            button.grid(row=0, column=1, sticky="ew")
            nav_buttons[page_key] = button
            nav_markers[page_key] = marker
            nav_row += 1

    ttk.Label(
        sidebar,
        text=f"Mówik {APP_VERSION}\nWindows 10/11",
        style="SidebarMeta.TLabel",
        justify="left",
    ).grid(row=21, column=0, sticky="sw", padx=px(19), pady=px(18))

    ttk.Separator(main).grid(row=3, column=0, sticky="ew")
    footer = ttk.Frame(
        main,
        style="Surface.TFrame",
        padding=(px(32), px(14), px(32), px(16)),
    )
    footer.grid(row=4, column=0, sticky="ew")
    footer.columnconfigure(1, weight=1)

    # Widok startowy podaje użytkownikowi najważniejszy przepływ bez
    # wystawiania na pierwszy plan parametrów technicznych Whispera.
    start_tab.columnconfigure(0, weight=1)
    hero = tk.Frame(
        start_tab,
        background=colors["primary_soft"],
        highlightbackground=colors["primary_border"],
        highlightthickness=1,
        padx=px(24),
        pady=px(22),
    )
    hero.grid(row=0, column=0, sticky="ew", pady=(0, px(16)))
    hero.columnconfigure(0, weight=1)
    tk.Label(
        hero,
        text=t("GOTOWY DO DYKTOWANIA", "READY TO DICTATE"),
        background=colors["primary_soft"],
        foreground=colors["primary"],
        font=(ui_font_family, 9, "bold"),
    ).grid(row=0, column=0, sticky="w")
    tk.Label(
        hero,
        text=t(
            "Przytrzymaj skrót, powiedz zdanie i puść",
            "Hold the shortcut, speak, then release",
        ),
        background=colors["primary_soft"],
        foreground=colors["text"],
        font=(display_font_family, 18, "bold"),
        justify="left",
        wraplength=px(470),
    ).grid(row=1, column=0, sticky="w", pady=(px(5), px(5)))
    tk.Label(
        hero,
        text=t(
            "Mówik rozpozna głos lokalnie i wklei tekst do aktywnego okna. "
            "Nie wysyła nagrań do chmury.",
            "Mówik recognizes speech locally and pastes text into the active "
            "window. Recordings are never sent to the cloud.",
        ),
        background=colors["primary_soft"],
        foreground=colors["muted"],
        font=(ui_font_family, 10),
        justify="left",
        wraplength=px(470),
    ).grid(row=2, column=0, sticky="w")
    shortcut_keycap = tk.Label(
        hero,
        textvariable=shortcut_summary_var,
        background=colors["surface"],
        foreground=colors["text"],
        font=(ui_font_family, 12, "bold"),
        relief="solid",
        borderwidth=1,
        padx=px(18),
        pady=px(10),
    )
    shortcut_keycap.grid(
        row=0,
        column=1,
        rowspan=2,
        sticky="e",
        padx=(px(20), 0),
    )
    ttk.Button(
        hero,
        text=t("Zmień skrót", "Change shortcut"),
        command=lambda: show_page("dictation"),
    ).grid(
        row=2,
        column=1,
        sticky="e",
        padx=(px(20), 0),
        pady=(px(10), 0),
    )

    overview = ttk.Frame(start_tab, style="Surface.TFrame")
    overview.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))
    for column in range(4):
        overview.columnconfigure(column, weight=1, uniform="overview")

    def add_overview_card(
        parent, column: int, eyebrow: str, value_var: tk.Variable, description: str
    ) -> None:
        card = tk.Frame(
            parent,
            background=colors["surface_alt"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            padx=px(16),
            pady=px(14),
        )
        card.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(
                0 if column == 0 else px(6),
                0 if column == 3 else px(6),
            ),
        )
        tk.Label(
            card,
            text=eyebrow,
            background=colors["surface_alt"],
            foreground=colors["muted"],
            font=(ui_font_family, 9, "bold"),
        ).pack(anchor="w")
        value_label = tk.Label(
            card,
            textvariable=value_var,
            background=colors["surface_alt"],
            foreground=colors["text"],
            font=(ui_font_family, 10, "bold"),
            justify="left",
            anchor="w",
            wraplength=px(150),
        )
        value_label.pack(anchor="w", fill="x", pady=(px(5), px(3)))
        description_label = tk.Label(
            card,
            text=description,
            background=colors["surface_alt"],
            foreground=colors["muted"],
            font=(ui_font_family, 9),
            justify="left",
            anchor="w",
            wraplength=px(150),
        )
        description_label.pack(anchor="w", fill="x")

        def fit_card_text(event) -> None:
            wrap_length = max(px(96), event.width - px(34))
            value_label.configure(wraplength=wrap_length)
            description_label.configure(wraplength=wrap_length)

        card.bind("<Configure>", fit_card_text, add="+")

    add_overview_card(
        overview,
        0,
        t("SKRÓT", "SHORTCUT"),
        trigger_var,
        t("Przytrzymaj podczas mówienia", "Hold while speaking"),
    )
    add_overview_card(
        overview,
        1,
        t("KOMENDY", "COMMANDS"),
        custom_command_summary_var,
        t("Osobny tryb akcji głosowych", "Separate voice-action mode"),
    )
    add_overview_card(
        overview,
        2,
        t("MIKROFON", "MICROPHONE"),
        microphone_var,
        t("Aktywne źródło dźwięku", "Active audio source"),
    )
    add_overview_card(
        overview,
        3,
        t("PROFIL JAKOŚCI", "QUALITY PROFILE"),
        quality_profile_var,
        t("Balans szybkości i jakości", "Speed and accuracy balance"),
    )

    privacy_panel = tk.Frame(
        start_tab,
        background=colors["success_soft"],
        highlightbackground=colors["success_border"],
        highlightthickness=1,
        padx=px(18),
        pady=px(14),
    )
    privacy_panel.grid(row=2, column=0, sticky="ew", pady=(0, px(16)))
    tk.Label(
        privacy_panel,
        text=t(
            "Prywatność jest ustawieniem domyślnym",
            "Privacy is the default",
        ),
        background=colors["success_soft"],
        foreground=colors["success"],
        font=(ui_font_family, 10, "bold"),
    ).pack(anchor="w")
    tk.Label(
        privacy_panel,
        text=t(
            "Dźwięk istnieje tylko chwilowo w pamięci RAM. Nagrania nie są "
            "zapisywane, a treść dyktowania nie trafia do logów.",
            "Audio exists only briefly in memory. Recordings are not saved, "
            "and dictated content is never written to logs.",
        ),
        background=colors["success_soft"],
        foreground=colors["success"],
        font=(ui_font_family, 9),
        justify="left",
        wraplength=px(700),
    ).pack(anchor="w", pady=(px(4), 0))

    ui_language_frame = ttk.LabelFrame(
        start_tab,
        text=t("Język interfejsu", "Interface language"),
        padding=px(16),
    )
    ui_language_frame.grid(row=3, column=0, sticky="ew")
    ui_language_frame.columnconfigure(0, weight=1)
    ui_language_combo = ttk.Combobox(
        ui_language_frame,
        textvariable=ui_language_var,
        values=list(ui_language_values.keys()),
        state="readonly",
    )
    ui_language_combo.grid(row=0, column=0, sticky="ew")
    ttk.Label(
        ui_language_frame,
        text=t(
            "Zmiana będzie widoczna po wybraniu „Zapisz i uruchom ponownie”.",
            "The change takes effect after choosing “Save and restart”.",
        ),
        style="Muted.TLabel",
        wraplength=px(700),
    ).grid(row=1, column=0, sticky="w", pady=(px(7), 0))

    for tab in (
        general_tab,
        audio_tab,
        text_tab,
        commands_tab,
        sounds_tab,
        ollama_tab,
        files_tab,
    ):
        tab.columnconfigure(1, weight=1)

    presets = ttk.LabelFrame(
        general_tab,
        text=t("Profil jakości", "Quality profile"),
        padding=px(16),
    )
    presets.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    for column in range(3):
        presets.columnconfigure(column, weight=1)

    profile_buttons: dict[str, ttk.Button] = {}
    def refresh_profile_buttons(*args) -> None:
        selected_profile = selected_profile_key()
        for profile_name, button in profile_buttons.items():
            button.configure(
                style=(
                    "SelectedProfile.TButton"
                    if profile_name == selected_profile
                    else "Profile.TButton"
                )
            )

    def set_profile(profile_name: str) -> None:
        profile = QUICK_PROFILES[profile_name]
        changes = profile["changes"]
        model_var.set(display_for_value(model_values, changes["model"]))
        device_var.set(display_for_value(device_values, changes["device"]))
        beam_size_var.set(str(changes["beam_size"]))
        set_status(
            t(
                "Wybrano profil „{profile}”. Zapisz i uruchom ponownie, aby go aktywować.",
                "Selected the “{profile}” profile. Save and restart to activate it.",
                profile=profile_labels[profile_name],
            ),
            "warning",
        )
        refresh_profile_buttons()

    for column, (profile_name, button_text) in enumerate(
        (
            (
                "light",
                t(
                    "Szybki\nNajmniejsze opóźnienie",
                    "Fast\nLowest latency",
                ),
            ),
            (
                "balanced",
                t(
                    "Zalecany\nNajlepszy balans",
                    "Recommended\nBest balance",
                ),
            ),
            (
                "accurate",
                t(
                    "Najdokładniejszy\nWyższa jakość, wolniej",
                    "Most accurate\nHigher quality, slower",
                ),
            ),
        )
    ):
        button = ttk.Button(
            presets,
            text=button_text,
            style="Profile.TButton",
            command=lambda selected=profile_name: set_profile(selected),
        )
        button.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(
                0 if column == 0 else px(5),
                0 if column == 2 else px(5),
            ),
        )
        profile_buttons[profile_name] = button
    model_var.trace_add("write", refresh_profile_buttons)
    device_var.trace_add("write", refresh_profile_buttons)
    beam_size_var.trace_add("write", refresh_profile_buttons)
    refresh_profile_buttons()

    def add_field(
        parent,
        row: int,
        label: str,
        widget,
        hint: Optional[str] = None,
    ) -> None:
        ttk.Label(parent, text=label, style="Field.TLabel").grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, px(14)),
            pady=px(7),
        )
        widget.grid(row=row, column=1, sticky="ew", pady=px(7))
        if hint:
            ttk.Label(
                parent,
                text=hint,
                style="Muted.TLabel",
                wraplength=px(220),
                justify="left",
            ).grid(
                row=row,
                column=2,
                sticky="w",
                padx=(px(14), 0),
                pady=px(7),
            )

    def create_disclosure(
        parent,
        row: int,
        canvas: tk.Canvas,
        summary: str,
        *,
        name: str,
        initially_expanded: bool = False,
        pady: tuple[int, int] = (0, 0),
    ) -> dict[str, Any]:
        container = tk.Frame(
            parent,
            name=name,
            background=colors["surface_alt"],
            highlightbackground=colors["border"],
            highlightthickness=1,
        )
        container.grid(
            row=row,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=pady,
        )
        container.columnconfigure(0, weight=1)
        # Ciało musi mieć tło kontenera. Ze stylem Surface (ciemniejszym niż
        # surface_alt karty) po rozwinięciu pojawiał się niezamierzony,
        # wgłębiony prostokąt zamiast jednolitej powierzchni.
        body = ttk.Frame(
            container,
            name="body",
            style="Alt.TFrame",
            padding=(px(14), px(8), px(14), px(14)),
        )
        body.columnconfigure(1, weight=1)
        state = {"expanded": not initially_expanded}

        def set_expanded(expanded: bool, focus_widget=None) -> None:
            expanded = bool(expanded)
            if not expanded:
                current_focus = root.focus_get()
                if current_focus is not None and widget_is_inside(
                    current_focus, body
                ):
                    toggle_button.focus_set()
            state["expanded"] = expanded
            if expanded:
                body.grid(row=2, column=0, sticky="ew")
                toggle_button.configure(
                    text=t(
                        "▾  Ukryj ustawienia zaawansowane",
                        "▾  Hide advanced settings",
                    ),
                    style="DisclosureOpen.TButton",
                )
            else:
                body.grid_remove()
                toggle_button.configure(
                    text=t(
                        "▸  Pokaż ustawienia zaawansowane",
                        "▸  Show advanced settings",
                    ),
                    style="Disclosure.TButton",
                )

            def finish_layout() -> None:
                scroll_region = canvas.bbox("all")
                if scroll_region is not None:
                    canvas.configure(scrollregion=scroll_region)
                if focus_widget is not None and expanded:
                    focus_widget.focus_set()

            root.after_idle(finish_layout)

        def toggle() -> None:
            set_expanded(not state["expanded"])

        toggle_button = ttk.Button(
            container,
            name="toggle",
            command=toggle,
            takefocus=True,
        )
        toggle_button.grid(row=0, column=0, sticky="ew")
        tk.Label(
            container,
            text=summary,
            background=colors["surface_alt"],
            foreground=colors["muted"],
            font=(ui_font_family, 9),
            justify="left",
            anchor="w",
            wraplength=px(700),
            padx=px(14),
            pady=0,
        ).grid(row=1, column=0, sticky="ew", pady=(0, px(11)))
        toggle_button.bind(
            "<Right>", lambda event: set_expanded(True) or "break"
        )
        toggle_button.bind(
            "<Left>", lambda event: set_expanded(False) or "break"
        )
        set_expanded(initially_expanded)
        return {
            "container": container,
            "body": body,
            "button": toggle_button,
            "set_expanded": set_expanded,
            "reveal": lambda widget=None: set_expanded(True, widget),
            "expanded": lambda: bool(state["expanded"]),
        }

    dictation_basics = ttk.LabelFrame(
        general_tab,
        text=t("Najważniejsze ustawienia", "Everyday settings"),
        padding=px(16),
    )
    dictation_basics.grid(
        row=1,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    dictation_basics.columnconfigure(1, weight=1)

    trigger_row = ttk.Frame(dictation_basics)
    trigger_row.columnconfigure(0, weight=1)
    trigger_combo = ttk.Combobox(
        trigger_row,
        textvariable=trigger_var,
        values=list(trigger_values.keys()),
        state="readonly",
    )
    trigger_combo.grid(row=0, column=0, sticky="ew")

    def capture_trigger(
        target_var: Optional[tk.StringVar] = None,
        target_combo: Optional[Any] = None,
    ) -> None:
        if target_var is None:
            target_var = trigger_var
        if target_combo is None:
            target_combo = trigger_combo
        dialog = tk.Toplevel(root)
        dialog.title(
            t(
                "{app} — wykrywanie przycisku",
                "{app} — Detect shortcut",
                app=APP_DISPLAY_NAME,
            )
        )
        dialog.resizable(False, False)
        dialog.transient(root)
        dialog.grab_set()
        apply_dark_window_chrome(dialog)
        try:
            dialog.attributes("-topmost", True)
        except tk.TclError:
            pass

        frame = ttk.Frame(dialog, padding=px(20))
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            frame,
            text=t(
                "Naciśnij wybrany klawisz albo przycisk myszy",
                "Press the desired key or mouse button",
            ),
            font=(display_font_family, 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=t(
                "Nasłuchiwanie zacznie się za chwilę, aby nie przechwycić "
                "kliknięcia tego okna. Esc anuluje. Najwygodniejszy jest "
                "klawisz funkcyjny albo boczny przycisk myszy.",
                "Listening starts shortly so this window's click is not "
                "captured. Press Esc to cancel. A function key or side mouse "
                "button is usually most convenient.",
            ),
            wraplength=px(500),
        ).grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(px(8), px(14)),
        )
        capture_status_var = tk.StringVar(
            value=t("Przygotowanie…", "Preparing…")
        )
        ttk.Label(
            frame,
            textvariable=capture_status_var,
            font=(ui_font_family, 11),
        ).grid(row=2, column=0, sticky="w")

        captured_events: queue.Queue[tuple[str, str]] = queue.Queue()
        finished = threading.Event()
        listeners: dict[str, Any] = {"keyboard": None, "mouse": None}

        def stop_capture_listeners() -> None:
            for listener in listeners.values():
                if listener is not None:
                    try:
                        listener.stop()
                    except Exception:
                        logging.debug(
                            "Nie udało się zatrzymać nasłuchiwania przycisku",
                            exc_info=True,
                        )

        def close_capture() -> None:
            if finished.is_set():
                return
            finished.set()
            stop_capture_listeners()
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        def accept_capture(trigger_type: str, name: str) -> None:
            if finished.is_set() or not name:
                return
            finished.set()
            stop_capture_listeners()
            trigger = f"{trigger_type}:{name}".lower()
            label = ensure_trigger_display(trigger)
            target_combo["values"] = list(trigger_values.keys())
            target_var.set(label)
            note = t(
                "Ustawiono {trigger}.",
                "Set to {trigger}.",
                trigger=localized_trigger_display_name(trigger),
            )
            if trigger in {"mouse:left", "mouse:right"}:
                note += t(
                    " Ten przycisk może kolidować ze zwykłą obsługą Windows.",
                    " This button may interfere with normal Windows operation.",
                )
            set_status(
                note
                + t(
                    " Kliknij „Zapisz i uruchom ponownie”.",
                    " Click “Save and restart”.",
                ),
                "warning",
            )
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        def on_capture_key(key) -> Optional[bool]:
            name = key_name(key)
            if not name:
                return None
            captured_events.put(("cancel", "") if name == "esc" else ("keyboard", name))
            return False

        def on_capture_mouse(x, y, button, pressed) -> Optional[bool]:
            if not pressed:
                return None
            captured_events.put(("mouse", mouse_name(button)))
            return False

        def poll_capture_queue() -> None:
            if finished.is_set() or not dialog.winfo_exists():
                return
            try:
                event_type, name = captured_events.get_nowait()
            except queue.Empty:
                dialog.after(35, poll_capture_queue)
                return
            if event_type == "cancel":
                close_capture()
            else:
                accept_capture(event_type, name)

        def begin_capture_listening() -> None:
            if finished.is_set() or not dialog.winfo_exists():
                return
            capture_status_var.set(
                t(
                    "Nasłuchuję — naciśnij teraz wybrany przycisk…",
                    "Listening — press the desired button now…",
                )
            )
            cancel_button.configure(
                state="disabled",
                text=t("Anuluj: Esc", "Cancel: Esc"),
            )
            try:
                listeners.update(
                    start_shortcut_capture_listeners(
                        on_capture_key,
                        on_capture_mouse,
                    )
                )
            except Exception:
                finished.set()
                stop_capture_listeners()
                logging.exception("Nie udało się uruchomić wykrywania przycisku")
                try:
                    dialog.grab_release()
                except tk.TclError:
                    pass
                messagebox.showerror(
                    t(
                        "{app} — wykrywanie przycisku",
                        "{app} — Shortcut detection",
                        app=APP_DISPLAY_NAME,
                    ),
                    t(
                        "Nie udało się uruchomić wykrywania. Żaden przycisk nie jest przechwytywany.",
                        "Shortcut detection could not start. No input is being captured.",
                    ),
                    parent=dialog,
                )
                dialog.destroy()
                return
            poll_capture_queue()

        cancel_button = ttk.Button(
            frame,
            text=t("Anuluj", "Cancel"),
            command=close_capture,
        )
        cancel_button.grid(
            row=3,
            column=0,
            sticky="e",
            pady=(px(18), 0),
        )
        dialog.protocol("WM_DELETE_WINDOW", close_capture)
        dialog.after(650, begin_capture_listening)
        dialog.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - dialog.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")

    ttk.Button(
        trigger_row,
        text=t("Wykryj…", "Detect…"),
        command=capture_trigger,
    ).grid(row=0, column=1, padx=(px(8), 0))
    add_field(
        dictation_basics,
        0,
        t("Przycisk dyktowania", "Dictation shortcut"),
        trigger_row,
        t(
            "Kliknij „Wykryj…”, a potem naciśnij dowolny klawisz lub przycisk myszy.",
            "Click “Detect…”, then press any key or mouse button.",
        ),
    )

    microphone_row = ttk.Frame(dictation_basics)
    microphone_row.columnconfigure(0, weight=1)
    microphone_combo = ttk.Combobox(
        microphone_row,
        textvariable=microphone_var,
        state="readonly",
    )
    microphone_combo.grid(row=0, column=0, sticky="ew")

    def refresh_microphones(selected: Any = microphone_refresh_unset) -> None:
        if selected is microphone_refresh_unset:
            current_state = microphone_choice_state["current"]
            current_label = microphone_var.get()
            if current_state is not None and current_label in current_state.values:
                selected = copy.deepcopy(current_state.values[current_label])
            else:
                selected = copy.deepcopy(config.get("microphone"))
        try:
            state = build_microphone_choice_state(
                selected,
                sd.query_devices(),
                sd.query_hostapis(),
                translator,
                show_all=bool(microphone_show_all_var.get()),
                default_input_index=default_input_device_index(),
            )
        except Exception:
            logging.warning(
                "Nie udało się bezpiecznie odświeżyć listy mikrofonów"
            )
            state = build_unavailable_microphone_choice_state(
                selected,
                translator,
            )
        microphone_choice_state["current"] = state
        microphone_values.clear()
        microphone_values.update(state.values)
        microphone_combo["values"] = list(microphone_values.keys())
        microphone_var.set(state.selected_label)

    ttk.Button(
        microphone_row,
        text=t("Odśwież", "Refresh"),
        command=lambda: refresh_microphones(),
    ).grid(row=0, column=1, padx=(px(8), 0))

    level_row = ttk.Frame(microphone_row)
    level_row.columnconfigure(1, weight=1)
    level_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(6), 0))
    level_bar = ttk.Progressbar(level_row, mode="determinate", maximum=100)
    level_bar.grid(row=0, column=1, sticky="ew", padx=(px(8), px(8)))
    level_hint = ttk.Label(level_row, style="Muted.TLabel", text="")
    level_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(px(4), 0))

    def stop_level_preview(hint: str = "") -> None:
        microphone_level_monitor.stop()
        level_bar["value"] = 0
        level_hint.configure(text=hint)
        level_button.configure(text=t("Sprawdź", "Test"))

    def poll_level() -> None:
        if not microphone_level_monitor.active:
            return
        level_bar["value"] = microphone_level_monitor.level_percent()
        root.after(80, poll_level)

    def start_level_preview() -> None:
        label = microphone_var.get()
        selected = microphone_values.get(label)
        try:
            device = (
                None
                if selected is None
                else audio_devices.resolve_microphone_device(
                    selected,
                    sd.query_devices(),
                    sd.query_hostapis(),
                )
            )
            microphone_level_monitor.start(device)
        except Exception:
            logging.warning("Nie udało się otworzyć podglądu mikrofonu")
            stop_level_preview(
                t(
                    "Nie udało się otworzyć tego wejścia — może być zajęte "
                    "przez inny program.",
                    "Could not open this input — another program may be "
                    "using it.",
                )
            )
            return
        level_button.configure(text=t("Zatrzymaj", "Stop"))
        level_hint.configure(
            text=t(
                "Powiedz coś — pasek pokazuje, co słyszy to wejście.",
                "Say something — the bar shows what this input hears.",
            )
        )
        poll_level()

    def toggle_level_preview() -> None:
        if microphone_level_monitor.active:
            stop_level_preview()
        else:
            start_level_preview()

    level_button = ttk.Button(
        level_row,
        text=t("Sprawdź", "Test"),
        command=toggle_level_preview,
    )
    level_button.grid(row=0, column=0, sticky="w")

    def on_microphone_selected(event=None) -> None:
        del event
        if microphone_level_monitor.active:
            # Podgląd zawsze dotyczy wejścia widocznego w polu wyboru.
            start_level_preview()

    microphone_combo.bind("<<ComboboxSelected>>", on_microphone_selected, add="+")

    def toggle_microphone_variants() -> None:
        stop_level_preview()
        refresh_microphones()

    ttk.Checkbutton(
        microphone_row,
        text=t(
            "Pokaż wszystkie warianty (sterowniki i częstotliwości)",
            "Show every variant (drivers and sample rates)",
        ),
        variable=microphone_show_all_var,
        command=toggle_microphone_variants,
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(px(6), 0))

    add_field(dictation_basics, 1, t("Mikrofon", "Microphone"), microphone_row)
    microphone_row.grid_configure(columnspan=2)
    refresh_microphones(config.get("microphone"))

    language_combo = ttk.Combobox(
        dictation_basics,
        textvariable=language_var,
        values=list(language_values.keys()),
        state="readonly",
    )
    add_field(
        dictation_basics,
        2,
        t("Język dyktowania", "Dictation language"),
        language_combo,
    )
    try:
        custom_cpu_threads = int(cpu_threads_var.get()) != 0
    except ValueError:
        custom_cpu_threads = True
    dictation_disclosure = create_disclosure(
        general_tab,
        2,
        general_canvas,
        t(
            "Konkretny model, wybór GPU/CPU, dokładność i liczba wątków.",
            "Exact model, GPU/CPU selection, accuracy, and CPU thread count.",
        ),
        name="advanced_dictation",
        initially_expanded=(
            selected_profile_key() is None or custom_cpu_threads
        ),
    )
    dictation_advanced = dictation_disclosure["body"]

    model_combo = ttk.Combobox(
        dictation_advanced,
        textvariable=model_var,
        values=list(model_values.keys()),
        state="readonly",
    )
    add_field(
        dictation_advanced,
        0,
        t("Model mowy", "Speech model"),
        model_combo,
        t(
            "Nowy model może zostać pobrany po zapisaniu ustawień.",
            "A new model may be downloaded after the settings are saved.",
        ),
    )
    device_combo = ttk.Combobox(
        dictation_advanced,
        textvariable=device_var,
        values=list(device_values.keys()),
        state="readonly",
    )
    add_field(
        dictation_advanced,
        1,
        t("Miejsce przetwarzania", "Processing device"),
        device_combo,
        t(
            "Automatyczny wybór jest najlepszy dla większości komputerów.",
            "Automatic selection is best for most computers.",
        ),
    )
    beam_spin = ttk.Spinbox(
        dictation_advanced, from_=1, to=10, textvariable=beam_size_var, width=8
    )
    add_field(
        dictation_advanced,
        2,
        t("Dokładność rozpoznawania", "Recognition accuracy"),
        beam_spin,
        t(
            "1 = najszybciej; 5 = dokładniej, ale wolniej.",
            "1 = fastest; 5 = more accurate, but slower.",
        ),
    )
    threads_spin = ttk.Spinbox(
        dictation_advanced, from_=0, to=256, textvariable=cpu_threads_var, width=8
    )
    add_field(
        dictation_advanced,
        3,
        t("Liczba wątków CPU", "CPU thread count"),
        threads_spin,
        t("0 oznacza automatyczny dobór.", "0 selects automatically."),
    )

    speech_detection_frame = ttk.LabelFrame(
        audio_tab,
        text=t("Automatyczne wykrywanie mowy", "Automatic speech detection"),
        padding=px(16),
    )
    speech_detection_frame.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    ttk.Checkbutton(
        speech_detection_frame,
        text=t(
            "Pomijaj ciszę i nagrania bez wyraźnej mowy",
            "Skip silence and recordings without clear speech",
        ),
        variable=vad_enabled_var,
    ).grid(row=0, column=0, sticky="w", pady=(0, px(4)))
    ttk.Label(
        speech_detection_frame,
        text=t(
            "Zalecane dla większości osób. Wyłącz tylko wtedy, gdy Mówik pomija bardzo cichy głos.",
            "Recommended for most people. Turn it off only if Mówik skips very quiet speech.",
        ),
        style="Muted.TLabel",
        wraplength=px(720),
    ).grid(row=1, column=0, sticky="w")

    default_vad = DEFAULT_CONFIG["vad"]
    audio_has_custom_values = any(
        (
            str(pre_roll_var.get()) != str(DEFAULT_CONFIG["pre_roll_ms"]),
            str(post_roll_var.get()) != str(DEFAULT_CONFIG["post_roll_ms"]),
            str(minimum_recording_var.get())
            != str(DEFAULT_CONFIG["minimum_recording_ms"]),
            str(maximum_recording_var.get())
            != str(DEFAULT_CONFIG["maximum_recording_seconds"]),
            str(minimum_rms_var.get()) != str(DEFAULT_CONFIG["minimum_rms"]),
            str(vad_threshold_var.get()) != str(default_vad["threshold"]),
            str(vad_min_speech_var.get())
            != str(default_vad["min_speech_duration_ms"]),
            str(vad_min_silence_var.get())
            != str(default_vad["min_silence_duration_ms"]),
            str(vad_speech_pad_var.get()) != str(default_vad["speech_pad_ms"]),
        )
    )
    audio_disclosure = create_disclosure(
        audio_tab,
        1,
        audio_canvas,
        t(
            "Bufory początku i końca, czułość oraz precyzyjne parametry ciszy. Zmień je tylko, jeśli słowa są ucinane albo szum uruchamia rozpoznawanie.",
            "Start/end buffers, sensitivity, and detailed silence controls. Change these only if words are clipped or noise triggers recognition.",
        ),
        name="advanced_microphone",
        initially_expanded=audio_has_custom_values,
    )
    audio_advanced = audio_disclosure["body"]

    capture_frame = ttk.LabelFrame(
        audio_advanced,
        text=t("Bufor i czułość mikrofonu", "Microphone buffer and sensitivity"),
        padding=px(16),
    )
    capture_frame.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    capture_frame.columnconfigure(1, weight=1)
    pre_roll_spin = ttk.Spinbox(
        capture_frame,
        from_=0,
        to=2000,
        textvariable=pre_roll_var,
    )
    add_field(
        capture_frame,
        0,
        t("Bufor przed naciśnięciem (ms)", "Pre-roll buffer (ms)"),
        pre_roll_spin,
        t("Chroni początek pierwszego słowa.", "Protects the first word's start."),
    )
    post_roll_spin = ttk.Spinbox(
        capture_frame,
        from_=0,
        to=2000,
        textvariable=post_roll_var,
    )
    add_field(
        capture_frame,
        1,
        t("Bufor po puszczeniu (ms)", "Post-release buffer (ms)"),
        post_roll_spin,
        t("Chroni końcówkę ostatniego słowa.", "Protects the last word's ending."),
    )
    minimum_recording_spin = ttk.Spinbox(
        capture_frame,
        from_=0,
        to=10000,
        textvariable=minimum_recording_var,
    )
    add_field(
        capture_frame,
        2,
        t("Minimalne nagranie (ms)", "Minimum recording (ms)"),
        minimum_recording_spin,
    )
    maximum_recording_spin = ttk.Spinbox(
        capture_frame,
        from_=1,
        to=MAXIMUM_RECORDING_SECONDS,
        textvariable=maximum_recording_var,
    )
    add_field(
        capture_frame,
        3,
        t(
            "Maksymalne nagranie (s)",
            "Maximum recording (s)",
        ),
        maximum_recording_spin,
        t(
            "Nagranie zostanie zakończone po tym czasie. Przy wysokiej częstotliwości "
            "mikrofonu bezpieczny limit pamięci może go skrócić; efektywny limit "
            "pojawi się wtedy w statusie.",
            "Recording stops after this time. At high microphone sample rates, the "
            "safe memory cap may shorten it; the effective limit then appears in "
            "the status.",
        ),
    )
    minimum_rms_entry = ttk.Entry(
        capture_frame,
        textvariable=minimum_rms_var,
    )
    add_field(
        capture_frame,
        4,
        t("Minimalny poziom dźwięku", "Minimum audio level"),
        minimum_rms_entry,
        t(
            "Niższa wartość zwiększa czułość na cichy głos.",
            "A lower value increases sensitivity to quiet speech.",
        ),
    )

    vad_frame = ttk.LabelFrame(
        audio_advanced,
        text=t(
            "Precyzyjne wykrywanie ciszy",
            "Fine-tune silence detection",
        ),
        padding=px(16),
    )
    vad_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    vad_frame.columnconfigure(1, weight=1)
    vad_controls: list[ttk.Widget] = []
    vad_threshold_entry = ttk.Entry(vad_frame, textvariable=vad_threshold_var)
    vad_controls.append(vad_threshold_entry)
    add_field(
        vad_frame,
        0,
        t("Czułość wykrywania", "Detection sensitivity"),
        vad_threshold_entry,
        t(
            "Zakres 0–1; wyższa wartość mocniej odrzuca szum.",
            "Range 0–1; a higher value rejects more noise.",
        ),
    )
    vad_speech_spin = ttk.Spinbox(
        vad_frame, from_=0, to=10000, textvariable=vad_min_speech_var
    )
    vad_controls.append(vad_speech_spin)
    add_field(
        vad_frame,
        1,
        t("Minimalna długość mowy (ms)", "Minimum speech length (ms)"),
        vad_speech_spin,
    )
    vad_silence_spin = ttk.Spinbox(
        vad_frame, from_=0, to=10000, textvariable=vad_min_silence_var
    )
    vad_controls.append(vad_silence_spin)
    add_field(
        vad_frame,
        2,
        t("Minimalna długość ciszy (ms)", "Minimum silence length (ms)"),
        vad_silence_spin,
    )
    vad_pad_spin = ttk.Spinbox(
        vad_frame, from_=0, to=3000, textvariable=vad_speech_pad_var
    )
    vad_controls.append(vad_pad_spin)
    add_field(
        vad_frame,
        3,
        t("Margines mowy (ms)", "Speech padding (ms)"),
        vad_pad_spin,
    )

    def sync_vad_controls(*args) -> None:
        state = "normal" if vad_enabled_var.get() else "disabled"
        for control in vad_controls:
            control.configure(state=state)

    vad_enabled_var.trace_add("write", sync_vad_controls)
    sync_vad_controls()

    text_frame = ttk.LabelFrame(
        text_tab,
        text=t("Miejsce docelowe i format", "Destination and formatting"),
        padding=px(16),
    )
    text_frame.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    text_frame.columnconfigure(1, weight=1)
    paste_enabled_check = ttk.Checkbutton(
        text_frame,
        text=t(
            "Automatycznie wklejaj tekst do aktywnego okna",
            "Automatically paste text into the active window",
        ),
        variable=paste_enabled_var,
    )
    paste_enabled_check.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="w",
        pady=px(4),
    )
    copy_to_clipboard_check = ttk.Checkbutton(
        text_frame,
        text=t(
            "Kopiuj rozpoznany tekst również do schowka",
            "Also copy recognized text to the clipboard",
        ),
        variable=copy_to_clipboard_var,
    )
    copy_to_clipboard_check.grid(
        row=1,
        column=0,
        columnspan=3,
        sticky="w",
        pady=px(4),
    )
    append_space_check = ttk.Checkbutton(
        text_frame,
        text=t(
            "Dodawaj spację po wklejonym zdaniu",
            "Add a space after the pasted sentence",
        ),
        variable=append_space_var,
    )
    append_space_check.grid(
        row=2,
        column=0,
        columnspan=3,
        sticky="w",
        pady=px(4),
    )
    voice_commands_text_var = tk.StringVar()

    def refresh_voice_commands_text(*args) -> None:
        transcription_language = str(
            language_values.get(language_var.get(), language_var.get())
        )
        if transcription_language == "pl":
            voice_commands_text_var.set(
                t(
                    "Rozpoznawaj komendy „nowa linia” i „nowy akapit”",
                    "Recognize the “nowa linia” and “nowy akapit” commands",
                )
            )
        elif transcription_language == "en":
            voice_commands_text_var.set(
                t(
                    "Rozpoznawaj komendy „new line” i „new paragraph”",
                    "Recognize the “new line” and “new paragraph” commands",
                )
            )
        else:
            voice_commands_text_var.set(
                t(
                    "Rozpoznawaj komendy akapitu po polsku i angielsku",
                    "Recognize paragraph commands in English and Polish",
                )
            )

    ttk.Checkbutton(
        text_frame,
        textvariable=voice_commands_text_var,
        variable=voice_commands_var,
    ).grid(row=3, column=0, columnspan=3, sticky="w", pady=px(4))
    language_var.trace_add("write", refresh_voice_commands_text)
    refresh_voice_commands_text()
    ttk.Label(
        text_frame,
        text=t(
            "Gdy schowek jest włączony, pozostaje w nim dokładna transkrypcja "
            "bez automatycznie dodanej spacji.",
            "When clipboard copying is enabled, it keeps the exact transcript "
            "without the automatically appended space.",
        ),
        style="Muted.TLabel",
        wraplength=px(760),
    ).grid(
        row=4,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(px(8), 0),
    )

    dictionary_frame = ttk.LabelFrame(
        text_tab,
        text=t(
            "Prywatny słownik nazw i terminów",
            "Private dictionary of names and terms",
        ),
        padding=px(16),
    )
    dictionary_frame.grid(
        row=1,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    dictionary_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        dictionary_frame,
        text=t(
            "Podpowiadaj modelowi zapis własnych nazw, marek i skrótów",
            "Suggest preferred spellings of names, brands, and abbreviations",
        ),
        variable=dictionary_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=px(4))

    def open_path(path: Path) -> bool:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            messagebox.showerror(
                APP_DISPLAY_NAME,
                t(
                    "Nie udało się otworzyć:\n{path}\n\n{error}",
                    "Could not open:\n{path}\n\n{error}",
                    path=path,
                    error=exc,
                ),
                parent=root,
            )
            return False

    dictionary_edit_button = ttk.Button(
        dictionary_frame,
        text=t("Edytuj słownik…", "Edit dictionary…"),
        command=lambda: open_path(DICTIONARY_PATH),
    )
    dictionary_edit_button.grid(
        row=1,
        column=0,
        columnspan=2,
        sticky="w",
        pady=(px(8), px(2)),
    )

    text_disclosure = create_disclosure(
        text_tab,
        2,
        text_canvas,
        t(
            "Opóźnienie wklejania i limit słownika. Domyślne wartości działają w większości aplikacji.",
            "Paste delay and dictionary limit. The defaults work in most applications.",
        ),
        name="advanced_text",
        initially_expanded=(
            str(paste_delay_var.get()) != str(DEFAULT_CONFIG["paste"]["delay_ms"])
            or str(dictionary_max_terms_var.get())
            != str(DEFAULT_CONFIG["dictionary"]["max_terms"])
        ),
    )
    text_advanced = text_disclosure["body"]
    paste_delay_spin = ttk.Spinbox(
        text_advanced, from_=0, to=5000, textvariable=paste_delay_var
    )
    add_field(
        text_advanced,
        0,
        t("Opóźnienie wklejania (ms)", "Paste delay (ms)"),
        paste_delay_spin,
        t(
            "Zwiększ tylko wtedy, gdy aplikacja docelowa pomija tekst.",
            "Increase only if the target application misses the pasted text.",
        ),
    )
    dictionary_limit_spin = ttk.Spinbox(
        text_advanced,
        from_=0,
        to=5000,
        textvariable=dictionary_max_terms_var,
    )
    add_field(
        text_advanced,
        1,
        t("Maksymalna liczba haseł", "Maximum number of entries"),
        dictionary_limit_spin,
    )

    def sync_paste_controls(*args) -> None:
        state = "normal" if paste_enabled_var.get() else "disabled"
        append_space_check.configure(state=state)
        paste_delay_spin.configure(state=state)

    paste_enabled_var.trace_add("write", sync_paste_controls)
    sync_paste_controls()

    def sync_dictionary_controls(*args) -> None:
        state = "normal" if dictionary_enabled_var.get() else "disabled"
        dictionary_limit_spin.configure(state=state)
        dictionary_edit_button.configure(state=state)

    dictionary_enabled_var.trace_add("write", sync_dictionary_controls)
    sync_dictionary_controls()

    commands_activation = ttk.LabelFrame(
        commands_tab,
        text=t("Tryb komend", "Command mode"),
        padding=px(16),
    )
    commands_activation.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    commands_activation.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        commands_activation,
        text=t(
            "Włącz własne komendy głosowe",
            "Enable custom voice commands",
        ),
        variable=custom_commands_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, px(10)))

    custom_trigger_row = ttk.Frame(commands_activation)
    custom_trigger_row.columnconfigure(0, weight=1)
    custom_trigger_combo = ttk.Combobox(
        custom_trigger_row,
        textvariable=custom_commands_trigger_var,
        values=list(trigger_values.keys()),
        state="readonly",
    )
    custom_trigger_combo.grid(row=0, column=0, sticky="ew")
    custom_trigger_detect_button = ttk.Button(
        custom_trigger_row,
        text=t("Wykryj…", "Detect…"),
        command=lambda: capture_trigger(
            custom_commands_trigger_var,
            custom_trigger_combo,
        ),
    )
    custom_trigger_detect_button.grid(row=0, column=1, padx=(px(8), 0))
    add_field(
        commands_activation,
        1,
        t("Przycisk komend", "Command shortcut"),
        custom_trigger_row,
        t(
            "Domyślnie F7. Musi być inny niż przycisk zwykłego dyktowania.",
            "F7 by default. It must differ from the regular dictation shortcut.",
        ),
    )
    ttk.Label(
        commands_activation,
        text=t(
            "Przytrzymaj skrót, wypowiedz tylko pełną frazę komendy i puść. "
            "Jeśli Mówik nie znajdzie dokładnego dopasowania, nie wykona żadnej akcji.",
            "Hold the shortcut, say only the full command phrase, and release it. "
            "If Mówik finds no exact match, it performs no action.",
        ),
        style="Muted.TLabel",
        wraplength=px(760),
    ).grid(
        row=2,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(px(10), 0),
    )

    commands_frame = ttk.LabelFrame(
        commands_tab,
        text=t("Twoje komendy", "Your commands"),
        padding=px(16),
    )
    commands_frame.grid(
        row=1,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    commands_frame.columnconfigure(0, weight=1)
    commands_toolbar = ttk.Frame(commands_frame)
    commands_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, px(12)))
    commands_toolbar.columnconfigure(0, weight=1)
    custom_command_count_var = tk.StringVar()
    ttk.Label(
        commands_toolbar,
        textvariable=custom_command_count_var,
        style="Muted.TLabel",
    ).grid(row=0, column=0, sticky="w")

    action_labels = {
        "paste_text": t("Wklej tekst", "Insert text"),
        "open": t(
            "Otwórz program, plik lub stronę",
            "Open an app, file, or website",
        ),
        "open_terminal": t(
            "Otwórz terminal",
            "Open terminal",
        ),
    }
    action_values = {label: action for action, label in action_labels.items()}
    command_cards = ttk.Frame(commands_frame)
    command_cards.grid(row=1, column=0, sticky="ew")
    command_cards.columnconfigure(0, weight=1)

    def update_custom_commands_revision() -> None:
        custom_commands_revision_var.set(
            json.dumps(
                {
                    "items": custom_command_items,
                    "unmanaged": unmanaged_custom_command_items,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def custom_command_items_for_save() -> list[Any]:
        result: list[Any] = []
        for item in custom_command_items:
            normalized = normalize_custom_command_phrase(item["phrase"])
            command_id = item.get("id")
            original = (
                original_custom_command_items_by_key.get(f"id:{command_id}")
                if isinstance(command_id, str) and command_id
                else None
            )
            if original is None:
                original = original_custom_command_items_by_key.get(normalized)
            merged = copy.deepcopy(original) if original is not None else {}
            # Migracja dawnego pola `text` bez pozostawiania dwóch źródeł
            # oraz usunięcie znanych pól poprzedniego typu akcji.
            merged.pop("text", None)
            if item.get("action") != "open_terminal":
                merged.pop("options", None)
            merged.update(item)
            result.append(merged)
        result.extend(copy.deepcopy(unmanaged_custom_command_items))
        return result

    def unmanaged_custom_command_phrase_keys() -> set[str]:
        keys: set[str] = set()
        for raw_item in unmanaged_custom_command_items:
            if not isinstance(raw_item, dict):
                continue
            phrase = raw_item.get("phrase")
            if not isinstance(phrase, str) or "\x00" in phrase:
                continue
            normalized = normalize_custom_command_phrase(phrase)
            if normalized:
                keys.add(normalized)
        return keys

    def open_custom_command_editor(index: Optional[int] = None) -> None:
        editing = index is not None
        existing = (
            custom_command_items[index]
            if index is not None
            else {
                "id": f"cc_{uuid.uuid4().hex}",
                "phrase": "",
                "match": "exact",
                "action": "paste_text",
                "value": "",
                "confirm": False,
            }
        )
        dialog = tk.Toplevel(root)
        dialog.title(
            t(
                "{app} — edytuj komendę" if editing else "{app} — nowa komenda",
                "{app} — Edit command" if editing else "{app} — New command",
                app=APP_DISPLAY_NAME,
            )
        )
        dialog.transient(root)
        dialog.grab_set()
        dialog.resizable(True, True)
        dialog.minsize(px(600), px(540))
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        apply_dark_window_chrome(dialog)

        editor = ttk.Frame(dialog, padding=px(22))
        editor.grid(row=0, column=0, sticky="nsew")
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(7, weight=1)
        ttk.Label(
            editor,
            text=t(
                "Edytuj własną komendę" if editing else "Dodaj własną komendę",
                "Edit custom command" if editing else "Add a custom command",
            ),
            font=(display_font_family, 15, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            editor,
            text=t(
                "Komenda zadziała tylko po przytrzymaniu osobnego skrótu.",
                "The command works only while holding the separate shortcut.",
            ),
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(px(4), px(16)))

        ttk.Label(
            editor,
            text=t("Wypowiadana fraza", "Spoken phrase"),
            font=(ui_font_family, 10, "bold"),
        ).grid(row=2, column=0, sticky="w")
        phrase_var = tk.StringVar(value=str(existing.get("phrase", "")))
        phrase_entry = ttk.Entry(editor, textvariable=phrase_var)
        phrase_entry.grid(row=3, column=0, sticky="ew", pady=(px(6), px(14)))

        ttk.Label(
            editor,
            text=t("Co ma zrobić Mówik", "What Mówik should do"),
            font=(ui_font_family, 10, "bold"),
        ).grid(row=4, column=0, sticky="w")
        selected_action = str(existing.get("action", "paste_text"))
        action_var = tk.StringVar(
            value=action_labels.get(selected_action, action_labels["paste_text"])
        )
        action_combo = ttk.Combobox(
            editor,
            textvariable=action_var,
            values=list(action_values.keys()),
            state="readonly",
        )
        action_combo.grid(row=5, column=0, sticky="ew", pady=(px(6), px(14)))

        existing_options = existing.get("options", {})
        if not isinstance(existing_options, dict):
            existing_options = {}
        cwd_source_labels = {
            "active_explorer": t(
                "Folder aktywnego Eksploratora",
                "Active File Explorer folder",
            ),
            "fixed": t("Wybrany folder", "Selected folder"),
            "home": t("Folder domowy", "Home folder"),
        }
        terminal_host_labels = {
            "auto": t("Automatycznie", "Automatic"),
            "windows_terminal": "Windows Terminal",
            "classic": t("Klasyczna konsola", "Classic console"),
        }
        terminal_shell_labels = {
            "default": t("Domyślna powłoka", "Default shell"),
            "powershell": "PowerShell",
            "cmd": "Command Prompt",
        }
        cwd_source_values = {
            label: value for value, label in cwd_source_labels.items()
        }
        terminal_host_values = {
            label: value for value, label in terminal_host_labels.items()
        }
        terminal_shell_values = {
            label: value for value, label in terminal_shell_labels.items()
        }
        cwd_source_var = tk.StringVar(
            value=cwd_source_labels.get(
                str(existing_options.get("cwd_source", "active_explorer")),
                cwd_source_labels["active_explorer"],
            )
        )
        terminal_host_var = tk.StringVar(
            value=terminal_host_labels.get(
                str(existing_options.get("host", "auto")),
                terminal_host_labels["auto"],
            )
        )
        terminal_shell_var = tk.StringVar(
            value=terminal_shell_labels.get(
                str(existing_options.get("shell", "default")),
                terminal_shell_labels["default"],
            )
        )
        terminal_fixed_cwd_var = tk.StringVar(
            value=str(existing_options.get("fixed_cwd", ""))
        )
        terminal_spoken_tail_var = tk.BooleanVar(
            value=str(existing.get("match", "exact")) == "prefix_tail"
        )

        terminal_section = ttk.LabelFrame(
            editor,
            text=t("Ustawienia terminala", "Terminal settings"),
            padding=px(12),
        )
        terminal_section.grid(row=6, column=0, sticky="ew", pady=(0, px(14)))
        for terminal_column in range(3):
            terminal_section.columnconfigure(terminal_column, weight=1)

        for column, (label, variable, values) in enumerate(
            (
                (
                    t("Folder startowy", "Starting folder"),
                    cwd_source_var,
                    tuple(cwd_source_values),
                ),
                (
                    t("Aplikacja terminala", "Terminal app"),
                    terminal_host_var,
                    tuple(terminal_host_values),
                ),
                (
                    t("Powłoka", "Shell"),
                    terminal_shell_var,
                    tuple(terminal_shell_values),
                ),
            )
        ):
            ttk.Label(
                terminal_section,
                text=label,
                font=(ui_font_family, 9, "bold"),
            ).grid(row=0, column=column, sticky="w", padx=(0, px(8)))
            ttk.Combobox(
                terminal_section,
                textvariable=variable,
                values=values,
                state="readonly",
            ).grid(row=1, column=column, sticky="ew", padx=(0, px(8)))

        fixed_folder_row = ttk.Frame(terminal_section)
        fixed_folder_row.grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(px(10), 0),
        )
        fixed_folder_row.columnconfigure(0, weight=1)
        terminal_fixed_cwd_entry = ttk.Entry(
            fixed_folder_row,
            textvariable=terminal_fixed_cwd_var,
        )
        terminal_fixed_cwd_entry.grid(row=0, column=0, sticky="ew")

        def browse_terminal_folder() -> None:
            selected = filedialog.askdirectory(
                parent=dialog,
                title=t(
                    "Wybierz folder startowy terminala",
                    "Choose the terminal starting folder",
                ),
                initialdir=(
                    terminal_fixed_cwd_var.get().strip() or str(Path.home())
                ),
            )
            if selected:
                terminal_fixed_cwd_var.set(selected)

        terminal_fixed_cwd_button = ttk.Button(
            fixed_folder_row,
            text=t("Wybierz…", "Browse…"),
            command=browse_terminal_folder,
        )
        terminal_fixed_cwd_button.grid(
            row=0,
            column=1,
            padx=(px(8), 0),
        )

        terminal_spoken_tail_check = ttk.Checkbutton(
            terminal_section,
            text=t(
                "Resztę wypowiedzi przygotuj jako szkic polecenia",
                "Use the rest of the utterance as a command draft",
            ),
            variable=terminal_spoken_tail_var,
        )
        terminal_spoken_tail_check.grid(
            row=3,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(px(10), 0),
        )
        ttk.Label(
            terminal_section,
            text=t(
                "Szkic zostanie skopiowany do schowka. Wklejasz go sam przez Ctrl+V "
                "i sam naciskasz Enter — Mówik nigdy nie uruchamia polecenia.",
                "The draft is copied to the clipboard. You paste it with Ctrl+V "
                "and press Enter yourself — Mówik never runs the command.",
            ),
            style="Muted.TLabel",
            wraplength=px(680),
        ).grid(
            row=4,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(px(7), 0),
        )

        value_section = ttk.Frame(editor)
        value_section.grid(row=7, column=0, sticky="nsew")
        value_section.columnconfigure(0, weight=1)
        value_section.rowconfigure(2, weight=1)
        value_label_var = tk.StringVar()
        value_hint_var = tk.StringVar()
        ttk.Label(
            value_section,
            textvariable=value_label_var,
            font=(ui_font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            value_section,
            textvariable=value_hint_var,
            style="Muted.TLabel",
            wraplength=px(700),
        ).grid(row=1, column=0, sticky="ew", pady=(px(4), px(6)))
        value_text = tk.Text(
            value_section,
            height=8,
            wrap="word",
            undo=True,
            font=(ui_font_family, 10),
            background=colors["control"],
            foreground=colors["text"],
            insertbackground=colors["text"],
            selectbackground=colors["selection"],
            selectforeground=colors["white"],
            inactiveselectbackground=colors["primary_border"],
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=colors["control_border"],
            highlightcolor=colors["primary"],
            padx=px(10),
            pady=px(9),
        )
        value_text.grid(row=2, column=0, sticky="nsew")
        value_text.insert("1.0", str(existing.get("value", "")))

        confirm_var = tk.BooleanVar(value=bool(existing.get("confirm", False)))
        confirm_check = ttk.Checkbutton(
            editor,
            text=t(
                "Pytaj o potwierdzenie przed wykonaniem akcji",
                "Ask for confirmation before running the action",
            ),
            variable=confirm_var,
        )
        confirm_check.grid(row=8, column=0, sticky="w", pady=(px(12), 0))
        editor_error_var = tk.StringVar()
        tk.Label(
            editor,
            textvariable=editor_error_var,
            background=colors["surface"],
            foreground=colors["danger"],
            font=(ui_font_family, 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=px(700),
        ).grid(row=9, column=0, sticky="ew", pady=(px(8), 0))

        action_state = {"last": selected_action}

        def refresh_terminal_controls(*args) -> None:
            source = cwd_source_values.get(
                cwd_source_var.get(),
                "active_explorer",
            )
            fixed_state = "normal" if source == "fixed" else "disabled"
            terminal_fixed_cwd_entry.configure(state=fixed_state)
            terminal_fixed_cwd_button.configure(state=fixed_state)

        def refresh_action_fields(*args) -> None:
            action = action_values.get(action_var.get(), "paste_text")
            if action == "open_terminal":
                value_section.grid_remove()
                confirm_check.grid_remove()
                terminal_section.grid()
                confirm_var.set(False)
                refresh_terminal_controls()
                action_state["last"] = action
                return

            terminal_section.grid_remove()
            value_section.grid()
            confirm_check.grid()
            if action == "paste_text":
                value_label_var.set(t("Tekst do wklejenia", "Text to insert"))
                value_hint_var.set(
                    t(
                        "Tekst zostanie wklejony dokładnie. Wielowierszowy tekst "
                        "zawsze wymaga potwierdzenia.",
                        "Text is inserted exactly. Multi-line text always "
                        "requires confirmation.",
                    )
                )
                confirm_var.set(False)
                confirm_check.configure(state="disabled")
            elif action == "open":
                value_label_var.set(
                    t("Program, ścieżka lub adres URL", "App, path, or URL")
                )
                value_hint_var.set(
                    t(
                        "Wybierz istniejącą lokalną ścieżkę bezwzględną albo adres HTTPS. "
                        "Skrypty, skróty i ścieżki sieciowe są blokowane.",
                        "Choose an existing absolute local path or an HTTPS URL. "
                        "Scripts, shortcuts, and network paths are blocked.",
                    )
                )
                confirm_var.set(True)
                confirm_check.configure(state="disabled")
            action_state["last"] = action

        action_var.trace_add("write", refresh_action_fields)
        cwd_source_var.trace_add("write", refresh_terminal_controls)
        terminal_spoken_tail_var.trace_add("write", refresh_terminal_controls)
        refresh_action_fields()

        buttons = ttk.Frame(editor)
        buttons.grid(row=10, column=0, sticky="e", pady=(px(18), 0))

        def close_editor() -> None:
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        def save_command() -> None:
            phrase = phrase_var.get().strip()
            value = value_text.get("1.0", "end-1c")
            action = action_values.get(action_var.get(), "paste_text")
            requested_match_mode = (
                command_engine.MATCH_PREFIX_TAIL
                if action == command_engine.ACTION_OPEN_TERMINAL
                and terminal_spoken_tail_var.get()
                else command_engine.MATCH_EXACT
            )
            if not normalize_custom_command_phrase(phrase):
                editor_error_var.set(
                    t(
                        "Wpisz frazę zawierającą litery, cyfry lub symbole.",
                        "Enter a phrase containing letters, numbers, or symbols.",
                    )
                )
                phrase_entry.focus_set()
                return
            if "\x00" in phrase or len(phrase) > MAX_CUSTOM_COMMAND_PHRASE_LENGTH:
                editor_error_var.set(
                    t(
                        "Fraza może mieć maksymalnie {limit} znaków.",
                        "The phrase can contain at most {limit} characters.",
                        limit=MAX_CUSTOM_COMMAND_PHRASE_LENGTH,
                    )
                )
                phrase_entry.focus_set()
                return
            normalized_phrase = normalize_custom_command_phrase(phrase)
            for other_index, other in enumerate(custom_command_items):
                if other_index == index:
                    continue
                if (
                    normalize_custom_command_phrase(other["phrase"])
                    == normalized_phrase
                    and str(other.get("match", command_engine.MATCH_EXACT))
                    == requested_match_mode
                ):
                    editor_error_var.set(
                        t(
                            "Istnieje już komenda z taką samą frazą.",
                            "A command with the same phrase already exists.",
                        )
                    )
                    phrase_entry.focus_set()
                    return
            if normalized_phrase in unmanaged_custom_command_phrase_keys():
                editor_error_var.set(
                    t(
                        "Ta fraza występuje w ukrytym, nieprawidłowym wpisie config.json. "
                        "Najpierw popraw lub usuń ten wpis ręcznie.",
                        "This phrase appears in a hidden invalid config.json entry. "
                        "Correct or remove that entry first.",
                    )
                )
                phrase_entry.focus_set()
                return
            match_mode = requested_match_mode
            options: Optional[dict[str, Any]] = None
            if action == "open_terminal":
                value = ""
                cwd_source = cwd_source_values.get(
                    cwd_source_var.get(),
                    "active_explorer",
                )
                fixed_cwd: Optional[str] = None
                if cwd_source == "fixed":
                    raw_fixed_cwd = terminal_fixed_cwd_var.get().strip()
                    if not raw_fixed_cwd:
                        editor_error_var.set(
                            t(
                                "Wybierz istniejący folder startowy terminala.",
                                "Choose an existing terminal starting folder.",
                            )
                        )
                        terminal_fixed_cwd_entry.focus_set()
                        return
                    resolved_directory = windows_actions.resolve_working_directory(
                        "fixed",
                        raw_fixed_cwd,
                    )
                    if not resolved_directory.ok or resolved_directory.path is None:
                        editor_error_var.set(
                            t(
                                "Folder startowy musi być istniejącym lokalnym katalogiem.",
                                "The starting folder must be an existing local directory.",
                            )
                        )
                        terminal_fixed_cwd_entry.focus_set()
                        return
                    fixed_cwd = str(resolved_directory.path)
                options = {
                    "cwd_source": cwd_source,
                    "host": terminal_host_values.get(
                        terminal_host_var.get(),
                        "auto",
                    ),
                    "shell": terminal_shell_values.get(
                        terminal_shell_var.get(),
                        "default",
                    ),
                    "draft_delivery": "clipboard",
                }
                if fixed_cwd is not None:
                    options["fixed_cwd"] = fixed_cwd
            else:
                if not value.strip():
                    editor_error_var.set(
                        t(
                            "Wpisz tekst, ścieżkę albo adres do otwarcia.",
                            "Enter text, a path, or an address to open.",
                        )
                    )
                    value_text.focus_set()
                    return
                if "\x00" in value or len(value) > MAX_CUSTOM_COMMAND_VALUE_LENGTH:
                    editor_error_var.set(
                        t(
                            "Zawartość może mieć maksymalnie {limit} znaków.",
                            "The content can contain at most {limit} characters.",
                            limit=MAX_CUSTOM_COMMAND_VALUE_LENGTH,
                        )
                    )
                    value_text.focus_set()
                    return
                if action == "open" and any(mark in value for mark in "\r\n"):
                    editor_error_var.set(
                        t(
                            "Program, ścieżka lub adres URL muszą mieścić się w jednym wierszu.",
                            "The app, path, or URL must fit on one line.",
                        )
                    )
                    value_text.focus_set()
                    return
                if action == "open":
                    try:
                        value = resolve_custom_command_open_target(value)
                    except CustomOpenTargetError:
                        editor_error_var.set(
                            t(
                                "Wybierz istniejący lokalny plik lub folder albo poprawny "
                                "adres HTTPS. Skrypty, skróty i ścieżki sieciowe są niedozwolone.",
                                "Choose an existing local file or folder, or a valid HTTPS "
                                "URL. Scripts, shortcuts, and network paths are not allowed.",
                            )
                        )
                        value_text.focus_set()
                        return
            command: dict[str, Any] = {
                "id": str(existing.get("id") or f"cc_{uuid.uuid4().hex}"),
                "phrase": phrase,
                "match": match_mode,
                "action": action,
                "value": value,
                "confirm": action == "open",
            }
            if options is not None:
                command["options"] = options
            if not command_engine.CommandRegistry.from_items(
                [command]
            ).definitions:
                editor_error_var.set(
                    t(
                        "Ta konfiguracja komendy nie jest bezpieczna lub kompletna.",
                        "This command configuration is unsafe or incomplete.",
                    )
                )
                return
            if index is None:
                if len(custom_command_items) >= MAX_CUSTOM_COMMANDS:
                    editor_error_var.set(
                        t(
                            "Osiągnięto limit {limit} komend.",
                            "The limit of {limit} commands has been reached.",
                            limit=MAX_CUSTOM_COMMANDS,
                        )
                    )
                    return
                was_empty = not custom_command_items
                custom_command_items.append(command)
                if was_empty:
                    custom_commands_enabled_var.set(True)
            else:
                custom_command_items[index] = command
            update_custom_commands_revision()
            render_custom_command_cards()
            close_editor()

        ttk.Button(
            buttons,
            text=t("Anuluj", "Cancel"),
            command=close_editor,
        ).grid(row=0, column=0, padx=(0, px(8)))
        ttk.Button(
            buttons,
            text=t(
                "Zapisz zmiany" if editing else "Dodaj komendę",
                "Save changes" if editing else "Add command",
            ),
            style="Accent.TButton",
            command=save_command,
        ).grid(row=0, column=1)
        dialog.protocol("WM_DELETE_WINDOW", close_editor)
        dialog.bind("<Escape>", lambda event: close_editor())
        dialog.update_idletasks()
        width = min(px(760), max(px(600), root.winfo_width() - px(80)))
        height = min(px(760), max(px(580), root.winfo_height() - px(70)))
        x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        phrase_entry.focus_set()

    def remove_custom_command(index: int) -> None:
        if not messagebox.askyesno(
            t(
                "{app} — usuń komendę",
                "{app} — Delete command",
                app=APP_DISPLAY_NAME,
            ),
            t(
                "Usunąć wybraną komendę?",
                "Delete the selected command?",
            ),
            parent=root,
        ):
            return
        del custom_command_items[index]
        if not custom_command_items:
            custom_commands_enabled_var.set(False)
        update_custom_commands_revision()
        render_custom_command_cards()

    def render_custom_command_cards() -> None:
        for child in command_cards.winfo_children():
            child.destroy()
        count = len(custom_command_items)
        custom_command_count_var.set(
            t(
                "Zapisano: {count}",
                "Saved: {count}",
                count=localized_command_count(count),
            )
        )
        if not custom_command_items:
            empty = tk.Frame(
                command_cards,
                background=colors["surface_alt"],
                highlightbackground=colors["border"],
                highlightthickness=1,
                padx=px(18),
                pady=px(20),
            )
            empty.grid(row=0, column=0, sticky="ew")
            tk.Label(
                empty,
                text=t(
                    "Nie masz jeszcze własnych komend.",
                    "You do not have any custom commands yet.",
                ),
                background=colors["surface_alt"],
                foreground=colors["text"],
                font=(ui_font_family, 11, "bold"),
            ).grid(row=0, column=0, sticky="w")
            tk.Label(
                empty,
                text=t(
                    "Dodaj pierwszą frazę i wybierz, co Mówik ma zrobić.",
                    "Add your first phrase and choose what Mówik should do.",
                ),
                background=colors["surface_alt"],
                foreground=colors["muted"],
                font=(ui_font_family, 9),
            ).grid(row=1, column=0, sticky="w", pady=(px(5), 0))
            return

        for item_index, item in enumerate(custom_command_items):
            card = tk.Frame(
                command_cards,
                background=colors["surface_alt"],
                highlightbackground=colors["border"],
                highlightthickness=1,
                padx=px(14),
                pady=px(12),
            )
            card.grid(
                row=item_index,
                column=0,
                sticky="ew",
                pady=(0, px(8)),
            )
            card.columnconfigure(0, weight=1)
            tk.Label(
                card,
                text=f'“{item["phrase"]}”',
                background=colors["surface_alt"],
                foreground=colors["text"],
                font=(ui_font_family, 10, "bold"),
                anchor="w",
                justify="left",
                wraplength=px(560),
            ).grid(row=0, column=0, sticky="ew")
            badges = tk.Frame(card, background=colors["surface_alt"])
            badges.grid(row=0, column=1, sticky="e", padx=(px(12), 0))
            tk.Label(
                badges,
                text=action_labels.get(item["action"], item["action"]),
                background=colors["primary_soft"],
                foreground=colors["primary"],
                font=(ui_font_family, 8, "bold"),
                padx=px(8),
                pady=px(3),
            ).pack(side="left")
            if item["action"] == "open_terminal":
                item_options = item.get("options", {})
                if not isinstance(item_options, dict):
                    item_options = {}
                source_code = str(
                    item_options.get("cwd_source", "active_explorer")
                )
                source_preview = {
                    "active_explorer": t(
                        "aktywny Eksplorator",
                        "active File Explorer",
                    ),
                    "fixed": str(item_options.get("fixed_cwd", "")),
                    "home": t("folder domowy", "home folder"),
                }.get(source_code, source_code)
                shell_preview = {
                    "default": t("domyślna powłoka", "default shell"),
                    "powershell": "PowerShell",
                    "cmd": "Command Prompt",
                }.get(
                    str(item_options.get("shell", "default")),
                    str(item_options.get("shell", "default")),
                )
                preview = t(
                    "Folder: {folder} · Powłoka: {shell}",
                    "Folder: {folder} · Shell: {shell}",
                    folder=source_preview,
                    shell=shell_preview,
                )
                if item.get("match") == "prefix_tail":
                    preview += t(
                        " · reszta wypowiedzi → schowek; bez Enter",
                        " · utterance tail → clipboard; no Enter",
                    )
            else:
                preview = str(item["value"]).replace("\r", "").replace("\n", " ↵ ")
            if len(preview) > 125:
                preview = preview[:122] + "…"
            tk.Label(
                card,
                text=preview,
                background=colors["surface_alt"],
                foreground=colors["muted"],
                font=(ui_font_family, 9),
                anchor="w",
                justify="left",
                wraplength=px(610),
            ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(px(7), 0))
            card_actions = ttk.Frame(card, style="Alt.TFrame")
            card_actions.grid(row=2, column=0, columnspan=2, sticky="e", pady=(px(9), 0))
            ttk.Button(
                card_actions,
                text=t("Edytuj", "Edit"),
                cursor="hand2",
                command=lambda selected=item_index: open_custom_command_editor(
                    selected
                ),
            ).grid(row=0, column=0, padx=(0, px(6)))
            ttk.Button(
                card_actions,
                text=t("Usuń", "Delete"),
                style="Danger.TButton",
                cursor="hand2",
                command=lambda selected=item_index: remove_custom_command(selected),
            ).grid(row=0, column=1)

    ttk.Button(
        commands_toolbar,
        text=t("+ Dodaj komendę", "+ Add command"),
        style="Accent.TButton",
        command=open_custom_command_editor,
    ).grid(row=0, column=1, sticky="e")
    render_custom_command_cards()

    unmanaged_commands_notice = tk.Frame(
        commands_tab,
        background=colors["warning_soft"],
        highlightbackground=colors["warning_border"],
        highlightthickness=1,
        padx=px(16),
        pady=px(13),
    )
    unmanaged_commands_notice.columnconfigure(0, weight=1)
    unmanaged_commands_notice_var = tk.StringVar()
    tk.Label(
        unmanaged_commands_notice,
        text=t(
            "Nieobsługiwane wpisy w config.json",
            "Unsupported entries in config.json",
        ),
        background=colors["warning_soft"],
        foreground=colors["warning"],
        font=(ui_font_family, 10, "bold"),
    ).grid(row=0, column=0, sticky="w")
    tk.Label(
        unmanaged_commands_notice,
        textvariable=unmanaged_commands_notice_var,
        background=colors["warning_soft"],
        foreground=colors["muted"],
        font=(ui_font_family, 9),
        justify="left",
        anchor="w",
        wraplength=px(620),
    ).grid(row=1, column=0, sticky="ew", pady=(px(5), 0))

    def open_config_for_custom_command_repair() -> None:
        prompt = t(
            "Panel ustawień zostanie zamknięty, aby późniejszy zapis nie cofnął "
            "ręcznej naprawy pliku.",
            "Settings will close so a later save cannot overwrite your manual "
            "repair of the file.",
        )
        if dirty_state["dirty"]:
            prompt += t(
                " Niezapisane zmiany zostaną odrzucone.",
                " Unsaved changes will be discarded.",
            )
        if not messagebox.askyesno(
            t(
                "{app} — otwórz konfigurację",
                "{app} — Open configuration",
                app=APP_DISPLAY_NAME,
            ),
            prompt,
            parent=root,
        ):
            return
        if open_path(CONFIG_PATH):
            root.destroy()

    ttk.Button(
        unmanaged_commands_notice,
        text=t("Otwórz config.json", "Open config.json"),
        command=open_config_for_custom_command_repair,
    ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(px(14), 0))

    def refresh_unmanaged_custom_commands_notice() -> None:
        count = len(unmanaged_custom_command_items)
        if not count and custom_commands_schema_is_supported:
            unmanaged_commands_notice.grid_remove()
            return
        if not custom_commands_schema_is_supported:
            unmanaged_commands_notice_var.set(
                t(
                    "Własne komendy używają nieobsługiwanego schematu. Ta sekcja "
                    "pozostanie dokładnie bez zmian i nie zostanie uruchomiona. "
                    "Otwórz konfigurację, aby poprawić ją ręcznie; panel zostanie zamknięty.",
                    "Custom commands use an unsupported schema. This section will "
                    "remain exactly unchanged and will not run. Open the configuration "
                    "to correct it manually; Settings will close.",
                )
            )
        else:
            unmanaged_commands_notice_var.set(
                t(
                    "Liczba ukrytych wpisów: {count}. Są nieprawidłowe, niejednoznaczne "
                    "albo pochodzą z nowszej wersji. Nie zostaną usunięte przy zapisie. "
                    "Otwórz konfigurację, aby poprawić je ręcznie; panel zostanie zamknięty.",
                    "Hidden entries: {count}. They are invalid, ambiguous, or come "
                    "from a newer version. They will not be removed when you save. "
                    "Open the configuration to correct them manually; Settings will close.",
                    count=count,
                )
            )
        unmanaged_commands_notice.grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(0, px(14)),
        )

    refresh_unmanaged_custom_commands_notice()

    commands_notice = tk.Frame(
        commands_tab,
        background=colors["warning_soft"],
        highlightbackground=colors["warning_border"],
        highlightthickness=1,
        padx=px(16),
        pady=px(13),
    )
    commands_notice.grid(row=3, column=0, columnspan=3, sticky="ew")
    tk.Label(
        commands_notice,
        text=t("Bezpieczeństwo i prywatność", "Safety and privacy"),
        background=colors["warning_soft"],
        foreground=colors["text"],
        font=(ui_font_family, 10, "bold"),
    ).grid(row=0, column=0, sticky="w")
    tk.Label(
        commands_notice,
        text=t(
            "Mówik nie wykonuje zapisanych poleceń systemowych. Może otworzyć widoczny "
            "terminal i skopiować jednowierszowy szkic do schowka, ale to Ty wklejasz go "
            "i naciskasz Enter. Otwieranie zawsze wymaga potwierdzenia; skrypty, skróty "
            "i ścieżki sieciowe są blokowane. Wszystkie własne komendy są blokowane, "
            "gdy Mówik działa jako administrator. Nie zapisuj tu haseł ani sekretów.",
            "Mówik does not execute saved system commands. It can open a visible terminal "
            "and copy a one-line draft to the clipboard, but you paste it and press Enter. "
            "Opening always requires confirmation; scripts, shortcuts, and network paths "
            "are blocked. All custom commands are blocked while Mówik runs as "
            "administrator. Do not store passwords or secrets here.",
        ),
        background=colors["warning_soft"],
        foreground=colors["muted"],
        font=(ui_font_family, 9),
        justify="left",
        anchor="w",
        wraplength=px(760),
    ).grid(row=1, column=0, sticky="ew", pady=(px(5), 0))

    def sync_custom_command_controls(*args) -> None:
        state = "readonly" if custom_commands_enabled_var.get() else "disabled"
        custom_trigger_combo.configure(state=state)
        custom_trigger_detect_button.configure(
            state="normal" if custom_commands_enabled_var.get() else "disabled"
        )

    def mark_custom_commands_enabled_touched(*args) -> None:
        custom_commands_enabled_touched["value"] = True

    custom_commands_enabled_var.trace_add(
        "write", sync_custom_command_controls
    )
    custom_commands_enabled_var.trace_add(
        "write", mark_custom_commands_enabled_touched
    )
    sync_custom_command_controls()

    feedback_frame = ttk.LabelFrame(
        sounds_tab,
        text=t("Informacje zwrotne", "Feedback"),
        padding=px(16),
    )
    feedback_frame.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    ttk.Checkbutton(
        feedback_frame,
        text=t("Sygnały dźwiękowe", "Sound cues"),
        variable=sounds_var,
    ).grid(
        row=0,
        column=0,
        sticky="w",
        padx=(0, px(25)),
        pady=px(4),
    )
    ttk.Checkbutton(
        feedback_frame,
        text=t("Powiadomienia Windows", "Windows notifications"),
        variable=notifications_var,
    ).grid(row=0, column=1, sticky="w", pady=px(4))
    ttk.Checkbutton(
        feedback_frame,
        text=t(
            "Kapsuła dyktowania na żywo",
            "Live dictation capsule",
        ),
        variable=floating_indicator_var,
    ).grid(
        row=1,
        column=0,
        columnspan=2,
        sticky="w",
        pady=(px(7), px(2)),
    )
    ttk.Label(
        feedback_frame,
        text=t(
            "Pokazuje reagującą falę głosu, stan rozpoznawania i krótki podgląd "
            "gotowego tekstu bez przejmowania fokusu.",
            "Shows a voice-reactive waveform, recognition state, and a short "
            "preview of the finished text without taking focus.",
        ),
        style="Muted.TLabel",
        wraplength=px(700),
    ).grid(
        row=2,
        column=0,
        columnspan=2,
        sticky="w",
        padx=(px(25), 0),
        pady=(0, px(4)),
    )
    sounds_have_custom_values = bool(loop_recording_sound_var.get()) or any(
        bool(variable.get().strip()) for variable in sound_path_vars.values()
    )
    sounds_disclosure = create_disclosure(
        sounds_tab,
        1,
        sounds_canvas,
        t(
            "Własne pliki WAV, odsłuch, zapętlanie i folder dźwięków.",
            "Custom WAV files, previews, looping, and the sounds folder.",
        ),
        name="advanced_sounds",
        initially_expanded=sounds_have_custom_values,
    )
    sounds_advanced = sounds_disclosure["body"]

    loop_sound_check = ttk.Checkbutton(
        sounds_advanced,
        text=t(
            "Zapętlaj własny dźwięk nagrywania podczas trzymania przycisku",
            "Loop the custom recording sound while the shortcut is held",
        ),
        variable=loop_recording_sound_var,
    )
    loop_sound_check.grid(
        row=0,
        column=0,
        sticky="w",
        pady=(0, px(10)),
    )

    def sync_sound_controls(*args) -> None:
        can_loop = bool(sounds_var.get()) and bool(
            sound_path_vars["start"].get().strip()
        )
        loop_sound_check.configure(state="normal" if can_loop else "disabled")

    sounds_var.trace_add("write", sync_sound_controls)
    sound_path_vars["start"].trace_add("write", sync_sound_controls)
    sync_sound_controls()

    sound_labels = {
        "start": t("Naciśnięcie / trzymanie", "Press / hold"),
        "stop": t("Puszczenie przycisku", "Release"),
        "done": t("Tekst gotowy", "Text ready"),
        "error": t("Błąd lub brak mowy", "Error or no speech"),
    }

    def choose_sound(kind: str) -> None:
        current = resolve_sound_path(sound_path_vars[kind].get())
        initial_dir = str(current.parent) if current is not None else str(Path.home())
        selected = filedialog.askopenfilename(
            parent=root,
            title=t(
                "Wybierz dźwięk: {sound}",
                "Choose sound: {sound}",
                sound=sound_labels[kind],
            ),
            initialdir=initial_dir,
            filetypes=(
                (t("Dźwięk WAV", "WAV audio"), "*.wav"),
                (t("Wszystkie pliki", "All files"), "*.*"),
            ),
        )
        if not selected:
            return
        try:
            validate_wave_file(Path(selected), translator)
        except AppError:
            messagebox.showerror(
                APP_DISPLAY_NAME,
                t(
                    "Nie można użyć wybranego pliku. Wybierz prawidłowy plik WAV "
                    "mniejszy niż 50 MB.",
                    "The selected file cannot be used. Choose a valid WAV file "
                    "smaller than 50 MB.",
                ),
                parent=root,
            )
            return
        sound_path_vars[kind].set(selected)
        set_status(
            t(
                "Wybrano dźwięk „{name}”. Zapis skopiuje go do folderu Mówika.",
                "Selected “{name}”. Saving will copy it to the Mówik folder.",
                name=Path(selected).name,
            ),
            "warning",
        )

    def preview_sound(kind: str) -> None:
        if os.name != "nt":
            messagebox.showerror(
                APP_DISPLAY_NAME,
                t(
                    "Odsłuch jest dostępny na Windowsie.",
                    "Sound preview is available on Windows.",
                ),
                parent=root,
            )
            return
        try:
            import winsound

            path = resolve_sound_path(sound_path_vars[kind].get())
            if path is not None:
                validate_wave_file(path, translator)
                winsound.PlaySound(
                    str(path),
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
            else:
                play_builtin_sound_async(kind, "SoundPreview")
        except Exception as exc:
            messagebox.showerror(
                APP_DISPLAY_NAME,
                t(
                    "Nie udało się odtworzyć dźwięku:\n\n{error}",
                    "Could not play the sound:\n\n{error}",
                    error=exc,
                ),
                parent=root,
            )

    custom_sound_frame = ttk.LabelFrame(
        sounds_advanced,
        text=t("Własne dźwięki WAV", "Custom WAV sounds"),
        padding=px(16),
    )
    custom_sound_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    custom_sound_frame.columnconfigure(0, weight=1)
    ttk.Label(
        custom_sound_frame,
        text=t(
            "Stan „Wbudowany” oznacza krótki sygnał Mówika. Wybrany plik zostanie "
            "skopiowany do %APPDATA%\\Mowik\\sounds.",
            "“Built-in” uses Mówik's short default cue. A selected file is copied "
            "to %APPDATA%\\Mowik\\sounds.",
        ),
        style="Muted.TLabel",
        wraplength=px(760),
    ).grid(row=0, column=0, sticky="ew", pady=(0, px(10)))

    sound_display_vars: dict[str, tk.StringVar] = {}

    def refresh_sound_display(kind: str) -> None:
        value = sound_path_vars[kind].get().strip()
        sound_display_vars[kind].set(
            value if value else t("Wbudowany", "Built-in")
        )

    def reset_sound(kind: str) -> None:
        sound_path_vars[kind].set("")
        if kind == "start":
            loop_recording_sound_var.set(False)

    for row_index, kind in enumerate(("start", "stop", "done", "error"), start=1):
        sound_display_vars[kind] = tk.StringVar()
        sound_path_vars[kind].trace_add(
            "write",
            lambda *args, selected_kind=kind: refresh_sound_display(selected_kind),
        )
        refresh_sound_display(kind)
        sound_row = ttk.Frame(custom_sound_frame, style="Surface.TFrame")
        sound_row.grid(
            row=row_index,
            column=0,
            sticky="ew",
            pady=(px(5), px(7)),
        )
        sound_row.columnconfigure(0, weight=1)
        ttk.Label(
            sound_row,
            text=sound_labels[kind],
            style="Field.TLabel",
        ).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, px(5)),
        )
        ttk.Entry(
            sound_row,
            textvariable=sound_display_vars[kind],
            state="readonly",
        ).grid(row=1, column=0, sticky="ew")
        buttons = ttk.Frame(sound_row)
        buttons.grid(row=1, column=1, sticky="e", padx=(px(8), 0))
        ttk.Button(
            buttons,
            text=t("Wybierz…", "Choose…"),
            command=lambda selected_kind=kind: choose_sound(selected_kind),
        ).grid(row=0, column=0, padx=(0, px(4)))
        ttk.Button(
            buttons,
            text=t("Odsłuch", "Preview"),
            command=lambda selected_kind=kind: preview_sound(selected_kind),
        ).grid(row=0, column=1, padx=px(4))
        ttk.Button(
            buttons,
            text=t("Przywróć", "Reset"),
            command=lambda selected_kind=kind: reset_sound(selected_kind),
        ).grid(row=0, column=2, padx=(px(4), 0))

    sounds_footer = ttk.Frame(sounds_advanced)
    sounds_footer.grid(
        row=2,
        column=0,
        columnspan=3,
        sticky="w",
        pady=(px(12), 0),
    )
    ttk.Button(
        sounds_footer,
        text=t("Otwórz folder dźwięków", "Open sounds folder"),
        command=lambda: open_path(SOUNDS_DIR),
    ).grid(row=0, column=0)

    ollama_intro = tk.Frame(
        ollama_tab,
        background=colors["primary_soft"],
        highlightbackground=colors["primary_border"],
        highlightthickness=1,
        padx=px(18),
        pady=px(14),
    )
    ollama_intro.grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    tk.Label(
        ollama_intro,
        text=t("Funkcja opcjonalna", "Optional feature"),
        background=colors["primary_soft"],
        foreground=colors["primary"],
        font=(ui_font_family, 9, "bold"),
    ).pack(anchor="w")
    tk.Label(
        ollama_intro,
        text=t(
            "Mówik rozpoznaje mowę bez Ollamy. Lokalny model językowy może "
            "jedynie poprawić interpunkcję i oczywiste literówki.",
            "Mówik recognizes speech without Ollama. A local language model "
            "can only improve punctuation and obvious spelling mistakes.",
        ),
        background=colors["primary_soft"],
        foreground=colors["muted"],
        font=(ui_font_family, 9),
        justify="left",
        wraplength=px(720),
    ).pack(anchor="w", pady=(px(4), 0))

    ollama_frame = ttk.LabelFrame(
        ollama_tab,
        text=t("Lokalna korekta tekstu", "Local text correction"),
        padding=px(16),
    )
    ollama_frame.grid(
        row=1,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(0, px(14)),
    )
    ollama_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        ollama_frame,
        text=t(
            "Włącz lokalną korektę przez Ollamę",
            "Enable local correction with Ollama",
        ),
        variable=ollama_enabled_var,
    ).grid(
        row=0,
        column=0,
        columnspan=3,
        sticky="w",
        pady=(0, px(7)),
    )
    ollama_model_entry = ttk.Entry(ollama_frame, textvariable=ollama_model_var)
    add_field(
        ollama_frame,
        1,
        t("Nazwa modelu", "Model name"),
        ollama_model_entry,
        t(
            "Wpisz nazwę modelu pobranego wcześniej w Ollamie.",
            "Enter the name of a model already downloaded in Ollama.",
        ),
    )
    ttk.Label(
        ollama_frame,
        text=t(
            "Włączenie korektora wydłuży oczekiwanie. Mówik odrzuca korektę, "
            "jeżeli za mocno zmienia tekst, liczby lub negacje.",
            "Enabling correction increases processing time. Mówik rejects a "
            "correction if it changes the text, numbers, or negations too much.",
        ),
        style="Muted.TLabel",
        wraplength=px(720),
    ).grid(
        row=2,
        column=0,
        columnspan=3,
        sticky="ew",
        pady=(px(12), 0),
    )

    default_ollama = DEFAULT_CONFIG["ollama_cleanup"]
    ollama_disclosure = create_disclosure(
        ollama_tab,
        2,
        ollama_canvas,
        t(
            "Adres lokalnej usługi Ollama i limit oczekiwania na odpowiedź.",
            "Local Ollama service address and response timeout.",
        ),
        name="advanced_ollama",
        initially_expanded=(
            str(ollama_url_var.get()) != str(default_ollama["url"])
            or str(ollama_timeout_var.get())
            != str(default_ollama["timeout_seconds"])
        ),
    )
    ollama_advanced = ollama_disclosure["body"]
    ollama_url_entry = ttk.Entry(ollama_advanced, textvariable=ollama_url_var)
    add_field(
        ollama_advanced,
        0,
        t("Adres Ollamy", "Ollama address"),
        ollama_url_entry,
    )
    ollama_timeout_spin = ttk.Spinbox(
        ollama_advanced,
        from_=1,
        to=MAX_OLLAMA_TIMEOUT_SECONDS,
        textvariable=ollama_timeout_var,
    )
    add_field(
        ollama_advanced,
        1,
        t("Limit czasu (s)", "Timeout (s)"),
        ollama_timeout_spin,
    )

    ollama_controls = (
        ollama_url_entry,
        ollama_model_entry,
        ollama_timeout_spin,
    )

    def sync_ollama_controls(*args) -> None:
        state = "normal" if ollama_enabled_var.get() else "disabled"
        for control in ollama_controls:
            control.configure(state=state)

    ollama_enabled_var.trace_add("write", sync_ollama_controls)
    sync_ollama_controls()

    files_tab.columnconfigure(0, weight=1)
    diagnostics_intro = tk.Frame(
        files_tab,
        background=colors["success_soft"],
        highlightbackground=colors["success_border"],
        highlightthickness=1,
        padx=px(18),
        pady=px(14),
    )
    diagnostics_intro.grid(
        row=0,
        column=0,
        sticky="ew",
        pady=(0, px(14)),
    )
    tk.Label(
        diagnostics_intro,
        text=t(
            "Mówik {version} · dane pozostają na tym komputerze",
            "Mówik {version} · data stays on this computer",
            version=APP_VERSION,
        ),
        background=colors["success_soft"],
        foreground=colors["success"],
        font=(ui_font_family, 10, "bold"),
    ).pack(anchor="w")
    tk.Label(
        diagnostics_intro,
        text=t(
            "Jeżeli coś nie działa, zacznij od logu. Nie zawiera on treści "
            "dyktowanych zdań.",
            "If something is not working, start with the log. It never contains "
            "the content of dictated sentences.",
        ),
        background=colors["success_soft"],
        foreground=colors["success"],
        font=(ui_font_family, 9),
        justify="left",
        wraplength=px(700),
    ).pack(anchor="w", pady=(px(4), 0))

    data_card = ttk.LabelFrame(
        files_tab,
        text=t("Diagnostyka i dane", "Diagnostics and data"),
        padding=px(16),
    )
    data_card.grid(row=1, column=0, sticky="ew", pady=(0, px(14)))
    data_card.columnconfigure(0, weight=1)
    ttk.Label(
        data_card,
        text=str(LOG_PATH),
        style="Muted.TLabel",
        wraplength=px(700),
    ).grid(row=0, column=0, sticky="ew", pady=(0, px(9)))
    files_buttons = ttk.Frame(data_card)
    files_buttons.grid(row=1, column=0, sticky="w")
    ttk.Button(
        files_buttons,
        text=t("Otwórz log diagnostyczny", "Open diagnostic log"),
        command=lambda: open_path(LOG_PATH),
    ).grid(row=0, column=0, padx=(0, px(8)))
    ttk.Button(
        files_buttons,
        text=t("Otwórz folder danych", "Open data folder"),
        command=lambda: open_path(LOCALDATA_DIR),
    ).grid(row=0, column=1)

    help_disclosure = create_disclosure(
        files_tab,
        2,
        files_canvas,
        t(
            "Bezpośrednia edycja pliku config.json — tylko do diagnostyki i niestandardowych zmian.",
            "Direct config.json editing — only for diagnostics and custom changes.",
        ),
        name="advanced_help",
    )
    help_advanced = help_disclosure["body"]
    config_card = ttk.LabelFrame(
        help_advanced,
        text=t("Plik konfiguracyjny", "Configuration file"),
        padding=px(16),
    )
    config_card.grid(row=0, column=0, columnspan=3, sticky="ew")
    config_card.columnconfigure(0, weight=1)
    ttk.Label(
        config_card,
        text=str(CONFIG_PATH),
        style="Muted.TLabel",
        wraplength=px(700),
    ).grid(row=0, column=0, sticky="ew", pady=(0, px(9)))
    ttk.Button(
        config_card,
        text=t("Otwórz config.json…", "Open config.json…"),
        command=lambda: open_path(CONFIG_PATH),
    ).grid(row=1, column=0, sticky="w")

    def reveal_validation_error(
        page_key: str,
        disclosure: dict[str, Any],
        widget,
    ) -> None:
        show_page(page_key)
        disclosure["reveal"](widget)

    def validation_reveal(page_key: str, disclosure, widget):
        return lambda: reveal_validation_error(page_key, disclosure, widget)

    def focus_validation_error(page_key: str, widget) -> None:
        show_page(page_key)
        root.after_idle(widget.focus_set)

    def parse_int(
        variable: tk.StringVar,
        label: str,
        minimum: int,
        maximum: int,
        *,
        on_error=None,
    ) -> int:
        try:
            value = int(variable.get().strip())
        except ValueError as exc:
            if on_error is not None:
                on_error()
            raise AppError(
                t(
                    "Pole „{label}” musi być liczbą całkowitą.",
                    "The “{label}” field must be an integer.",
                    label=label,
                )
            ) from exc
        if not minimum <= value <= maximum:
            if on_error is not None:
                on_error()
            raise AppError(
                t(
                    "Pole „{label}” musi być w zakresie {minimum}–{maximum}.",
                    "The “{label}” field must be between {minimum} and {maximum}.",
                    label=label,
                    minimum=minimum,
                    maximum=maximum,
                )
            )
        return value

    def parse_float(
        variable: tk.StringVar,
        label: str,
        minimum: float,
        maximum: float,
        *,
        on_error=None,
    ) -> float:
        try:
            value = float(variable.get().strip().replace(",", "."))
        except ValueError as exc:
            if on_error is not None:
                on_error()
            raise AppError(
                t(
                    "Pole „{label}” musi być liczbą.",
                    "The “{label}” field must be a number.",
                    label=label,
                )
            ) from exc
        if not minimum <= value <= maximum:
            if on_error is not None:
                on_error()
            raise AppError(
                t(
                    "Pole „{label}” musi być w zakresie {minimum}–{maximum}.",
                    "The “{label}” field must be between {minimum} and {maximum}.",
                    label=label,
                    minimum=minimum,
                    maximum=maximum,
                )
            )
        return value

    def parse_retained_int(
        variable: tk.StringVar,
        label: str,
        minimum: int,
        maximum: int,
        *,
        active: bool,
        previous: Any,
        fallback: int,
        on_error,
    ) -> int:
        try:
            return parse_int(
                variable,
                label,
                minimum,
                maximum,
                on_error=on_error if active else None,
            )
        except AppError:
            if active:
                raise
        for candidate in (previous, fallback):
            try:
                retained = int(candidate)
            except (TypeError, ValueError):
                continue
            if minimum <= retained <= maximum:
                return retained
        return fallback

    def parse_retained_float(
        variable: tk.StringVar,
        label: str,
        minimum: float,
        maximum: float,
        *,
        active: bool,
        previous: Any,
        fallback: float,
        on_error,
    ) -> float:
        try:
            return parse_float(
                variable,
                label,
                minimum,
                maximum,
                on_error=on_error if active else None,
            )
        except AppError:
            if active:
                raise
        for candidate in (previous, fallback):
            try:
                retained = float(str(candidate).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if minimum <= retained <= maximum:
                return retained
        return fallback

    pending_sound_sources: dict[str, Any] = {}

    def collect_config() -> dict[str, Any]:
        nonlocal pending_sound_sources
        updated = load_config()
        trigger = str(trigger_values.get(trigger_var.get(), trigger_var.get()))
        try:
            dictation_trigger_identity = split_trigger(trigger)
        except AppError as exc:
            focus_validation_error("dictation", trigger_combo)
            raise AppError(
                t(
                    "Wybierz prawidłowy przycisk dyktowania.",
                    "Choose a valid dictation shortcut.",
                )
            ) from exc
        custom_trigger = str(
            trigger_values.get(
                custom_commands_trigger_var.get(),
                custom_commands_trigger_var.get(),
            )
        )
        custom_commands_active = bool(custom_commands_enabled_var.get())
        try:
            custom_trigger_identity = split_trigger(custom_trigger)
        except AppError as exc:
            if custom_commands_active:
                focus_validation_error("commands", custom_trigger_combo)
                raise AppError(
                    t(
                        "Wybierz prawidłowy przycisk własnych komend.",
                        "Choose a valid custom-command shortcut.",
                    )
                ) from exc
            custom_trigger = "keyboard:f7"
            custom_trigger_identity = ("keyboard", "f7")
        if custom_commands_active and not custom_command_items:
            focus_validation_error("commands", custom_trigger_combo)
            raise AppError(
                t(
                    "Dodaj co najmniej jedną komendę albo wyłącz tryb komend.",
                    "Add at least one command or disable command mode.",
                )
            )
        if (
            custom_commands_active
            and custom_trigger_identity == dictation_trigger_identity
        ):
            focus_validation_error("commands", custom_trigger_combo)
            raise AppError(
                t(
                    "Przycisk komend musi być inny niż przycisk dyktowania.",
                    "The command shortcut must differ from the dictation shortcut.",
                )
            )
        custom_commands_enabled_for_save = custom_commands_active
        if (
            preserve_original_custom_commands_enabled
            and not custom_commands_enabled_touched["value"]
            and not custom_command_items
        ):
            custom_commands_enabled_for_save = (
                custom_commands_config.get("enabled", False) is True
            )
        model = str(model_values.get(model_var.get(), model_var.get())).strip()
        device = str(device_values.get(device_var.get(), device_var.get())).strip()
        language = str(
            language_values.get(language_var.get(), language_var.get())
        ).strip()
        ui_language = str(
            ui_language_values.get(
                ui_language_var.get(),
                ui_language_var.get(),
            )
        ).strip()
        if not model:
            reveal_validation_error(
                "dictation", dictation_disclosure, model_combo
            )
            raise AppError(t("Wybierz model mowy.", "Choose a speech model."))
        if device not in {"auto", "cpu", "cuda"}:
            reveal_validation_error(
                "dictation", dictation_disclosure, device_combo
            )
            raise AppError(
                t(
                    "Urządzenie musi mieć wartość auto, cpu albo cuda.",
                    "The processing device must be auto, cpu, or cuda.",
                )
            )
        if not language:
            focus_validation_error("dictation", language_combo)
            raise AppError(
                t("Wybierz język dyktowania.", "Choose a dictation language.")
            )
        if ui_language not in {"auto", "pl", "en"}:
            focus_validation_error("start", ui_language_combo)
            raise AppError(
                t(
                    "Wybierz język interfejsu.",
                    "Choose an interface language.",
                )
            )
        current_microphone_state = microphone_choice_state["current"]
        if current_microphone_state is None:
            focus_validation_error("dictation", microphone_combo)
            raise AppError(
                t(
                    "Nie udało się wczytać ustawień mikrofonu. Odśwież listę i "
                    "wybierz urządzenie.",
                    "The microphone settings could not be loaded. Refresh the "
                    "list and choose a device.",
                )
            )
        try:
            selected_microphone = microphone_config_value_for_choice(
                current_microphone_state,
                microphone_var.get(),
                translator,
            )
        except AppError:
            focus_validation_error("dictation", microphone_combo)
            raise

        updated["trigger"] = trigger
        updated["model"] = model
        updated["device"] = device
        updated["language"] = language
        updated["ui_language"] = ui_language
        updated["microphone"] = selected_microphone
        updated["cpu_threads"] = parse_int(
            cpu_threads_var,
            t("Wątki CPU", "CPU threads"),
            0,
            256,
            on_error=validation_reveal(
                "dictation", dictation_disclosure, threads_spin
            ),
        )
        updated["beam_size"] = parse_int(
            beam_size_var,
            t("Dokładność rozpoznawania", "Recognition accuracy"),
            1,
            10,
            on_error=validation_reveal(
                "dictation", dictation_disclosure, beam_spin
            ),
        )
        updated["pre_roll_ms"] = parse_int(
            pre_roll_var,
            t("Bufor przed naciśnięciem", "Pre-roll buffer"),
            0,
            2000,
            on_error=validation_reveal(
                "audio", audio_disclosure, pre_roll_spin
            ),
        )
        updated["post_roll_ms"] = parse_int(
            post_roll_var,
            t("Bufor po puszczeniu", "Post-release buffer"),
            0,
            2000,
            on_error=validation_reveal(
                "audio", audio_disclosure, post_roll_spin
            ),
        )
        updated["minimum_recording_ms"] = parse_int(
            minimum_recording_var,
            t("Minimalne nagranie", "Minimum recording"),
            0,
            10000,
            on_error=validation_reveal(
                "audio", audio_disclosure, minimum_recording_spin
            ),
        )
        updated["maximum_recording_seconds"] = parse_int(
            maximum_recording_var,
            t("Maksymalne nagranie", "Maximum recording"),
            1,
            MAXIMUM_RECORDING_SECONDS,
            on_error=validation_reveal(
                "audio", audio_disclosure, maximum_recording_spin
            ),
        )
        if (
            updated["minimum_recording_ms"]
            > updated["maximum_recording_seconds"] * 1_000
        ):
            reveal_validation_error(
                "audio", audio_disclosure, minimum_recording_spin
            )
            raise AppError(
                t(
                    "Minimalne nagranie nie może być dłuższe niż maksymalne nagranie.",
                    "The minimum recording time cannot exceed the maximum recording time.",
                )
            )
        updated["minimum_rms"] = parse_float(
            minimum_rms_var,
            t("Minimalna głośność RMS", "Minimum RMS level"),
            0.0,
            1.0,
            on_error=validation_reveal(
                "audio", audio_disclosure, minimum_rms_entry
            ),
        )

        updated.setdefault("vad", {})
        vad_active = bool(vad_enabled_var.get())
        updated["vad"].update(
            {
                "enabled": vad_active,
                "threshold": parse_retained_float(
                    vad_threshold_var,
                    t("Próg VAD", "VAD threshold"),
                    0.0,
                    1.0,
                    active=vad_active,
                    previous=updated["vad"].get("threshold"),
                    fallback=float(DEFAULT_CONFIG["vad"]["threshold"]),
                    on_error=validation_reveal(
                        "audio", audio_disclosure, vad_threshold_entry
                    ),
                ),
                "min_speech_duration_ms": parse_retained_int(
                    vad_min_speech_var,
                    t("Minimalna mowa", "Minimum speech"),
                    0,
                    10000,
                    active=vad_active,
                    previous=updated["vad"].get("min_speech_duration_ms"),
                    fallback=int(
                        DEFAULT_CONFIG["vad"]["min_speech_duration_ms"]
                    ),
                    on_error=validation_reveal(
                        "audio", audio_disclosure, vad_speech_spin
                    ),
                ),
                "min_silence_duration_ms": parse_retained_int(
                    vad_min_silence_var,
                    t("Minimalna cisza", "Minimum silence"),
                    0,
                    10000,
                    active=vad_active,
                    previous=updated["vad"].get("min_silence_duration_ms"),
                    fallback=int(
                        DEFAULT_CONFIG["vad"]["min_silence_duration_ms"]
                    ),
                    on_error=validation_reveal(
                        "audio", audio_disclosure, vad_silence_spin
                    ),
                ),
                "speech_pad_ms": parse_retained_int(
                    vad_speech_pad_var,
                    t("Margines mowy", "Speech padding"),
                    0,
                    3000,
                    active=vad_active,
                    previous=updated["vad"].get("speech_pad_ms"),
                    fallback=int(DEFAULT_CONFIG["vad"]["speech_pad_ms"]),
                    on_error=validation_reveal(
                        "audio", audio_disclosure, vad_pad_spin
                    ),
                ),
            }
        )
        updated.setdefault("dictionary", {})
        dictionary_active = bool(dictionary_enabled_var.get())
        updated["dictionary"].update(
            {
                "enabled": dictionary_active,
                "max_terms": parse_retained_int(
                    dictionary_max_terms_var,
                    t("Maksymalna liczba haseł", "Maximum number of entries"),
                    0,
                    5000,
                    active=dictionary_active,
                    previous=updated["dictionary"].get("max_terms"),
                    fallback=int(DEFAULT_CONFIG["dictionary"]["max_terms"]),
                    on_error=validation_reveal(
                        "text", text_disclosure, dictionary_limit_spin
                    ),
                ),
            }
        )
        updated.setdefault("paste", {})
        paste_enabled = bool(paste_enabled_var.get())
        copy_to_clipboard = bool(copy_to_clipboard_var.get())
        if not paste_enabled and not copy_to_clipboard:
            focus_validation_error("text", paste_enabled_check)
            raise AppError(
                t(
                    "Włącz automatyczne wklejanie albo kopiowanie do schowka.",
                    "Enable automatic pasting or copying to the clipboard.",
                )
            )
        updated["paste"].update(
            {
                "enabled": paste_enabled,
                "copy_to_clipboard": copy_to_clipboard,
                "append_space": bool(append_space_var.get()),
                "delay_ms": parse_retained_int(
                    paste_delay_var,
                    t("Opóźnienie przed Ctrl+V", "Delay before Ctrl+V"),
                    0,
                    5000,
                    active=paste_enabled,
                    previous=updated["paste"].get("delay_ms"),
                    fallback=int(DEFAULT_CONFIG["paste"]["delay_ms"]),
                    on_error=validation_reveal(
                        "text", text_disclosure, paste_delay_spin
                    ),
                ),
            }
        )
        updated.setdefault("feedback", {})
        updated["feedback"].update(
            {
                "sounds": bool(sounds_var.get()),
                "notifications": bool(notifications_var.get()),
                "floating_indicator": bool(floating_indicator_var.get()),
                "loop_recording_sound": bool(loop_recording_sound_var.get()),
            }
        )
        updated.setdefault("voice_commands", {})
        updated["voice_commands"]["enabled"] = bool(voice_commands_var.get())
        # Nowszy/obcy schemat pozostaje nieprzezroczysty. Stary panel nie może
        # go aktywować, interpretować ani po cichu obniżyć jego wersji.
        updated["custom_commands"] = custom_commands_settings_for_save(
            updated.get("custom_commands", {}),
            enabled=custom_commands_enabled_for_save,
            trigger=custom_trigger,
            items=custom_command_items_for_save(),
        )
        updated.setdefault("ollama_cleanup", {})
        ollama_active = bool(ollama_enabled_var.get())
        updated["ollama_cleanup"].update(
            {
                "enabled": ollama_active,
                "url": ollama_url_var.get().strip() or "http://127.0.0.1:11434",
                "model": ollama_model_var.get().strip(),
                "timeout_seconds": parse_retained_int(
                    ollama_timeout_var,
                    t("Limit czasu Ollamy", "Ollama timeout"),
                    1,
                    MAX_OLLAMA_TIMEOUT_SECONDS,
                    active=ollama_active,
                    previous=updated["ollama_cleanup"].get("timeout_seconds"),
                    fallback=int(
                        DEFAULT_CONFIG["ollama_cleanup"]["timeout_seconds"]
                    ),
                    on_error=validation_reveal(
                        "integrations", ollama_disclosure, ollama_timeout_spin
                    ),
                ),
            }
        )
        if updated["ollama_cleanup"]["enabled"] and not updated[
            "ollama_cleanup"
        ]["model"]:
            focus_validation_error("integrations", ollama_model_entry)
            raise AppError(
                t(
                    "Korekta Ollama jest włączona, ale pole „Nazwa modelu” jest puste.",
                    "Ollama correction is enabled, but the “Model name” field is empty.",
                )
            )
        if updated["ollama_cleanup"]["enabled"]:
            try:
                updated["ollama_cleanup"]["url"] = normalize_ollama_base_url(
                    updated["ollama_cleanup"]["url"],
                    translator,
                )
            except AppError:
                focus_validation_error("integrations", ollama_url_entry)
                raise
        try:
            pending_sound_sources = {
                kind: sound_path_vars[kind].get()
                for kind in ("start", "stop", "done", "error")
            }
            custom_sound_values: dict[str, str] = {}
            for kind, source_value in pending_sound_sources.items():
                source = resolve_sound_path(source_value)
                if source is None:
                    custom_sound_values[kind] = ""
                    continue
                validate_wave_file(source.resolve(), translator)
                custom_sound_values[kind] = str(Path("sounds") / f"{kind}.wav")
            updated["feedback"]["custom_sounds"] = custom_sound_values
        except Exception:
            show_page("sounds")
            sounds_disclosure["reveal"]()
            raise
        return updated

    tracked_variables: list[tk.Variable] = [
        trigger_var,
        custom_commands_enabled_var,
        custom_commands_trigger_var,
        custom_commands_revision_var,
        model_var,
        device_var,
        language_var,
        ui_language_var,
        cpu_threads_var,
        beam_size_var,
        microphone_var,
        pre_roll_var,
        post_roll_var,
        minimum_recording_var,
        maximum_recording_var,
        minimum_rms_var,
        vad_enabled_var,
        vad_threshold_var,
        vad_min_speech_var,
        vad_min_silence_var,
        vad_speech_pad_var,
        dictionary_enabled_var,
        dictionary_max_terms_var,
        paste_enabled_var,
        copy_to_clipboard_var,
        append_space_var,
        paste_delay_var,
        sounds_var,
        notifications_var,
        floating_indicator_var,
        loop_recording_sound_var,
        voice_commands_var,
        ollama_enabled_var,
        ollama_url_var,
        ollama_model_var,
        ollama_timeout_var,
        *sound_path_vars.values(),
    ]
    dirty_state: dict[str, Any] = {
        "baseline": tuple(variable.get() for variable in tracked_variables),
        "dirty": False,
    }
    restart_pending = {"value": False}

    status_dot = tk.Label(
        footer,
        text="●",
        background=colors["surface"],
        foreground=colors["success"],
        font=(ui_font_family, 10, "bold"),
    )
    status_dot.grid(row=0, column=0, sticky="w")
    ttk.Label(
        footer,
        textvariable=status_var,
        style="Muted.TLabel",
        wraplength=px(360),
    ).grid(
        row=0,
        column=1,
        sticky="w",
        padx=(px(7), px(12)),
    )
    footer.columnconfigure(1, weight=1)

    def update_status_indicator(*args) -> None:
        if status_level["value"] == "error":
            color = colors["danger"]
        elif (
            dirty_state["dirty"]
            or restart_pending["value"]
            or status_level["value"] == "warning"
        ):
            color = colors["warning"]
        else:
            color = colors["success"]
        status_dot.configure(foreground=color)

    def refresh_dirty_state(*args) -> None:
        current = tuple(variable.get() for variable in tracked_variables)
        was_dirty = bool(dirty_state["dirty"])
        dirty_state["dirty"] = current != dirty_state["baseline"]
        if dirty_state["dirty"] and not was_dirty:
            set_status(
                t("Masz niezapisane zmiany.", "You have unsaved changes."),
                "warning",
            )
        elif not dirty_state["dirty"] and was_dirty:
            if restart_pending["value"]:
                set_status(
                    t(
                        "Ustawienia są zapisane i czekają na ponowne uruchomienie.",
                        "Settings are saved and waiting for a restart.",
                    ),
                    "warning",
                )
            else:
                set_status(
                    t(
                        "Wszystko gotowe — ustawienia są zapisane.",
                        "Everything is ready — settings are saved.",
                    )
                )
        apply_button.configure(
            state=(
                "normal"
                if dirty_state["dirty"] or restart_pending["value"]
                else "disabled"
            )
        )
        update_status_indicator()

    for variable in tracked_variables:
        variable.trace_add("write", refresh_dirty_state)
    status_var.trace_add("write", update_status_indicator)

    def save_from_window(apply_now: bool) -> None:
        nonlocal config, config_revision
        settings_saved = False
        try:
            updated = collect_config()
            config_revision = save_config_with_custom_sounds(
                updated,
                pending_sound_sources,
                translator,
                expected_revision=config_revision,
            )
            settings_saved = True
            config = updated
            for kind, managed_source in settings_sound_sources_from_config(
                updated
            ).items():
                sound_path_vars[kind].set(managed_source)
            dirty_state["baseline"] = tuple(
                variable.get() for variable in tracked_variables
            )
            dirty_state["dirty"] = False
            if apply_now:
                runtime_result = restart_or_launch_app_after_settings(translator)
                restart_pending["value"] = False
                apply_button.configure(state="disabled")
                if runtime_result == "restart_requested":
                    message = t(
                        "Zapisano — Mówik stosuje zmiany…",
                        "Saved — Mówik is applying changes…",
                    )
                else:
                    message = t(
                        "Zapisano — uruchamiam Mówika…",
                        "Saved — starting Mówik…",
                    )
                set_status(message)
                root.after(180, root.destroy)
            else:
                restart_pending["value"] = True
                apply_button.configure(state="normal")
                set_status(
                    t(
                        "Zapisano. Zmiany zaczną działać po ponownym uruchomieniu Mówika.",
                        "Saved. Changes take effect after Mówik is restarted.",
                    ),
                    "warning",
                )
        except Exception as exc:
            logging.exception("Nie udało się zapisać lub zastosować ustawień")
            if settings_saved and apply_now:
                restart_pending["value"] = True
                apply_button.configure(state="normal")
                set_status(
                    t(
                        "Ustawienia zapisano, ale nie udało się uruchomić Mówika ponownie.",
                        "Settings were saved, but Mówik could not be restarted.",
                    ),
                    "error",
                )
            else:
                apply_button.configure(
                    state=(
                        "normal"
                        if dirty_state["dirty"] or restart_pending["value"]
                        else "disabled"
                    )
                )
                set_status(
                    t(
                        "Błąd zapisywania ustawień.",
                        "Could not save settings.",
                    ),
                    "error",
                )
            messagebox.showerror(
                t(
                    "{app} — błąd ustawień",
                    "{app} — Settings error",
                    app=APP_DISPLAY_NAME,
                ),
                str(exc),
                parent=root,
            )

    def restore_defaults() -> None:
        trigger_var.set(ensure_trigger_display(DEFAULT_CONFIG["trigger"]))
        if custom_commands_schema_is_supported:
            default_custom_commands = DEFAULT_CONFIG["custom_commands"]
            custom_commands_enabled_var.set(
                bool(default_custom_commands["enabled"])
            )
            custom_commands_trigger_var.set(
                ensure_trigger_display(default_custom_commands["trigger"])
            )
            custom_command_items.clear()
            custom_command_items.extend(
                dict(item) for item in default_custom_commands["items"]
            )
            original_custom_command_items_by_key.clear()
            unmanaged_custom_command_items.clear()
            update_custom_commands_revision()
            render_custom_command_cards()
            refresh_unmanaged_custom_commands_notice()
        model_var.set(display_for_value(model_values, DEFAULT_CONFIG["model"]))
        device_var.set(display_for_value(device_values, DEFAULT_CONFIG["device"]))
        language_var.set(
            display_for_value(language_values, DEFAULT_CONFIG["language"])
        )
        ui_language_var.set(
            display_for_value(
                ui_language_values,
                DEFAULT_CONFIG["ui_language"],
            )
        )
        cpu_threads_var.set(str(DEFAULT_CONFIG["cpu_threads"]))
        beam_size_var.set(str(DEFAULT_CONFIG["beam_size"]))
        microphone_var.set(display_for_value(microphone_values, None))
        pre_roll_var.set(str(DEFAULT_CONFIG["pre_roll_ms"]))
        post_roll_var.set(str(DEFAULT_CONFIG["post_roll_ms"]))
        minimum_recording_var.set(str(DEFAULT_CONFIG["minimum_recording_ms"]))
        maximum_recording_var.set(
            str(DEFAULT_CONFIG["maximum_recording_seconds"])
        )
        minimum_rms_var.set(str(DEFAULT_CONFIG["minimum_rms"]))
        default_vad = DEFAULT_CONFIG["vad"]
        vad_enabled_var.set(bool(default_vad["enabled"]))
        vad_threshold_var.set(str(default_vad["threshold"]))
        vad_min_speech_var.set(str(default_vad["min_speech_duration_ms"]))
        vad_min_silence_var.set(str(default_vad["min_silence_duration_ms"]))
        vad_speech_pad_var.set(str(default_vad["speech_pad_ms"]))
        default_dictionary = DEFAULT_CONFIG["dictionary"]
        dictionary_enabled_var.set(bool(default_dictionary["enabled"]))
        dictionary_max_terms_var.set(str(default_dictionary["max_terms"]))
        default_paste = DEFAULT_CONFIG["paste"]
        paste_enabled_var.set(bool(default_paste["enabled"]))
        copy_to_clipboard_var.set(bool(default_paste["copy_to_clipboard"]))
        append_space_var.set(bool(default_paste["append_space"]))
        paste_delay_var.set(str(default_paste["delay_ms"]))
        default_feedback = DEFAULT_CONFIG["feedback"]
        sounds_var.set(bool(default_feedback["sounds"]))
        notifications_var.set(bool(default_feedback["notifications"]))
        floating_indicator_var.set(
            bool(default_feedback["floating_indicator"])
        )
        loop_recording_sound_var.set(
            bool(default_feedback["loop_recording_sound"])
        )
        for sound_kind in ("start", "stop", "done", "error"):
            sound_path_vars[sound_kind].set("")
        voice_commands_var.set(bool(DEFAULT_CONFIG["voice_commands"]["enabled"]))
        default_ollama = DEFAULT_CONFIG["ollama_cleanup"]
        ollama_enabled_var.set(bool(default_ollama["enabled"]))
        ollama_url_var.set(str(default_ollama["url"]))
        ollama_model_var.set(str(default_ollama["model"]))
        ollama_timeout_var.set(str(default_ollama["timeout_seconds"]))
        refresh_dirty_state()
        if dirty_state["dirty"]:
            set_status(
                t(
                    "Przywrócono domyślne wartości obsługiwanych ustawień. "
                    "Zapisz i uruchom ponownie, aby je zachować.",
                    "Supported settings were reset to defaults. Save and restart "
                    "to keep them.",
                ),
                "warning",
            )
        else:
            set_status(
                t(
                    "Wszystkie obsługiwane ustawienia mają już wartości domyślne.",
                    "All supported settings are already at their defaults.",
                )
            )

    def confirm_restore_defaults() -> None:
        prompt = t(
            "Przywrócić wszystkie obsługiwane ustawienia, również zaawansowane? "
            "Nic nie zostanie zapisane, dopóki nie wybierzesz „Zapisz i uruchom ponownie”.",
            "Reset every supported setting, including advanced options? Nothing is saved "
            "until you choose “Save and restart”.",
        )
        if not custom_commands_schema_is_supported:
            prompt += t(
                " Nieobsługiwana sekcja własnych komend pozostanie bez zmian.",
                " The unsupported custom-command section will remain unchanged.",
            )
        if not messagebox.askyesno(
            t(
                "{app} — przywróć wszystkie ustawienia",
                "{app} — Reset all settings",
                app=APP_DISPLAY_NAME,
            ),
            prompt,
            parent=root,
        ):
            return
        restore_defaults()

    def close_window() -> None:
        if dirty_state["dirty"] and not messagebox.askyesno(
            t(
                "{app} — niezapisane zmiany",
                "{app} — Unsaved changes",
                app=APP_DISPLAY_NAME,
            ),
            t(
                "Zamknąć okno i odrzucić niezapisane zmiany?",
                "Close the window and discard unsaved changes?",
            ),
            parent=root,
        ):
            return
        root.destroy()

    ttk.Button(
        footer,
        text=t("Przywróć wszystko…", "Reset all…"),
        command=confirm_restore_defaults,
    ).grid(row=0, column=2, padx=px(4))
    ttk.Button(
        footer,
        text=t("Zamknij", "Close"),
        command=close_window,
    ).grid(
        row=0, column=3, padx=px(4)
    )
    apply_button = ttk.Button(
        footer,
        text=t("Zapisz i uruchom ponownie", "Save and restart"),
        style="Primary.TButton",
        command=lambda: save_from_window(True),
        state="disabled",
    )
    apply_button.grid(row=0, column=4, padx=(px(4), 0))

    root.bind("<Control-s>", lambda event: save_from_window(False))
    root.bind(
        "<Control-Return>",
        lambda event: (
            save_from_window(True)
            if dirty_state["dirty"] or restart_pending["value"]
            else None
        ),
    )
    root.bind("<Escape>", lambda event: close_window())
    root.protocol("WM_DELETE_WINDOW", close_window)
    show_page("start")
    refresh_dirty_state()
    update_status_indicator()
    try:
        root.mainloop()
    finally:
        # Zamknięcie okna każdą drogą musi zwolnić podsłuchiwane wejście.
        microphone_level_monitor.stop()
    return 0


def preferred_default_input_device() -> tuple[Optional[int], Any]:
    """Prefer the Windows WASAPI default endpoint over PortAudio's MME default."""

    devices = tuple(sd.query_devices())
    host_apis = tuple(sd.query_hostapis())
    for host_api in host_apis:
        if "wasapi" not in str(host_api.get("name", "")).casefold():
            continue
        index = host_api.get("default_input_device")
        if type(index) is not int or not 0 <= index < len(devices):
            continue
        device_info = devices[index]
        try:
            if int(device_info.get("max_input_channels", 0)) > 0:
                return index, device_info
        except (TypeError, ValueError):
            continue
    return None, sd.query_devices(None, "input")


class ContinuousRecorder:
    _RECOVERY_RETRY_SECONDS = 2.0
    _RECOVERY_ERROR_AFTER_ATTEMPTS = 3
    _RECOVERY_ERROR_AFTER_SECONDS = 4.0

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = SAMPLE_RATE
        self.device_selector = copy.deepcopy(config.get("microphone"))
        self.device: Optional[int] = None
        self.pre_roll_samples = 0
        self.maximum_recording_samples = 0
        self._set_sample_rate(SAMPLE_RATE)
        self._lock = threading.Lock()
        self._ring: deque[np.ndarray] = deque()
        self._ring_samples = 0
        self._recording = False
        self._recorded: list[np.ndarray] = []
        self._recording_samples = 0
        self._released_recording_samples: Optional[int] = None
        self._meter_level = 0.0
        self.last_recording_samples = 0
        self.recording_limit_reached = threading.Event()
        self._audio_status_count = 0
        self._idle_audio_status_count = 0
        self._stream_lock = threading.RLock()
        self._stream_finished = threading.Event()
        self._next_recovery_at = 0.0
        self._last_working_stream_attempt: Optional[tuple[str, int, str]] = None
        self._recovery_failure_count = 0
        self._recovery_first_failure_at: Optional[float] = None
        self._recovery_error_reported = False
        self._stream_monitoring_enabled = False
        self._stream: Optional[sd.InputStream] = None

    def _set_sample_rate(self, sample_rate: int) -> None:
        self.sample_rate = min(
            MAX_CAPTURE_SAMPLE_RATE,
            max(8_000, int(round(sample_rate))),
        )
        self.pre_roll_samples = int(
            self.sample_rate
            * max(0, int(self.config.get("pre_roll_ms", 300)))
            / 1000
        )
        maximum_seconds = int(
            self.config.get(
                "maximum_recording_seconds",
                DEFAULT_MAXIMUM_RECORDING_SECONDS,
            )
        )
        maximum_seconds = max(1, min(MAXIMUM_RECORDING_SECONDS, maximum_seconds))
        requested_samples = self.sample_rate * maximum_seconds
        # Rezerwa obejmuje surowe bloki, wynik concatenate oraz największy
        # tymczasowy bufor normalizacji/RMS. Sam limit listy bloków nie chronił
        # wcześniej procesu przed 2-3-krotnym skokiem pamięci po puszczeniu klawisza.
        memory_safe_total_samples = MAX_RECORDING_BUFFER_BYTES // (
            np.dtype(np.float32).itemsize * MAX_RECORDING_PIPELINE_BUFFERS
        )
        memory_safe_recording_samples = max(
            1,
            memory_safe_total_samples - self.pre_roll_samples,
        )
        self.maximum_recording_samples = min(
            requested_samples,
            memory_safe_recording_samples,
        )
        self.maximum_recording_seconds = max(
            1.0,
            self.maximum_recording_samples / self.sample_rate,
        )
        if self.maximum_recording_samples < requested_samples:
            logging.warning(
                "Limit nagrania skrócono do %.1f s, aby szczytowe użycie "
                "buforów nie przekroczyło %d MiB",
                self.maximum_recording_seconds,
                MAX_RECORDING_BUFFER_BYTES // (1024 * 1024),
            )

    def _reset_stream_audio_state(self) -> None:
        """Drop samples and meter state that belong to the previous stream."""

        with self._lock:
            self._ring.clear()
            self._ring_samples = 0
            self._meter_level = 0.0

    def _stream_finished_callback(self) -> None:
        self._stream_finished.set()

    def _open_stream(self, sample_rate: int, latency: str) -> sd.InputStream:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            blocksize=0,
            latency=latency,
            callback=self._callback,
            finished_callback=self._stream_finished_callback,
        )
        try:
            stream.start()
        except Exception:
            try:
                stream.close(ignore_errors=False)
            except Exception:
                logging.exception(
                    "Błąd zamykania mikrofonu po nieudanym uruchomieniu"
                )
            raise
        return stream

    def start(self) -> None:
        with self._stream_lock:
            self._start_unlocked()
            self._stream_monitoring_enabled = True

    def _start_unlocked(self) -> None:
        translator = Translator.from_config(self.config)
        attempts: list[tuple[str, str, Optional[int]]] = []
        if self.device_selector is None:
            try:
                self.device, device_info = preferred_default_input_device()
                default_rate = int(
                    round(float(device_info["default_samplerate"]))
                )
            except Exception:
                self.device = None
                default_rate = SAMPLE_RATE
            attempts.extend(
                [
                    ("default", "high", default_rate),
                    ("default", "low", default_rate),
                ]
            )
            if default_rate != SAMPLE_RATE:
                attempts.append(("default", "high", SAMPLE_RATE))
        else:
            _, _, original_identity = (
                _resolve_runtime_microphone_with_identity(
                    self.device_selector,
                    translator,
                    prefer_wasapi=False,
                )
            )
            # A legacy index is meaningful only for the snapshot in which it
            # was resolved. Pin it to that device's identity before any retry
            # so hot-plug cannot redirect recording to a different microphone.
            if type(self.device_selector) is int:
                self.device_selector = original_identity
            self.device, _, preferred_identity = (
                _resolve_runtime_microphone_with_identity(
                    self.device_selector,
                    translator,
                    prefer_wasapi=True,
                )
            )
            attempts.extend(
                [
                    ("preferred", "high", None),
                    ("preferred", "low", None),
                ]
            )
            original_selector = audio_devices.parse_microphone_selector(
                original_identity
            )
            preferred_selector = audio_devices.parse_microphone_selector(
                preferred_identity
            )
            if audio_devices.microphone_identity_key(
                original_selector
            ) != audio_devices.microphone_identity_key(preferred_selector):
                attempts.extend(
                    [
                        ("original", "high", None),
                        ("original", "low", None),
                    ]
                )

        cached_attempt = self._last_working_stream_attempt
        allowed_cached_endpoints = (
            {"default"}
            if self.device_selector is None
            else {"preferred", "original"}
        )
        if (
            cached_attempt is not None
            and cached_attempt[0] in allowed_cached_endpoints
        ):
            # A native MME fallback that already worked should be the first
            # recovery attempt instead of repeatedly probing broken WASAPI.
            cached_endpoint, cached_rate, cached_latency = cached_attempt
            attempts.insert(
                0,
                (cached_endpoint, cached_latency, cached_rate),
            )

        last_error: Optional[Exception] = None
        seen: set[tuple[tuple[Any, ...], int, str]] = set()
        for endpoint, latency, rate_override in attempts:
            try:
                sample_rate = rate_override
                resolved_identity: Optional[dict[str, Any]] = None
                if self.device_selector is not None:
                    # Resolve immediately before every attempt.  The fallback
                    # deliberately returns to the exact saved endpoint rather
                    # than whichever numeric PortAudio index is now present.
                    self.device, device_info, resolved_identity = (
                        _resolve_runtime_microphone_with_identity(
                            self.device_selector,
                            translator,
                            prefer_wasapi=endpoint == "preferred",
                        )
                    )
                    if sample_rate is None:
                        sample_rate = int(
                            round(float(device_info["default_samplerate"]))
                        )
                if sample_rate is None:
                    raise ValueError("missing sample rate")
                if resolved_identity is None:
                    endpoint_key: tuple[Any, ...] = ("default", self.device)
                else:
                    endpoint_key = audio_devices.microphone_identity_key(
                        audio_devices.parse_microphone_selector(resolved_identity)
                    )
                attempt_key = (endpoint_key, sample_rate, latency)
                if attempt_key in seen:
                    continue
                seen.add(attempt_key)
                self._set_sample_rate(sample_rate)
                sample_rate = self.sample_rate
                self._stream_finished.clear()
                self._stream = self._open_stream(sample_rate, latency)
                self._last_working_stream_attempt = (
                    endpoint,
                    sample_rate,
                    latency,
                )
                self._reset_stream_audio_state()
                logging.info(
                    "Mikrofon uruchomiony: %s, %d Hz, latency=%s, endpoint=%s",
                    self.device if self.device is not None else "domyślny",
                    sample_rate,
                    latency,
                    endpoint,
                )
                return
            except Exception as exc:
                last_error = exc
                # Individual endpoint failures are diagnostics.  In
                # particular, a broken optional WASAPI equivalent is expected
                # to fall back to the exact saved endpoint without producing
                # warnings on every otherwise-successful startup.
                logging.info(
                    "Nie udało się otworzyć mikrofonu (%s, %s Hz, %s): %s",
                    endpoint,
                    sample_rate if sample_rate is not None else "?",
                    latency,
                    exc,
                )

        raise AppError(
            translator.t(
                "Nie udało się otworzyć mikrofonu: {error}",
                "Could not open the microphone: {error}",
                error=last_error,
            )
        )

    def ensure_stream_alive(self) -> bool:
        """Recover an idle stream after a driver reset or device hot-plug."""

        if not self._stream_monitoring_enabled:
            return True
        with self._lock:
            if self._recording:
                return True
        with self._stream_lock:
            # ``close`` may have disabled monitoring while this thread waited
            # for the stream lock.  Never resurrect audio during shutdown.
            if not self._stream_monitoring_enabled:
                return True
            stream = self._stream
            try:
                active = bool(stream is not None and stream.active)
            except Exception:
                active = False
            if active and not self._stream_finished.is_set():
                return True
            now = time.monotonic()
            if now < self._next_recovery_at:
                return False
            self._next_recovery_at = now + self._RECOVERY_RETRY_SECONDS
            self._close_stream_unlocked(stream)
            self._stream = None
            try:
                self._start_unlocked()
            except Exception as exc:
                if self._recovery_first_failure_at is None:
                    self._recovery_first_failure_at = now
                self._recovery_failure_count += 1
                elapsed = max(0.0, now - self._recovery_first_failure_at)
                exhausted_budget = (
                    self._recovery_failure_count
                    >= self._RECOVERY_ERROR_AFTER_ATTEMPTS
                    and elapsed >= self._RECOVERY_ERROR_AFTER_SECONDS
                )
                if exhausted_budget and not self._recovery_error_reported:
                    self._recovery_error_reported = True
                    logging.exception(
                        "Nie udało się ponownie otworzyć mikrofonu po %d próbach",
                        self._recovery_failure_count,
                    )
                else:
                    logging.warning(
                        "Przejściowo nie udało się ponownie otworzyć mikrofonu "
                        "(próba %d): %s",
                        self._recovery_failure_count,
                        exc,
                    )
                return False
            self._next_recovery_at = 0.0
            self._recovery_failure_count = 0
            self._recovery_first_failure_at = None
            self._recovery_error_reported = False
            logging.info("Połączenie z mikrofonem zostało przywrócone")
            return True

    @staticmethod
    def _close_stream_unlocked(stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.stop(ignore_errors=False)
        except Exception:
            logging.exception("Błąd zatrzymywania mikrofonu")
        try:
            stream.close(ignore_errors=False)
        except Exception:
            logging.exception("Błąd zamykania mikrofonu")

    def close(self) -> None:
        with self._stream_lock:
            self._stream_monitoring_enabled = False
            stream = self._stream
            self._stream = None
            self._close_stream_unlocked(stream)

    def abort(self) -> None:
        """Discard a capture without concatenating a potentially large buffer."""

        with self._lock:
            self._recording = False
            self._recorded = []
            self._recording_samples = 0
            self._released_recording_samples = None
            self.last_recording_samples = 0
            self._audio_status_count = 0
            self.recording_limit_reached.clear()

    def _callback(self, indata, frames, time_info, status) -> None:
        chunk = np.asarray(indata[:, 0], dtype=np.float32).copy()
        with self._lock:
            if self._recording:
                if chunk.size:
                    peak = max(
                        abs(float(np.min(chunk))),
                        abs(float(np.max(chunk))),
                    )
                    visual_level = min(1.0, math.sqrt(max(0.0, peak)))
                    self._meter_level = max(
                        visual_level,
                        self._meter_level * 0.72,
                    )
                if status:
                    self._audio_status_count += 1
                remaining = max(
                    0,
                    self.maximum_recording_samples - self._recording_samples,
                )
                if remaining:
                    captured = chunk[:remaining]
                    self._recorded.append(captured)
                    self._recording_samples += len(captured)
                if self._recording_samples >= self.maximum_recording_samples:
                    self.recording_limit_reached.set()
            elif status:
                self._idle_audio_status_count += 1
            # Ring zawsze opisuje najnowszy dźwięk. Gdyby zatrzymać go na czas
            # nagrania, szybkie kolejne naciśnięcie dostałoby pre-roll sprzed
            # poprzedniej wypowiedzi zamiast jej rzeczywistego końca.
            self._ring.append(chunk)
            self._ring_samples += len(chunk)
            while self._ring and self._ring_samples > self.pre_roll_samples:
                excess = self._ring_samples - self.pre_roll_samples
                oldest = self._ring[0]
                if len(oldest) <= excess:
                    self._ring.popleft()
                    self._ring_samples -= len(oldest)
                else:
                    # Zachowaj dokładnie zadany pre-roll zamiast tracić cały
                    # blok (1024 próbki to aż 64 ms przy 16 kHz).
                    self._ring[0] = oldest[excess:].copy()
                    self._ring_samples -= excess

    def log_pending_audio_statuses(self) -> None:
        """Move idle PortAudio diagnostics out of its real-time callback."""

        with self._lock:
            count = self._idle_audio_status_count
            self._idle_audio_status_count = 0
        if count:
            logging.warning(
                "PortAudio zgłosiło problemy wejścia poza nagraniem: %d",
                count,
            )

    def begin(self) -> None:
        if not self.ensure_stream_alive():
            # Ten komunikat trafia wprost na pasek statusu i do powiadomienia,
            # więc musi być przetłumaczony i mówić, co zrobić.
            raise AppError(
                Translator.from_config(self.config).t(
                    "Mikrofon jest niedostępny — sprawdź podłączenie.",
                    "The microphone is unavailable — check the connection.",
                )
            )
        with self._lock:
            if self._recording:
                return
            self._recorded = [part.copy() for part in self._ring]
            self._recording_samples = 0
            self._released_recording_samples = None
            self._meter_level = 0.0
            self._audio_status_count = 0
            self.recording_limit_reached.clear()
            self._recording = True

    def current_level(self) -> float:
        """Zwróć wygładzony poziom nagrania dla lekkiej animacji kapsuły."""

        with self._lock:
            return min(1.0, max(0.0, float(self._meter_level)))

    def mark_release(self) -> None:
        """Snapshot samples captured while the shortcut was physically held."""

        with self._lock:
            if self._recording and self._released_recording_samples is None:
                self._released_recording_samples = self._recording_samples

    def finish(self) -> np.ndarray:
        audio_status_count = 0
        with self._lock:
            if not self._recording:
                self.last_recording_samples = 0
                self._released_recording_samples = None
                return np.empty(0, dtype=np.float32)
            self._recording = False
            parts = self._recorded
            self._recorded = []
            self.last_recording_samples = (
                self._recording_samples
                if self._released_recording_samples is None
                else self._released_recording_samples
            )
            self._recording_samples = 0
            self._released_recording_samples = None
            self._meter_level = 0.0
            audio_status_count = self._audio_status_count
            self._audio_status_count = 0
        if audio_status_count:
            logging.warning(
                "PortAudio zgłosiło problemy wejścia podczas nagrania: %d",
                audio_status_count,
            )
        if not parts:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(parts).astype(np.float32, copy=False)


class MowikApp:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.translator = Translator.from_config(config)
        self.trigger_type, self.trigger_name = split_trigger(
            str(config["trigger"]),
            self.translator,
        )
        self._custom_command_registry = command_engine.CommandRegistry.from_config(
            config
        )
        self._custom_command_lookup = custom_command_lookup(config)
        self.command_trigger_type: Optional[str] = None
        self.command_trigger_name: Optional[str] = None
        custom_settings = config.get("custom_commands", {})
        custom_enabled = bool(
            isinstance(custom_settings, dict)
            and custom_settings.get("enabled", False) is True
            and self._custom_command_registry.definitions
        )
        if custom_enabled:
            try:
                command_trigger = split_trigger(
                    str(custom_settings.get("trigger", "keyboard:f7")),
                    self.translator,
                )
                if command_trigger == (self.trigger_type, self.trigger_name):
                    logging.error(
                        "Wyłączono własne komendy: skrót koliduje z dyktowaniem"
                    )
                else:
                    self.command_trigger_type, self.command_trigger_name = (
                        command_trigger
                    )
            except AppError:
                # Ręcznie uszkodzona opcjonalna sekcja nie może wyłączyć F8.
                logging.exception("Wyłączono własne komendy: nieprawidłowy skrót")
        self.process_elevated = windows_actions.is_process_elevated()
        self.stop_event = threading.Event()
        self.model_ready = threading.Event()
        self._startup_failure_lock = threading.Lock()
        self._startup_failure: Optional[str] = None
        self._startup_failure_notified_at: Optional[float] = None
        self.busy_lock = threading.Lock()
        self.busy = False
        self._input_lock = threading.Lock()
        self._pressed_inputs: set[tuple[str, str]] = set()
        self._active_input: Optional[tuple[tuple[str, str], str]] = None
        self._command_context_lock = threading.Lock()
        self._pending_command_context: Optional[
            command_engine.ExecutionContext
        ] = None
        self._pending_command_context_ready: Optional[threading.Event] = None
        self._explorer_context_slots = threading.BoundedSemaphore(
            MAX_PENDING_EXPLORER_CONTEXTS
        )
        self.key_down = False
        self.capture_active = False
        self.capture_mode: Optional[str] = None
        self._capture_generation = 0
        self._capture_timer: Optional[RecordingDeadlineTimer] = None
        self._release_started_at: Optional[float] = None
        self.model: Optional[WhisperModel] = None
        self.model_name = ""
        self.model_device = ""
        self.recorder: Optional[ContinuousRecorder] = None
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None
        self.jobs: queue.Queue[Optional[SpeechJob | np.ndarray]] = queue.Queue()
        self.tray: Optional[pystray.Icon] = None
        self.status = self.translator.t("Uruchamianie…", "Starting…")
        self.tray_state = "idle"
        self._status_lock = threading.Lock()
        feedback = config.get("feedback", {})
        self.dictation_indicator = FloatingStatusIndicator(
            bool(feedback.get("floating_indicator", True)),
            self.translator,
        )
        self._restart_lock = threading.Lock()
        self._restart_started = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_owner_ident: Optional[int] = None
        self._shutdown_complete = threading.Event()
        self._microphone_state_lock = threading.Lock()
        self._microphone_unavailable = False
        # Request zapisany przez poprzedni proces nie może zrestartować
        # świeżo uruchomionej aplikacji. Próg pochodzi z początku procesu,
        # więc obejmuje także request wysłany tuż po utworzeniu mutexu.
        self._restart_requests_not_before_ns = PROCESS_STARTED_AT_NS
        self.worker = threading.Thread(
            target=self._job_worker, name="TranscriptionWorker", daemon=True
        )
        self.control_worker = threading.Thread(
            target=self._control_watcher, name="ControlWatcher", daemon=True
        )

    def _error_notification(self) -> str:
        return self.translator.t(
            "Szczegóły zapisano w logu: {path}",
            "Details were saved to the log: {path}",
            path=LOG_PATH,
        )

    def _failure_reason(self, error: BaseException) -> Optional[str]:
        """Zwróć wyłącznie oczyszczony opis z AppError, nigdy surowego wyjątku."""

        if isinstance(error, AppError):
            reason = str(error).strip()
            if reason:
                return reason
        return None

    def _failure_notification(self, error: BaseException) -> str:
        """Powiedz, co się stało; sam odsyłacz do logu kazał użytkownikowi zgadywać."""

        reason = self._failure_reason(error) or self.translator.t(
            "Powód techniczny: {error}.",
            "Technical reason: {error}.",
            error=type(error).__name__,
        )
        return f"{reason} {self._error_notification()}"

    def _describe_startup_failure(self, error: BaseException) -> str:
        """Nazwij powód po imieniu; sam log nie mówi użytkownikowi nic na żywo."""

        return self._failure_reason(error) or self.translator.t(
            "Nie udało się uruchomić modelu ({error}).",
            "The model failed to start ({error}).",
            error=type(error).__name__,
        )

    def _remember_startup_failure(self, reason: Optional[str]) -> None:
        with self._startup_failure_lock:
            self._startup_failure = reason
            self._startup_failure_notified_at = None

    def _startup_failure_message(self) -> tuple[Optional[str], bool]:
        """Zwróć powód i informację, czy wypada znowu pokazać powiadomienie."""

        now = time.monotonic()
        with self._startup_failure_lock:
            reason = self._startup_failure
            if reason is None:
                return None, False
            last = self._startup_failure_notified_at
            if last is not None and now - last < STARTUP_FAILURE_NOTIFY_INTERVAL:
                return reason, False
            self._startup_failure_notified_at = now
            return reason, True

    def _model_status(self, status: str) -> None:
        if not self.stop_event.is_set():
            self.set_status(status, state="processing")

    def _command_mode_enabled(self) -> bool:
        return bool(
            self.command_trigger_type
            and self.command_trigger_name
            and self._custom_command_registry.definitions
        )

    def _ready_status(self) -> str:
        dictation_label = trigger_display_name(
            str(self.config["trigger"]),
            self.translator,
        )
        if not self._command_mode_enabled():
            status = self.translator.t(
                "Gotowy — {trigger_label}",
                "Ready — {trigger_label}",
                trigger_label=dictation_label,
            )
        else:
            command_trigger = self.config.get("custom_commands", {}).get(
                "trigger", "keyboard:f7"
            )
            command_label = trigger_display_name(
                str(command_trigger),
                self.translator,
            )
            status = self.translator.t(
                "Gotowy — dyktowanie: {dictation} · komendy: {commands}",
                "Ready — dictation: {dictation} · commands: {commands}",
                dictation=dictation_label,
                commands=command_label,
            )
        requested_limit = float(
            self.config.get(
                "maximum_recording_seconds",
                DEFAULT_MAXIMUM_RECORDING_SECONDS,
            )
        )
        effective_limit = getattr(
            self.recorder, "maximum_recording_seconds", requested_limit
        )
        if (
            isinstance(effective_limit, (int, float))
            and float(effective_limit) + 0.001 < requested_limit
        ):
            status += self.translator.t(
                " · limit nagrania: {seconds} s",
                " · recording limit: {seconds} s",
                seconds=max(1, int(float(effective_limit))),
            )
        return status

    def start(self) -> None:
        self.worker.start()
        self.control_worker.start()
        self._start_listeners()
        threading.Thread(
            target=self._load_runtime, name="ModelLoader", daemon=True
        ).start()

    def _control_watcher(self) -> None:
        while not self.stop_event.wait(0.35):
            try:
                request_text = take_fresh_restart_request(
                    self._restart_requests_not_before_ns
                )
                if request_text is None:
                    recorder = self.recorder
                    if recorder is not None:
                        recorder.log_pending_audio_statuses()
                        if not recorder.ensure_stream_alive():
                            with self._microphone_state_lock:
                                if self._microphone_unavailable:
                                    continue
                                self._microphone_unavailable = True
                                self.set_status(
                                    self.translator.t(
                                        "Mikrofon został odłączony — próbuję połączyć ponownie…",
                                        "Microphone disconnected — reconnecting…",
                                    ),
                                    state="processing",
                                )
                        else:
                            with self._microphone_state_lock:
                                if not self._microphone_unavailable:
                                    continue
                                self._microphone_unavailable = False
                                if self.model_ready.is_set():
                                    self.set_status(
                                        self._ready_status(),
                                        state="ready",
                                    )
                    continue
                logging.info("Odebrano prośbę o restart ustawień: %s", request_text)
                self.set_status(
                    self.translator.t(
                        "Stosuję nowe ustawienia…",
                        "Applying new settings…",
                    ),
                    state="processing",
                )
                self.restart(restart_request=request_text)
                return
            except Exception:
                logging.exception("Nie udało się obsłużyć prośby o restart")
                # Żądanie restartu zostało już skonsumowane z dysku, więc nikt
                # go nie powtórzy. Bez tego status zostawał na "Stosuję nowe
                # ustawienia…" na zawsze, a użytkownik był przekonany, że
                # zmiany weszły — mimo że proces działa na starej konfiguracji.
                try:
                    self.set_status(
                        self.translator.t(
                            "Nie udało się zastosować ustawień — uruchom "
                            "Mówika ponownie ręcznie.",
                            "Could not apply the settings — please restart "
                            "Mówik manually.",
                        ),
                        error=True,
                    )
                except Exception:
                    logging.exception(
                        "Nie udało się zgłosić nieudanego restartu"
                    )

    @staticmethod
    def _ignore_own_injected_keys(msg: Any, data: Any) -> bool:
        """Odrzuć w hooku zdarzenia, które Mówik sam przed chwilą wstrzyknął.

        Zwrócenie ``False`` pomija `PostThreadMessage` i całe przetwarzanie po
        stronie Pythona, ale **nie** tłumi zdarzenia — znak normalnie dociera do
        aplikacji docelowej. Skraca to procedurę hooka przy każdym wpisywanym
        znaku, co jest istotne podwójnie: przyspiesza wpisywanie i zmniejsza
        ryzyko, że Windows usunie hooka za przekroczenie LowLevelHooksTimeout
        (objawiające się jako „skrót nagle przestał działać do restartu").
        """

        try:
            if data.dwExtraInfo == MOWIK_INJECTED_INPUT_SIGNATURE:
                return False
        except AttributeError:
            # Starsze/inne wydania pynput mogą nie eksponować dwExtraInfo —
            # wtedy po prostu zachowujemy się jak dotąd.
            pass
        return True

    def _start_listeners(self) -> None:
        self.keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
            win32_event_filter=self._ignore_own_injected_keys,
        )
        self.keyboard_listener.start()
        self.mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
        self.mouse_listener.start()

    def _load_runtime(self) -> None:
        recorder: Optional[ContinuousRecorder] = None
        recorder_published = False
        try:
            self.set_status(
                self.translator.t(
                    "Przygotowuję mikrofon…",
                    "Preparing the microphone…",
                ),
                state="processing",
            )
            recorder = ContinuousRecorder(self.config)
            recorder.start()
            with self._shutdown_lock:
                cancelled = self._shutdown_started or self.stop_event.is_set()
                if not cancelled:
                    self.recorder = recorder
                    recorder_published = True
                    self.dictation_indicator.set_level_source(
                        recorder.current_level
                    )
            if cancelled:
                recorder.close()
                return
            model, model_name, device = create_model(
                self.config,
                self._model_status,
            )
            should_close = False
            with self._shutdown_lock:
                cancelled = self._shutdown_started or self.stop_event.is_set()
                if cancelled:
                    # Decyzja o zamknięciu musi paść pod tym samym zamkiem, pod
                    # którym zerujemy pole. Wcześniej pole było zerowane tutaj,
                    # a warunek zamknięcia sprawdzany w osobnym bloku — gdzie
                    # nie mógł już zajść, bo pole było puste. Efekt: gdy
                    # shutdown() ustawił flagę, ale nie przejął jeszcze
                    # recordera, strumień mikrofonu zostawał otwarty na stałe.
                    if self.recorder is recorder:
                        self.recorder = None
                        should_close = True
                    elif not recorder_published:
                        should_close = True
                else:
                    self.model = model
                    self.model_name = model_name
                    self.model_device = device
                    self.model_ready.set()
                    self._remember_startup_failure(None)
            if cancelled:
                if should_close:
                    recorder.close()
                return
            dictation_label = trigger_display_name(
                str(self.config["trigger"]),
                self.translator,
            )
            instruction = self.translator.t(
                "Trzymaj {trigger_label} i mów.",
                "Hold {trigger_label} and speak.",
                trigger_label=dictation_label,
            )
            if self._command_mode_enabled():
                command_label = trigger_display_name(
                    str(
                        self.config.get("custom_commands", {}).get(
                            "trigger", "keyboard:f7"
                        )
                    ),
                    self.translator,
                )
                instruction = self.translator.t(
                    "Dyktowanie: {dictation}. Własne komendy: {commands}.",
                    "Dictation: {dictation}. Custom commands: {commands}.",
                    dictation=dictation_label,
                    commands=command_label,
                )
            if not self.stop_event.is_set():
                with self._microphone_state_lock:
                    if self._microphone_unavailable:
                        self.set_status(
                            self.translator.t(
                                "Mikrofon został odłączony — próbuję połączyć ponownie…",
                                "Microphone disconnected — reconnecting…",
                            ),
                            state="processing",
                        )
                    else:
                        self.set_status(
                            self._ready_status(),
                            notify=self.translator.t(
                                "Model {model_name} działa na {device}. {instruction}",
                                "Model {model_name} is running on {device}. {instruction}",
                                model_name=model_name,
                                device=device,
                                instruction=instruction,
                            ),
                            state="ready",
                        )
        except Exception as exc:
            logging.exception("Błąd inicjalizacji")
            self.model_ready.clear()
            self._remember_startup_failure(self._describe_startup_failure(exc))
            if recorder is not None:
                should_close = False
                with self._shutdown_lock:
                    if self.recorder is recorder:
                        self.recorder = None
                        should_close = True
                    elif not recorder_published:
                        should_close = True
                if should_close:
                    try:
                        recorder.close()
                    except Exception:
                        logging.exception("Nie udało się zamknąć mikrofonu po błędzie")
            if self.stop_event.is_set():
                return
            reason, _ = self._startup_failure_message()
            self.set_status(
                reason or self.translator.t("Błąd uruchomienia", "Startup error"),
                notify=" ".join(
                    part for part in (reason, self._error_notification()) if part
                ),
                error=True,
                state="error",
            )

    def _on_key_press(self, key) -> None:
        self._handle_input_event("keyboard", key_name(key), True)

    def _on_key_release(self, key) -> None:
        self._handle_input_event("keyboard", key_name(key), False)

    def _on_mouse_click(self, x, y, button, pressed) -> None:
        self._handle_input_event("mouse", mouse_name(button), bool(pressed))

    def _execution_context_from_windows(
        self,
        context: windows_actions.ForegroundContext,
    ) -> command_engine.ExecutionContext:
        return command_engine.ExecutionContext(
            foreground_hwnd=context.hwnd or None,
            foreground_pid=context.pid or None,
            explorer_path=(
                str(context.explorer_path)
                if context.explorer_path is not None
                else None
            ),
            captured_at=context.captured_at_monotonic or time.monotonic(),
            process_elevated=self.process_elevated,
        )

    def _begin_command_context_capture(self) -> None:
        """Zamroź aktywne okno przy F7 i rozwiąż Explorer poza hookiem."""

        identity = windows_actions.capture_foreground_identity()
        ready = threading.Event()
        with self._command_context_lock:
            self._pending_command_context = self._execution_context_from_windows(
                identity
            )
            self._pending_command_context_ready = ready

        if not self._explorer_context_slots.acquire(blocking=False):
            logging.warning(
                "Pominięto rozwiązywanie kontekstu Explorera: limit zadań"
            )
            ready.set()
            return

        def resolve() -> None:
            try:
                resolved = windows_actions.resolve_explorer_context(identity)
                execution_context = self._execution_context_from_windows(resolved)
                with self._command_context_lock:
                    if self._pending_command_context_ready is ready:
                        self._pending_command_context = execution_context
            except Exception:
                # Nie logujemy ścieżki ani szczegółów COM. Brak kontekstu
                # wyłączy tylko akcję "aktywny Explorer".
                logging.warning(
                    "Nie udało się odczytać kontekstu aktywnego Explorera"
                )
            finally:
                ready.set()
                self._explorer_context_slots.release()

        worker = threading.Thread(
            target=resolve,
            name="ExplorerContext",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            # Brak wolnego wątku degraduje się do "brak kontekstu Explorera",
            # co jest obsługiwane. Podniesienie wyjątku ubiłoby cały hook.
            self._explorer_context_slots.release()
            ready.set()
            logging.warning(
                "Nie udało się rozwiązać kontekstu Explorera: brak wątku"
            )

    def _take_command_context(self) -> command_engine.ExecutionContext:
        with self._command_context_lock:
            ready = self._pending_command_context_ready
        if ready is not None:
            ready.wait(1.7)
        with self._command_context_lock:
            context = self._pending_command_context
            self._pending_command_context = None
            self._pending_command_context_ready = None
        if context is not None:
            return context
        return command_engine.ExecutionContext(
            foreground_hwnd=None,
            foreground_pid=None,
            explorer_path=None,
            captured_at=time.monotonic(),
            process_elevated=self.process_elevated,
        )

    def _discard_command_context(self) -> None:
        with self._command_context_lock:
            self._pending_command_context = None
            self._pending_command_context_ready = None

    def _mode_for_input(self, identity: tuple[str, str]) -> Optional[str]:
        if identity == (self.trigger_type, self.trigger_name):
            return "dictation"
        if self._command_mode_enabled() and identity == (
            self.command_trigger_type,
            self.command_trigger_name,
        ):
            return "custom_command"
        return None

    def _handle_input_event(
        self,
        input_type: str,
        name: str,
        pressed: bool,
    ) -> None:
        if self.stop_event.is_set() or not name:
            return
        identity = (input_type, name)
        should_begin: Optional[str] = None
        should_end = False
        with self._input_lock:
            if pressed:
                if identity in self._pressed_inputs:
                    return
                self._pressed_inputs.add(identity)
                mode = self._mode_for_input(identity)
                if mode is not None and self._active_input is None:
                    self._active_input = (identity, mode)
                    self.key_down = True
                    should_begin = mode
            else:
                self._pressed_inputs.discard(identity)
                if self._active_input is not None and self._active_input[0] == identity:
                    self._active_input = None
                    self.key_down = False
                    should_end = True
        # pynput zatrzymuje listenera, gdy callback rzuci wyjątek, a zgłasza go
        # dopiero w join(), którego nikt tu nie woła. Bez tej osłony jedna
        # nieudana próba startu wątku wyłączałaby skrót na stałe, w milczeniu.
        try:
            if should_begin is not None:
                if should_begin == "custom_command":
                    self._begin_command_context_capture()
                self.begin_dictation(should_begin)
                if should_begin == "custom_command" and not self.capture_active:
                    self._discard_command_context()
            elif should_end:
                self.end_dictation()
        except Exception:
            logging.exception("Błąd obsługi skrótu dyktowania")
            if should_begin == "custom_command":
                self._discard_command_context()

    def begin_dictation(self, mode: str = "dictation") -> None:
        if mode not in {"dictation", "custom_command"}:
            raise ValueError(f"Unknown capture mode: {mode}")
        with self._shutdown_lock:
            if self._shutdown_started or self.stop_event.is_set():
                return
            recorder = self.recorder
        if not self.model_ready.is_set() or recorder is None:
            self.dictation_indicator.error()
            self.beep("error")
            reason, may_notify = self._startup_failure_message()
            if reason is None:
                self.set_status(
                    self.translator.t(
                        "Model jeszcze się ładuje — chwilę to potrwa",
                        "The model is still loading — this takes a moment",
                    ),
                    state="idle",
                )
                return
            # Sam sygnał dźwiękowy kazał użytkownikowi zgadywać, co się stało.
            self.set_status(
                reason,
                notify=" ".join(
                    part
                    for part in (
                        reason,
                        self.translator.t(
                            "Dyktowanie nie ruszy, dopóki model się nie wczyta.",
                            "Dictation cannot start until the model loads.",
                        ),
                    )
                    if part
                )
                if may_notify
                else None,
                error=True,
                state="error",
            )
            return
        with self.busy_lock:
            if self.busy:
                self.beep("error")
                self.set_status(
                    self.translator.t(
                        "Kończę poprzednią transkrypcję…",
                        "Finishing the previous transcription…",
                    ),
                    state="processing",
                )
                return
            self.busy = True
        timer: Optional[RecordingDeadlineTimer] = None
        capture_generation: Optional[int] = None

        def rollback_started_capture() -> None:
            nonlocal timer
            if timer is not None:
                timer.cancel()
            with self._input_lock:
                if self._capture_timer is timer:
                    self._capture_timer = None
                if (
                    capture_generation is not None
                    and self._capture_generation == capture_generation
                ):
                    self.capture_active = False
                    self.capture_mode = None
                    self._capture_generation += 1
            abort = getattr(recorder, "abort", None)
            if callable(abort):
                try:
                    abort()
                except Exception:
                    logging.exception("Nie udało się odrzucić przerwanego nagrania")
            self._release_busy()

        try:
            recorder.begin()
            maximum_seconds = int(
                self.config.get(
                    "maximum_recording_seconds",
                    DEFAULT_MAXIMUM_RECORDING_SECONDS,
                )
            )
            recorder_limit = getattr(recorder, "maximum_recording_seconds", None)
            if not isinstance(recorder_limit, (int, float)):
                recorder_limit = maximum_seconds
            timer_delay = max(
                1.0,
                min(
                    float(MAXIMUM_RECORDING_SECONDS),
                    float(maximum_seconds),
                    float(recorder_limit),
                ),
            )
            # Commit the recording only while shutdown is excluded. This lock
            # also keeps status/indicator updates ordered before any cleanup.
            with self._shutdown_lock:
                if self._shutdown_started or self.stop_event.is_set():
                    raise OperationCancelled()
                with self._input_lock:
                    if self.stop_event.is_set():
                        raise OperationCancelled()
                    self.capture_active = True
                    self.capture_mode = mode
                    self._capture_generation += 1
                    capture_generation = self._capture_generation
                    timer = RecordingDeadlineTimer(
                        timer_delay,
                        self._capture_limit_elapsed,
                        (capture_generation,),
                    )
                    self._capture_timer = timer
                    timer.start()
                if mode == "custom_command":
                    self.dictation_indicator.recording(command=True)
                else:
                    self.dictation_indicator.recording()
                self.beep("start")
                self.set_status(
                    self.translator.t(
                        "Słucham komendy…"
                        if mode == "custom_command"
                        else "Nagrywanie…",
                        "Listening for a command…"
                        if mode == "custom_command"
                        else "Recording…",
                    ),
                    state="recording",
                )
        except OperationCancelled:
            rollback_started_capture()
            return
        except Exception as exc:
            rollback_started_capture()
            with self._shutdown_lock:
                if self._shutdown_started or self.stop_event.is_set():
                    return
                self.dictation_indicator.error()
                logging.exception("Nie udało się rozpocząć nagrywania")
                self.set_status(
                    self._failure_reason(exc)
                    or self.translator.t("Błąd nagrywania", "Recording error"),
                    notify=self._failure_notification(exc),
                    error=True,
                    state="error",
                )
                self.beep("error")
            return

    def _cancel_capture_timer(self) -> None:
        # ``_capture_timer`` jest przypisywany pod ``_input_lock``; odczyt bez
        # tego zamka mógł zgubić świeżo utworzony timer przy zamykaniu.
        with self._input_lock:
            timer = self._capture_timer
            self._capture_timer = None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _capture_limit_elapsed(self, generation: int) -> None:
        with self._input_lock:
            if (
                self.stop_event.is_set()
                or generation != self._capture_generation
                or not self.capture_active
            ):
                return
            self._active_input = None
            self.key_down = False
        logging.warning("Nagranie osiągnęło bezpieczny limit czasu")
        self.end_dictation()

    def end_dictation(self) -> None:
        with self._input_lock:
            if not self.capture_active:
                return
            self.capture_active = False
            mode = self.capture_mode or "dictation"
            self.capture_mode = None
            self._capture_generation += 1
        self._cancel_capture_timer()
        released_at = time.perf_counter()
        self._release_started_at = released_at
        recorder = self.recorder
        mark_release = getattr(recorder, "mark_release", None)
        if callable(mark_release):
            # Freeze the sample boundary immediately after the physical release.
            # Foreground/control inspection may cross a process boundary and must
            # never extend the recorded utterance or its minimum-duration check.
            mark_release()
        delivery_foreground: Optional[tuple[int, ...]] = None
        try:
            delivery_foreground = capture_paste_target_identity()
        except Exception:
            # A missing identity must fail closed in paste_text instead of
            # allowing a delayed transcript into whichever window is next.
            logging.warning("Nie udało się przypiąć pola docelowego dyktowania")
            delivery_foreground = (0, 0, 0, 0, 0)
        if mode == "custom_command":
            self.dictation_indicator.processing(command=True)
        else:
            self.dictation_indicator.processing()
        # Kończymy także dłuższy, niezapętlony WAV przypisany do nagrywania.
        self.stop_feedback_sound()
        post_roll_thread = threading.Thread(
            target=self._finish_dictation_safely,
            args=(mode, released_at, delivery_foreground),
            name="PostRoll",
            daemon=True,
        )
        try:
            post_roll_thread.start()
        except Exception as exc:
            logging.exception("Nie udało się uruchomić kończenia nagrania")
            abort = getattr(recorder, "abort", None)
            if callable(abort):
                try:
                    abort()
                except Exception:
                    logging.exception("Nie udało się odrzucić nagrania")
            if mode == "custom_command":
                self._discard_command_context()
            self._release_busy()
            self.dictation_indicator.error()
            self.set_status(
                self._failure_reason(exc)
                or self.translator.t("Błąd nagrywania", "Recording error"),
                notify=self._failure_notification(exc),
                error=True,
                state="error",
            )
            self.beep("error")

    def _finish_dictation_safely(
        self,
        mode: str = "dictation",
        released_at: Optional[float] = None,
        delivery_foreground: Optional[tuple[int, ...]] = None,
    ) -> None:
        try:
            self._finish_dictation_after_tail(
                mode,
                released_at,
                delivery_foreground,
            )
        except Exception as exc:
            logging.exception("Nie udało się zakończyć nagrywania")
            self._release_busy()
            self.dictation_indicator.error()
            self.set_status(
                self._failure_reason(exc)
                or self.translator.t("Błąd nagrywania", "Recording error"),
                notify=self._failure_notification(exc),
                error=True,
                state="error",
            )
            self.beep("error")

    def _finish_dictation_after_tail(
        self,
        mode: str = "dictation",
        released_at: Optional[float] = None,
        delivery_foreground: Optional[tuple[int, ...]] = None,
    ) -> None:
        post_roll = max(0, int(self.config.get("post_roll_ms", 120))) / 1000
        if post_roll:
            time.sleep(post_roll)
        if self.stop_event.is_set():
            if mode == "custom_command":
                self._discard_command_context()
            self._release_busy()
            self.dictation_indicator.hide()
            return
        recorder = self.recorder
        if recorder is None:
            if mode == "custom_command":
                self._discard_command_context()
            self._release_busy()
            self.dictation_indicator.hide()
            return
        capture_sample_rate = int(recorder.sample_rate)
        audio = recorder.finish()
        self.beep("stop")
        recorded_samples = getattr(recorder, "last_recording_samples", None)
        if not isinstance(recorded_samples, int):
            # Zachowaj zgodność z prostymi adapterami/test doubles starszych
            # wersji, które zwracają wyłącznie tablicę audio.
            recorded_samples = len(audio)
        minimum_samples = int(
            capture_sample_rate
            * max(0, int(self.config.get("minimum_recording_ms", 250)))
            / 1000
        )
        if recorded_samples < minimum_samples:
            if mode == "custom_command":
                self._discard_command_context()
            self.dictation_indicator.error()
            self.set_status(
                self.translator.t(
                    "Nagranie było zbyt krótkie",
                    "The recording was too short",
                ),
                state="ready",
            )
            self._release_busy()
            return
        self.set_status(
            self.translator.t(
                "Rozpoznaję komendę…"
                if mode == "custom_command"
                else "Rozpoznaję mowę…",
                "Recognizing command…"
                if mode == "custom_command"
                else "Transcribing…",
            ),
            state="processing",
        )
        execution_context = (
            self._take_command_context()
            if mode == "custom_command"
            else None
        )
        self.jobs.put(
            SpeechJob(
                audio,
                mode,
                released_at,
                execution_context,
                delivery_foreground,
                capture_sample_rate,
            )
        )

    def _job_worker(self) -> None:
        while True:
            try:
                queued_job = self.jobs.get(timeout=0.25)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue
            if queued_job is None:
                self.jobs.task_done()
                break
            if isinstance(queued_job, SpeechJob):
                job = queued_job
            else:
                # Zgodność z kolejką z wersji 2.6 i prostymi integracjami.
                job = SpeechJob(np.asarray(queued_job), "dictation", None)
            manual_multiline_delivery = False
            command_status_handled = False
            try:
                if self.stop_event.is_set():
                    continue
                if job.sample_rate is None:
                    text = self.transcribe(job.audio, mode=job.mode)
                else:
                    text = self.transcribe(
                        job.audio,
                        mode=job.mode,
                        sample_rate=job.sample_rate,
                    )
                if self.stop_event.is_set():
                    continue
                if not text:
                    self.dictation_indicator.error()
                    self.set_status(
                        self.translator.t(
                            "Nie rozpoznałem komendy"
                            if job.mode == "custom_command"
                            else "Nie wykryłem wyraźnej mowy",
                            "No command was recognized"
                            if job.mode == "custom_command"
                            else "No clear speech detected",
                        ),
                        state="ready",
                    )
                    self.beep("error")
                    continue

                if job.mode == "custom_command":
                    outcome = self._deliver_custom_command(
                        text,
                        job.execution_context,
                        job.delivery_foreground,
                    )
                    if not outcome:
                        continue
                    # Komenda mogła zostawić instrukcję dla użytkownika
                    # ("wklej ręcznie przez Ctrl+V"). Bez tego wyjątku
                    # nadpisywaliśmy ją zaraz potem statusem "Gotowy".
                    command_status_handled = outcome == COMMAND_STATUS_HANDLED
                else:
                    self._set_text_delivery_status()
                    if self.stop_event.is_set():
                        continue
                    paste_settings = self.config.get("paste", {})
                    paste_enabled = bool(paste_settings.get("enabled", True))
                    copy_enabled = bool(
                        paste_settings.get("copy_to_clipboard", True)
                    )
                    multiline = "\r" in text or "\n" in text
                    paste_kwargs: dict[str, Any] = {
                        "cancel_event": self.stop_event,
                    }
                    if job.delivery_foreground is not None:
                        paste_kwargs["expected_foreground"] = (
                            job.delivery_foreground
                        )
                    if multiline and copy_enabled:
                        # Także przy wyłączonym auto-wklejaniu: tekst trafia do
                        # schowka, więc użytkownik musi o tym usłyszeć. Wcześniej
                        # ta ścieżka kończyła się statusem "Gotowy", mimo że nic
                        # nie zostało wklejone.
                        windows_set_clipboard_text(text, self.translator)
                        manual_multiline_delivery = True
                    elif multiline and paste_enabled:
                        raise AppError(
                            self.translator.t(
                                "Tekst wielowierszowy wymaga włączonego schowka.",
                                "Multi-line text requires clipboard copying.",
                            )
                        )
                    else:
                        paste_text(text, self.config, **paste_kwargs)
                    logging.info(
                        "Dostarczono tekst (%d znaków; wklejanie=%s; schowek=%s)",
                        len(text),
                        bool(paste_settings.get("enabled", True)),
                        bool(paste_settings.get("copy_to_clipboard", True)),
                    )
                if job.released_at is not None:
                    logging.info(
                        "Latencja release -> wynik (%s): %.3f s",
                        job.mode,
                        time.perf_counter() - job.released_at,
                    )
                if command_status_handled:
                    pass
                elif manual_multiline_delivery:
                    self.set_status(
                        self.translator.t(
                            "Tekst wielowierszowy skopiowano — wklej go ręcznie przez Ctrl+V",
                            "Multi-line text copied — paste it manually with Ctrl+V",
                        ),
                        notify=self.translator.t(
                            "Mówik nie wkleił go automatycznie ze względów bezpieczeństwa.",
                            "Mówik did not paste it automatically for safety.",
                        ),
                        state="ready",
                    )
                else:
                    self.set_status(
                        self._ready_status(),
                        state="ready",
                    )
                if job.mode == "custom_command":
                    self.dictation_indicator.success(command=True)
                else:
                    self.dictation_indicator.success(text)
                self.beep("done")
            except OperationCancelled:
                logging.info("Anulowano dostarczanie tekstu podczas zamykania")
                self.dictation_indicator.hide()
            except Exception as exc:
                logging.exception("Błąd przetwarzania trybu %s", job.mode)
                self.dictation_indicator.error()
                self.set_status(
                    self._failure_reason(exc)
                    or self.translator.t(
                        "Błąd wykonywania komendy"
                        if job.mode == "custom_command"
                        else "Błąd dyktowania",
                        "Command failed"
                        if job.mode == "custom_command"
                        else "Dictation error",
                    ),
                    notify=self._failure_notification(exc),
                    error=True,
                    state="error",
                )
                self.beep("error")
            finally:
                self._release_busy()
                self.jobs.task_done()
                # Bez tego lokalne referencje trzymają bufor audio poprzedniej
                # wypowiedzi (dziesiątki MB) aż do następnego zadania, czyli
                # potencjalnie przez cały czas bezczynności.
                job = None
                queued_job = None

    def _set_text_delivery_status(self) -> None:
        paste_settings = self.config.get("paste", {})
        paste_enabled = bool(paste_settings.get("enabled", True))
        copy_enabled = bool(paste_settings.get("copy_to_clipboard", True))
        if paste_enabled and copy_enabled:
            message = self.translator.t(
                "Wklejam i kopiuję tekst…",
                "Pasting and copying text…",
            )
        elif paste_enabled:
            message = self.translator.t("Wklejam tekst…", "Pasting text…")
        else:
            message = self.translator.t(
                "Kopiuję tekst do schowka…",
                "Copying text to the clipboard…",
            )
        self.set_status(message, state="processing")

    def _deny_custom_command(
        self,
        action: str,
        reason: str,
        message: str,
    ) -> bool:
        """Reject safely without putting a command payload in diagnostics."""

        logging.warning(
            "Zablokowano własną komendę (akcja=%s; powód=%s)",
            action,
            reason,
        )
        self.dictation_indicator.error()
        self.set_status(message, state="ready")
        self.beep("error")
        return False

    def _deliver_custom_command(
        self,
        transcript: str,
        execution_context: Optional[command_engine.ExecutionContext] = None,
        delivery_foreground: Optional[tuple[int, ...]] = None,
    ) -> bool | str:
        """Wykonaj własną komendę.

        Zwraca ``False`` gdy nic nie dostarczono, ``True`` po zwykłym sukcesie
        i ``COMMAND_STATUS_HANDLED`` gdy ustawiono już własny status końcowy,
        którego wywołujący nie powinien nadpisywać.
        """

        status_handled = False
        if self.stop_event.is_set():
            return False
        match = self._custom_command_registry.match(transcript)
        if match is None:
            self.dictation_indicator.error()
            self.set_status(
                self.translator.t(
                    "Nie znaleziono pasującej komendy",
                    "No matching command was found",
                ),
                state="ready",
            )
            self.beep("error")
            return False

        if execution_context is None:
            context = command_engine.ExecutionContext(
                foreground_hwnd=None,
                foreground_pid=None,
                explorer_path=None,
                captured_at=time.monotonic(),
                process_elevated=self.process_elevated,
            )
        else:
            # A captured context may be supplied by a delayed recognition job,
            # but it may never downgrade the process token observed by Mówik.
            context = command_engine.ExecutionContext(
                foreground_hwnd=execution_context.foreground_hwnd,
                foreground_pid=execution_context.foreground_pid,
                explorer_path=execution_context.explorer_path,
                captured_at=execution_context.captured_at,
                process_elevated=(
                    self.process_elevated or execution_context.process_elevated
                ),
            )
        requested_action = match.definition.action
        if context.process_elevated:
            return self._deny_custom_command(
                requested_action,
                "elevated_process_denied",
                self.translator.t(
                    "Zamknij Mówika uruchomionego jako administrator i otwórz go normalnie",
                    "Close the elevated Mówik process and start it normally",
                ),
            )
        context_denial = custom_command_context_denial(
            context,
            require_foreground=(requested_action == "paste_text"),
        )
        if context_denial is not None:
            if context_denial == "stale_command_context":
                message = self.translator.t(
                    "Komenda wygasła — przytrzymaj ponownie przycisk komend",
                    "The command expired — hold the command shortcut again",
                )
            elif context_denial == "command_target_unavailable":
                message = self.translator.t(
                    "Nie można bezpiecznie ustalić okna docelowego",
                    "The target window could not be established safely",
                )
            else:
                message = self.translator.t(
                    "Kontekst komendy jest nieprawidłowy — spróbuj ponownie",
                    "The command context is invalid — try again",
                )
            return self._deny_custom_command(
                requested_action,
                context_denial,
                message,
            )

        target_identity: Optional[tuple[int, ...]] = None
        if requested_action == "paste_text":
            target_identity = delivery_foreground or (
                int(context.foreground_hwnd),
                int(context.foreground_pid),
            )
            if target_identity[:2] != (
                int(context.foreground_hwnd),
                int(context.foreground_pid),
            ):
                return self._deny_custom_command(
                    requested_action,
                    "delivery_target_mismatch",
                    self.translator.t(
                        "Pole docelowe zmieniło się — tekst nie został wklejony",
                        "The target field changed, so the text was not pasted",
                    ),
                )
            if not foreground_identity_matches(target_identity):
                return self._deny_custom_command(
                    requested_action,
                    "foreground_changed",
                    self.translator.t(
                        "Aktywne okno zmieniło się — tekst nie został wklejony",
                        "The active window changed, so the text was not pasted",
                    ),
                )
        plan = command_engine.build_action_plan(match, context)
        if not plan.allowed:
            reason = plan.denial_reason or "action_denied"
            if reason == "explorer_path_unavailable":
                message = self.translator.t(
                    "Nie mogę ustalić folderu aktywnego Eksploratora",
                    "The active File Explorer folder could not be determined",
                )
            elif reason == "elevated_process_denied":
                message = self.translator.t(
                    "Zamknij Mówika uruchomionego jako administrator i otwórz go normalnie",
                    "Close the elevated Mówik process and start it normally",
                )
            else:
                message = self.translator.t(
                    "Komenda została zablokowana ze względów bezpieczeństwa",
                    "The command was blocked for safety",
                )
            logging.warning(
                "Zablokowano własną komendę (akcja=%s; powód=%s)",
                match.definition.action,
                reason,
            )
            self.dictation_indicator.error()
            self.set_status(message, state="ready")
            self.beep("error")
            return False

        action = plan.action
        value = plan.payload
        multiline_paste = action == "paste_text" and (
            "\r" in value or "\n" in value
        )
        if plan.requires_confirmation:
            self.set_status(
                self.translator.t(
                    "Czekam na potwierdzenie akcji…",
                    "Waiting for action confirmation…",
                ),
                state="processing",
            )
            if not confirm_custom_command_action(
                action,
                value,
                self.translator,
            ):
                self.dictation_indicator.hide()
                self.set_status(
                    self.translator.t(
                        "Anulowano akcję komendy",
                        "Command action was cancelled",
                    ),
                    state="ready",
                )
                return False
        elif action == "paste_text":
            self._set_text_delivery_status()
        else:
            self.set_status(
                self.translator.t(
                    "Przygotowuję akcję…",
                    "Preparing action…",
                ),
                state="processing",
            )

        # Zamknięcie Mówika podczas rozpoznawania lub potwierdzenia nie może
        # pozostawić opóźnionej akcji do wykonania.
        if self.stop_event.is_set():
            return False
        context_denial = custom_command_context_denial(
            context,
            require_foreground=(action == "paste_text"),
        )
        if context_denial is not None:
            return self._deny_custom_command(
                action,
                context_denial,
                self.translator.t(
                    "Komenda wygasła lub utraciła bezpieczny kontekst",
                    "The command expired or lost its safe context",
                ),
            )
        if target_identity is not None and not foreground_identity_matches(
            target_identity
        ):
            return self._deny_custom_command(
                action,
                "foreground_changed_after_confirmation",
                self.translator.t(
                    "Aktywne okno zmieniło się — tekst nie został wklejony",
                    "The active window changed, so the text was not pasted",
                ),
            )
        if action == "paste_text" and plan.requires_confirmation and not multiline_paste:
            self._set_text_delivery_status()
        launched_terminal: Optional[windows_actions.TerminalHandle] = None

        def cleanup_launched_terminal() -> None:
            nonlocal launched_terminal
            handle = launched_terminal
            launched_terminal = None
            if handle is None:
                return
            result = windows_actions.terminate_terminal(handle)
            cleanup_complete = result.status == "terminated" or (
                result.status == "already_exited" and handle.host == "console"
            )
            if not cleanup_complete:
                logging.warning(
                    "Nie udało się potwierdzić zamknięcia terminala "
                    "po nieudanej akcji (status=%s, powód=%s)",
                    result.status,
                    result.reason,
                )

        try:
            if action == "paste_text":
                if multiline_paste:
                    paste_settings = self.config.get("paste", {})
                    copy_enabled = bool(
                        isinstance(paste_settings, dict)
                        and paste_settings.get("copy_to_clipboard") is True
                    )
                    if not copy_enabled:
                        return self._deny_custom_command(
                            action,
                            "multiline_clipboard_disabled",
                            self.translator.t(
                                "Włącz kopiowanie do schowka, aby użyć tekstu wielowierszowego",
                                "Enable clipboard copying to use multi-line text",
                            ),
                        )
                    if self.stop_event.is_set():
                        raise OperationCancelled()
                    windows_set_clipboard_text(value, self.translator)
                    self.set_status(
                        self.translator.t(
                            "Tekst wielowierszowy skopiowano — wklej go ręcznie przez Ctrl+V",
                            "Multi-line text copied — paste it manually with Ctrl+V",
                        ),
                        notify=self.translator.t(
                            "Mówik nie wkleił go automatycznie ze względów bezpieczeństwa.",
                            "Mówik did not paste it automatically for safety.",
                        ),
                        state="ready",
                    )
                    status_handled = True
                else:
                    paste_text(
                        value,
                        self.config,
                        append_space_override=False,
                        expected_foreground=target_identity,
                        verify_clipboard_before_paste=True,
                        cancel_event=self.stop_event,
                    )
            elif action == "open":
                open_custom_command_target(value, self.translator)
            elif action == "open_terminal":
                options = plan.terminal_options or command_engine.TerminalOptions()
                windows_context = windows_actions.ForegroundContext(
                    hwnd=context.foreground_hwnd or 0,
                    pid=context.foreground_pid or 0,
                    explorer_path=(
                        Path(context.explorer_path)
                        if context.explorer_path
                        else None
                    ),
                    captured_at_monotonic=context.captured_at,
                )
                directory = windows_actions.resolve_working_directory(
                    options.cwd_source,
                    options.fixed_cwd,
                    windows_context,
                )
                if not directory.ok or directory.path is None:
                    raise AppError("terminal_working_directory_unavailable")
                launched = windows_actions.launch_terminal(
                    options.host,
                    options.shell,
                    directory.path,
                )
                if not launched.ok or launched.handle is None:
                    raise AppError("terminal_launch_failed")
                launched_terminal = launched.handle
                if self.stop_event.is_set():
                    cleanup_launched_terminal()
                    return False
                if value:
                    delivery = windows_actions.deliver_terminal_draft(
                        launched.handle,
                        value,
                    )
                    if self.stop_event.is_set():
                        cleanup_launched_terminal()
                        return False
                    if delivery.status == "copied_only":
                        self.set_status(
                            self.translator.t(
                                "Terminal otwarty — szkic skopiowano do schowka",
                                "Terminal opened — draft copied to the clipboard",
                            ),
                            notify=self.translator.t(
                                "Wklej szkic ręcznie przez Ctrl+V; Enter naciskasz sam.",
                                "Paste the draft with Ctrl+V; you press Enter yourself.",
                            ),
                            state="ready",
                        )
                        status_handled = True
                        launched_terminal = None
                    else:
                        cleanup_launched_terminal()
                        raise AppError("terminal_draft_delivery_failed")
                else:
                    launched_terminal = None
            else:
                raise AppError(
                    self.translator.t(
                        "Nieznany typ własnej komendy.",
                        "Unknown custom-command action.",
                    )
                )
        except OperationCancelled:
            cleanup_launched_terminal()
            raise
        except Exception as exc:
            cleanup_launched_terminal()
            # Nie przekazujemy do logu treści ścieżki, szablonu ani polecenia.
            logging.error(
                "Akcja własnej komendy nie powiodła się (typ=%s, błąd=%s)",
                action,
                type(exc).__name__,
            )
            raise AppError(
                self.translator.t(
                    "Nie udało się wykonać własnej komendy.",
                    "The custom command could not be completed.",
                )
            ) from None
        logging.info(
            "Uruchomiono akcję własnej komendy (akcja=%s; długość=%d)",
            action,
            len(value),
        )
        return COMMAND_STATUS_HANDLED if status_handled else True

    def transcribe(
        self,
        audio: np.ndarray,
        mode: str = "dictation",
        sample_rate: Optional[int] = None,
    ) -> str:
        if mode not in {"dictation", "custom_command"}:
            raise ValueError(f"Unknown transcription mode: {mode}")
        pipeline_started = time.perf_counter()
        model = self.model
        if model is None:
            raise AppError(
                self.translator.t(
                    "Model nie jest załadowany.",
                    "The model is not loaded.",
                )
            )
        audio_array = np.asarray(audio).reshape(-1)
        if (
            audio_array.dtype != np.float32
            or not audio_array.flags.c_contiguous
            or not audio_array.flags.writeable
        ):
            audio_array = np.array(audio_array, dtype=np.float32, order="C", copy=True)
        audio = audio_array
        if audio.size == 0:
            return ""
        np.subtract(audio, float(np.mean(audio, dtype=np.float64)), out=audio)
        np.clip(audio, -1.0, 1.0, out=audio)
        square_sum = 0.0
        for offset in range(0, audio.size, 1_048_576):
            chunk = audio[offset : offset + 1_048_576]
            square_sum += float(np.dot(chunk, chunk))
        rms = math.sqrt(square_sum / audio.size)
        if sample_rate is None:
            sample_rate = (
                self.recorder.sample_rate
                if self.recorder is not None
                else SAMPLE_RATE
            )
        sample_rate = max(1, int(sample_rate))
        audio_duration = len(audio) / sample_rate
        logging.info("Audio: %.2f s, %d Hz, RMS=%.6f", audio_duration, sample_rate, rms)
        if rms < float(self.config.get("minimum_rms", 0.0015)):
            return ""

        audio_input: Any = audio
        if sample_rate != SAMPLE_RATE:
            # faster-whisper/PyAV resampluje plik WAV do 16 kHz w pamięci.
            # Nie zapisujemy nagrania na dysku.
            pcm = np.empty(audio.shape, dtype=np.int16)
            np.multiply(audio, 32767, out=pcm, casting="unsafe")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(memoryview(pcm).cast("B"))
            buffer.seek(0)
            audio_input = buffer

        dictionary_terms = load_dictionary(self.config)
        hotword_terms = list(dictionary_terms)
        if mode == "custom_command":
            hotword_terms = [
                definition.phrase
                for definition in self._custom_command_registry.definitions
            ] + hotword_terms
        prompt_language = resolve_transcription_language(self.config)
        glossary = build_transcription_prompt(hotword_terms, prompt_language)

        vad_settings = self.config.get("vad", {})
        vad_enabled = bool(vad_settings.get("enabled", True))
        vad_parameters = {
            "threshold": float(vad_settings.get("threshold", 0.45)),
            "min_speech_duration_ms": int(
                vad_settings.get("min_speech_duration_ms", 120)
            ),
            "min_silence_duration_ms": int(
                vad_settings.get("min_silence_duration_ms", 250)
            ),
            "speech_pad_ms": int(vad_settings.get("speech_pad_ms", 300)),
        }

        language = prompt_language

        whisper_started = time.perf_counter()
        segments, info = model.transcribe(
            audio_input,
            language=language,
            task="transcribe",
            beam_size=max(1, int(self.config.get("beam_size", 2))),
            # Skalar zamienia się w jednoelementową listę temperatur, przez co
            # `compression_ratio_threshold` wykrywał zapętlenie („że że że"),
            # ustawiał `needs_fallback` — i nie miał na co spaść, więc zepsuty
            # wynik trafiał do użytkownika. Kolejne przebiegi uruchamiają się
            # wyłącznie wtedy, gdy pierwszy i tak był do wyrzucenia, więc w
            # normalnym przypadku pętla przerywa po pierwszym obiegu i nie
            # kosztuje ani milisekundy.
            temperature=[0.0, 0.2, 0.4],
            condition_on_previous_text=False,
            hotwords=glossary or None,
            vad_filter=vad_enabled,
            vad_parameters=vad_parameters if vad_enabled else None,
            # Bez znaczników czasu pętla dekodowania przesuwa okno zawsze o
            # pełne 30 s, więc gdy model skończył zdanie w połowie okna, reszta
            # przepadała: zmierzone 33% utraconego tekstu przy 90 s nagrania i
            # 41% przy 199 s. Ze znacznikami `seek` cofa się do ostatniego
            # timestampu i dokańcza materiał. Dla dyktowań poniżej 30 s (czyli
            # typowego push-to-talk) nie zmienia się nic — to jedno okno.
            without_timestamps=False,
        )
        transcript = "".join(segment.text for segment in segments)
        whisper_elapsed = time.perf_counter() - whisper_started
        # RTF liczony względem surowego nagrania wygląda lepiej, niż jest —
        # przy dyktowaniu z pauzami dekoder dostaje tylko część materiału.
        # Logujemy obie długości, żeby ocena zmian nie była zniekształcona.
        reported_speech = getattr(info, "duration_after_vad", None)
        speech_duration = (
            float(reported_speech)
            if isinstance(reported_speech, (int, float))
            else audio_duration
        )
        logging.info(
            "Whisper: język=%s prawdopodobieństwo=%.3f, znaków=%d, "
            "czas=%.3f s, RTF=%.3f, mowy=%.2f s (RTF mowy=%.3f)",
            getattr(info, "language", "?"),
            float(getattr(info, "language_probability", 0.0)),
            len(transcript),
            whisper_elapsed,
            whisper_elapsed / max(0.001, audio_duration),
            speech_duration,
            whisper_elapsed / max(0.001, speech_duration or audio_duration),
        )
        transcript = strip_whisper_hallucinations(transcript)
        transcript = normalize_transcript(transcript)
        if mode == "custom_command":
            logging.info(
                "Pipeline komendy: whisper=%.3f s, razem=%.3f s",
                whisper_elapsed,
                time.perf_counter() - pipeline_started,
            )
            return transcript
        transcript = apply_voice_commands(transcript, self.config)
        cleanup_started = time.perf_counter()
        transcript = cleanup_with_ollama(
            transcript, self.config, dictionary_terms
        )
        cleanup_elapsed = time.perf_counter() - cleanup_started
        logging.info(
            "Pipeline: whisper=%.3f s, korekta=%.3f s, razem=%.3f s",
            whisper_elapsed,
            cleanup_elapsed,
            time.perf_counter() - pipeline_started,
        )
        return normalize_transcript(transcript)

    def _release_busy(self) -> None:
        with self.busy_lock:
            self.busy = False
            self._release_started_at = None

    def stop_feedback_sound(self) -> None:
        if os.name != "nt":
            return
        try:
            import winsound

            winsound.PlaySound(None, 0)
        except Exception:
            logging.debug("Nie udało się zatrzymać dźwięku", exc_info=True)

    def beep(self, kind: str) -> None:
        feedback = self.config.get("feedback", {})
        if not feedback.get("sounds", True):
            return
        if os.name != "nt":
            return
        custom_path = configured_sound_path(self.config, kind)
        loop = bool(
            kind == "start"
            and custom_path is not None
            and feedback.get("loop_recording_sound", False)
        )

        if custom_path is not None:
            try:
                import winsound

                flags = (
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT
                )
                if loop:
                    flags |= winsound.SND_LOOP
                winsound.PlaySound(str(custom_path), flags)
                return
            except Exception:
                logging.warning(
                    "Nie udało się odtworzyć własnego dźwięku %s; używam tonu.",
                    kind,
                    exc_info=True,
                )

        play_builtin_sound_async(kind)

    def set_status(
        self,
        status: str,
        notify: Optional[str] = None,
        error: bool = False,
        state: Optional[str] = None,
    ) -> None:
        tray_state = state or tray_state_for_status(status, error)
        if tray_state not in {"idle", "ready", "recording", "processing", "error"}:
            tray_state = "error" if error else "idle"
        with self._status_lock:
            self.status = status
            self.tray_state = tray_state
        log_status = logging.error if error else logging.info
        log_status(
            "Stan aplikacji: state=%s status=%s",
            tray_state,
            status,
        )
        tray = self.tray
        if tray is not None:
            try:
                tray.icon = make_tray_image(tray_state)
                tray.title = f"{APP_DISPLAY_NAME} — {status}"[:127]
                tray.update_menu()
            except Exception:
                logging.debug("Nie udało się odświeżyć ikony", exc_info=True)
            if notify and self.config.get("feedback", {}).get(
                "notifications", True
            ):
                try:
                    tray.notify(notify, APP_DISPLAY_NAME)
                except Exception:
                    logging.debug("Powiadomienie systemowe nie zadziałało", exc_info=True)

    def tray_status_text(self, item=None) -> str:
        with self._status_lock:
            return self.translator.t(
                "Status: {status}",
                "Status: {status}",
                status=self.status,
            )

    def open_settings(self, icon=None, item=None) -> None:
        try:
            subprocess.Popen(
                settings_process_args(),
                cwd=str(APP_ROOT),
            )
        except Exception as exc:
            logging.exception("Nie udało się otworzyć panelu ustawień")
            self.set_status(
                self.translator.t(
                    "Błąd otwierania ustawień",
                    "Could not open settings",
                ),
                notify=self._failure_notification(exc),
                error=True,
                state="error",
            )

    def _open_tray_path(
        self,
        path: Path,
        polish_label: str,
        english_label: str,
    ) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            logging.exception("Nie udało się otworzyć ścieżki z menu: %s", path)
            self.set_status(
                self.translator.t(
                    "Nie udało się otworzyć: {label}",
                    "Could not open: {label}",
                    label=self.translator.t(polish_label, english_label),
                ),
                notify=self._failure_notification(exc),
                error=True,
                state="error",
            )

    def open_config(self, icon=None, item=None) -> None:
        self._open_tray_path(CONFIG_PATH, "konfiguracja", "configuration")

    def open_dictionary(self, icon=None, item=None) -> None:
        self._open_tray_path(DICTIONARY_PATH, "słownik", "dictionary")

    def open_log(self, icon=None, item=None) -> None:
        self._open_tray_path(LOG_PATH, "dziennik", "log")

    def open_app_folder(self, icon=None, item=None) -> None:
        self._open_tray_path(APPDATA_DIR, "folder aplikacji", "application folder")

    def apply_profile(self, profile_name: str) -> None:
        try:
            profile = QUICK_PROFILES[profile_name]
            profile_display = profile["display"][self.translator.language]
            current_config, revision = load_config_with_revision()
            updated = apply_quick_profile(current_config, profile_name)
            save_config(updated, expected_revision=revision)
            self.set_status(
                self.translator.t(
                    "Włączam profil {label}…",
                    "Activating the {label} profile…",
                    label=profile_display["label"],
                ),
                notify=self.translator.t(
                    "Profil {label}: {description}. "
                    "Mówik uruchomi się ponownie.",
                    "{label} profile: {description}. "
                    "Mówik will restart.",
                    label=profile_display["label"],
                    description=profile_display["description"],
                ),
                state="processing",
            )
            self.restart()
        except Exception as exc:
            logging.exception("Nie udało się zastosować profilu %s", profile_name)
            self.set_status(
                self._failure_reason(exc)
                or self.translator.t(
                    "Błąd zmiany profilu",
                    "Could not change profile",
                ),
                notify=self._failure_notification(exc),
                error=True,
                state="error",
            )

    def apply_light_profile(self, icon=None, item=None) -> None:
        self.apply_profile("light")

    def apply_balanced_profile(self, icon=None, item=None) -> None:
        self.apply_profile("balanced")

    def apply_accurate_profile(self, icon=None, item=None) -> None:
        self.apply_profile("accurate")

    def restart(
        self,
        icon=None,
        item=None,
        *,
        restart_request: Optional[str] = None,
    ) -> None:
        with self._restart_lock:
            if self._restart_started:
                return
            self._restart_started = True
        try:
            requested_at_ns: Optional[int] = None
            if restart_request is not None:
                requested_at_ns = parse_restart_request_timestamp_ns(
                    restart_request
                )
                if requested_at_ns is None:
                    raise ValueError("invalid restart request token")
            args = application_restart_process_args(requested_at_ns)
            subprocess.Popen(args, cwd=str(APP_ROOT))
            if restart_request is not None:
                # Receipt means that creating the replacement process
                # succeeded, not merely that the request file was consumed.
                acknowledge_restart_request(restart_request)
        except Exception:
            with self._restart_lock:
                self._restart_started = False
            raise
        else:
            self.shutdown(icon, item)

    def shutdown(self, icon=None, item=None) -> None:
        current_ident = threading.get_ident()
        wait_for_shutdown = False
        with self._shutdown_lock:
            if self._shutdown_started:
                # Cleanup callbacks can re-enter shutdown synchronously. The
                # owner must return instead of waiting for its own completion.
                if self._shutdown_owner_ident == current_ident:
                    return
                wait_for_shutdown = True
            else:
                self._shutdown_started = True
                self._shutdown_owner_ident = current_ident
        if wait_for_shutdown:
            self._shutdown_complete.wait()
            return

        def cleanup(label: str, action) -> None:
            try:
                action()
            except Exception:
                logging.exception("Błąd zamykania zasobu: %s", label)

        try:
            self.stop_event.set()
            self._cancel_capture_timer()
            with self._input_lock:
                self._pressed_inputs.clear()
                self._active_input = None
                self.key_down = False
                self.capture_active = False
                self.capture_mode = None
                self._capture_generation += 1
            cleanup("indicator", self.dictation_indicator.close)
            cleanup("feedback", self.stop_feedback_sound)
            self.model_ready.clear()
            self.jobs.put(None)

            keyboard_listener = self.keyboard_listener
            self.keyboard_listener = None
            if keyboard_listener is not None:
                cleanup("keyboard listener", keyboard_listener.stop)
            mouse_listener = self.mouse_listener
            self.mouse_listener = None
            if mouse_listener is not None:
                cleanup("mouse listener", mouse_listener.stop)
            with self._shutdown_lock:
                recorder = self.recorder
                self.recorder = None
            if recorder is not None:
                abort = getattr(recorder, "abort", None)
                if callable(abort):
                    cleanup("recording buffer", abort)
                cleanup("microphone", recorder.close)
            tray = icon if icon is not None else self.tray
            if tray is not None:
                cleanup("tray", tray.stop)
        finally:
            with self._shutdown_lock:
                self._shutdown_owner_ident = None
            self._shutdown_complete.set()

    def run_tray(
        self,
        started_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        light_display = QUICK_PROFILES["light"]["display"][
            self.translator.language
        ]
        balanced_display = QUICK_PROFILES["balanced"]["display"][
            self.translator.language
        ]
        accurate_display = QUICK_PROFILES["accurate"]["display"][
            self.translator.language
        ]
        profiles_menu = pystray.Menu(
            pystray.MenuItem(
                self.translator.t(
                    "{label} — small / dokładność 1",
                    "{label} — small / accuracy 1",
                    label=light_display["label"],
                ),
                self.apply_light_profile,
            ),
            pystray.MenuItem(
                self.translator.t(
                    "{label} — Turbo / dokładność 2",
                    "{label} — Turbo / accuracy 2",
                    label=balanced_display["label"],
                ),
                self.apply_balanced_profile,
            ),
            pystray.MenuItem(
                self.translator.t(
                    "{label} — large-v3 / dokładność 5",
                    "{label} — large-v3 / accuracy 5",
                    label=accurate_display["label"],
                ),
                self.apply_accurate_profile,
            ),
        )
        menu = pystray.Menu(
            pystray.MenuItem(self.tray_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                self.translator.t("Panel ustawień…", "Settings…"),
                self.open_settings,
                default=True,
            ),
            pystray.MenuItem(
                self.translator.t("Szybki profil", "Quick profile"),
                profiles_menu,
            ),
            pystray.MenuItem(
                self.translator.t("Otwórz słownik", "Open dictionary"),
                self.open_dictionary,
            ),
            pystray.MenuItem(
                self.translator.t("Edytuj config.json", "Edit config.json"),
                self.open_config,
            ),
            pystray.MenuItem(
                self.translator.t("Otwórz log", "Open log"),
                self.open_log,
            ),
            pystray.MenuItem(
                self.translator.t(
                    "Folder konfiguracji",
                    "Configuration folder",
                ),
                self.open_app_folder,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                self.translator.t("Uruchom ponownie", "Restart"),
                self.restart,
            ),
            pystray.MenuItem(
                self.translator.t("Zakończ", "Exit"),
                self.shutdown,
            ),
        )
        self.tray = pystray.Icon(
            APP_NAME,
            make_tray_image(self.tray_state),
            f"{APP_DISPLAY_NAME} — {self.status}",
            menu,
        )
        indicator_ready = self.dictation_indicator.start()
        try:
            self.start()
            tray_setup = None
            if started_callback is not None:
                def tray_setup(icon) -> None:
                    icon.visible = True
                    try:
                        started_callback()
                    except Exception:
                        logging.exception(
                            "Nie udało się potwierdzić startu po restarcie"
                        )
            if indicator_ready:
                if tray_setup is None:
                    self.tray.run_detached()
                else:
                    self.tray.run_detached(setup=tray_setup)
                self.dictation_indicator.run()
            else:
                if tray_setup is None:
                    self.tray.run()
                else:
                    self.tray.run(setup=tray_setup)
        finally:
            self.shutdown()
            if indicator_ready:
                # Jeśli start aplikacji nie doszedł do głównej pętli, ta krótka
                # pętla przetworzy polecenie zamknięcia i zniszczy Tk w tym samym
                # (głównym) wątku, w którym zostało utworzone.
                self.dictation_indicator.run()


def _create_windows_named_mutex(name: str) -> tuple[int, bool]:
    if os.name != "nt":
        raise OSError("Windows named mutexes are unavailable on this platform")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle), ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def settings_already_open_message(translator: Translator) -> tuple[str, str]:
    return (
        translator.t(
            "Centrum Mówika jest już otwarte. Przejdź do istniejącego okna ustawień.",
            "Mówik Center is already open. Switch to the existing settings window.",
        ),
        translator.t(
            "Mówik — ustawienia",
            "Mówik — Settings",
        ),
    )


def show_settings_already_open(translator: Translator) -> None:
    message, title = settings_already_open_message(translator)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    user32.MessageBoxW.restype = ctypes.c_int
    # MB_OK | MB_ICONINFORMATION | MB_TOPMOST
    user32.MessageBoxW(None, message, title, 0x00000040 | 0x00040000)


def acquire_settings_instance(
    translator: Optional[Translator] = None,
) -> Optional[int]:
    """Zwróć mutex panelu albo poinformować o już otwartym oknie."""

    translator = translator or Translator("auto")
    handle, already_exists = _create_windows_named_mutex(SETTINGS_MUTEX_NAME)
    if not already_exists:
        return handle
    try:
        show_settings_already_open(translator)
    finally:
        release_single_instance(handle)
    return None


def acquire_single_instance(
    translator: Optional[Translator] = None,
    *,
    notify_existing: bool = True,
) -> Optional[int]:
    if os.name != "nt":
        return None
    translator = translator or Translator("pl")
    # ``_create_windows_named_mutex`` zeruje kod błędu przed CreateMutexW.
    # Bez tego zalegający ERROR_ALREADY_EXISTS z wcześniejszego wywołania
    # Win32 w tym wątku mógłby zablokować start pierwszej instancji.
    handle, already_exists = _create_windows_named_mutex(MUTEX_NAME)
    if not already_exists:
        return handle
    if notify_existing:
        ctypes.windll.user32.MessageBoxW(
            None,
            translator.t(
                "Mówik jest już uruchomiony. "
                "Poszukaj ikony mikrofonu przy zegarze.",
                "Mówik is already running. "
                "Look for the microphone icon in the system tray.",
            ),
            APP_DISPLAY_NAME,
            0x40,
        )
    release_single_instance(handle)
    return None


def release_single_instance(handle: Optional[int]) -> None:
    if handle and os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def show_fatal_error(
    message: str,
    translator: Optional[Translator] = None,
    *,
    include_log_details: bool = True,
    write_log: bool = True,
) -> None:
    translator = translator or Translator("auto")
    display_message = message
    if include_log_details:
        display_message = translator.t(
            "{message}\n\nSzczegóły: {path}",
            "{message}\n\nDetails: {path}",
            message=message,
            path=LOG_PATH,
        )
    if write_log:
        logging.error("Błąd krytyczny: %s", message)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None,
            display_message,
            translator.t(
                "{name} — błąd",
                "{name} — error",
                name=APP_DISPLAY_NAME,
            ),
            0x10,
        )
    else:
        print(display_message, file=sys.stderr)


def list_devices(translator: Optional[Translator] = None) -> int:
    translator = translator or Translator()
    print(sd.query_devices())
    print(
        translator.t(
            "\nDomyślne urządzenia (wejście, wyjście): {devices}",
            "\nDefault devices (input, output): {devices}",
            devices=sd.default.device,
        )
    )
    return 0


def download_model_command(
    config: dict[str, Any],
    *,
    force_download: bool = True,
) -> int:
    translator = Translator.from_config(config)

    def status(text: str) -> None:
        print(text, flush=True)

    model, model_name, device = create_model(
        config,
        status,
        force_download=force_download,
    )
    del model
    print(
        translator.t(
            "\nGotowe. Model {model_name} jest zapisany lokalnie i działa na {device}.",
            "\nDone. Model {model_name} is stored locally and runs on {device}.",
            model_name=model_name,
            device=device,
        )
    )
    print(
        translator.t(
            "Folder modeli: {path}",
            "Model folder: {path}",
            path=MODEL_DIR,
        )
    )
    return 0


def test_ollama_command(config: dict[str, Any]) -> int:
    translator = Translator.from_config(config)
    transcription_language = str(config.get("language", "auto")).lower().strip()
    samples = {
        "pl": "to jest test lokalnego korektora i on nie może zmienić sensu",
        "en": "this is a test of the local proofreader and it must preserve meaning",
        "de": "dies ist ein Test des lokalen Korrektors und er darf die Bedeutung nicht ändern",
        "fr": "ceci est un test du correcteur local et il ne doit pas changer le sens",
        "es": "esta es una prueba del corrector local y no debe cambiar el sentido",
        "uk": "це тест локального коректора і він не повинен змінювати зміст",
    }
    if transcription_language == "auto":
        transcription_language = "pl" if translator.is_polish else "en"
    sample = samples.get(transcription_language, samples["en"])
    result = cleanup_with_ollama(sample, config, load_dictionary(config))
    print(translator.t("Wejście:", "Input:"), sample)
    print(translator.t("Wynik:  ", "Result: "), result)
    if result == sample and config.get("ollama_cleanup", {}).get("enabled", False):
        print(
            translator.t(
                "Uwaga: wynik nie został zmieniony; sprawdź log oraz "
                "konfigurację Ollama.",
                "Warning: the result was unchanged; check the log and your "
                "Ollama configuration.",
            )
        )
    return 0


def runtime_gui_smoke_test_command() -> int:
    """Fail fast when a source or frozen build cannot create a Tk GUI."""

    root: Any = None
    # ``return`` wewnątrz ``finally`` porzuca trwający wyjątek i od Pythona
    # 3.14 jest ostrzeżeniem składniowym, więc wynik zbieramy w zmiennej.
    status = 0
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.update_idletasks()
    except Exception as exc:
        print(f"Tk GUI smoke test failed: {exc}", file=sys.stderr)
        status = 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception as exc:
                print(f"Tk GUI cleanup failed: {exc}", file=sys.stderr)
                status = 1

    # Kapsuła dyktowania nie przechodzi przez Tk: rysuje ją Pillow, a na ekran
    # trafia przez GDI. Sam test Tk nie wykryłby więc brakującego w bundlu
    # ImageFilter, niezaładowanego kroju ani zepsutej konwersji do BGRA.
    try:
        frame = render_status_indicator_frame(
            "recording",
            3,
            level=0.5,
            label="Smoke test",
        )
        if frame.size != (STATUS_INDICATOR_WIDTH, STATUS_INDICATOR_HEIGHT):
            raise AppError(f"Unexpected indicator frame size: {frame.size}")
        if frame.getbbox() is None:
            raise AppError("The indicator frame came out empty")
        payload = _premultiplied_bgra(frame)
        if len(payload) != frame.width * frame.height * 4:
            raise AppError("Premultiplied indicator payload has a wrong size")
    except Exception as exc:
        print(f"Dictation indicator smoke test failed: {exc}", file=sys.stderr)
        status = 1
    return status


def parse_args(translator: Optional[Translator] = None) -> argparse.Namespace:
    translator = translator or Translator()
    parser = argparse.ArgumentParser(
        description=translator.t(
            "Mówik — lokalne dyktowanie push-to-talk dla Windows.",
            "Mówik — private local push-to-talk dictation for Windows.",
        )
    )
    parser.add_argument("--version", action="version", version=f"{APP_DISPLAY_NAME} {APP_VERSION}")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help=translator.t("Pokaż mikrofony", "List microphones"),
    )
    model_action_group = parser.add_mutually_exclusive_group()
    model_action_group.add_argument(
        "--download-model",
        action="store_true",
        help=translator.t(
            "Pobierz model ponownie i zakończ",
            "Download the model again and exit",
        ),
    )
    model_action_group.add_argument(
        "--ensure-model",
        action="store_true",
        help=translator.t(
            "Zapewnij lokalną kopię modelu i zakończ",
            "Ensure a local model copy and exit",
        ),
    )
    parser.add_argument(
        "--create-config",
        action="store_true",
        help=translator.t(
            "Utwórz domyślne pliki konfiguracji i zakończ",
            "Create default configuration files and exit",
        ),
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help=translator.t(
            "Otwórz graficzny panel ustawień",
            "Open the graphical settings panel",
        ),
    )
    parser.add_argument(
        "--test-ollama",
        action="store_true",
        help=translator.t(
            "Sprawdź opcjonalny lokalny korektor Ollama",
            "Test the optional local Ollama proofreader",
        ),
    )
    parser.add_argument(
        "--console-log",
        action="store_true",
        help=translator.t(
            "Pokaż log również w konsoli",
            "Also show the log in the console",
        ),
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=0.0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--restart-started-token",
        type=parse_restart_started_cli_token,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-gui-smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    cli_translator = Translator()
    args = parse_args(cli_translator)
    if getattr(args, "runtime_gui_smoke_test", False):
        return runtime_gui_smoke_test_command()
    require_non_elevated_runtime(cli_translator)
    setup_logging(
        console=(
            args.console_log
            or args.download_model
            or getattr(args, "ensure_model", False)
            or args.list_devices
        )
    )
    create_default_files()

    if args.create_config:
        print(
            cli_translator.t(
                "Konfiguracja: {path}",
                "Configuration: {path}",
                path=CONFIG_PATH,
            )
        )
        print(
            cli_translator.t(
                "Słownik:       {path}",
                "Dictionary:    {path}",
                path=DICTIONARY_PATH,
            )
        )
        return 0
    if args.settings:
        # Ustaw DPI przed jakimkolwiek oknem (także komunikatem o drugiej
        # instancji), a mutex trzymaj przez cały czas życia panelu.
        enable_windows_dpi_awareness()
        settings_translator = Translator.from_config(load_config())
        if os.name != "nt":
            return run_settings_window()
        settings_mutex_handle = acquire_settings_instance(settings_translator)
        if settings_mutex_handle is None:
            return 0
        try:
            return run_settings_window()
        finally:
            release_single_instance(settings_mutex_handle)
    if args.list_devices:
        return list_devices(cli_translator)

    config = load_config()
    translator = Translator.from_config(config)
    if args.restart_delay > 0:
        time.sleep(min(args.restart_delay, 5.0))
    if args.download_model:
        return download_model_command(config, force_download=True)
    if getattr(args, "ensure_model", False):
        return download_model_command(config, force_download=False)
    if args.test_ollama:
        return test_ollama_command(config)

    if os.name != "nt":
        raise AppError(
            translator.t(
                "Aplikacja okienkowa Mówik jest przeznaczona dla Windows 10/11.",
                "The Mówik desktop application is designed for Windows 10/11.",
            )
        )

    enable_windows_dpi_awareness()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Mowik.LocalDictation"
        )
    except Exception:
        pass

    restart_deadline = (
        time.monotonic() + RESTART_MUTEX_WAIT_SECONDS
        if args.restart_delay > 0
        else None
    )
    while True:
        mutex_handle = acquire_single_instance(
            translator,
            notify_existing=restart_deadline is None,
        )
        if mutex_handle is not None:
            break
        if restart_deadline is None:
            return 0
        if time.monotonic() >= restart_deadline:
            logging.error("Nowa instancja nie przejęła mutexu po restarcie")
            return 0
        time.sleep(0.2)
    try:
        app = MowikApp(config)
        restart_started_token = getattr(args, "restart_started_token", None)
        started_callback = (
            None
            if restart_started_token is None
            else lambda: announce_restart_started(restart_started_token)
        )
        app.run_tray(started_callback=started_callback)
    finally:
        release_single_instance(mutex_handle)
    return 0


def run_entrypoint() -> int:
    """Run the CLI while keeping elevation rejection free of user-data I/O."""

    try:
        return main()
    except KeyboardInterrupt:
        return 130
    except ElevatedRuntimeError as exc:
        show_fatal_error(
            str(exc),
            Translator(),
            include_log_details=False,
            write_log=False,
        )
        return 1
    except Exception as exc:
        setup_logging(console=True)
        logging.error("Błąd krytyczny:\n%s", traceback.format_exc())
        try:
            fatal_translator = Translator.from_config(load_config())
        except Exception:
            fatal_translator = Translator()
        show_fatal_error(str(exc), fatal_translator)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_entrypoint())
