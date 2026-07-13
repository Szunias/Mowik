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
from functools import lru_cache
from typing import Any, Optional

from mowik_i18n import Translator


_CUDA_DLL_DIRECTORY_HANDLES: list[Any] = []
_CUDA_DLL_HANDLES: list[Any] = []


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
            for package in ("cuda_nvrtc", "cuda_runtime", "cudnn")
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

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from faster_whisper.audio import pad_or_trim
import ctranslate2
from PIL import Image, ImageDraw
from pynput import keyboard, mouse
import pystray
import pyperclip


APP_NAME = "Mowik"
APP_DISPLAY_NAME = "Mówik"
APP_VERSION = "2.4.0"
MUTEX_NAME = r"Local\MowikLocalDictation"
SAMPLE_RATE = 16_000

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
        "delay_ms": 25,
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
    fallback_translator = Translator("auto")
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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


def request_app_restart() -> None:
    """Poproś działającą instancję o ponowne uruchomienie po zapisie ustawień."""
    ensure_directories()
    temp_path = RESTART_REQUEST_PATH.with_name(
        f"{RESTART_REQUEST_PATH.name}.{os.getpid()}.tmp"
    )
    temp_path.write_text(f"{time.time():.6f}\n", encoding="ascii")
    os.replace(temp_path, RESTART_REQUEST_PATH)


@lru_cache(maxsize=4)
def _load_dictionary_snapshot(
    modified_ns: int, file_size: int, limit: int
) -> tuple[str, ...]:
    # modified_ns i file_size są częścią klucza cache; sam odczyt zawsze dotyczy
    # stałej ścieżki prywatnego słownika.
    try:
        lines = DICTIONARY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    terms: list[str] = []
    seen: set[str] = set()
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


def get_cuda_count() -> int:
    try:
        count = int(ctranslate2.get_cuda_device_count())
    except Exception:
        logging.exception("Nie udało się sprawdzić urządzeń CUDA")
        return 0
    if count > 0 and not windows_cuda_runtime_present():
        return 0
    return count


def resolve_model_plan(
    config: dict[str, Any],
    translator: Optional[Translator] = None,
) -> tuple[str, str, str]:
    translator = translator or Translator("pl")
    requested_device = str(config.get("device", "auto")).lower().strip()
    requested_model = str(config.get("model", "auto")).strip()
    cuda_available = get_cuda_count() > 0

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


def load_model_local_first(
    model_name: str,
    kwargs: dict[str, Any],
    status_callback=None,
    translator: Optional[Translator] = None,
) -> WhisperModel:
    """Załaduj cache bez odpytywania Hugging Face; sieć tylko przy braku plików."""
    translator = translator or Translator("pl")
    try:
        return WhisperModel(model_name, local_files_only=True, **kwargs)
    except Exception as local_error:
        logging.info(
            "Model %s nie uruchomił się wyłącznie z lokalnego cache (%s); "
            "sprawdzam/pobieram pliki.",
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
        return WhisperModel(model_name, local_files_only=False, **kwargs)


def warm_up_cuda_model(model: WhisperModel, config: dict[str, Any]) -> None:
    """Rozgrzej encoder i sprawdź CUDA bez uruchamiania dekodera na ciszy."""
    del config  # zachowany parametr ułatwia testowanie tej samej ścieżki
    features = model.feature_extractor(
        np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
    )
    model.encode(pad_or_trim(features))


def create_model(
    config: dict[str, Any], status_callback=None
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
    try:
        model = load_model_local_first(
            model_name,
            kwargs,
            status_callback,
            translator,
        )
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
            "Fallback CPU po błędzie CUDA (%s). Model: %s", exc, fallback_model
        )
        model = load_model_local_first(
            fallback_model,
            {
                "device": "cpu",
                "compute_type": "int8",
                "download_root": str(MODEL_DIR),
                "cpu_threads": cpu_threads,
            },
            status_callback,
            translator,
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
    if original_negations != corrected_negations:
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
    transcription_language = str(config.get("language", "auto")).strip().lower()
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
    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as exc:
        raise AppError(
            translator.t(
                "Nie udało się zapisać tekstu do schowka: {error}",
                "Could not copy text to the clipboard: {error}",
                error=exc,
            )
        ) from exc


def windows_type_unicode_text(
    text: str,
    translator: Optional[Translator] = None,
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
                translator.t(
                    "Windows wysłał tylko {sent} z {total} zdarzeń klawiatury.",
                    "Windows sent only {sent} of {total} keyboard events.",
                    sent=sent,
                    total=len(inputs),
                )
            )


def paste_text(text: str, config: dict[str, Any]) -> None:
    translator = Translator.from_config(config)
    settings = config.get("paste", {})
    paste_enabled = bool(settings.get("enabled", True))
    copy_to_clipboard = bool(settings.get("copy_to_clipboard", True))
    append_space = bool(settings.get("append_space", True))

    if not paste_enabled and not copy_to_clipboard:
        raise AppError(
            translator.t(
                "Włącz automatyczne wklejanie lub kopiowanie tekstu do schowka.",
                "Enable automatic pasting or copying text to the clipboard.",
            )
        )

    # Schowek zawiera dokładną transkrypcję. Ewentualną końcową spację
    # wysyłamy osobno, dzięki czemu nie zostaje dopisana do kopiowanego tekstu.
    if copy_to_clipboard:
        windows_set_clipboard_text(text, translator)

    if not paste_enabled:
        return

    delay = max(0, int(settings.get("delay_ms", 25))) / 1000
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
        windows_type_unicode_text(payload, translator)


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
    elif len(name) == 1:
        label = name.upper()
    elif re.fullmatch(r"f\d{1,2}", name):
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


def settings_process_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--settings"]
    return [sys.executable, str(Path(__file__).resolve()), "--settings"]


def run_settings_window() -> int:
    """Uruchom osobny panel ustawień oparty na wbudowanym Tkinterze."""
    config = load_config()
    translator = Translator.from_config(config)
    t = translator.t

    try:
        import tkinter as tk
        from tkinter import filedialog, font as tkfont, messagebox, ttk
    except ImportError as exc:
        raise AppError(
            t(
                "Brakuje składnika Tkinter. Zainstaluj standardowy 64-bitowy "
                "Python 3.10–3.12 z python.org albo uruchom "
                "NAPRAW_INSTALACJE.cmd.",
                "Tkinter is missing. Install standard 64-bit Python 3.10–3.12 "
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
    window_width = min(1120, max(480, screen_width - 64))
    window_height = min(780, max(420, screen_height - 96))
    window_x = max(0, (screen_width - window_width) // 2)
    window_y = max(0, (screen_height - window_height) // 2)
    root.geometry(
        f"{window_width}x{window_height}+{window_x}+{window_y}"
    )
    root.minsize(min(960, window_width), min(660, window_height))
    try:
        root.iconbitmap(default=str(RESOURCE_ROOT / "assets" / "Mowik.ico"))
    except tk.TclError:
        logging.debug("Nie udało się ustawić ikony okna", exc_info=True)

    colors = {
        "canvas": "#F3F6FA",
        "surface": "#FFFFFF",
        "surface_alt": "#F8FAFC",
        "surface_hover": "#EEF2F7",
        "sidebar": "#0F172A",
        "sidebar_active": "#1E3A5F",
        "sidebar_hover": "#182B46",
        "sidebar_success": "#12372D",
        "sidebar_success_text": "#86E7BD",
        "text": "#0F172A",
        "muted": "#5B667A",
        "border": "#D8E0EA",
        "primary": "#2563EB",
        "primary_hover": "#1D4ED8",
        "primary_soft": "#EFF6FF",
        "primary_border": "#BFDBFE",
        "success": "#0F6B45",
        "success_soft": "#ECFDF5",
        "success_border": "#B7E4CF",
        "warning": "#9A5A00",
        "danger": "#B4232F",
        "white": "#FFFFFF",
    }
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

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    base_font = (ui_font_family, 10)
    style.configure(".", font=base_font)
    style.configure("TFrame", background=colors["surface"])
    style.configure("App.TFrame", background=colors["canvas"])
    style.configure("Surface.TFrame", background=colors["surface"])
    style.configure("Sidebar.TFrame", background=colors["sidebar"])
    style.configure("TLabel", background=colors["surface"], foreground=colors["text"])
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
        foreground="#CBD5E1",
        font=(ui_font_family, 9),
    )
    style.configure(
        "SidebarSection.TLabel",
        background=colors["sidebar"],
        foreground="#94A3B8",
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
        padding=(14, 9),
    )
    style.map(
        "TButton",
        background=[
            ("pressed", "#E3E9F1"),
            ("active", colors["surface_hover"]),
        ],
        bordercolor=[("focus", colors["primary"]), ("active", "#C9D2DF")],
    )
    style.configure(
        "Primary.TButton",
        background=colors["primary"],
        foreground=colors["white"],
        bordercolor=colors["primary"],
        lightcolor=colors["primary"],
        darkcolor=colors["primary"],
        font=(ui_font_family, 10, "bold"),
        padding=(17, 10),
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", "#AFC4F2"),
            ("pressed", "#1E40AF"),
            ("active", colors["primary_hover"]),
        ],
        foreground=[("disabled", "#F5F8FF")],
        bordercolor=[("disabled", "#AFC4F2")],
    )
    style.configure(
        "Nav.TButton",
        background=colors["sidebar"],
        foreground="#C7D5E6",
        bordercolor=colors["sidebar"],
        lightcolor=colors["sidebar"],
        darkcolor=colors["sidebar"],
        anchor="w",
        padding=(18, 11),
        font=(ui_font_family, 10),
    )
    style.map(
        "Nav.TButton",
        background=[("active", colors["sidebar_hover"])],
        foreground=[("active", colors["white"])],
        bordercolor=[("focus", "#60A5FA")],
    )
    style.configure(
        "NavActive.TButton",
        background=colors["sidebar_active"],
        foreground=colors["white"],
        bordercolor=colors["sidebar_active"],
        lightcolor=colors["sidebar_active"],
        darkcolor=colors["sidebar_active"],
        anchor="w",
        padding=(18, 11),
        font=(ui_font_family, 10, "bold"),
    )
    style.map(
        "NavActive.TButton",
        background=[("active", colors["sidebar_active"])],
        foreground=[("active", colors["white"])],
        bordercolor=[("focus", "#60A5FA")],
    )
    style.configure(
        "Profile.TButton",
        anchor="w",
        padding=(14, 12),
        font=(ui_font_family, 9),
    )
    style.configure(
        "SelectedProfile.TButton",
        background=colors["primary_soft"],
        foreground=colors["primary_hover"],
        bordercolor=colors["primary_border"],
        lightcolor=colors["primary_border"],
        darkcolor=colors["primary_border"],
        anchor="w",
        padding=(14, 12),
        font=(ui_font_family, 9, "bold"),
    )
    style.map(
        "SelectedProfile.TButton",
        background=[("active", "#DDE9FF")],
        bordercolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TEntry",
        fieldbackground=colors["surface"],
        foreground=colors["text"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        insertcolor=colors["text"],
        padding=9,
    )
    style.configure(
        "TCombobox",
        fieldbackground=colors["surface"],
        foreground=colors["text"],
        background=colors["surface_alt"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        arrowcolor=colors["muted"],
        padding=7,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", colors["surface"])],
        selectbackground=[("readonly", colors["surface"])],
        selectforeground=[("readonly", colors["text"])],
        bordercolor=[("focus", colors["primary"])],
    )
    style.configure(
        "TSpinbox",
        fieldbackground=colors["surface"],
        foreground=colors["text"],
        background=colors["surface_alt"],
        bordercolor=colors["border"],
        lightcolor=colors["border"],
        darkcolor=colors["border"],
        arrowcolor=colors["muted"],
        padding=7,
    )
    style.configure(
        "TCheckbutton",
        background=colors["surface"],
        foreground=colors["text"],
        padding=(0, 4),
    )
    style.map(
        "TCheckbutton",
        background=[("active", colors["surface"])],
        foreground=[("disabled", "#98A3B3")],
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
        padding=(4, 0, 8, 0),
    )
    style.configure(
        "Vertical.TScrollbar",
        background="#C8D1DF",
        troughcolor=colors["surface"],
        bordercolor=colors["surface"],
        arrowcolor=colors["muted"],
    )
    style.configure("TSeparator", background=colors["border"])

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
        t("base — bardzo lekki", "base — very lightweight"): "base",
        t("small — lekki (~0,5 GB)", "small — lightweight (~0.5 GB)"): "small",
        t("medium — dokładniejszy", "medium — more accurate"): "medium",
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
        t("Polski", "Polish"): "pl",
        t("Angielski", "English"): "en",
        t("Niemiecki", "German"): "de",
        t("Francuski", "French"): "fr",
        t("Hiszpański", "Spanish"): "es",
        t("Ukraiński", "Ukrainian"): "uk",
        t("Wykryj automatycznie", "Detect automatically"): "auto",
    }
    ui_language_values: dict[str, Any] = {
        t("Automatycznie (Windows)", "Automatic (Windows)"): "auto",
        "Polski": "pl",
        "English": "en",
    }
    microphone_values: dict[str, Any] = {}

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
        elif len(name) == 1:
            label = name.upper()
        elif re.fullmatch(r"f\d{1,2}", name):
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

    pre_roll_var = tk.StringVar(value=str(config.get("pre_roll_ms", 300)))
    post_roll_var = tk.StringVar(value=str(config.get("post_roll_ms", 120)))
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
    paste_delay_var = tk.StringVar(value=str(paste.get("delay_ms", 25)))

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

    status_var = tk.StringVar(
        value=t(
            "Wszystko gotowe — ustawienia są zapisane.",
            "Everything is ready — settings are saved.",
        )
    )
    status_level = {"value": "success"}
    shortcut_summary_var = tk.StringVar()

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

    sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=216)
    sidebar.grid(row=0, column=0, sticky="ns")
    sidebar.grid_propagate(False)
    sidebar.columnconfigure(0, weight=1)
    sidebar.rowconfigure(20, weight=1)

    brand = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(18, 24, 18, 18))
    brand.grid(row=0, column=0, sticky="ew")
    brand.columnconfigure(1, weight=1)
    try:
        from PIL import ImageTk

        brand_image = make_tray_image("brand").resize((38, 38))
        root._mowik_brand_icon = ImageTk.PhotoImage(brand_image)  # type: ignore[attr-defined]
        ttk.Label(
            brand,
            image=root._mowik_brand_icon,  # type: ignore[attr-defined]
            style="SidebarMeta.TLabel",
        ).grid(row=0, column=0, rowspan=2, padx=(0, 11))
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
        padx=12,
        pady=7,
        anchor="w",
    )
    privacy_badge.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 20))

    main = ttk.Frame(shell, style="Surface.TFrame")
    main.grid(row=0, column=1, sticky="nsew")
    main.rowconfigure(2, weight=1)
    main.columnconfigure(0, weight=1)

    header = ttk.Frame(main, style="Surface.TFrame", padding=(32, 24, 32, 18))
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(0, weight=1)
    ttk.Label(header, textvariable=page_title_var, style="Title.TLabel").grid(
        row=0, column=0, sticky="w"
    )
    ttk.Label(
        header,
        textvariable=page_subtitle_var,
        style="Subtitle.TLabel",
        wraplength=650,
    ).grid(row=1, column=0, sticky="w", pady=(3, 0))
    local_chip = tk.Label(
        header,
        text=t("Prywatnie i offline", "Private and offline"),
        background=colors["success_soft"],
        foreground=colors["success"],
        font=(ui_font_family, 9, "bold"),
        padx=11,
        pady=6,
    )
    local_chip.grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 0))
    ttk.Separator(main).grid(row=1, column=0, sticky="ew")

    page_host = ttk.Frame(main, style="Surface.TFrame", padding=(32, 0, 22, 0))
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
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        content = ttk.Frame(
            canvas,
            style="Surface.TFrame",
            padding=(0, 16, 8, 28),
        )
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

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
    sounds_page, sounds_tab, sounds_canvas = create_scrollable_page(page_host)
    ollama_page, ollama_tab, ollama_canvas = create_scrollable_page(page_host)
    files_page, files_tab, files_canvas = create_scrollable_page(page_host)

    page_frames = {
        "start": start_page,
        "dictation": general_page,
        "audio": audio_page,
        "text": text_page,
        "sounds": sounds_page,
        "integrations": ollama_page,
        "help": files_page,
    }
    page_canvases = {
        "start": start_canvas,
        "dictation": general_canvas,
        "audio": audio_canvas,
        "text": text_canvas,
        "sounds": sounds_canvas,
        "integrations": ollama_canvas,
        "help": files_canvas,
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

    nav_buttons: dict[str, ttk.Button] = {}
    active_page_key = {"value": "start"}

    def scroll_active_page(event) -> None:
        if event.delta:
            page_canvases[active_page_key["value"]].yview_scroll(
                int(-event.delta / 120), "units"
            )

    root.bind_all("<MouseWheel>", scroll_active_page)

    def show_page(page_key: str) -> None:
        title, subtitle = page_meta[page_key]
        active_page_key["value"] = page_key
        page_title_var.set(title)
        page_subtitle_var.set(subtitle)
        page_frames[page_key].tkraise()
        for key, button in nav_buttons.items():
            button.configure(
                style="NavActive.TButton" if key == page_key else "Nav.TButton"
            )
        root.after_idle(lambda: page_canvases[page_key].yview_moveto(0))

    nav_row = 2
    for section, items in (
        (t("CENTRUM", "CENTER"), (("start", t("Start", "Home")),)),
        (
            t("USTAWIENIA", "SETTINGS"),
            (
                ("dictation", t("Dyktowanie", "Dictation")),
                ("audio", t("Mikrofon i mowa", "Microphone and speech")),
                ("text", t("Tekst i słownik", "Text and dictionary")),
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
            padx=19,
            pady=(7 if nav_row == 2 else 18, 6),
        )
        nav_row += 1
        for page_key, label in items:
            button = ttk.Button(
                sidebar,
                text=label,
                style="Nav.TButton",
                command=lambda selected=page_key: show_page(selected),
            )
            button.grid(row=nav_row, column=0, sticky="ew", padx=10, pady=1)
            nav_buttons[page_key] = button
            nav_row += 1

    ttk.Label(
        sidebar,
        text=f"Mówik {APP_VERSION}\nWindows 10/11",
        style="SidebarMeta.TLabel",
        justify="left",
    ).grid(row=21, column=0, sticky="sw", padx=19, pady=18)

    ttk.Separator(main).grid(row=3, column=0, sticky="ew")
    footer = ttk.Frame(main, style="Surface.TFrame", padding=(32, 14, 32, 16))
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
        padx=24,
        pady=22,
    )
    hero.grid(row=0, column=0, sticky="ew", pady=(0, 16))
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
        wraplength=470,
    ).grid(row=1, column=0, sticky="w", pady=(5, 5))
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
        wraplength=470,
    ).grid(row=2, column=0, sticky="w")
    shortcut_keycap = tk.Label(
        hero,
        textvariable=shortcut_summary_var,
        background=colors["surface"],
        foreground=colors["text"],
        font=(ui_font_family, 12, "bold"),
        relief="solid",
        borderwidth=1,
        padx=18,
        pady=10,
    )
    shortcut_keycap.grid(row=0, column=1, rowspan=2, sticky="e", padx=(20, 0))
    ttk.Button(
        hero,
        text=t("Zmień skrót", "Change shortcut"),
        command=lambda: show_page("dictation"),
    ).grid(row=2, column=1, sticky="e", padx=(20, 0), pady=(10, 0))

    overview = ttk.Frame(start_tab, style="Surface.TFrame")
    overview.grid(row=1, column=0, sticky="ew", pady=(0, 16))
    for column in range(3):
        overview.columnconfigure(column, weight=1, uniform="overview")

    def add_overview_card(
        parent, column: int, eyebrow: str, value_var: tk.Variable, description: str
    ) -> None:
        card = tk.Frame(
            parent,
            background=colors["surface_alt"],
            highlightbackground=colors["border"],
            highlightthickness=1,
            padx=16,
            pady=14,
        )
        card.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 6, 0 if column == 2 else 6),
        )
        tk.Label(
            card,
            text=eyebrow,
            background=colors["surface_alt"],
            foreground=colors["muted"],
            font=(ui_font_family, 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            card,
            textvariable=value_var,
            background=colors["surface_alt"],
            foreground=colors["text"],
            font=(ui_font_family, 10, "bold"),
            justify="left",
            anchor="w",
            wraplength=190,
        ).pack(anchor="w", fill="x", pady=(5, 3))
        tk.Label(
            card,
            text=description,
            background=colors["surface_alt"],
            foreground=colors["muted"],
            font=(ui_font_family, 9),
            justify="left",
            anchor="w",
            wraplength=190,
        ).pack(anchor="w", fill="x")

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
        t("MIKROFON", "MICROPHONE"),
        microphone_var,
        t("Aktywne źródło dźwięku", "Active audio source"),
    )
    add_overview_card(
        overview,
        2,
        t("MODEL", "MODEL"),
        model_var,
        t("Rozpoznawanie lokalne", "Local recognition"),
    )

    privacy_panel = tk.Frame(
        start_tab,
        background=colors["success_soft"],
        highlightbackground=colors["success_border"],
        highlightthickness=1,
        padx=18,
        pady=14,
    )
    privacy_panel.grid(row=2, column=0, sticky="ew", pady=(0, 16))
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
        wraplength=700,
    ).pack(anchor="w", pady=(4, 0))

    quick_actions = ttk.LabelFrame(
        start_tab,
        text=t("Szybkie działania", "Quick actions"),
        padding=16,
    )
    quick_actions.grid(row=4, column=0, sticky="ew", pady=(16, 0))
    for column in range(3):
        quick_actions.columnconfigure(column, weight=1)
    ttk.Button(
        quick_actions,
        text=t("Ustaw dyktowanie", "Set up dictation"),
        command=lambda: show_page("dictation"),
    ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
    ttk.Button(
        quick_actions,
        text=t("Otwórz słownik", "Open dictionary"),
        command=lambda: show_page("text"),
    ).grid(row=0, column=1, sticky="ew", padx=5)
    ttk.Button(
        quick_actions,
        text=t("Diagnostyka", "Diagnostics"),
        command=lambda: show_page("help"),
    ).grid(row=0, column=2, sticky="ew", padx=(5, 0))

    ui_language_frame = ttk.LabelFrame(
        start_tab,
        text=t("Język interfejsu", "Interface language"),
        padding=16,
    )
    ui_language_frame.grid(row=3, column=0, sticky="ew")
    ui_language_frame.columnconfigure(0, weight=1)
    ttk.Combobox(
        ui_language_frame,
        textvariable=ui_language_var,
        values=list(ui_language_values.keys()),
        state="readonly",
    ).grid(row=0, column=0, sticky="ew")
    ttk.Label(
        ui_language_frame,
        text=t(
            "Zmiana będzie widoczna po zastosowaniu ustawień i ponownym uruchomieniu Mówika.",
            "The change takes effect after applying settings and restarting Mówik.",
        ),
        style="Muted.TLabel",
        wraplength=700,
    ).grid(row=1, column=0, sticky="w", pady=(7, 0))

    for tab in (
        general_tab,
        audio_tab,
        text_tab,
        sounds_tab,
        ollama_tab,
        files_tab,
    ):
        tab.columnconfigure(1, weight=1)

    presets = ttk.LabelFrame(
        general_tab,
        text=t("Profil jakości", "Quality profile"),
        padding=16,
    )
    presets.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
    for column in range(3):
        presets.columnconfigure(column, weight=1)

    profile_buttons: dict[str, ttk.Button] = {}
    profile_labels = {
        "light": t("Szybki", "Fast"),
        "balanced": t("Zalecany", "Recommended"),
        "accurate": t("Najdokładniejszy", "Most accurate"),
    }

    def refresh_profile_buttons(*args) -> None:
        selected_model = str(model_values.get(model_var.get(), model_var.get()))
        try:
            selected_beam = int(beam_size_var.get())
        except ValueError:
            selected_beam = -1
        for profile_name, button in profile_buttons.items():
            changes = QUICK_PROFILES[profile_name]["changes"]
            selected = (
                selected_model == changes["model"]
                and selected_beam == changes["beam_size"]
            )
            button.configure(
                style="SelectedProfile.TButton" if selected else "Profile.TButton"
            )

    def set_profile(profile_name: str) -> None:
        profile = QUICK_PROFILES[profile_name]
        changes = profile["changes"]
        model_var.set(display_for_value(model_values, changes["model"]))
        device_var.set(display_for_value(device_values, changes["device"]))
        beam_size_var.set(str(changes["beam_size"]))
        set_status(
            t(
                "Wybrano profil „{profile}”. Zastosuj zmiany, aby go uruchomić.",
                "Selected the “{profile}” profile. Apply changes to activate it.",
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
                    "Szybki\nsmall · najmniejsze obciążenie",
                    "Fast\nsmall · lowest load",
                ),
            ),
            (
                "balanced",
                t(
                    "Zalecany\nTurbo · dobry balans",
                    "Recommended\nTurbo · balanced",
                ),
            ),
            (
                "accurate",
                t(
                    "Najdokładniejszy\nlarge-v3 · najwyższa jakość",
                    "Most accurate\nlarge-v3 · highest quality",
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
            padx=(0 if column == 0 else 5, 0 if column == 2 else 5),
        )
        profile_buttons[profile_name] = button
    model_var.trace_add("write", refresh_profile_buttons)
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
            row=row, column=0, sticky="w", padx=(0, 14), pady=7
        )
        widget.grid(row=row, column=1, sticky="ew", pady=7)
        if hint:
            ttk.Label(
                parent,
                text=hint,
                style="Muted.TLabel",
                wraplength=220,
                justify="left",
            ).grid(
                row=row, column=2, sticky="w", padx=(14, 0), pady=7
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
        try:
            dialog.attributes("-topmost", True)
        except tk.TclError:
            pass

        frame = ttk.Frame(dialog, padding=20)
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
            wraplength=500,
        ).grid(row=1, column=0, sticky="ew", pady=(8, 14))
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
            trigger_combo["values"] = list(trigger_values.keys())
            trigger_var.set(label)
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
                    " Kliknij „Zastosuj zmiany”.",
                    " Click “Apply changes”.",
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

        cancel_button = ttk.Button(
            frame,
            text=t("Anuluj", "Cancel"),
            command=close_capture,
        )
        cancel_button.grid(row=3, column=0, sticky="e", pady=(18, 0))
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
    ).grid(row=0, column=1, padx=(8, 0))
    add_field(
        general_tab,
        1,
        t("Przycisk dyktowania", "Dictation shortcut"),
        trigger_row,
        t(
            "Kliknij „Wykryj…”, a potem naciśnij dowolny klawisz lub przycisk myszy.",
            "Click “Detect…”, then press any key or mouse button.",
        ),
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
        microphone_values[
            t("Domyślny mikrofon Windows", "Default Windows microphone")
        ] = None
        try:
            devices = sd.query_devices()
            for index, info in enumerate(devices):
                if int(info.get("max_input_channels", 0)) <= 0:
                    continue
                name = str(
                    info.get(
                        "name",
                        t(
                            "Urządzenie {index}",
                            "Device {index}",
                            index=index,
                        ),
                    )
                )
                microphone_values[f"{index}: {name}"] = index
        except Exception as exc:
            logging.warning("Nie udało się pobrać listy mikrofonów: %s", exc)
        microphone_combo["values"] = list(microphone_values.keys())
        microphone_var.set(
            display_for_value(
                microphone_values,
                selected,
                custom_prefix=t("Zapisane urządzenie", "Saved device"),
            )
        )
        microphone_combo["values"] = list(microphone_values.keys())

    ttk.Button(
        microphone_row,
        text=t("Odśwież", "Refresh"),
        command=lambda: refresh_microphones(),
    ).grid(row=0, column=1, padx=(8, 0))
    add_field(general_tab, 2, t("Mikrofon", "Microphone"), microphone_row)
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
        t("Model mowy", "Speech model"),
        model_combo,
        t(
            "Nowy model może zostać pobrany przy zastosowaniu zmian.",
            "A new model may be downloaded when changes are applied.",
        ),
    )
    device_combo = ttk.Combobox(
        general_tab,
        textvariable=device_var,
        values=list(device_values.keys()),
        state="readonly",
    )
    add_field(
        general_tab,
        4,
        t("Miejsce przetwarzania", "Processing device"),
        device_combo,
        t(
            "Automatyczny wybór jest najlepszy dla większości komputerów.",
            "Automatic selection is best for most computers.",
        ),
    )
    language_combo = ttk.Combobox(
        general_tab,
        textvariable=language_var,
        values=list(language_values.keys()),
        state="readonly",
    )
    add_field(
        general_tab,
        5,
        t("Język dyktowania", "Dictation language"),
        language_combo,
    )
    beam_spin = ttk.Spinbox(
        general_tab, from_=1, to=10, textvariable=beam_size_var, width=8
    )
    add_field(
        general_tab,
        6,
        t("Dokładność rozpoznawania", "Recognition accuracy"),
        beam_spin,
        t(
            "1 = najszybciej; 5 = dokładniej, ale wolniej.",
            "1 = fastest; 5 = more accurate, but slower.",
        ),
    )
    threads_spin = ttk.Spinbox(
        general_tab, from_=0, to=256, textvariable=cpu_threads_var, width=8
    )
    add_field(
        general_tab,
        7,
        t("Liczba wątków CPU", "CPU thread count"),
        threads_spin,
        t("0 oznacza automatyczny dobór.", "0 selects automatically."),
    )

    capture_frame = ttk.LabelFrame(
        audio_tab,
        text=t("Bufor i czułość mikrofonu", "Microphone buffer and sensitivity"),
        padding=16,
    )
    capture_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
    capture_frame.columnconfigure(1, weight=1)
    add_field(
        capture_frame,
        0,
        t("Bufor przed naciśnięciem (ms)", "Pre-roll buffer (ms)"),
        ttk.Spinbox(capture_frame, from_=0, to=2000, textvariable=pre_roll_var),
        t("Chroni początek pierwszego słowa.", "Protects the first word's start."),
    )
    add_field(
        capture_frame,
        1,
        t("Bufor po puszczeniu (ms)", "Post-release buffer (ms)"),
        ttk.Spinbox(capture_frame, from_=0, to=2000, textvariable=post_roll_var),
        t("Chroni końcówkę ostatniego słowa.", "Protects the last word's ending."),
    )
    add_field(
        capture_frame,
        2,
        t("Minimalne nagranie (ms)", "Minimum recording (ms)"),
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
        t("Minimalny poziom dźwięku", "Minimum audio level"),
        ttk.Entry(capture_frame, textvariable=minimum_rms_var),
        t(
            "Niższa wartość zwiększa czułość na cichy głos.",
            "A lower value increases sensitivity to quiet speech.",
        ),
    )

    vad_frame = ttk.LabelFrame(
        audio_tab,
        text=t(
            "Wykrywanie ciszy — ustawienia zaawansowane",
            "Silence detection — advanced settings",
        ),
        padding=16,
    )
    vad_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    vad_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        vad_frame,
        text=t(
            "Automatycznie pomijaj ciszę i dźwięki bez wyraźnej mowy",
            "Automatically skip silence and audio without clear speech",
        ),
        variable=vad_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
    vad_controls: list[ttk.Widget] = []
    vad_threshold_entry = ttk.Entry(vad_frame, textvariable=vad_threshold_var)
    vad_controls.append(vad_threshold_entry)
    add_field(
        vad_frame,
        1,
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
        2,
        t("Minimalna długość mowy (ms)", "Minimum speech length (ms)"),
        vad_speech_spin,
    )
    vad_silence_spin = ttk.Spinbox(
        vad_frame, from_=0, to=10000, textvariable=vad_min_silence_var
    )
    vad_controls.append(vad_silence_spin)
    add_field(
        vad_frame,
        3,
        t("Minimalna długość ciszy (ms)", "Minimum silence length (ms)"),
        vad_silence_spin,
    )
    vad_pad_spin = ttk.Spinbox(
        vad_frame, from_=0, to=3000, textvariable=vad_speech_pad_var
    )
    vad_controls.append(vad_pad_spin)
    add_field(
        vad_frame,
        4,
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
        padding=16,
    )
    text_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
    text_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        text_frame,
        text=t(
            "Automatycznie wklejaj tekst do aktywnego okna",
            "Automatically paste text into the active window",
        ),
        variable=paste_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        text_frame,
        text=t(
            "Kopiuj rozpoznany tekst również do schowka",
            "Also copy recognized text to the clipboard",
        ),
        variable=copy_to_clipboard_var,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
    append_space_check = ttk.Checkbutton(
        text_frame,
        text=t(
            "Dodawaj spację po wklejonym zdaniu",
            "Add a space after the pasted sentence",
        ),
        variable=append_space_var,
    )
    append_space_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
    ttk.Checkbutton(
        text_frame,
        text=t(
            "Rozpoznawaj komendy akapitu (PL: „nowa linia”, „nowy akapit”; EN: „new line”, „new paragraph”)",
            "Recognize paragraph commands (EN: “new line”, “new paragraph”; PL: “nowa linia”, “nowy akapit”)",
        ),
        variable=voice_commands_var,
    ).grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
    paste_delay_spin = ttk.Spinbox(
        text_frame, from_=0, to=5000, textvariable=paste_delay_var
    )
    add_field(
        text_frame,
        4,
        t("Opóźnienie wklejania (ms)", "Paste delay (ms)"),
        paste_delay_spin,
        t(
            "Zwiększ tylko wtedy, gdy aplikacja docelowa pomija tekst.",
            "Increase only if the target application misses the pasted text.",
        ),
    )
    ttk.Label(
        text_frame,
        text=t(
            "Gdy schowek jest włączony, pozostaje w nim dokładna transkrypcja "
            "bez automatycznie dodanej spacji.",
            "When clipboard copying is enabled, it keeps the exact transcript "
            "without the automatically appended space.",
        ),
        style="Muted.TLabel",
        wraplength=760,
    ).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def sync_paste_controls(*args) -> None:
        state = "normal" if paste_enabled_var.get() else "disabled"
        append_space_check.configure(state=state)
        paste_delay_spin.configure(state=state)

    paste_enabled_var.trace_add("write", sync_paste_controls)
    sync_paste_controls()

    dictionary_frame = ttk.LabelFrame(
        text_tab,
        text=t(
            "Prywatny słownik nazw i terminów",
            "Private dictionary of names and terms",
        ),
        padding=16,
    )
    dictionary_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    dictionary_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        dictionary_frame,
        text=t(
            "Podpowiadaj modelowi zapis własnych nazw, marek i skrótów",
            "Suggest preferred spellings of names, brands, and abbreviations",
        ),
        variable=dictionary_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
    add_field(
        dictionary_frame,
        1,
        t("Maksymalna liczba haseł", "Maximum number of entries"),
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

    ttk.Button(
        dictionary_frame,
        text=t("Edytuj słownik…", "Edit dictionary…"),
        command=lambda: open_path(DICTIONARY_PATH),
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 2))

    feedback_frame = ttk.LabelFrame(
        sounds_tab,
        text=t("Informacje zwrotne", "Feedback"),
        padding=16,
    )
    feedback_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
    ttk.Checkbutton(
        feedback_frame,
        text=t("Sygnały dźwiękowe", "Sound cues"),
        variable=sounds_var,
    ).grid(row=0, column=0, sticky="w", padx=(0, 25), pady=4)
    ttk.Checkbutton(
        feedback_frame,
        text=t("Powiadomienia Windows", "Windows notifications"),
        variable=notifications_var,
    ).grid(row=0, column=1, sticky="w", pady=4)
    loop_sound_check = ttk.Checkbutton(
        feedback_frame,
        text=t(
            "Zapętlaj własny dźwięk nagrywania podczas trzymania przycisku",
            "Loop the custom recording sound while the shortcut is held",
        ),
        variable=loop_recording_sound_var,
    )
    loop_sound_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=4)

    def sync_sound_controls(*args) -> None:
        loop_sound_check.configure(state="normal" if sounds_var.get() else "disabled")

    sounds_var.trace_add("write", sync_sound_controls)
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
        sounds_tab,
        text=t("Własne dźwięki WAV", "Custom WAV sounds"),
        padding=16,
    )
    custom_sound_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    custom_sound_frame.columnconfigure(0, weight=1)
    ttk.Label(
        custom_sound_frame,
        text=t(
            "Pozostaw puste, aby używać krótkiego sygnału wbudowanego. "
            "Wybrany plik zostanie skopiowany do %APPDATA%\\Mowik\\sounds.",
            "Leave empty to use the short built-in cue. The selected file is "
            "copied to %APPDATA%\\Mowik\\sounds.",
        ),
        style="Muted.TLabel",
        wraplength=760,
    ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

    for row_index, kind in enumerate(("start", "stop", "done", "error"), start=1):
        sound_row = ttk.Frame(custom_sound_frame, style="Surface.TFrame")
        sound_row.grid(
            row=row_index, column=0, sticky="ew", pady=(5, 7)
        )
        sound_row.columnconfigure(0, weight=1)
        ttk.Label(
            sound_row,
            text=sound_labels[kind],
            style="Field.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        ttk.Entry(
            sound_row,
            textvariable=sound_path_vars[kind],
            state="readonly",
        ).grid(row=1, column=0, sticky="ew")
        buttons = ttk.Frame(sound_row)
        buttons.grid(row=1, column=1, sticky="e", padx=(8, 0))
        ttk.Button(
            buttons,
            text=t("Wybierz…", "Choose…"),
            command=lambda selected_kind=kind: choose_sound(selected_kind),
        ).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(
            buttons,
            text=t("Odsłuch", "Preview"),
            command=lambda selected_kind=kind: preview_sound(selected_kind),
        ).grid(row=0, column=1, padx=4)
        ttk.Button(
            buttons,
            text=t("Wbudowany", "Built-in"),
            command=lambda selected_kind=kind: sound_path_vars[selected_kind].set(""),
        ).grid(row=0, column=2, padx=(4, 0))

    sounds_footer = ttk.Frame(sounds_tab)
    sounds_footer.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
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
        padx=18,
        pady=14,
    )
    ollama_intro.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
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
        wraplength=720,
    ).pack(anchor="w", pady=(4, 0))

    ollama_frame = ttk.LabelFrame(
        ollama_tab,
        text=t("Lokalna korekta tekstu", "Local text correction"),
        padding=16,
    )
    ollama_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
    ollama_frame.columnconfigure(1, weight=1)
    ttk.Checkbutton(
        ollama_frame,
        text=t(
            "Włącz lokalną korektę przez Ollamę",
            "Enable local correction with Ollama",
        ),
        variable=ollama_enabled_var,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 7))
    ollama_url_entry = ttk.Entry(ollama_frame, textvariable=ollama_url_var)
    add_field(
        ollama_frame,
        1,
        t("Adres Ollamy", "Ollama address"),
        ollama_url_entry,
    )
    ollama_model_entry = ttk.Entry(ollama_frame, textvariable=ollama_model_var)
    add_field(
        ollama_frame,
        2,
        t("Nazwa modelu", "Model name"),
        ollama_model_entry,
        t(
            "Wpisz nazwę modelu pobranego wcześniej w Ollamie.",
            "Enter the name of a model already downloaded in Ollama.",
        ),
    )
    ollama_timeout_spin = ttk.Spinbox(
        ollama_frame, from_=1, to=600, textvariable=ollama_timeout_var
    )
    add_field(
        ollama_frame,
        3,
        t("Limit czasu (s)", "Timeout (s)"),
        ollama_timeout_spin,
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
        wraplength=720,
    ).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))

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
        padx=18,
        pady=14,
    )
    diagnostics_intro.grid(row=0, column=0, sticky="ew", pady=(0, 14))
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
        wraplength=700,
    ).pack(anchor="w", pady=(4, 0))

    config_card = ttk.LabelFrame(
        files_tab,
        text=t("Konfiguracja zaawansowana", "Advanced configuration"),
        padding=16,
    )
    config_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
    config_card.columnconfigure(0, weight=1)
    ttk.Label(
        config_card,
        text=str(CONFIG_PATH),
        style="Muted.TLabel",
        wraplength=700,
    ).grid(row=0, column=0, sticky="ew", pady=(0, 9))
    ttk.Button(
        config_card,
        text=t("Otwórz config.json…", "Open config.json…"),
        command=lambda: open_path(CONFIG_PATH),
    ).grid(row=1, column=0, sticky="w")

    data_card = ttk.LabelFrame(
        files_tab,
        text=t("Dane, modele i diagnostyka", "Data, models, and diagnostics"),
        padding=16,
    )
    data_card.grid(row=2, column=0, sticky="ew")
    data_card.columnconfigure(0, weight=1)
    ttk.Label(
        data_card,
        text=str(LOCALDATA_DIR),
        style="Muted.TLabel",
        wraplength=700,
    ).grid(row=0, column=0, sticky="ew", pady=(0, 9))
    files_buttons = ttk.Frame(data_card)
    files_buttons.grid(row=1, column=0, sticky="w")
    ttk.Button(
        files_buttons,
        text=t("Otwórz folder danych", "Open data folder"),
        command=lambda: open_path(LOCALDATA_DIR),
    ).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(
        files_buttons,
        text=t("Otwórz log diagnostyczny", "Open diagnostic log"),
        command=lambda: open_path(LOG_PATH),
    ).grid(row=0, column=1)

    def parse_int(
        variable: tk.StringVar, label: str, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(variable.get().strip())
        except ValueError as exc:
            raise AppError(
                t(
                    "Pole „{label}” musi być liczbą całkowitą.",
                    "The “{label}” field must be an integer.",
                    label=label,
                )
            ) from exc
        if not minimum <= value <= maximum:
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
        variable: tk.StringVar, label: str, minimum: float, maximum: float
    ) -> float:
        try:
            value = float(variable.get().strip().replace(",", "."))
        except ValueError as exc:
            raise AppError(
                t(
                    "Pole „{label}” musi być liczbą.",
                    "The “{label}” field must be a number.",
                    label=label,
                )
            ) from exc
        if not minimum <= value <= maximum:
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

    def collect_config() -> dict[str, Any]:
        updated = load_config()
        trigger = str(trigger_values.get(trigger_var.get(), trigger_var.get()))
        try:
            split_trigger(trigger)
        except AppError as exc:
            raise AppError(
                t(
                    "Wybierz prawidłowy przycisk dyktowania.",
                    "Choose a valid dictation shortcut.",
                )
            ) from exc
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
            raise AppError(t("Wybierz model mowy.", "Choose a speech model."))
        if device not in {"auto", "cpu", "cuda"}:
            raise AppError(
                t(
                    "Urządzenie musi mieć wartość auto, cpu albo cuda.",
                    "The processing device must be auto, cpu, or cuda.",
                )
            )
        if not language:
            raise AppError(
                t("Wybierz język dyktowania.", "Choose a dictation language.")
            )
        if ui_language not in {"auto", "pl", "en"}:
            raise AppError(
                t(
                    "Wybierz język interfejsu.",
                    "Choose an interface language.",
                )
            )
        if microphone_var.get() not in microphone_values:
            raise AppError(
                t("Wybierz mikrofon z listy.", "Choose a microphone from the list.")
            )

        updated["trigger"] = trigger
        updated["model"] = model
        updated["device"] = device
        updated["language"] = language
        updated["ui_language"] = ui_language
        updated["microphone"] = microphone_values[microphone_var.get()]
        updated["cpu_threads"] = parse_int(
            cpu_threads_var, t("Wątki CPU", "CPU threads"), 0, 256
        )
        updated["beam_size"] = parse_int(
            beam_size_var,
            t("Dokładność rozpoznawania", "Recognition accuracy"),
            1,
            10,
        )
        updated["pre_roll_ms"] = parse_int(
            pre_roll_var,
            t("Bufor przed naciśnięciem", "Pre-roll buffer"),
            0,
            2000,
        )
        updated["post_roll_ms"] = parse_int(
            post_roll_var,
            t("Bufor po puszczeniu", "Post-release buffer"),
            0,
            2000,
        )
        updated["minimum_recording_ms"] = parse_int(
            minimum_recording_var,
            t("Minimalne nagranie", "Minimum recording"),
            0,
            10000,
        )
        updated["minimum_rms"] = parse_float(
            minimum_rms_var,
            t("Minimalna głośność RMS", "Minimum RMS level"),
            0.0,
            1.0,
        )

        updated.setdefault("vad", {})
        updated["vad"].update(
            {
                "enabled": bool(vad_enabled_var.get()),
                "threshold": parse_float(
                    vad_threshold_var,
                    t("Próg VAD", "VAD threshold"),
                    0.0,
                    1.0,
                ),
                "min_speech_duration_ms": parse_int(
                    vad_min_speech_var,
                    t("Minimalna mowa", "Minimum speech"),
                    0,
                    10000,
                ),
                "min_silence_duration_ms": parse_int(
                    vad_min_silence_var,
                    t("Minimalna cisza", "Minimum silence"),
                    0,
                    10000,
                ),
                "speech_pad_ms": parse_int(
                    vad_speech_pad_var,
                    t("Margines mowy", "Speech padding"),
                    0,
                    3000,
                ),
            }
        )
        updated.setdefault("dictionary", {})
        updated["dictionary"].update(
            {
                "enabled": bool(dictionary_enabled_var.get()),
                "max_terms": parse_int(
                    dictionary_max_terms_var,
                    t("Maksymalna liczba haseł", "Maximum number of entries"),
                    0,
                    5000,
                ),
            }
        )
        updated.setdefault("paste", {})
        paste_enabled = bool(paste_enabled_var.get())
        copy_to_clipboard = bool(copy_to_clipboard_var.get())
        if not paste_enabled and not copy_to_clipboard:
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
                "delay_ms": parse_int(
                    paste_delay_var,
                    t("Opóźnienie przed Ctrl+V", "Delay before Ctrl+V"),
                    0,
                    5000,
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
                    ollama_timeout_var,
                    t("Limit czasu Ollamy", "Ollama timeout"),
                    1,
                    600,
                ),
            }
        )
        if updated["ollama_cleanup"]["enabled"] and not updated[
            "ollama_cleanup"
        ]["model"]:
            raise AppError(
                t(
                    "Korekta Ollama jest włączona, ale pole „Nazwa modelu” jest puste.",
                    "Ollama correction is enabled, but the “Model name” field is empty.",
                )
            )
        updated["feedback"]["custom_sounds"] = {
            kind: import_custom_sound(
                kind,
                sound_path_vars[kind].get(),
                translator,
            )
            for kind in ("start", "stop", "done", "error")
        }
        return updated

    tracked_variables: list[tk.Variable] = [
        trigger_var,
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
        wraplength=360,
    ).grid(row=0, column=1, sticky="w", padx=(7, 12))
    footer.columnconfigure(1, weight=1)

    def update_status_indicator(*args) -> None:
        if status_level["value"] == "error":
            color = colors["danger"]
        elif dirty_state["dirty"] or status_level["value"] == "warning":
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
            set_status(
                t(
                    "Wszystko gotowe — ustawienia są zapisane.",
                    "Everything is ready — settings are saved.",
                )
            )
        apply_button.configure(
            state="normal" if dirty_state["dirty"] else "disabled"
        )
        update_status_indicator()

    for variable in tracked_variables:
        variable.trace_add("write", refresh_dirty_state)
    status_var.trace_add("write", update_status_indicator)

    def save_from_window(apply_now: bool) -> None:
        nonlocal config
        try:
            updated = collect_config()
            save_config(updated)
            config = updated
            dirty_state["baseline"] = tuple(
                variable.get() for variable in tracked_variables
            )
            dirty_state["dirty"] = False
            apply_button.configure(state="disabled")
            if apply_now:
                request_app_restart()
                set_status(
                    t(
                        "Zapisano — Mówik stosuje zmiany…",
                        "Saved — Mówik is applying changes…",
                    )
                )
                root.after(180, root.destroy)
            else:
                set_status(
                    t(
                        "Zapisano. Zmiany zaczną działać po ponownym uruchomieniu Mówika.",
                        "Saved. Changes take effect after Mówik is restarted.",
                    )
                )
        except Exception as exc:
            logging.exception("Nie udało się zapisać ustawień")
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
        dirty_state["dirty"] = True
        apply_button.configure(state="normal")
        set_status(
            t(
                "Przywrócono wartości domyślne. Zastosuj, aby je zapisać.",
                "Defaults restored. Apply changes to save them.",
            ),
            "warning",
        )

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
        text=t("Przywróć domyślne", "Restore defaults"),
        command=restore_defaults,
    ).grid(row=0, column=2, padx=4)
    ttk.Button(
        footer,
        text=t("Zamknij", "Close"),
        command=close_window,
    ).grid(
        row=0, column=3, padx=4
    )
    apply_button = ttk.Button(
        footer,
        text=t("Zastosuj zmiany", "Apply changes"),
        style="Primary.TButton",
        command=lambda: save_from_window(True),
        state="disabled",
    )
    apply_button.grid(row=0, column=4, padx=(4, 0))

    root.bind("<Control-s>", lambda event: save_from_window(False))
    root.bind(
        "<Control-Return>",
        lambda event: save_from_window(True) if dirty_state["dirty"] else None,
    )
    root.bind("<Escape>", lambda event: close_window())
    root.protocol("WM_DELETE_WINDOW", close_window)
    show_page("start")
    refresh_dirty_state()
    update_status_indicator()
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

        raise AppError(
            Translator.from_config(self.config).t(
                "Nie udało się otworzyć mikrofonu: {error}",
                "Could not open the microphone: {error}",
                error=last_error,
            )
        )

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
        self.translator = Translator.from_config(config)
        self.trigger_type, self.trigger_name = split_trigger(
            str(config["trigger"]),
            self.translator,
        )
        self.stop_event = threading.Event()
        self.model_ready = threading.Event()
        self.busy_lock = threading.Lock()
        self.busy = False
        self.key_down = False
        self.capture_active = False
        self._release_started_at: Optional[float] = None
        self.model: Optional[WhisperModel] = None
        self.model_name = ""
        self.model_device = ""
        self.recorder: Optional[ContinuousRecorder] = None
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None
        self.jobs: queue.Queue[Optional[np.ndarray]] = queue.Queue()
        self.tray: Optional[pystray.Icon] = None
        self.status = self.translator.t("Uruchamianie…", "Starting…")
        self.tray_state = "idle"
        self._status_lock = threading.Lock()
        self._restart_lock = threading.Lock()
        self._restart_started = False
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

    def _model_status(self, status: str) -> None:
        self.set_status(status, state="processing")

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
                self.set_status(
                    self.translator.t(
                        "Stosuję nowe ustawienia…",
                        "Applying new settings…",
                    ),
                    state="processing",
                )
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
            self.set_status(
                self.translator.t(
                    "Przygotowuję mikrofon…",
                    "Preparing the microphone…",
                ),
                state="processing",
            )
            recorder = ContinuousRecorder(self.config)
            recorder.start()
            self.recorder = recorder
            model, model_name, device = create_model(
                self.config,
                self._model_status,
            )
            self.model = model
            self.model_name = model_name
            self.model_device = device
            self.model_ready.set()
            trigger_label = trigger_display_name(
                str(self.config["trigger"]),
                self.translator,
            )
            self.set_status(
                self.translator.t(
                    "Gotowy — {trigger_label}",
                    "Ready — {trigger_label}",
                    trigger_label=trigger_label,
                ),
                notify=self.translator.t(
                    "Model {model_name} działa na {device}. "
                    "Trzymaj {trigger_label} i mów.",
                    "Model {model_name} is running on {device}. "
                    "Hold {trigger_label} and speak.",
                    model_name=model_name,
                    device=device,
                    trigger_label=trigger_label,
                ),
                state="ready",
            )
        except Exception as exc:
            logging.exception("Błąd inicjalizacji")
            self.set_status(
                self.translator.t("Błąd uruchomienia", "Startup error"),
                notify=self._error_notification(),
                error=True,
                state="error",
            )

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
            self.set_status(
                self.translator.t(
                    "Model jeszcze nie jest gotowy",
                    "The model is not ready yet",
                ),
                state="idle",
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
        try:
            self.recorder.begin()
            self.capture_active = True
        except Exception as exc:
            self._release_busy()
            logging.exception("Nie udało się rozpocząć nagrywania")
            self.set_status(
                self.translator.t("Błąd nagrywania", "Recording error"),
                notify=self._error_notification(),
                error=True,
                state="error",
            )
            self.beep("error")
            return
        self.beep("start")
        self.set_status(
            self.translator.t("Nagrywanie…", "Recording…"),
            state="recording",
        )

    def end_dictation(self) -> None:
        if not self.capture_active:
            return
        self.capture_active = False
        self._release_started_at = time.perf_counter()
        # Kończymy także dłuższy, niezapętlony WAV przypisany do nagrywania.
        self.stop_feedback_sound()
        threading.Thread(
            target=self._finish_dictation_safely,
            name="PostRoll",
            daemon=True,
        ).start()

    def _finish_dictation_safely(self) -> None:
        try:
            self._finish_dictation_after_tail()
        except Exception as exc:
            logging.exception("Nie udało się zakończyć nagrywania")
            self._release_busy()
            self.set_status(
                self.translator.t("Błąd nagrywania", "Recording error"),
                notify=self._error_notification(),
                error=True,
                state="error",
            )
            self.beep("error")

    def _finish_dictation_after_tail(self) -> None:
        post_roll = max(0, int(self.config.get("post_roll_ms", 120))) / 1000
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
            self.translator.t("Rozpoznaję mowę…", "Transcribing…"),
            state="processing",
        )
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
                    self.set_status(
                        self.translator.t(
                            "Nie wykryłem wyraźnej mowy",
                            "No clear speech detected",
                        ),
                        state="ready",
                    )
                    self.beep("error")
                    continue
                paste_settings = self.config.get("paste", {})
                paste_enabled = bool(paste_settings.get("enabled", True))
                copy_enabled = bool(
                    paste_settings.get("copy_to_clipboard", True)
                )
                if paste_enabled and copy_enabled:
                    self.set_status(
                        self.translator.t(
                            "Wklejam i kopiuję tekst…",
                            "Pasting and copying text…",
                        ),
                        state="processing",
                    )
                elif paste_enabled:
                    self.set_status(
                        self.translator.t(
                            "Wklejam tekst…",
                            "Pasting text…",
                        ),
                        state="processing",
                    )
                else:
                    self.set_status(
                        self.translator.t(
                            "Kopiuję tekst do schowka…",
                            "Copying text to the clipboard…",
                        ),
                        state="processing",
                    )
                paste_text(text, self.config)
                logging.info(
                    "Dostarczono tekst (%d znaków; wklejanie=%s; schowek=%s)",
                    len(text),
                    paste_enabled,
                    copy_enabled,
                )
                if self._release_started_at is not None:
                    logging.info(
                        "Latencja F8 release -> tekst: %.3f s",
                        time.perf_counter() - self._release_started_at,
                    )
                self.set_status(
                    self.translator.t(
                        "Gotowy — {trigger_label}",
                        "Ready — {trigger_label}",
                        trigger_label=trigger_display_name(
                            str(self.config["trigger"]),
                            self.translator,
                        ),
                    ),
                    state="ready",
                )
                self.beep("done")
            except Exception as exc:
                logging.exception("Błąd przetwarzania dyktowania")
                self.set_status(
                    self.translator.t(
                        "Błąd dyktowania",
                        "Dictation error",
                    ),
                    notify=self._error_notification(),
                    error=True,
                    state="error",
                )
                self.beep("error")
            finally:
                self._release_busy()
                self.jobs.task_done()

    def transcribe(self, audio: np.ndarray) -> str:
        pipeline_started = time.perf_counter()
        model = self.model
        if model is None:
            raise AppError(
                self.translator.t(
                    "Model nie jest załadowany.",
                    "The model is not loaded.",
                )
            )
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return ""
        audio = np.clip(audio - float(np.mean(audio)), -1.0, 1.0)
        rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
        sample_rate = (
            self.recorder.sample_rate if self.recorder is not None else SAMPLE_RATE
        )
        audio_duration = len(audio) / sample_rate
        logging.info("Audio: %.2f s, %d Hz, RMS=%.6f", audio_duration, sample_rate, rms)
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

        configured_language = str(self.config.get("language", "auto")).strip()
        language: Optional[str] = None if configured_language.lower() == "auto" else configured_language

        whisper_started = time.perf_counter()
        segments, info = model.transcribe(
            audio_input,
            language=language,
            task="transcribe",
            beam_size=max(1, int(self.config.get("beam_size", 2))),
            temperature=0.0,
            condition_on_previous_text=False,
            hotwords=glossary[:1800] if glossary else None,
            vad_filter=vad_enabled,
            vad_parameters=vad_parameters if vad_enabled else None,
            without_timestamps=True,
        )
        transcript = "".join(segment.text for segment in segments)
        whisper_elapsed = time.perf_counter() - whisper_started
        logging.info(
            "Whisper: język=%s prawdopodobieństwo=%.3f, znaków=%d, "
            "czas=%.3f s, RTF=%.3f",
            getattr(info, "language", "?"),
            float(getattr(info, "language_probability", 0.0)),
            len(transcript),
            whisper_elapsed,
            whisper_elapsed / max(0.001, audio_duration),
        )
        transcript = normalize_transcript(transcript)
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
                notify=self._error_notification(),
                error=True,
                state="error",
            )

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
            profile_display = profile["display"][self.translator.language]
            updated = apply_quick_profile(load_config(), profile_name)
            save_config(updated)
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
                self.translator.t(
                    "Błąd zmiany profilu",
                    "Could not change profile",
                ),
                notify=self._error_notification(),
                error=True,
                state="error",
            )

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
            subprocess.Popen(args, cwd=str(APP_ROOT))
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
        self.start()
        self.tray.run()


def acquire_single_instance(
    translator: Optional[Translator] = None,
) -> Optional[int]:
    if os.name != "nt":
        return None
    translator = translator or Translator("pl")
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
            translator.t(
                "Mówik jest już uruchomiony. "
                "Poszukaj ikony mikrofonu przy zegarze.",
                "Mówik is already running. "
                "Look for the microphone icon in the system tray.",
            ),
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


def show_fatal_error(
    message: str,
    translator: Optional[Translator] = None,
) -> None:
    translator = translator or Translator("auto")
    display_message = translator.t(
        "{message}\n\nSzczegóły: {path}",
        "{message}\n\nDetails: {path}",
        message=message,
        path=LOG_PATH,
    )
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


def download_model_command(config: dict[str, Any]) -> int:
    translator = Translator.from_config(config)

    def status(text: str) -> None:
        print(text, flush=True)

    model, model_name, device = create_model(config, status)
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
    parser.add_argument(
        "--download-model",
        action="store_true",
        help=translator.t(
            "Pobierz/załaduj model i zakończ",
            "Download/load the model and exit",
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
    return parser.parse_args()


def main() -> int:
    cli_translator = Translator()
    args = parse_args(cli_translator)
    setup_logging(console=args.console_log or args.download_model or args.list_devices)
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
        return run_settings_window()
    if args.list_devices:
        return list_devices(cli_translator)

    config = load_config()
    translator = Translator.from_config(config)
    if args.restart_delay > 0:
        time.sleep(min(args.restart_delay, 5.0))
    if args.download_model:
        return download_model_command(config)
    if args.test_ollama:
        return test_ollama_command(config)

    if os.name != "nt":
        raise AppError(
            translator.t(
                "Aplikacja okienkowa Mówik jest przeznaczona dla Windows 10/11.",
                "The Mówik desktop application is designed for Windows 10/11.",
            )
        )

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Mowik.LocalDictation"
        )
    except Exception:
        pass

    mutex_handle = acquire_single_instance(translator)
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
        try:
            fatal_translator = Translator.from_config(load_config())
        except Exception:
            fatal_translator = Translator()
        show_fatal_error(str(exc), fatal_translator)
        raise SystemExit(1)
