"""Small, dependency-free localization helpers for Mówik.

The transcription language and the interface language are intentionally
separate. The automatic interface setting follows the Windows display
language: Polish Windows uses Polish, while every other locale falls back to
English.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import locale
import os
from typing import Any, Mapping, Optional


SUPPORTED_UI_LANGUAGES = ("auto", "pl", "en")


def _system_ui_language() -> str:
    if os.name == "nt":
        try:
            language_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            language_name = locale.windows_locale.get(language_id, "")
            if language_name:
                return language_name
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    try:
        language_name = locale.getlocale()[0]
    except (TypeError, ValueError):
        language_name = None
    return language_name or "en"


def resolve_ui_language(
    value: str = "auto",
    system_language: Optional[str] = None,
) -> str:
    """Resolve auto, pl, or en to the concrete language used by the UI."""

    requested = str(value or "auto").strip().lower().replace("_", "-")
    if requested in {"pl", "polish"}:
        return "pl"
    if requested in {"en", "english"}:
        return "en"
    if requested != "auto":
        return "en"
    detected = (
        str(system_language or _system_ui_language())
        .strip()
        .lower()
        .replace("_", "-")
    )
    return "pl" if detected == "pl" or detected.startswith("pl-") else "en"


@dataclass(frozen=True)
class Translator:
    """Choose and format one of two colocated UI strings."""

    language: str = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(self, "language", resolve_ui_language(self.language))

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Translator":
        return cls(str(config.get("ui_language", "auto")))

    @property
    def is_polish(self) -> bool:
        return self.language == "pl"

    def t(self, polish: str, english: str, **values: Any) -> str:
        template = polish if self.is_polish else english
        return template.format(**values) if values else template

    __call__ = t
