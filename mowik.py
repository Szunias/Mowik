"""Mówik — lokalne dyktowanie push-to-talk dla Windows.

Przytrzymaj skonfigurowany klawisz, mów, puść. Nagranie jest lokalnie
transkrybowane przez faster-whisper i wklejane do aktywnego pola tekstowego.
Opcjonalne czyszczenie lokalnym LLM przez Ollama jest domyślnie wyłączone.
"""

from __future__ import annotations

import argparse
import io
import ctypes
import difflib
import json
import logging
from logging.handlers import RotatingFileHandler
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
import urllib.error
import urllib.request
import wave
from collections import deque
from typing import Any, Optional

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import ctranslate2
from PIL import Image, ImageDraw
from pynput import keyboard, mouse
import pystray
import pyperclip


APP_NAME = "Mowik"
APP_DISPLAY_NAME = "Mówik"
APP_VERSION = "2.2.0"
MUTEX_NAME = r"Local\MowikLocalDictation"
SAMPLE_RATE = 16_000

APPDATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
LOCALDATA_DIR = Path(os.environ.get("LOCALAPPDATA", APPDATA_DIR)) / APP_NAME
CONFIG_PATH = APPDATA_DIR / "config.json"
DICTIONARY_PATH = APPDATA_DIR / "slownik.txt"
SOUNDS_DIR = APPDATA_DIR / "sounds"
MODEL_DIR = LOCALDATA_DIR / "models"
LOG_PATH = LOCALDATA_DIR / "mowik.log"
CONTROL_DIR = LOCALDATA_DIR / "control"
RESTART_REQUEST_PATH = CONTROL_DIR / "restart.request"

DEFAULT_CONFIG: dict[str, Any] = {
    "trigger": "keyboard:f8",
    "language": "pl",
    "model": "auto",
    "device": "auto",
    "cpu_threads": 0,
    "beam_size": 5,
    "pre_roll_ms": 300,
    "post_roll_ms": 160,
    "minimum_recording_ms": 250,
    "minimum_rms": 0.0015,
    "microphone": None,
    "vad": {
        "enabled": True,
        "threshold": 0.45,
        "min_speech_duration_ms": 120,
        "min_silence_duration_ms": 250,
        "speech_pad_ms": 180,
    },
    "dictionary": {
        "enabled": True,
        "max_terms": 120,
    },
    "paste": {
        "enabled": True,
        "copy_to_clipboard": True,
        "append_space": True,
        "delay_ms": 90,
    },
    "feedback": {
        "sounds": True,
        "notifications": True,
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
    "ollama_cleanup": {
        "enabled": False,
        "url": "http://127.0.0.1:11434",
        "model": "",
        "timeout_seconds": 45,
    },
}

QUICK_PROFILES: dict[str, dict[str, Any]] = {
    "light": {
        "label": "Lekki",
        "description": "small, beam 1 — najmniejsze obciążenie",
        "changes": {
            "model": "small",
            "device": "auto",
            "beam_size": 1,
        },
    },
    "balanced": {
        "label": "Zbalansowany",
        "description": "large-v3-turbo, beam 2 — zalecany",
        "changes": {
            "model": "large-v3-turbo",
            "device": "auto",
            "beam_size": 2,
        },
    },
    "accurate": {
        "label": "Dokładny",
        "description": "large-v3, beam 5 — najwyższa jakość",
        "changes": {
            "model": "large-v3",
            "device": "auto",
            "beam_size": 5,
        },
    },
}

DICTIONARY_TEMPLATE = """# Jedna nazwa lub fraza w każdym wierszu.
# Linie zaczynające się od # są ignorowane.
# Dopisz nazwiska, nazwy firm, projekty, miejscowości i fachowe terminy.
OpenAI
ChatGPT
Mówik
"""

VOICE_COMMAND_REPLACEMENTS = [
    (re.compile(r"\bnowy akapit\b[,.]?", re.IGNORECASE), "\n\n"),
    (re.compile(r"\bnowa linia\b[,.]?", re.IGNORECASE), "\n"),
]


class AppError(RuntimeError):
    pass


def ensure_directories() -> None:
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    LOCALDATA_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(console: bool = False) -> None:
    ensure_directories()
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            LOG_PATH,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def deep_merge(defaults: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, default_value in defaults.items():
        if key not in loaded:
            result[key] = default_value
        elif isinstance(default_value, dict):
            if isinstance(loaded[key], dict):
                result[key] = deep_merge(default_value, loaded[key])
            else:
                # Zły typ w ręcznie edytowanym config.json nie może
                # wywrócić programu w trakcie dyktowania.
                result[key] = default_value
        else:
            result[key] = loaded[key]
    for key, value in loaded.items():
        if key not in result:
            result[key] = value
    return result


def create_default_files() -> None:
    ensure_directories()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if not DICTIONARY_PATH.exists():
        DICTIONARY_PATH.write_text(DICTIONARY_TEMPLATE, encoding="utf-8")


def load_config() -> dict[str, Any]:
    create_default_files()
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(f"Nie można odczytać {CONFIG_PATH}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise AppError("Plik config.json musi zawierać obiekt JSON.")
    return deep_merge(DEFAULT_CONFIG, loaded)


def save_config(config: dict[str, Any]) -> None:
    """Zapisz konfigurację atomowo, aby nie pozostawić uciętego JSON-a."""
    ensure_directories()
    temp_path = CONFIG_PATH.with_name(f"{CONFIG_PATH.name}.{os.getpid()}.tmp")
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, CONFIG_PATH)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def apply_quick_profile(config: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profile = QUICK_PROFILES.get(profile_name)
    if profile is None:
        raise AppError(f"Nieznany profil: {profile_name}")
    result = deep_merge(DEFAULT_CONFIG, config)
    for key, value in profile["changes"].items():
        result[key] = value
    return result


def request_app_restart() -> None:
    """Poproś działającą instancję o ponowne uruchomienie po zapisie ustawień."""
    ensure_directories()
    temp_path = RESTART_REQUEST_PATH.with_name(
        f"{RESTART_REQUEST_PATH.name}.{os.getpid()}.tmp"
    )
    temp_path.write_text(f"{time.time():.6f}\n", encoding="ascii")
    os.replace(temp_path, RESTART_REQUEST_PATH)


def load_dictionary(config: dict[str, Any]) -> list[str]:
    settings = config.get("dictionary", {})
    if not settings.get("enabled", True):
        return []
    try:
        lines = DICTIONARY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    limit = max(0, int(settings.get("max_terms", 120)))
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        normalized = value.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(value)
        if limit and len(terms) >= limit:
            break
    return terms


def windows_cuda_runtime_present() -> bool:
    if os.name != "nt":
        return True
    required_dlls = ("cublas64_12.dll", "cudnn64_9.dll")
    for dll_name in required_dlls:
        try:
            ctypes.WinDLL(dll_name)
        except OSError:
            logging.info("Brak biblioteki GPU w PATH: %s", dll_name)
            return False
    return True


def get_cuda_count() -> int:
    try:
        count = int(ctranslate2.get_cuda_device_count())
    except Exception:
        logging.exception("Nie udało się sprawdzić urządzeń CUDA")
        return 0
    if count > 0 and not windows_cuda_runtime_present():
        return 0
    return count


def resolve_model_plan(config: dict[str, Any]) -> tuple[str, str, str]:
    requested_device = str(config.get("device", "auto")).lower().strip()
    requested_model = str(config.get("model", "auto")).strip()
    cuda_available = get_cuda_count() > 0

    if requested_device == "auto":
        device = "cuda" if cuda_available else "cpu"
    elif requested_device in {"cuda", "cpu"}:
        device = requested_device
    else:
        raise AppError("device musi mieć wartość: auto, cuda albo cpu.")

    if requested_model.lower() == "auto":
        model_name = "large-v3" if device == "cuda" else "large-v3-turbo"
    else:
        model_name = requested_model

    compute_type = "float16" if device == "cuda" else "int8"
    return model_name, device, compute_type


def create_model(config: dict[str, Any], status_callback=None) -> tuple[WhisperModel, str, str]:
    model_name, device, compute_type = resolve_model_plan(config)
    if status_callback:
        status_callback(f"Ładowanie modelu {model_name} ({device})…")

    kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "download_root": str(MODEL_DIR),
    }
    cpu_threads = int(config.get("cpu_threads", 0) or 0)
    if cpu_threads > 0:
        kwargs["cpu_threads"] = cpu_threads

    logging.info(
        "Ładowanie modelu: model=%s device=%s compute_type=%s",
        model_name,
        device,
        compute_type,
    )
    try:
        model = WhisperModel(model_name, **kwargs)
        return model, model_name, device
    except Exception as exc:
        logging.exception("Nie udało się uruchomić modelu na %s", device)
        if device != "cuda":
            raise

        # Automatyczny bezpiecznik: jeżeli CUDA jest wykryta, ale brakuje bibliotek
        # lub sterownik jest niezgodny, program nadal ma działać na CPU.
        # Używamy już pobranego modelu, aby po błędzie CUDA nie ściągać
        # od razu drugiego, wielogigabajtowego wariantu.
        fallback_model = model_name
        if status_callback:
            status_callback(
                "CUDA nie ruszyła — przełączam na CPU. Szczegóły zapisano w logu."
            )
        logging.warning(
            "Fallback CPU po błędzie CUDA (%s). Model: %s", exc, fallback_model
        )
        model = WhisperModel(
            fallback_model,
            device="cpu",
            compute_type="int8",
            download_root=str(MODEL_DIR),
            **({"cpu_threads": cpu_threads} if cpu_threads > 0 else {}),
        )
        return model, fallback_model, "cpu"


def normalize_transcript(text: str) -> str:
    text = text.replace("\u00a0", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text


def apply_voice_commands(text: str, config: dict[str, Any]) -> str:
    if not config.get("voice_commands", {}).get("enabled", False):
        return text
    result = text
    for pattern, replacement in VOICE_COMMAND_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    return normalize_transcript(result)


def strip_llm_wrapping(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
        if value.lower().startswith("text\n"):
            value = value[5:]
    value = re.sub(
        r"^(poprawiony tekst|wynik|transkrypcja)\s*:\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "„", "”"}:
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
    if len(re.findall(r"\bnie\b", original, flags=re.IGNORECASE)) != len(
        re.findall(r"\bnie\b", corrected, flags=re.IGNORECASE)
    ):
        return False
    return True


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

    base_url = str(settings.get("url", "http://127.0.0.1:11434")).rstrip("/")
    timeout = max(1, int(settings.get("timeout_seconds", 45)))
    glossary = ", ".join(dictionary_terms[:80]) or "brak"
    system_prompt = (
        "Jesteś bardzo zachowawczym korektorem polskiej transkrypcji mowy. "
        "Zwróć wyłącznie poprawiony tekst, bez komentarza i bez cudzysłowu. "
        "Wolno poprawić interpunkcję, wielkie litery, oczywiste literówki i "
        "jednoznaczne błędy rozpoznania dźwięku. Nie parafrazuj, nie skracaj, "
        "nie dodawaj informacji. Nigdy nie zmieniaj liczb, nazw własnych, negacji "
        "ani znaczenia. Gdy nie masz pewności, pozostaw fragment bez zmian."
    )
    user_prompt = f"Słownik preferowanych zapisów: {glossary}\n\nTekst:\n{text}"
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        corrected = strip_llm_wrapping(
            str(body.get("message", {}).get("content", ""))
        )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        logging.warning("Korekta Ollama niedostępna: %s", exc)
        return text

    corrected = normalize_transcript(corrected)
    if not llm_result_is_safe(text, corrected):
        logging.warning("Odrzucono zbyt agresywną korektę LLM")
        return text
    return corrected


CUSTOM_SOUND_KINDS = {"start", "stop", "done", "error"}


def resolve_sound_path(value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = APPDATA_DIR / path
    return path


def validate_wave_file(path: Path) -> None:
    if path.suffix.lower() != ".wav":
        raise AppError("Własny dźwięk musi być plikiem WAV.")
    if not path.is_file():
        raise AppError(f"Nie znaleziono pliku dźwięku: {path}")
    if path.stat().st_size > 50 * 1024 * 1024:
        raise AppError("Plik WAV jest większy niż 50 MB.")
    try:
        with wave.open(str(path), "rb") as wav_file:
            if wav_file.getnframes() <= 0:
                raise AppError(f"Plik WAV nie zawiera dźwięku: {path}")
            if wav_file.getcomptype() != "NONE":
                raise AppError(
                    "Plik WAV musi używać nieskompresowanego formatu PCM."
                )
    except (OSError, wave.Error) as exc:
        raise AppError(f"Nieprawidłowy plik WAV „{path.name}”: {exc}") from exc


def import_custom_sound(kind: str, source_value: Any) -> str:
    """Skopiuj wskazany WAV do prywatnego folderu Mówika."""
    if kind not in CUSTOM_SOUND_KINDS:
        raise AppError(f"Nieznany rodzaj dźwięku: {kind}")
    source = resolve_sound_path(source_value)
    if source is None:
        return ""
    source = source.resolve()
    validate_wave_file(source)
    ensure_directories()
    destination = (SOUNDS_DIR / f"{kind}.wav").resolve()
    if source != destination:
        shutil.copy2(source, destination)
    validate_wave_file(destination)
    return str(Path("sounds") / destination.name)


def configured_sound_path(config: dict[str, Any], kind: str) -> Optional[Path]:
    feedback = config.get("feedback", {})
    custom = feedback.get("custom_sounds", {})
    if not isinstance(custom, dict):
        return None
    path = resolve_sound_path(custom.get(kind, ""))
    if path is None:
        return None
    if path.is_file():
        return path
    logging.warning("Brakuje własnego dźwięku %s: %s", kind, path)
    return None


def windows_set_clipboard_text(text: str) -> None:
    if os.name != "nt":
        raise AppError("Wklejanie jest obsługiwane wyłącznie na Windowsie.")
    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as exc:
        raise AppError(f"Nie udało się zapisać tekstu do schowka: {exc}") from exc


def windows_type_unicode_text(text: str) -> None:
    """Wpisz tekst przez Win32 SendInput bez używania schowka."""
    if os.name != "nt":
        raise AppError("Wpisywanie tekstu jest obsługiwane wyłącznie na Windowsie.")
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
        inputs: list[INPUT] = []
        for code_unit in code_units[offset : offset + 256]:
            press = INPUT()
            press.type = INPUT_KEYBOARD
            press.ki = KEYBDINPUT(
                0,
                code_unit,
                KEYEVENTF_UNICODE,
                0,
                0,
            )
            release = INPUT()
            release.type = INPUT_KEYBOARD
            release.ki = KEYBDINPUT(
                0,
                code_unit,
                KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                0,
                0,
            )
            inputs.extend((press, release))
        array_type = INPUT * len(inputs)
        array = array_type(*inputs)
        sent = int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))
        if sent != len(inputs):
            raise AppError(
                f"Windows wysłał tylko {sent} z {len(inputs)} zdarzeń klawiatury."
            )


def paste_text(text: str, config: dict[str, Any]) -> None:
    settings = config.get("paste", {})
    paste_enabled = bool(settings.get("enabled", True))
    copy_to_clipboard = bool(settings.get("copy_to_clipboard", True))
    append_space = bool(settings.get("append_space", True))

    if not paste_enabled and not copy_to_clipboard:
        raise AppError(
            "Włącz automatyczne wklejanie lub kopiowanie tekstu do schowka."
        )

    # Schowek zawiera dokładną transkrypcję. Ewentualną końcową spację
    # wysyłamy osobno, dzięki czemu nie zostaje dopisana do kopiowanego tekstu.
    if copy_to_clipboard:
        windows_set_clipboard_text(text)

    if not paste_enabled:
        return

    delay = max(0, int(settings.get("delay_ms", 90))) / 1000
    if delay:
        time.sleep(delay)

    if copy_to_clipboard:
        controller = keyboard.Controller()
        with controller.pressed(keyboard.Key.ctrl):
            controller.press("v")
            controller.release("v")
        if append_space and text and not text[-1].isspace():
            time.sleep(0.02)
            controller.press(keyboard.Key.space)
            controller.release(keyboard.Key.space)
    else:
        payload = text
        if append_space and text and not text[-1].isspace():
            payload += " "
        windows_type_unicode_text(payload)


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


def split_trigger(trigger: str) -> tuple[str, str]:
    parts = trigger.strip().lower().split(":", 1)
    if len(parts) != 2 or parts[0] not in {"keyboard", "mouse"} or not parts[1]:
        raise AppError(
            "trigger musi wyglądać np. tak: keyboard:f8 albo mouse:x2."
        )
    return parts[0], parts[1]


def trigger_display_name(trigger: str) -> str:
    trigger_type, name = split_trigger(trigger)
    if trigger_type == "mouse":
        mouse_labels = {
            "left": "lewy przycisk",
            "right": "prawy przycisk",
            "middle": "środkowy przycisk",
            "x1": "boczny przycisk X1",
            "x2": "boczny przycisk X2",
        }
        return f"Mysz: {mouse_labels.get(name, name.upper())}"

    key_labels = {
        "pause": "Pause/Break",
        "scroll_lock": "Scroll Lock",
        "caps_lock": "Caps Lock",
        "space": "Spacja",
        "tab": "Tab",
        "insert": "Insert",
        "delete": "Delete",
        "home": "Home",
        "end": "End",
        "page_up": "Page Up",
        "page_down": "Page Down",
        "ctrl": "Ctrl",
        "ctrl_l": "Lewy Ctrl",
        "ctrl_r": "Prawy Ctrl",
        "alt": "Alt",
        "alt_l": "Lewy Alt",
        "alt_r": "Prawy Alt",
        "shift": "Shift",
        "shift_l": "Lewy Shift",
        "shift_r": "Prawy Shift",
        "cmd": "Windows",
        "cmd_l": "Lewy Windows",
        "cmd_r": "Prawy Windows",
    }
    if name in key_labels:
        label = key_labels[name]
    elif len(name) == 1:
        label = name.upper()
    elif re.fullmatch(r"f\d{1,2}", name):
        label = name.upper()
    elif name.startswith("vk") and name[2:].isdigit():
        label = f"klawisz VK {name[2:]}"
    else:
        label = name.replace("_", " ").title()
    return f"Klawiatura: {label}"


def make_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=15, fill=(31, 41, 55, 255))
    draw.ellipse((20, 11, 44, 39), fill=(240, 240, 240, 255))
    draw.rounded_rectangle((28, 18, 36, 46), radius=4, fill=(31, 41, 55, 255))
    draw.arc((17, 26, 47, 50), start=15, end=165, fill=(240, 240, 240, 255), width=4)
    draw.line((32, 48, 32, 55), fill=(240, 240, 240, 255), width=4)
    draw.line((24, 55, 40, 55), fill=(240, 240, 240, 255), width=4)
    return image


def settings_process_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--settings"]
    return [sys.executable, str(Path(__file__).resolve()), "--settings"]


def run_settings_window() -> int:
    """Uruchom osobny panel ustawień oparty na wbudowanym Tkinterze."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise AppError(
            "Brakuje składnika Tkinter. Zainstaluj standardowy 64-bitowy Python "
            "3.10–3.12 z python.org albo uruchom NAPRAW_INSTALACJE.cmd."
        ) from exc

    config = load_config()
    root = tk.Tk()
    root.title(f"{APP_DISPLAY_NAME} — ustawienia")
    root.geometry("930x700")
    root.minsize(780, 620)

    try:
        from PIL import ImageTk

        root._mowik_icon = ImageTk.PhotoImage(make_tray_image())  # type: ignore[attr-defined]
        root.iconphoto(True, root._mowik_icon)  # type: ignore[attr-defined]
    except Exception:
        logging.debug("Nie udało się ustawić ikony okna ustawień", exc_info=True)

    trigger_values: dict[str, Any] = {
        "F6": "keyboard:f6",
        "F7": "keyboard:f7",
        "F8 (domyślnie)": "keyboard:f8",
        "F9": "keyboard:f9",
        "F10": "keyboard:f10",
        "F11": "keyboard:f11",
        "F12": "keyboard:f12",
        "Pause/Break": "keyboard:pause",
        "Scroll Lock": "keyboard:scroll_lock",
        "Boczny przycisk myszy 1 (X1)": "mouse:x1",
        "Boczny przycisk myszy 2 (X2)": "mouse:x2",
    }
    model_values: dict[str, Any] = {
        "Automatycznie": "auto",
        "tiny — najmniejszy": "tiny",
        "base — bardzo lekki": "base",
        "small — lekki (~0,5 GB)": "small",
        "medium — dokładniejszy": "medium",
        "large-v3-turbo — zalecany (~1,6 GB)": "large-v3-turbo",
        "large-v3 — najdokładniejszy (~3,1 GB)": "large-v3",
    }
    device_values: dict[str, Any] = {
        "Automatycznie": "auto",
        "Procesor (CPU)": "cpu",
        "NVIDIA CUDA (GPU)": "cuda",
    }
    microphone_values: dict[str, Any] = {}

    def display_for_value(
        mapping: dict[str, Any], value: Any, custom_prefix: str = "Niestandardowe"
    ) -> str:
        for label, stored in mapping.items():
            if stored == value:
                return label
        label = f"{custom_prefix}: {value}"
        mapping[label] = value
        return label

    def ensure_trigger_display(value: Any) -> str:
        trigger = str(value or "keyboard:f8").strip().lower()
        for label, stored in trigger_values.items():
            if stored == trigger:
                return label
        try:
            label = trigger_display_name(trigger)
        except AppError:
            label = f"Niestandardowe: {trigger}"
        trigger_values[label] = trigger
        return label

    trigger_var = tk.StringVar(
        value=ensure_trigger_display(config.get("trigger", "keyboard:f8"))
    )
    model_var = tk.StringVar(
        value=display_for_value(model_values, config.get("model", "auto"))
    )
    device_var = tk.StringVar(
        value=display_for_value(device_values, config.get("device", "auto"))
    )
    language_var = tk.StringVar(value=str(config.get("language", "pl")))
    cpu_threads_var = tk.StringVar(value=str(config.get("cpu_threads", 0)))
    beam_size_var = tk.StringVar(value=str(config.get("beam_size", 5)))
    microphone_var = tk.StringVar()

    pre_roll_var = tk.StringVar(value=str(config.get("pre_roll_ms", 300)))
    post_roll_var = tk.StringVar(value=str(config.get("post_roll_ms", 160)))
    minimum_recording_var = tk.StringVar(
        value=str(config.get("minimum_recording_ms", 250))
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
    paste_delay_var = tk.StringVar(value=str(paste.get("delay_ms", 90)))

    feedback = config.get("feedback", {})
    sounds_var = tk.BooleanVar(value=bool(feedback.get("sounds", True)))
    notifications_var = tk.BooleanVar(
        value=bool(feedback.get("notifications", True))
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

    status_var = tk.StringVar(value="Zmiany nie są jeszcze zapisane.")

    outer = ttk.Frame(root, padding=14)
    outer.grid(row=0, column=0, sticky="nsew")
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)
    outer.rowconfigure(2, weight=1)
    outer.columnconfigure(0, weight=1)

    ttk.Label(
        outer,
        text="Ustawienia Mówika",
        font=("Segoe UI", 16, "bold"),
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        outer,
        text=(
            "Zmień parametry bez edytowania pliku JSON. „Zapisz i zastosuj” "
            "automatycznie uruchomi Mówika ponownie."
        ),
        wraplength=810,
    ).grid(row=1, column=0, sticky="ew", pady=(2, 10))

    notebook = ttk.Notebook(outer)
    notebook.grid(row=2, column=0, sticky="nsew")

    general_tab = ttk.Frame(notebook, padding=14)
    audio_tab = ttk.Frame(notebook, padding=14)
    text_tab = ttk.Frame(notebook, padding=14)
    sounds_tab = ttk.Frame(notebook, padding=14)
    ollama_tab = ttk.Frame(notebook, padding=14)
    files_tab = ttk.Frame(notebook, padding=14)
    notebook.add(general_tab, text="Ogólne")
    notebook.add(audio_tab, text="Mikrofon i VAD")
    notebook.add(text_tab, text="Tekst i schowek")
    notebook.add(sounds_tab, text="Dźwięki")
    notebook.add(ollama_tab, text="Ollama (opcjonalnie)")
    notebook.add(files_tab, text="Pliki")

    for tab in (
        general_tab,
        audio_tab,
        text_tab,
        sounds_tab,
        ollama_tab,
        files_tab,
    ):
        tab.columnconfigure(1, weight=1)

    presets = ttk.LabelFrame(general_tab, text="Szybkie profile", padding=10)
    presets.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    for column in range(3):
        presets.columnconfigure(column, weight=1)

    def set_profile(profile_name: str) -> None:
        profile = QUICK_PROFILES[profile_name]
        changes = profile["changes"]
        model_var.set(display_for_value(model_values, changes["model"]))
        device_var.set(display_for_value(device_values, changes["device"]))
        beam_size_var.set(str(changes["beam_size"]))
        status_var.set(
            f"Wybrano profil „{profile['label']}”. Kliknij „Zapisz i zastosuj”."
        )

    ttk.Button(
        presets,
        text="Lekki\nsmall · beam 1",
        command=lambda: set_profile("light"),
    ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
    ttk.Button(
        presets,
        text="Zbalansowany\nTurbo · beam 2",
        command=lambda: set_profile("balanced"),
    ).grid(row=0, column=1, sticky="ew", padx=5)
    ttk.Button(
        presets,
        text="Dokładny\nlarge-v3 · beam 5",
        command=lambda: set_profile("accurate"),
    ).grid(row=0, column=2, sticky="ew", padx=(5, 0))

    def add_field(
        parent,
        row: int,
        label: str,
        widget,
        hint: Optional[str] = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=5
        )
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        if hint:
            ttk.Label(parent, text=hint, foreground="#666666").grid(
                row=row, column=2, sticky="w", padx=(10, 0), pady=5
            )

    trigger_row = ttk.Frame(general_tab)
    trigger_row.columnconfigure(0, weight=1)
    trigger_combo = ttk.Combobox(
        trigger_row,
        textvariable=trigger_var,
        values=list(trigger_values.keys()),
        state="readonly",
    )
    trigger_combo.grid(row=0, column=0, sticky="ew")

    def capture_trigger() -> None:
        dialog = tk.Toplevel(root)
        dialog.title(f"{APP_DISPLAY_NAME} — wykrywanie przycisku")
        dialog.resizable(False, False)
        dialog.transient(root)
        dialog.grab_set()
        try:
            dialog.attributes("-topmost", True)
        except tk.TclError:
            pass

        frame = ttk.Frame(dialog, padding=20)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            frame,
            text="Naciśnij wybrany klawisz albo przycisk myszy",
            font=("Segoe UI", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=(
                "Nasłuchiwanie zacznie się za chwilę, aby nie przechwycić "
                "kliknięcia tego okna. Esc anuluje. Najwygodniejszy jest "
                "klawisz funkcyjny albo boczny przycisk myszy."
            ),
            wraplength=500,
        ).grid(row=1, column=0, sticky="ew", pady=(8, 14))
        capture_status_var = tk.StringVar(value="Przygotowanie…")
        ttk.Label(
            frame,
            textvariable=capture_status_var,
            font=("Segoe UI", 11),
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
            trigger_combo["values"] = list(trigger_values.keys())
            trigger_var.set(label)
            note = f"Ustawiono {trigger_display_name(trigger)}."
            if trigger in {"mouse:left", "mouse:right"}:
                note += " Ten przycisk może kolidować ze zwykłą obsługą Windows."
            status_var.set(note + " Kliknij „Zapisz i zastosuj”.")
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
            capture_status_var.set("Nasłuchuję — naciśnij teraz wybrany przycisk…")
            cancel_button.configure(state="disabled", text="Anuluj: Esc")
            listeners["keyboard"] = keyboard.Listener(
                on_press=on_capture_key,
                suppress=True,
            )
            listeners["mouse"] = mouse.Listener(
                on_click=on_capture_mouse,
                suppress=True,
            )
            listeners["keyboard"].start()
            listeners["mouse"].start()
            poll_capture_queue()

        cancel_button = ttk.Button(frame, text="Anuluj", command=close_capture)
        cancel_button.grid(row=3, column=0, sticky="e", pady=(18, 0))
        dialog.protocol("WM_DELETE_WINDOW", close_capture)
        dialog.after(650, begin_capture_listening)
        dialog.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - dialog.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")

    ttk.Button(
        trigger_row,
        text="Wykryj…",
        command=capture_trigger,
    ).grid(row=0, column=1, padx=(8, 0))
    add_field(
        general_tab,
        1,
        "Przycisk dyktowania",
        trigger_row,
        "Kliknij „Wykryj…”, a potem naciśnij dowolny klawisz lub przycisk myszy.",
    )

    microphone_row = ttk.Frame(general_tab)
    microphone_row.columnconfigure(0, weight=1)
    microphone_combo = ttk.Combobox(
        microphone_row,
        textvariable=microphone_var,
        state="readonly",
    )
    microphone_combo.grid(row=0, column=0, sticky="ew")

    def refresh_microphones(selected: Any = None) -> None:
        if selected is None and microphone_var.get() in microphone_values:
            selected = microphone_values[microphone_var.get()]
        microphone_values.clear()
        microphone_values["Domyślny mikrofon Windows"] = None
        try:
            devices = sd.query_devices()
            for index, info in enumerate(devices):
                if int(info.get("max_input_channels", 0)) <= 0:
                    continue
                name = str(info.get("name", f"Urządzenie {index}"))
                microphone_values[f"{index}: {name}"] = index
        except Exception as exc:
            logging.warning("Nie udało się pobrać listy mikrofonów: %s", exc)
        microphone_combo["values"] = list(microphone_values.keys())
        microphone_var.set(
            display_for_value(
                microphone_values,
                selected,
                custom_prefix="Zapisane urządzenie",
            )
        )
        microphone_combo["values"] = list(microphone_values.keys())

    ttk.Button(
        microphone_row,
        text="Odśwież",
        command=lambda: refresh_microphones(),
    ).grid(row=0, column=1, padx=(8, 0))
    add_field(general_tab, 2, "Mikrofon", microphone_row)
    microphone_row.grid_configure(columnspan=2)
    refresh_microphones(config.get("microphone"))

    model_combo = ttk.Combobox(
        general_tab,
        textvariable=model_var,
        values=list(model_values.keys()),
        state="readonly",
    )
    add_field(
        general_tab,
        3,
        "Model mowy",
        model_combo,
        "Zmiana modelu może uruchomić jednorazowe pobieranie.",
    )
    device_combo = ttk.Combobox(
        general_tab,
        textvariable=device_var,
        values=list(device_values.keys()),
        state="readonly",
    )
    add_field(general_tab, 4, "Urządzenie obliczeniowe", device_combo)
    language_combo = ttk.Combobox(
        general_tab,
        textvariable=language_var,
        values=("pl", "en", "de", "fr", "es", "uk", "auto"),
    )
    add_field(general_tab, 5, "Język", language_combo, "Dla polskiego zostaw „pl”.")
    beam_spin = ttk.Spinbox(
        general_tab, from_=1, to=10, textvariable=beam_size_var, width=8
    )
    add_field(
        general_tab,
        6,
        "Beam size",
        beam_spin,
        "1 = szybciej; 5 = dokładniej, ale wolniej.",
    )
    threads_spin = ttk.Spinbox(
        general_tab, from_=0, to=256, textvariable=cpu_threads_var, width=8
    )
    add_field(
        general_tab,
        7,
        "Wątki CPU",
        threads_spin,
        "0 = dobór automatyczny.",
    )

    capture_frame = ttk.LabelFrame(audio_tab, text="Nagrywanie", padding=10)
    capture_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    capture_frame.columnconfigure(1, weight=1)
    add_field(
        capture_frame,
        0,
        "Bufor przed naciśnięciem (ms)",
        ttk.Spinbox(capture_frame, from_=0, to=2000, textvariable=pre_roll_var),
        "Chroni początek pierwszego słowa.",
    )
    add_field(
        capture_frame,
        1,
        "Bufor po puszczeniu (ms)",
        ttk.Spinbox(capture_frame, from_=0, to=2000, textvariable=post_roll_var),
        "Chroni końcówkę ostatniego słowa.",
    )
    add_field(
        capture_frame,
        2,
        "Minimalne nagranie (ms)",
        ttk.Spinbox(
            capture_frame,
            from_=0,
            to=10000,
            textvariable=minimum_recording_var,
        ),
    )
    add_field(
        capture_frame,
        3,
        "Minimalna głośność RMS",
        ttk.Entry(capture_frame, textvariable=minimum_rms_var),
        "Niżej = większa czułość na cichy dźwięk.",
    )

    vad_frame = ttk.LabelFrame(audio_tab, text="Wykrywanie mowy (VAD)", padding=10)
    vad_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    vad_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        vad_frame,
        text="Włącz filtrowanie ciszy",
        variable=vad_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
    add_field(
        vad_frame,
        1,
        "Próg VAD",
        ttk.Entry(vad_frame, textvariable=vad_threshold_var),
        "Zakres 0–1; wyżej = bardziej rygorystycznie.",
    )
    add_field(
        vad_frame,
        2,
        "Minimalna mowa (ms)",
        ttk.Spinbox(
            vad_frame, from_=0, to=10000, textvariable=vad_min_speech_var
        ),
    )
    add_field(
        vad_frame,
        3,
        "Minimalna cisza (ms)",
        ttk.Spinbox(
            vad_frame, from_=0, to=10000, textvariable=vad_min_silence_var
        ),
    )
    add_field(
        vad_frame,
        4,
        "Margines mowy (ms)",
        ttk.Spinbox(
            vad_frame, from_=0, to=3000, textvariable=vad_speech_pad_var
        ),
    )

    text_frame = ttk.LabelFrame(text_tab, text="Wklejanie i formatowanie", padding=10)
    text_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    text_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        text_frame,
        text="Automatycznie wklejaj tekst do aktywnego okna",
        variable=paste_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        text_frame,
        text="Kopiuj rozpoznany tekst również do schowka",
        variable=copy_to_clipboard_var,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        text_frame,
        text="Dodawaj spację po wklejonym zdaniu",
        variable=append_space_var,
    ).grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        text_frame,
        text="Rozpoznawaj komendy „nowa linia” i „nowy akapit”",
        variable=voice_commands_var,
    ).grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
    add_field(
        text_frame,
        4,
        "Opóźnienie przed Ctrl+V (ms)",
        ttk.Spinbox(text_frame, from_=0, to=5000, textvariable=paste_delay_var),
    )
    ttk.Label(
        text_frame,
        text=(
            "Gdy schowek jest włączony, pozostaje w nim dokładna transkrypcja "
            "bez automatycznie dodanej spacji."
        ),
        foreground="#666666",
        wraplength=820,
    ).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    dictionary_frame = ttk.LabelFrame(text_tab, text="Słownik", padding=10)
    dictionary_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    dictionary_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        dictionary_frame,
        text="Używaj prywatnego słownika nazw i terminów",
        variable=dictionary_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
    add_field(
        dictionary_frame,
        1,
        "Maksymalna liczba haseł",
        ttk.Spinbox(
            dictionary_frame,
            from_=0,
            to=5000,
            textvariable=dictionary_max_terms_var,
        ),
    )

    def open_path(path: Path) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_DISPLAY_NAME, f"Nie udało się otworzyć:\n{path}\n\n{exc}")

    ttk.Button(
        dictionary_frame,
        text="Otwórz słownik",
        command=lambda: open_path(DICTIONARY_PATH),
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 2))

    feedback_frame = ttk.LabelFrame(sounds_tab, text="Informacje zwrotne", padding=10)
    feedback_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    ttk.Checkbutton(
        feedback_frame, text="Sygnały dźwiękowe", variable=sounds_var
    ).grid(row=0, column=0, sticky="w", padx=(0, 25), pady=4)
    ttk.Checkbutton(
        feedback_frame,
        text="Powiadomienia Windows",
        variable=notifications_var,
    ).grid(row=0, column=1, sticky="w", pady=4)
    ttk.Checkbutton(
        feedback_frame,
        text="Zapętlaj własny dźwięk nagrywania podczas trzymania przycisku",
        variable=loop_recording_sound_var,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)

    sound_labels = {
        "start": "Naciśnięcie / trzymanie",
        "stop": "Puszczenie przycisku",
        "done": "Tekst gotowy",
        "error": "Błąd lub brak mowy",
    }
    sound_tones = {
        "start": (880, 120),
        "stop": (520, 100),
        "done": (1100, 100),
        "error": (260, 180),
    }

    def choose_sound(kind: str) -> None:
        current = resolve_sound_path(sound_path_vars[kind].get())
        initial_dir = str(current.parent) if current is not None else str(Path.home())
        selected = filedialog.askopenfilename(
            parent=root,
            title=f"Wybierz dźwięk: {sound_labels[kind]}",
            initialdir=initial_dir,
            filetypes=(("Dźwięk WAV", "*.wav"), ("Wszystkie pliki", "*.*")),
        )
        if not selected:
            return
        try:
            validate_wave_file(Path(selected))
        except AppError as exc:
            messagebox.showerror(APP_DISPLAY_NAME, str(exc), parent=root)
            return
        sound_path_vars[kind].set(selected)
        status_var.set(
            f"Wybrano dźwięk „{Path(selected).name}”. Zapis skopiuje go do folderu Mówika."
        )

    def preview_sound(kind: str) -> None:
        if os.name != "nt":
            messagebox.showerror(
                APP_DISPLAY_NAME,
                "Odsłuch jest dostępny na Windowsie.",
                parent=root,
            )
            return
        try:
            import winsound

            path = resolve_sound_path(sound_path_vars[kind].get())
            if path is not None:
                validate_wave_file(path)
                winsound.PlaySound(
                    str(path),
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
            else:
                frequency, duration = sound_tones[kind]
                threading.Thread(
                    target=lambda: winsound.Beep(frequency, duration),
                    name="SoundPreview",
                    daemon=True,
                ).start()
        except Exception as exc:
            messagebox.showerror(
                APP_DISPLAY_NAME,
                f"Nie udało się odtworzyć dźwięku:\n\n{exc}",
                parent=root,
            )

    custom_sound_frame = ttk.LabelFrame(
        sounds_tab,
        text="Własne dźwięki WAV",
        padding=10,
    )
    custom_sound_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    custom_sound_frame.columnconfigure(1, weight=1)
    ttk.Label(
        custom_sound_frame,
        text=(
            "Pozostaw puste, aby używać krótkiego sygnału wbudowanego. "
            "Wybrany plik zostanie skopiowany do %APPDATA%\\Mowik\\sounds."
        ),
        foreground="#666666",
        wraplength=820,
    ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))

    for row_index, kind in enumerate(("start", "stop", "done", "error"), start=1):
        ttk.Label(custom_sound_frame, text=sound_labels[kind]).grid(
            row=row_index,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=5,
        )
        ttk.Entry(
            custom_sound_frame,
            textvariable=sound_path_vars[kind],
            state="readonly",
        ).grid(row=row_index, column=1, sticky="ew", pady=5)
        buttons = ttk.Frame(custom_sound_frame)
        buttons.grid(row=row_index, column=2, sticky="e", padx=(8, 0), pady=5)
        ttk.Button(
            buttons,
            text="Wybierz WAV…",
            command=lambda selected_kind=kind: choose_sound(selected_kind),
        ).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(
            buttons,
            text="Odsłuchaj",
            command=lambda selected_kind=kind: preview_sound(selected_kind),
        ).grid(row=0, column=1, padx=4)
        ttk.Button(
            buttons,
            text="Wbudowany",
            command=lambda selected_kind=kind: sound_path_vars[selected_kind].set(""),
        ).grid(row=0, column=2, padx=(4, 0))

    sounds_footer = ttk.Frame(sounds_tab)
    sounds_footer.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
    ttk.Button(
        sounds_footer,
        text="Otwórz folder dźwięków",
        command=lambda: open_path(SOUNDS_DIR),
    ).grid(row=0, column=0)

    ttk.Label(
        ollama_tab,
        text=(
            "Ollama nie jest potrzebna do rozpoznawania mowy. Może jedynie "
            "opcjonalnie poprawić interpunkcję i oczywiste błędy po transkrypcji."
        ),
        wraplength=790,
    ).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    ttk.Checkbutton(
        ollama_tab,
        text="Włącz lokalną korektę przez Ollamę",
        variable=ollama_enabled_var,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=5)
    add_field(
        ollama_tab,
        2,
        "Adres Ollamy",
        ttk.Entry(ollama_tab, textvariable=ollama_url_var),
    )
    add_field(
        ollama_tab,
        3,
        "Nazwa modelu",
        ttk.Entry(ollama_tab, textvariable=ollama_model_var),
        "Np. model już pobrany w Ollamie.",
    )
    add_field(
        ollama_tab,
        4,
        "Limit czasu (s)",
        ttk.Spinbox(
            ollama_tab, from_=1, to=600, textvariable=ollama_timeout_var
        ),
    )
    ttk.Label(
        ollama_tab,
        text=(
            "Włączenie korektora wydłuży oczekiwanie. Mówik odrzuca korektę, "
            "jeżeli za mocno zmienia tekst, liczby lub negacje."
        ),
        foreground="#666666",
        wraplength=790,
    ).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))

    files_tab.columnconfigure(0, weight=1)
    ttk.Label(
        files_tab,
        text="Konfiguracja",
        font=("Segoe UI", 11, "bold"),
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(files_tab, text=str(CONFIG_PATH), wraplength=790).grid(
        row=1, column=0, sticky="ew", pady=(2, 8)
    )
    ttk.Button(
        files_tab,
        text="Otwórz surowy config.json (zaawansowane)",
        command=lambda: open_path(CONFIG_PATH),
    ).grid(row=2, column=0, sticky="w")

    ttk.Label(
        files_tab,
        text="Dane i modele",
        font=("Segoe UI", 11, "bold"),
    ).grid(row=3, column=0, sticky="w", pady=(20, 0))
    ttk.Label(files_tab, text=str(LOCALDATA_DIR), wraplength=790).grid(
        row=4, column=0, sticky="ew", pady=(2, 8)
    )
    files_buttons = ttk.Frame(files_tab)
    files_buttons.grid(row=5, column=0, sticky="w")
    ttk.Button(
        files_buttons,
        text="Otwórz folder danych",
        command=lambda: open_path(LOCALDATA_DIR),
    ).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(
        files_buttons,
        text="Otwórz log",
        command=lambda: open_path(LOG_PATH),
    ).grid(row=0, column=1)

    def parse_int(
        variable: tk.StringVar, label: str, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(variable.get().strip())
        except ValueError as exc:
            raise AppError(f"Pole „{label}” musi być liczbą całkowitą.") from exc
        if not minimum <= value <= maximum:
            raise AppError(
                f"Pole „{label}” musi być w zakresie {minimum}–{maximum}."
            )
        return value

    def parse_float(
        variable: tk.StringVar, label: str, minimum: float, maximum: float
    ) -> float:
        try:
            value = float(variable.get().strip().replace(",", "."))
        except ValueError as exc:
            raise AppError(f"Pole „{label}” musi być liczbą.") from exc
        if not minimum <= value <= maximum:
            raise AppError(
                f"Pole „{label}” musi być w zakresie {minimum}–{maximum}."
            )
        return value

    def collect_config() -> dict[str, Any]:
        updated = load_config()
        trigger = str(trigger_values.get(trigger_var.get(), trigger_var.get()))
        split_trigger(trigger)
        model = str(model_values.get(model_var.get(), model_var.get())).strip()
        device = str(device_values.get(device_var.get(), device_var.get())).strip()
        language = language_var.get().strip()
        if not model:
            raise AppError("Wybierz model mowy.")
        if device not in {"auto", "cpu", "cuda"}:
            raise AppError("Urządzenie musi mieć wartość auto, cpu albo cuda.")
        if not language:
            raise AppError("Pole „Język” nie może być puste.")
        if microphone_var.get() not in microphone_values:
            raise AppError("Wybierz mikrofon z listy.")

        updated["trigger"] = trigger
        updated["model"] = model
        updated["device"] = device
        updated["language"] = language
        updated["microphone"] = microphone_values[microphone_var.get()]
        updated["cpu_threads"] = parse_int(
            cpu_threads_var, "Wątki CPU", 0, 256
        )
        updated["beam_size"] = parse_int(beam_size_var, "Beam size", 1, 10)
        updated["pre_roll_ms"] = parse_int(
            pre_roll_var, "Bufor przed naciśnięciem", 0, 2000
        )
        updated["post_roll_ms"] = parse_int(
            post_roll_var, "Bufor po puszczeniu", 0, 2000
        )
        updated["minimum_recording_ms"] = parse_int(
            minimum_recording_var, "Minimalne nagranie", 0, 10000
        )
        updated["minimum_rms"] = parse_float(
            minimum_rms_var, "Minimalna głośność RMS", 0.0, 1.0
        )

        updated.setdefault("vad", {})
        updated["vad"].update(
            {
                "enabled": bool(vad_enabled_var.get()),
                "threshold": parse_float(
                    vad_threshold_var, "Próg VAD", 0.0, 1.0
                ),
                "min_speech_duration_ms": parse_int(
                    vad_min_speech_var, "Minimalna mowa", 0, 10000
                ),
                "min_silence_duration_ms": parse_int(
                    vad_min_silence_var, "Minimalna cisza", 0, 10000
                ),
                "speech_pad_ms": parse_int(
                    vad_speech_pad_var, "Margines mowy", 0, 3000
                ),
            }
        )
        updated.setdefault("dictionary", {})
        updated["dictionary"].update(
            {
                "enabled": bool(dictionary_enabled_var.get()),
                "max_terms": parse_int(
                    dictionary_max_terms_var, "Maksymalna liczba haseł", 0, 5000
                ),
            }
        )
        updated.setdefault("paste", {})
        paste_enabled = bool(paste_enabled_var.get())
        copy_to_clipboard = bool(copy_to_clipboard_var.get())
        if not paste_enabled and not copy_to_clipboard:
            raise AppError(
                "Włącz automatyczne wklejanie albo kopiowanie do schowka."
            )
        updated["paste"].update(
            {
                "enabled": paste_enabled,
                "copy_to_clipboard": copy_to_clipboard,
                "append_space": bool(append_space_var.get()),
                "delay_ms": parse_int(
                    paste_delay_var, "Opóźnienie przed Ctrl+V", 0, 5000
                ),
            }
        )
        updated.setdefault("feedback", {})
        updated["feedback"].update(
            {
                "sounds": bool(sounds_var.get()),
                "notifications": bool(notifications_var.get()),
                "loop_recording_sound": bool(loop_recording_sound_var.get()),
            }
        )
        updated.setdefault("voice_commands", {})
        updated["voice_commands"]["enabled"] = bool(voice_commands_var.get())
        updated.setdefault("ollama_cleanup", {})
        updated["ollama_cleanup"].update(
            {
                "enabled": bool(ollama_enabled_var.get()),
                "url": ollama_url_var.get().strip() or "http://127.0.0.1:11434",
                "model": ollama_model_var.get().strip(),
                "timeout_seconds": parse_int(
                    ollama_timeout_var, "Limit czasu Ollamy", 1, 600
                ),
            }
        )
        if updated["ollama_cleanup"]["enabled"] and not updated[
            "ollama_cleanup"
        ]["model"]:
            raise AppError(
                "Korekta Ollama jest włączona, ale pole „Nazwa modelu” jest puste."
            )
        updated["feedback"]["custom_sounds"] = {
            kind: import_custom_sound(kind, sound_path_vars[kind].get())
            for kind in ("start", "stop", "done", "error")
        }
        return updated

    def save_from_window(apply_now: bool) -> None:
        nonlocal config
        try:
            updated = collect_config()
            save_config(updated)
            config = updated
            if apply_now:
                request_app_restart()
                status_var.set("Zapisano. Mówik uruchamia się ponownie…")
                root.after(180, root.destroy)
            else:
                status_var.set(
                    "Zapisano. Zmiany zaczną działać po ponownym uruchomieniu Mówika."
                )
        except Exception as exc:
            logging.exception("Nie udało się zapisać ustawień")
            messagebox.showerror(
                f"{APP_DISPLAY_NAME} — błąd ustawień",
                str(exc),
                parent=root,
            )

    def restore_defaults() -> None:
        trigger_var.set(ensure_trigger_display(DEFAULT_CONFIG["trigger"]))
        model_var.set(display_for_value(model_values, DEFAULT_CONFIG["model"]))
        device_var.set(display_for_value(device_values, DEFAULT_CONFIG["device"]))
        language_var.set(str(DEFAULT_CONFIG["language"]))
        cpu_threads_var.set(str(DEFAULT_CONFIG["cpu_threads"]))
        beam_size_var.set(str(DEFAULT_CONFIG["beam_size"]))
        microphone_var.set(display_for_value(microphone_values, None))
        pre_roll_var.set(str(DEFAULT_CONFIG["pre_roll_ms"]))
        post_roll_var.set(str(DEFAULT_CONFIG["post_roll_ms"]))
        minimum_recording_var.set(str(DEFAULT_CONFIG["minimum_recording_ms"]))
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
        status_var.set("Przywrócono wartości domyślne w oknie. Zapisz, aby je zastosować.")

    footer = ttk.Frame(outer)
    footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
    footer.columnconfigure(0, weight=1)
    ttk.Label(footer, textvariable=status_var, wraplength=470).grid(
        row=0, column=0, sticky="w", padx=(0, 12)
    )
    ttk.Button(
        footer,
        text="Domyślne",
        command=restore_defaults,
    ).grid(row=0, column=1, padx=4)
    ttk.Button(footer, text="Anuluj", command=root.destroy).grid(
        row=0, column=2, padx=4
    )
    ttk.Button(
        footer,
        text="Zapisz",
        command=lambda: save_from_window(False),
    ).grid(row=0, column=3, padx=4)
    ttk.Button(
        footer,
        text="Zapisz i zastosuj",
        command=lambda: save_from_window(True),
    ).grid(row=0, column=4, padx=(4, 0))

    root.bind("<Control-s>", lambda event: save_from_window(False))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return 0


class ContinuousRecorder:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = SAMPLE_RATE
        self.device = config.get("microphone")
        self.pre_roll_samples = 0
        self._set_sample_rate(SAMPLE_RATE)
        self._lock = threading.Lock()
        self._ring: deque[np.ndarray] = deque()
        self._ring_samples = 0
        self._recording = False
        self._recorded: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None

    def _set_sample_rate(self, sample_rate: int) -> None:
        self.sample_rate = max(8_000, int(round(sample_rate)))
        self.pre_roll_samples = int(
            self.sample_rate
            * max(0, int(self.config.get("pre_roll_ms", 300)))
            / 1000
        )

    def _open_stream(self, sample_rate: int, latency: str) -> sd.InputStream:
        stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            blocksize=1024,
            latency=latency,
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        return stream

    def start(self) -> None:
        try:
            device_info = sd.query_devices(self.device, "input")
            default_rate = int(round(float(device_info["default_samplerate"])))
        except Exception:
            default_rate = SAMPLE_RATE

        attempts: list[tuple[int, str]] = [(SAMPLE_RATE, "low")]
        if default_rate != SAMPLE_RATE:
            attempts.extend([(default_rate, "low"), (default_rate, "high")])
        else:
            attempts.append((SAMPLE_RATE, "high"))

        last_error: Optional[Exception] = None
        seen: set[tuple[int, str]] = set()
        for sample_rate, latency in attempts:
            if (sample_rate, latency) in seen:
                continue
            seen.add((sample_rate, latency))
            try:
                self._set_sample_rate(sample_rate)
                self._stream = self._open_stream(sample_rate, latency)
                logging.info(
                    "Mikrofon uruchomiony: %s, %d Hz, latency=%s",
                    self.device if self.device is not None else "domyślny",
                    sample_rate,
                    latency,
                )
                return
            except Exception as exc:
                last_error = exc
                logging.warning(
                    "Nie udało się otworzyć mikrofonu przy %d Hz (%s): %s",
                    sample_rate,
                    latency,
                    exc,
                )

        raise AppError(f"Nie udało się otworzyć mikrofonu: {last_error}")

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                logging.exception("Błąd zatrzymywania mikrofonu")
            try:
                stream.close()
            except Exception:
                logging.exception("Błąd zamykania mikrofonu")

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            logging.warning("Status wejścia audio: %s", status)
        chunk = np.asarray(indata[:, 0], dtype=np.float32).copy()
        with self._lock:
            if self._recording:
                self._recorded.append(chunk)
            else:
                self._ring.append(chunk)
                self._ring_samples += len(chunk)
                while self._ring and self._ring_samples > self.pre_roll_samples:
                    removed = self._ring.popleft()
                    self._ring_samples -= len(removed)

    def begin(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recorded = [part.copy() for part in self._ring]
            self._recording = True

    def finish(self) -> np.ndarray:
        with self._lock:
            if not self._recording:
                return np.empty(0, dtype=np.float32)
            self._recording = False
            parts = self._recorded
            self._recorded = []
        if not parts:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(parts).astype(np.float32, copy=False)


class MowikApp:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.trigger_type, self.trigger_name = split_trigger(str(config["trigger"]))
        self.stop_event = threading.Event()
        self.model_ready = threading.Event()
        self.busy_lock = threading.Lock()
        self.busy = False
        self.key_down = False
        self.capture_active = False
        self.model: Optional[WhisperModel] = None
        self.model_name = ""
        self.model_device = ""
        self.recorder: Optional[ContinuousRecorder] = None
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None
        self.jobs: queue.Queue[Optional[np.ndarray]] = queue.Queue()
        self.tray: Optional[pystray.Icon] = None
        self.status = "Start…"
        self._status_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._restart_started = False
        self.worker = threading.Thread(
            target=self._job_worker, name="TranscriptionWorker", daemon=True
        )
        self.control_worker = threading.Thread(
            target=self._control_watcher, name="ControlWatcher", daemon=True
        )

    def start(self) -> None:
        self.worker.start()
        self.control_worker.start()
        self._start_listeners()
        threading.Thread(
            target=self._load_runtime, name="ModelLoader", daemon=True
        ).start()

    def _control_watcher(self) -> None:
        while not self.stop_event.wait(0.35):
            if not RESTART_REQUEST_PATH.exists():
                continue
            try:
                request_text = RESTART_REQUEST_PATH.read_text(
                    encoding="ascii"
                ).strip()
                RESTART_REQUEST_PATH.unlink(missing_ok=True)
                logging.info("Odebrano prośbę o restart ustawień: %s", request_text)
                self.set_status("Stosuję nowe ustawienia…")
                self.restart()
                return
            except Exception:
                logging.exception("Nie udało się obsłużyć prośby o restart")
                try:
                    RESTART_REQUEST_PATH.unlink(missing_ok=True)
                except OSError:
                    pass

    def _start_listeners(self) -> None:
        self.keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self.keyboard_listener.start()
        self.mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
        self.mouse_listener.start()

    def _load_runtime(self) -> None:
        try:
            self.set_status("Przygotowuję mikrofon…")
            recorder = ContinuousRecorder(self.config)
            recorder.start()
            self.recorder = recorder
            model, model_name, device = create_model(self.config, self.set_status)
            self.model = model
            self.model_name = model_name
            self.model_device = device
            self.model_ready.set()
            trigger_label = trigger_display_name(str(self.config["trigger"]))
            self.set_status(
                f"Gotowy — {trigger_label}",
                notify=(
                    f"Model {model_name} działa na {device}. "
                    f"Trzymaj {trigger_label} i mów."
                ),
            )
        except Exception as exc:
            logging.exception("Błąd inicjalizacji")
            self.set_status("Błąd uruchomienia", notify=str(exc), error=True)

    def _on_key_press(self, key) -> None:
        if self.stop_event.is_set() or self.trigger_type != "keyboard":
            return
        if key_name(key) != self.trigger_name:
            return
        if not self.key_down:
            self.key_down = True
            self.begin_dictation()

    def _on_key_release(self, key) -> None:
        if self.stop_event.is_set() or self.trigger_type != "keyboard":
            return
        if key_name(key) != self.trigger_name:
            return
        if self.key_down:
            self.key_down = False
            self.end_dictation()

    def _on_mouse_click(self, x, y, button, pressed) -> None:
        if self.stop_event.is_set() or self.trigger_type != "mouse":
            return
        if mouse_name(button) != self.trigger_name:
            return
        if pressed and not self.key_down:
            self.key_down = True
            self.begin_dictation()
        elif not pressed and self.key_down:
            self.key_down = False
            self.end_dictation()

    def begin_dictation(self) -> None:
        if not self.model_ready.is_set() or self.recorder is None:
            self.beep("error")
            self.set_status("Model jeszcze nie jest gotowy")
            return
        with self.busy_lock:
            if self.busy:
                self.beep("error")
                self.set_status("Kończę poprzednią transkrypcję…")
                return
            self.busy = True
        try:
            self.recorder.begin()
            self.capture_active = True
        except Exception:
            self._release_busy()
            raise
        self.set_status("Nagrywanie…")
        self.beep("start")

    def end_dictation(self) -> None:
        if not self.capture_active:
            return
        self.capture_active = False
        # Kończymy także dłuższy, niezapętlony WAV przypisany do nagrywania.
        self.stop_feedback_sound()
        threading.Thread(
            target=self._finish_dictation_after_tail,
            name="PostRoll",
            daemon=True,
        ).start()

    def _finish_dictation_after_tail(self) -> None:
        post_roll = max(0, int(self.config.get("post_roll_ms", 160))) / 1000
        if post_roll:
            time.sleep(post_roll)
        if self.stop_event.is_set():
            self._release_busy()
            return
        recorder = self.recorder
        if recorder is None:
            self._release_busy()
            return
        audio = recorder.finish()
        self.beep("stop")
        minimum_samples = int(
            recorder.sample_rate
            * max(0, int(self.config.get("minimum_recording_ms", 250)))
            / 1000
        )
        if len(audio) < minimum_samples:
            self.set_status("Nagranie było zbyt krótkie")
            self._release_busy()
            return
        self.set_status("Rozpoznaję mowę…")
        self.jobs.put(audio)

    def _job_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                audio = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if audio is None:
                break
            try:
                text = self.transcribe(audio)
                if not text:
                    self.set_status("Nie wykryłem wyraźnej mowy")
                    self.beep("error")
                    continue
                paste_settings = self.config.get("paste", {})
                paste_enabled = bool(paste_settings.get("enabled", True))
                copy_enabled = bool(
                    paste_settings.get("copy_to_clipboard", True)
                )
                if paste_enabled and copy_enabled:
                    self.set_status("Wklejam i kopiuję tekst…")
                elif paste_enabled:
                    self.set_status("Wklejam tekst…")
                else:
                    self.set_status("Kopiuję tekst do schowka…")
                paste_text(text, self.config)
                logging.info(
                    "Dostarczono tekst (%d znaków; wklejanie=%s; schowek=%s)",
                    len(text),
                    paste_enabled,
                    copy_enabled,
                )
                self.set_status(
                    f"Gotowy — {trigger_display_name(str(self.config['trigger']))}"
                )
                self.beep("done")
            except Exception as exc:
                logging.exception("Błąd przetwarzania dyktowania")
                self.set_status("Błąd dyktowania", notify=str(exc), error=True)
                self.beep("error")
            finally:
                self._release_busy()
                self.jobs.task_done()

    def transcribe(self, audio: np.ndarray) -> str:
        model = self.model
        if model is None:
            raise AppError("Model nie jest załadowany.")
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return ""
        audio = np.clip(audio - float(np.mean(audio)), -1.0, 1.0)
        rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
        sample_rate = (
            self.recorder.sample_rate if self.recorder is not None else SAMPLE_RATE
        )
        logging.info("Audio: %.2f s, %d Hz, RMS=%.6f", len(audio) / sample_rate, sample_rate, rms)
        if rms < float(self.config.get("minimum_rms", 0.0015)):
            return ""

        audio_input: Any = audio
        if sample_rate != SAMPLE_RATE:
            # faster-whisper/PyAV resampluje plik WAV do 16 kHz w pamięci.
            # Nie zapisujemy nagrania na dysku.
            pcm = np.asarray(np.clip(audio, -1.0, 1.0) * 32767, dtype=np.int16)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm.tobytes())
            buffer.seek(0)
            audio_input = buffer

        dictionary_terms = load_dictionary(self.config)
        glossary = ", ".join(dictionary_terms)
        initial_prompt = None
        if glossary:
            initial_prompt = glossary[:1800]

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
            "speech_pad_ms": int(vad_settings.get("speech_pad_ms", 180)),
        }

        configured_language = str(self.config.get("language", "pl")).strip()
        language: Optional[str] = None if configured_language.lower() == "auto" else configured_language

        segments, info = model.transcribe(
            audio_input,
            language=language,
            task="transcribe",
            beam_size=max(1, int(self.config.get("beam_size", 5))),
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt,
            hotwords=glossary[:1800] if glossary else None,
            vad_filter=vad_enabled,
            vad_parameters=vad_parameters if vad_enabled else None,
            without_timestamps=True,
        )
        transcript = "".join(segment.text for segment in segments)
        logging.info(
            "Whisper: język=%s prawdopodobieństwo=%.3f, znaków=%d",
            getattr(info, "language", "?"),
            float(getattr(info, "language_probability", 0.0)),
            len(transcript),
        )
        transcript = normalize_transcript(transcript)
        transcript = apply_voice_commands(transcript, self.config)
        transcript = cleanup_with_ollama(
            transcript, self.config, dictionary_terms
        )
        return normalize_transcript(transcript)

    def _release_busy(self) -> None:
        with self.busy_lock:
            self.busy = False

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
        tones = {
            "start": (880, 55),
            "stop": (520, 45),
            "done": (1100, 45),
            "error": (260, 120),
        }
        frequency, duration = tones.get(kind, (700, 50))
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

        def play_tone() -> None:
            try:
                import winsound

                winsound.Beep(frequency, duration)
            except Exception:
                logging.debug(
                    "Nie udało się odtworzyć sygnału awaryjnego",
                    exc_info=True,
                )

        threading.Thread(
            target=play_tone,
            name=f"Feedback-{kind}",
            daemon=True,
        ).start()

    def set_status(
        self,
        status: str,
        notify: Optional[str] = None,
        error: bool = False,
    ) -> None:
        with self._status_lock:
            self.status = status
        logging.error(status) if error else logging.info(status)
        tray = self.tray
        if tray is not None:
            try:
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
            return f"Status: {self.status}"

    def open_settings(self, icon=None, item=None) -> None:
        try:
            subprocess.Popen(
                settings_process_args(),
                cwd=str(Path(__file__).resolve().parent),
            )
        except Exception as exc:
            logging.exception("Nie udało się otworzyć panelu ustawień")
            self.set_status("Błąd otwierania ustawień", notify=str(exc), error=True)

    def open_config(self, icon=None, item=None) -> None:
        os.startfile(CONFIG_PATH)  # type: ignore[attr-defined]

    def open_dictionary(self, icon=None, item=None) -> None:
        os.startfile(DICTIONARY_PATH)  # type: ignore[attr-defined]

    def open_log(self, icon=None, item=None) -> None:
        os.startfile(LOG_PATH)  # type: ignore[attr-defined]

    def open_app_folder(self, icon=None, item=None) -> None:
        os.startfile(APPDATA_DIR)  # type: ignore[attr-defined]

    def apply_profile(self, profile_name: str) -> None:
        try:
            profile = QUICK_PROFILES[profile_name]
            updated = apply_quick_profile(load_config(), profile_name)
            save_config(updated)
            self.set_status(
                f"Włączam profil {profile['label']}…",
                notify=(
                    f"Profil {profile['label']}: {profile['description']}. "
                    "Mówik uruchomi się ponownie."
                ),
            )
            self.restart()
        except Exception as exc:
            logging.exception("Nie udało się zastosować profilu %s", profile_name)
            self.set_status("Błąd zmiany profilu", notify=str(exc), error=True)

    def apply_light_profile(self, icon=None, item=None) -> None:
        self.apply_profile("light")

    def apply_balanced_profile(self, icon=None, item=None) -> None:
        self.apply_profile("balanced")

    def apply_accurate_profile(self, icon=None, item=None) -> None:
        self.apply_profile("accurate")

    def restart(self, icon=None, item=None) -> None:
        with self._restart_lock:
            if self._restart_started:
                return
            self._restart_started = True
        try:
            if getattr(sys, "frozen", False):
                args = [sys.executable, "--restart-delay", "1.0"]
            else:
                args = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--restart-delay",
                    "1.0",
                ]
            subprocess.Popen(args, cwd=str(Path(__file__).resolve().parent))
        except Exception:
            with self._restart_lock:
                self._restart_started = False
            raise
        else:
            self.shutdown(icon, item)

    def shutdown(self, icon=None, item=None) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.stop_feedback_sound()
        self.model_ready.clear()
        self.jobs.put(None)
        if self.keyboard_listener is not None:
            self.keyboard_listener.stop()
        if self.mouse_listener is not None:
            self.mouse_listener.stop()
        if self.recorder is not None:
            self.recorder.close()
        if icon is not None:
            icon.stop()
        elif self.tray is not None:
            self.tray.stop()

    def run_tray(self) -> None:
        profiles_menu = pystray.Menu(
            pystray.MenuItem(
                "Lekki — small / beam 1",
                self.apply_light_profile,
            ),
            pystray.MenuItem(
                "Zbalansowany — Turbo / beam 2",
                self.apply_balanced_profile,
            ),
            pystray.MenuItem(
                "Dokładny — large-v3 / beam 5",
                self.apply_accurate_profile,
            ),
        )
        menu = pystray.Menu(
            pystray.MenuItem(self.tray_status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Panel ustawień…",
                self.open_settings,
                default=True,
            ),
            pystray.MenuItem("Szybki profil", profiles_menu),
            pystray.MenuItem("Otwórz słownik", self.open_dictionary),
            pystray.MenuItem("Edytuj config.json", self.open_config),
            pystray.MenuItem("Otwórz log", self.open_log),
            pystray.MenuItem("Folder konfiguracji", self.open_app_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Uruchom ponownie", self.restart),
            pystray.MenuItem("Zakończ", self.shutdown),
        )
        self.tray = pystray.Icon(
            APP_NAME,
            make_tray_image(),
            f"{APP_DISPLAY_NAME} — Start…",
            menu,
        )
        self.start()
        self.tray.run()


def acquire_single_instance() -> Optional[int]:
    if os.name != "nt":
        return None
    # use_last_error zachowuje kod błędu od razu po wywołaniu CreateMutexW;
    # zwykłe GetLastError może zostać nadpisane przez inne wywołania Win32.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Mówik jest już uruchomiony. Poszukaj ikony mikrofonu przy zegarze.",
            APP_DISPLAY_NAME,
            0x40,
        )
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_single_instance(handle: Optional[int]) -> None:
    if handle and os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def show_fatal_error(message: str) -> None:
    logging.error(message)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            f"{APP_DISPLAY_NAME} — błąd",
            0x10,
        )
    else:
        print(message, file=sys.stderr)


def list_devices() -> int:
    print(sd.query_devices())
    print(f"\nDomyślne urządzenia (wejście, wyjście): {sd.default.device}")
    return 0


def download_model_command(config: dict[str, Any]) -> int:
    def status(text: str) -> None:
        print(text, flush=True)

    model, model_name, device = create_model(config, status)
    del model
    print(f"\nGotowe. Model {model_name} jest zapisany lokalnie i działa na {device}.")
    print(f"Folder modeli: {MODEL_DIR}")
    return 0


def test_ollama_command(config: dict[str, Any]) -> int:
    sample = "to jest test lokalnego korektora i on ma nie zmieniac sensu"
    result = cleanup_with_ollama(sample, config, load_dictionary(config))
    print("Wejście:", sample)
    print("Wynik:  ", result)
    if result == sample and config.get("ollama_cleanup", {}).get("enabled", False):
        print("Uwaga: wynik nie został zmieniony; sprawdź log oraz konfigurację Ollama.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP_DISPLAY_NAME} {APP_VERSION}")
    parser.add_argument("--list-devices", action="store_true", help="Pokaż mikrofony")
    parser.add_argument(
        "--download-model",
        action="store_true",
        help="Pobierz/załaduj model i zakończ",
    )
    parser.add_argument(
        "--create-config",
        action="store_true",
        help="Utwórz domyślne pliki konfiguracji i zakończ",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="Otwórz graficzny panel ustawień",
    )
    parser.add_argument(
        "--test-ollama",
        action="store_true",
        help="Sprawdź opcjonalny lokalny korektor Ollama",
    )
    parser.add_argument(
        "--console-log", action="store_true", help="Pokaż log również w konsoli"
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=0.0,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(console=args.console_log or args.download_model or args.list_devices)
    create_default_files()

    if args.create_config:
        print(f"Konfiguracja: {CONFIG_PATH}")
        print(f"Słownik:       {DICTIONARY_PATH}")
        return 0
    if args.settings:
        return run_settings_window()
    if args.list_devices:
        return list_devices()

    config = load_config()
    if args.restart_delay > 0:
        time.sleep(min(args.restart_delay, 5.0))
    if args.download_model:
        return download_model_command(config)
    if args.test_ollama:
        return test_ollama_command(config)

    if os.name != "nt":
        raise AppError("Aplikacja okienkowa Mówik jest przeznaczona dla Windows 10/11.")

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Mowik.LocalDictation"
        )
    except Exception:
        pass

    mutex_handle = acquire_single_instance()
    if mutex_handle is None:
        return 0
    try:
        app = MowikApp(config)
        app.run_tray()
    finally:
        release_single_instance(mutex_handle)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        setup_logging(console=True)
        logging.error("Błąd krytyczny:\n%s", traceback.format_exc())
        show_fatal_error(f"{exc}\n\nSzczegóły: {LOG_PATH}")
        raise SystemExit(1)
