from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mowik_i18n import Translator, resolve_ui_language


class UiLanguageTests(unittest.TestCase):
    def test_auto_uses_polish_only_for_polish_windows(self) -> None:
        self.assertEqual(resolve_ui_language("auto", "pl_PL"), "pl")
        self.assertEqual(resolve_ui_language("auto", "en_US"), "en")
        self.assertEqual(resolve_ui_language("auto", "de_DE"), "en")

    def test_explicit_language_overrides_system_language(self) -> None:
        self.assertEqual(resolve_ui_language("en", "pl_PL"), "en")
        self.assertEqual(resolve_ui_language("pl", "en_US"), "pl")

    def test_unknown_setting_has_safe_english_fallback(self) -> None:
        self.assertEqual(resolve_ui_language("unsupported", "pl_PL"), "en")

    def test_translator_formats_selected_text(self) -> None:
        self.assertEqual(
            Translator("pl").t(
                "Model {name} jest gotowy",
                "Model {name} is ready",
                name="Turbo",
            ),
            "Model Turbo jest gotowy",
        )
        self.assertEqual(
            Translator("en").t(
                "Model {name} jest gotowy",
                "Model {name} is ready",
                name="Turbo",
            ),
            "Model Turbo is ready",
        )


if __name__ == "__main__":
    unittest.main()
