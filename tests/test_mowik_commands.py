from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mowik_commands import (
    ACTION_OPEN_TERMINAL,
    ACTION_PASTE_TEXT,
    ACTION_REGISTRY,
    BLOCKED_OPEN_SUFFIXES,
    MATCH_EXACT,
    MATCH_PREFIX_TAIL,
    OPERATION_DRAFT,
    OPERATION_OPEN,
    CommandRegistry,
    CommandValidationError,
    ExecutionContext,
    build_action_plan,
    parse_custom_commands,
    sanitize_terminal_draft,
)


def captured_context(explorer_path: str | None = r"C:\work\Mowik") -> ExecutionContext:
    return ExecutionContext(
        foreground_hwnd=123,
        foreground_pid=456,
        explorer_path=explorer_path,
        captured_at=42.5,
    )


class CommandConfigCompatibilityTests(unittest.TestCase):
    def test_old_flat_text_item_is_still_parsed_as_exact_paste(self) -> None:
        registry = parse_custom_commands(
            {
                "custom_commands": {
                    "enabled": True,
                    "trigger": "keyboard:f7",
                    "items": [
                        {
                            "phrase": "Wklej podpis",
                            "text": "Pozdrawiam,\nJan Kowalski",
                        }
                    ],
                }
            }
        )

        self.assertEqual(registry.issues, ())
        self.assertEqual(len(registry.definitions), 1)
        definition = registry.definitions[0]
        self.assertEqual(definition.action, ACTION_PASTE_TEXT)
        self.assertEqual(definition.value, "Pozdrawiam,\nJan Kowalski")
        self.assertEqual(definition.match_mode, MATCH_EXACT)
        self.assertFalse(definition.confirm)

        match = registry.match("  wklej, PODPIS! ")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.definition.id, definition.id)
        self.assertEqual(match.spoken_tail, "")

    def test_generated_id_is_stable_across_parses_and_item_order(self) -> None:
        first_item = {
            "phrase": "  Otworz terminal! ",
            "action": "open_terminal",
            "value": "",
            "match": "exact",
        }
        other_item = {
            "phrase": "Wklej adres",
            "action": "paste_text",
            "value": "Example Street 1",
        }

        first_registry = CommandRegistry.from_items([first_item, other_item])
        second_registry = CommandRegistry.from_items([other_item, first_item])

        first_id = first_registry.match("otworz terminal").definition.id  # type: ignore[union-attr]
        second_id = second_registry.match("OTWORZ, TERMINAL!").definition.id  # type: ignore[union-attr]
        self.assertEqual(first_id, second_id)
        self.assertRegex(first_id, r"^cc_[0-9a-f]{24}$")

    def test_explicit_id_is_preserved(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "terminal.project-main",
                    "phrase": "Terminal projektu",
                    "action": "open_terminal",
                    "value": "",
                }
            ]
        )

        self.assertEqual(registry.issues, ())
        self.assertEqual(registry.definitions[0].id, "terminal.project-main")


class CommandMatchingTests(unittest.TestCase):
    def test_exact_match_wins_before_prefix_and_longest_prefix_wins_for_tail(
        self,
    ) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "short-prefix",
                    "phrase": "terminal",
                    "action": "open_terminal",
                    "value": "",
                    "match": "prefix_tail",
                },
                {
                    "id": "long-prefix",
                    "phrase": "terminal tutaj",
                    "action": "open_terminal",
                    "value": "",
                    "match": "prefix_tail",
                },
                {
                    "id": "exact",
                    "phrase": "terminal tutaj",
                    "action": "open_terminal",
                    "value": "",
                    "match": "exact",
                },
            ]
        )

        exact = registry.match("Terminal tutaj!")
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact.definition.id, "exact")
        self.assertEqual(exact.spoken_tail, "")

        prefixed = registry.match('Terminal tutaj  git status --short && echo "OK"')
        self.assertIsNotNone(prefixed)
        assert prefixed is not None
        self.assertEqual(prefixed.definition.id, "long-prefix")
        self.assertEqual(prefixed.spoken_tail, 'git status --short && echo "OK"')

    def test_prefix_retains_raw_tail_but_requires_a_token_boundary(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "terminal-prefix",
                    "phrase": "terminal",
                    "action": "open_terminal",
                    "value": "",
                    "match": "prefix_tail",
                }
            ]
        )

        raw_tail = r'git log --format="%h %s" | findstr /i "Żółć"'
        result = registry.match(f"TERMINAL   {raw_tail}")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.spoken_tail, raw_tail)

        self.assertIsNone(registry.match("terminalista git status"))
        self.assertIsNone(registry.match("superterminal git status"))

    def test_duplicate_match_keys_and_ids_are_all_disabled_fail_closed(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "first",
                    "phrase": "Terminal tutaj",
                    "action": "open_terminal",
                    "value": "",
                },
                {
                    "id": "second",
                    "phrase": "terminal, TUTAJ!",
                    "action": "open_terminal",
                    "value": "",
                },
                {
                    "id": "reused-id",
                    "phrase": "Pierwsza akcja",
                    "action": "paste_text",
                    "value": "one",
                },
                {
                    "id": "reused-id",
                    "phrase": "Druga akcja",
                    "action": "paste_text",
                    "value": "two",
                },
                {
                    "id": "unique",
                    "phrase": "Bezpieczna akcja",
                    "action": "paste_text",
                    "value": "safe",
                },
            ]
        )

        self.assertIsNone(registry.match("terminal tutaj"))
        self.assertIsNone(registry.match("pierwsza akcja"))
        self.assertIsNone(registry.match("druga akcja"))
        unique = registry.match("bezpieczna akcja")
        self.assertIsNotNone(unique)
        assert unique is not None
        self.assertEqual(unique.definition.id, "unique")
        self.assertIn("duplicate_match", {issue.code for issue in registry.issues})
        self.assertIn("duplicate_id", {issue.code for issue in registry.issues})


class CommandSafetyPolicyTests(unittest.TestCase):
    def test_open_action_always_requires_confirmation_and_has_bounded_preview(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "phrase": "otwórz dokument",
                    "action": "open",
                    "value": r"C:\Work\notes.txt",
                    "confirm": False,
                }
            ]
        )
        match = registry.match("otwórz dokument")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertTrue(match.definition.confirm)
        self.assertTrue(build_action_plan(match, captured_context()).requires_confirmation)

        oversized = CommandRegistry.from_items(
            [
                {
                    "phrase": "otwórz długi cel",
                    "action": "open",
                    "value": "x" * 701,
                }
            ]
        )
        self.assertFalse(oversized.definitions)

    def test_open_target_syntax_rejects_disguised_or_active_content(self) -> None:
        unsafe_targets = [
            "http://example.com",
            "file:///C:/Windows/notepad.exe",
            "https://user:secret@example.com",
            "https://example.com\\path",
            "https://exa\tmple.com",
            "https://example.com/hidden\u2028line",
            r"\\server\share\tool.exe",
            r"C:\Work\safe.txt:payload.exe",
            r"C:\Work\trailing-dot.",
            r"C:\Work\trailing-space ",
        ]
        unsafe_targets.extend(
            rf"C:\Work\payload{suffix.upper()}"
            for suffix in sorted(BLOCKED_OPEN_SUFFIXES)
        )

        for index, target in enumerate(unsafe_targets):
            with self.subTest(target=repr(target)):
                registry = CommandRegistry.from_items(
                    [{
                        "phrase": f"unsafe target {index}",
                        "action": "open",
                        "value": target,
                    }]
                )
                self.assertFalse(registry.definitions)

    def test_hidden_controls_are_rejected_in_phrases_and_paste_values(self) -> None:
        hidden_characters = ("\t", "\x1b", "\u200b", "\u2066", "\u2028", "\u2029")
        for index, hidden in enumerate(hidden_characters):
            with self.subTest(kind="phrase", character=repr(hidden)):
                registry = CommandRegistry.from_items(
                    [{
                        "phrase": f"hidden{hidden}phrase {index}",
                        "action": "paste_text",
                        "value": "safe",
                    }]
                )
                self.assertFalse(registry.definitions)
            with self.subTest(kind="value", character=repr(hidden)):
                registry = CommandRegistry.from_items(
                    [{
                        "phrase": f"hidden value {index}",
                        "action": "paste_text",
                        "value": f"safe{hidden}text",
                    }]
                )
                self.assertFalse(registry.definitions)

    def test_legacy_run_command_records_are_disabled_fail_closed(self) -> None:
        self.assertNotIn("run_command", ACTION_REGISTRY)

        for record in (
            {
                "id": "legacy-static-command",
                "phrase": "Pokaż status repozytorium",
                "action": "run_command",
                "value": "git status --short",
                "match": "exact",
                "confirm": True,
            },
            {
                "id": "legacy-dynamic-command",
                "phrase": "wykonaj",
                "action": "run_command",
                "value": "{spoken_tail}",
                "match": "prefix_tail",
                "confirm": True,
            },
        ):
            with self.subTest(command_id=record["id"]):
                registry = CommandRegistry.from_items([record])

                self.assertEqual(registry.definitions, ())
                self.assertEqual(len(registry.issues), 1)
                self.assertEqual(registry.issues[0].code, "action_unknown")
                self.assertEqual(registry.issues[0].field, "action")
                self.assertIsNone(registry.match(record["phrase"]))


class TerminalPlanTests(unittest.TestCase):
    def test_targeted_terminal_input_option_is_rejected_fail_closed(self) -> None:
        registry = CommandRegistry.from_items(
            [{
                "phrase": "terminal tutaj",
                "action": "open_terminal",
                "match": "prefix_tail",
                "options": {
                    "cwd_source": "home",
                    "draft_delivery": "targeted",
                },
            }]
        )

        self.assertFalse(registry.definitions)
        self.assertIsNone(registry.match("terminal tutaj git status"))

    def test_terminal_and_open_actions_are_denied_while_mowik_is_elevated(self) -> None:
        elevated = ExecutionContext(123, 456, r"C:\Work", 42.5, True)
        terminal = CommandRegistry.from_items(
            [{
                "phrase": "terminal",
                "action": "open_terminal",
                "options": {"cwd_source": "home"},
            }]
        ).match("terminal")
        opened = CommandRegistry.from_items(
            [{
                "phrase": "kalkulator",
                "action": "open",
                "value": r"C:\Windows\System32\calc.exe",
            }]
        ).match("kalkulator")
        assert terminal is not None and opened is not None

        for match in (terminal, opened):
            with self.subTest(action=match.definition.action):
                plan = build_action_plan(match, elevated)
                self.assertFalse(plan.allowed)
                self.assertEqual(plan.denial_reason, "elevated_process_denied")
                self.assertFalse(plan.requires_confirmation)
                self.assertEqual(plan.operation, "deny")
                self.assertEqual(plan.payload, "")
                self.assertIsNone(plan.working_directory)

    def test_spoken_terminal_tail_is_only_a_draft_and_is_never_submitted(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "terminal-draft",
                    "phrase": "terminal tutaj wpisz",
                    "action": "open_terminal",
                    "value": "",
                    "match": "prefix_tail",
                }
            ]
        )

        match = registry.match("terminal tutaj wpisz git diff --check")
        self.assertIsNotNone(match)
        assert match is not None
        plan = build_action_plan(match, captured_context(r"C:\repo with spaces"))

        self.assertTrue(plan.allowed)
        self.assertFalse(plan.requires_confirmation)
        self.assertEqual(plan.operation, OPERATION_DRAFT)
        self.assertEqual(plan.payload, "git diff --check")
        self.assertEqual(plan.working_directory, r"C:\repo with spaces")
        self.assertFalse(plan.submits_terminal_input)

    def test_terminal_without_a_draft_only_opens_and_still_never_submits(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "open-terminal",
                    "phrase": "terminal tutaj",
                    "action": "open_terminal",
                    "value": "",
                }
            ]
        )
        match = registry.match("terminal tutaj")
        self.assertIsNotNone(match)
        assert match is not None

        plan = build_action_plan(match, captured_context())

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.operation, OPERATION_OPEN)
        self.assertEqual(plan.payload, "")
        self.assertFalse(plan.submits_terminal_input)

    def test_control_characters_and_newlines_are_rejected_in_terminal_drafts(
        self,
    ) -> None:
        for unsafe in (
            "git status\nwhoami",
            "git status\rwhoami",
            "git\tstatus",
            "echo ok\x00",
            "echo\u200bok",
            "echo first\u2028whoami",
            "echo first\u2029whoami",
        ):
            with self.subTest(unsafe=repr(unsafe)):
                with self.assertRaises(CommandValidationError):
                    sanitize_terminal_draft(unsafe)

        registry = CommandRegistry.from_items(
            [
                {
                    "id": "terminal-draft",
                    "phrase": "terminal wpisz",
                    "action": "open_terminal",
                    "value": "",
                    "match": "prefix_tail",
                }
            ]
        )
        match = registry.match("terminal wpisz git status\nwhoami")
        self.assertIsNotNone(match)
        assert match is not None
        plan = build_action_plan(match, captured_context())
        self.assertFalse(plan.allowed)
        self.assertEqual(plan.denial_reason, "unsafe_terminal_draft")
        self.assertEqual(plan.operation, "deny")
        self.assertEqual(plan.payload, "")
        self.assertIsNone(plan.working_directory)

    def test_oversized_multiline_paste_is_disabled_so_confirmation_is_complete(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "phrase": "wklej raport",
                    "action": "paste_text",
                    "value": "first\n" + ("x" * 700),
                }
            ]
        )
        self.assertFalse(registry.definitions)

    def test_missing_active_explorer_path_denies_terminal_action(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "explorer-terminal",
                    "phrase": "terminal tutaj",
                    "action": "open_terminal",
                    "value": "",
                }
            ]
        )
        match = registry.match("terminal tutaj")
        self.assertIsNotNone(match)
        assert match is not None

        plan = build_action_plan(match, captured_context(None))

        self.assertFalse(plan.allowed)
        self.assertEqual(plan.denial_reason, "explorer_path_unavailable")
        self.assertEqual(plan.payload, "")
        self.assertIsNone(plan.working_directory)

    def test_fixed_terminal_options_are_validated_and_copied_to_plan(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "fixed-terminal",
                    "phrase": "terminal projektu",
                    "action": "open_terminal",
                    "value": "git status --short",
                    "options": {
                        "cwd_source": "fixed",
                        "fixed_cwd": r"C:\src\Mowik",
                        "host": "windows_terminal",
                        "shell": "powershell",
                    },
                }
            ]
        )
        self.assertEqual(registry.issues, ())
        match = registry.match("terminal projektu")
        self.assertIsNotNone(match)
        assert match is not None

        plan = build_action_plan(match, captured_context(None))

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.operation, OPERATION_DRAFT)
        self.assertEqual(plan.payload, "git status --short")
        self.assertEqual(plan.working_directory, r"C:\src\Mowik")
        self.assertIsNotNone(plan.terminal_options)
        assert plan.terminal_options is not None
        self.assertEqual(plan.terminal_options.host, "windows_terminal")
        self.assertEqual(plan.terminal_options.shell, "powershell")
        self.assertFalse(plan.submits_terminal_input)

    def test_home_terminal_options_do_not_require_explorer_context(self) -> None:
        registry = CommandRegistry.from_items(
            [
                {
                    "id": "home-terminal",
                    "phrase": "terminal domowy",
                    "action": "open_terminal",
                    "value": "",
                    "options": {
                        "cwd_source": "home",
                        "host": "classic",
                        "shell": "cmd",
                    },
                }
            ]
        )
        match = registry.match("terminal domowy")
        self.assertIsNotNone(match)
        assert match is not None

        plan = build_action_plan(match, captured_context(None))

        self.assertTrue(plan.allowed)
        self.assertEqual(plan.operation, OPERATION_OPEN)
        self.assertIsNone(plan.working_directory)
        self.assertEqual(plan.terminal_options.cwd_source, "home")  # type: ignore[union-attr]
        self.assertEqual(plan.terminal_options.host, "classic")  # type: ignore[union-attr]
        self.assertEqual(plan.terminal_options.shell, "cmd")  # type: ignore[union-attr]

    def test_invalid_terminal_options_are_disabled_fail_closed(self) -> None:
        for options in (
            {"cwd_source": "fixed"},
            {"cwd_source": "active_explorer", "fixed_cwd": r"C:\unexpected"},
            {"host": "unknown-terminal"},
            {"shell": "bash"},
            {"unknown_option": True},
        ):
            with self.subTest(options=options):
                registry = CommandRegistry.from_items(
                    [
                        {
                            "phrase": "terminal",
                            "action": "open_terminal",
                            "value": "",
                            "options": options,
                        }
                    ]
                )
                self.assertEqual(registry.definitions, ())
                self.assertEqual(len(registry.issues), 1)
                self.assertEqual(registry.issues[0].field, "options")
                self.assertIsNone(registry.match("terminal"))


if __name__ == "__main__":
    unittest.main()
