"""Pure custom-command matching and safety planning for Mowik.

This module deliberately performs no operating-system actions.  It turns
untrusted configuration and a speech transcript into an immutable
``ActionPlan`` which an application-owned executor may carry out.  Keeping
matching, validation and policy here makes it possible to test the security
boundary without importing Mowik's UI, audio stack or Windows integrations.

The executor must honour every field of ``ActionPlan``.  In particular, a
terminal plan with ``operation == "draft"`` means *copy the draft for manual
review without submitting it*. This module never produces a dynamic
command-execution plan.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import ntpath
import re
from types import MappingProxyType
from typing import Any, Final, Optional
import urllib.parse
import unicodedata


MATCH_EXACT: Final = "exact"
MATCH_PREFIX_TAIL: Final = "prefix_tail"

ACTION_PASTE_TEXT: Final = "paste_text"
ACTION_OPEN: Final = "open"
ACTION_OPEN_TERMINAL: Final = "open_terminal"

OPERATION_PASTE: Final = "paste"
OPERATION_OPEN: Final = "open"
OPERATION_DRAFT: Final = "draft"

OUTCOME_SUCCEEDED: Final = "succeeded"
OUTCOME_CANCELLED: Final = "cancelled"
OUTCOME_DENIED: Final = "denied"
OUTCOME_FAILED: Final = "failed"

MAX_COMMANDS: Final = 200
MAX_PHRASE_LENGTH: Final = 120
MAX_VALUE_LENGTH: Final = 50_000
MAX_COMMAND_LINE_LENGTH: Final = 8_000
MAX_OPEN_TARGET_LENGTH: Final = 700
MAX_CONFIRMED_PASTE_LENGTH: Final = 700
MAX_COMMAND_ID_LENGTH: Final = 128

_VALID_MATCH_MODES: Final = frozenset({MATCH_EXACT, MATCH_PREFIX_TAIL})
_VALID_OUTCOMES: Final = frozenset(
    {OUTCOME_SUCCEEDED, OUTCOME_CANCELLED, OUTCOME_DENIED, OUTCOME_FAILED}
)
_VALID_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_VALID_CWD_SOURCES: Final = frozenset({"active_explorer", "fixed", "home"})
_VALID_TERMINAL_HOSTS: Final = frozenset(
    {"auto", "windows_terminal", "classic"}
)
_VALID_TERMINAL_SHELLS: Final = frozenset({"default", "powershell", "cmd"})
_VALID_DRAFT_DELIVERY: Final = frozenset({"clipboard"})
BLOCKED_OPEN_SUFFIXES: Final = frozenset(
    {
        ".bat", ".cmd", ".com", ".cpl", ".hta", ".js", ".jse", ".lnk",
        ".msi", ".msp", ".pif", ".ps1", ".psm1", ".reg", ".scr",
        ".url", ".vbe", ".vbs", ".wsf", ".wsh",
    }
)


class CommandValidationError(ValueError):
    """Raised by strict helpers when command data is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Foreground context captured when the command trigger was pressed.

    ``explorer_path`` is the filesystem directory resolved from the Explorer
    window active at capture time.  It must be ``None`` for virtual locations
    such as This PC or search results.  ``captured_at`` should normally use the
    application's monotonic clock so callers can reject stale plans.
    """

    foreground_hwnd: Optional[int]
    foreground_pid: Optional[int]
    explorer_path: Optional[str]
    captured_at: float
    process_elevated: bool = False


@dataclass(frozen=True, slots=True)
class TerminalOptions:
    """Validated terminal destination and implementation preferences."""

    cwd_source: str = "active_explorer"
    fixed_cwd: Optional[str] = None
    host: str = "auto"
    shell: str = "default"
    draft_delivery: str = "clipboard"


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    """A validated, immutable custom-command definition."""

    id: str
    phrase: str
    normalized_phrase: str
    action: str
    value: str
    confirm: bool
    match_mode: str = MATCH_EXACT
    terminal_options: Optional[TerminalOptions] = None

    @property
    def match(self) -> str:
        """Return the serialized name of the matching strategy."""

        return self.match_mode


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The selected definition and the unexecuted remainder of speech."""

    definition: CommandDefinition
    spoken_tail: str = ""


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Central policy result used while constructing an action plan."""

    allowed: bool
    requires_confirmation: bool
    reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """Immutable instructions for an application-owned action executor.

    ``payload`` is the saved text/target/command, except for ``open_terminal``
    where it is the sanitized terminal draft.  ``working_directory`` is set
    only for terminal actions and comes from the captured Explorer context.
    ``operation == "draft"`` is an explicit instruction to avoid Enter or any
    other form of automatic submission.
    """

    command_id: str
    action: str
    operation: str
    payload: str
    working_directory: Optional[str]
    context: ExecutionContext
    allowed: bool
    requires_confirmation: bool
    denial_reason: Optional[str] = None
    terminal_options: Optional[TerminalOptions] = None

    @property
    def submits_terminal_input(self) -> bool:
        """Always false for plans emitted by this version of the engine."""

        return False


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """Sanitized result of executing (or refusing) an ``ActionPlan``.

    ``code`` should be a stable non-secret identifier suitable for logs and
    localization.  It must not contain command text, paths or pasted content.
    """

    command_id: str
    action: str
    status: str
    code: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_OUTCOMES:
            raise ValueError(f"Unknown action outcome: {self.status!r}")

    @classmethod
    def from_plan(
        cls,
        plan: ActionPlan,
        status: str,
        code: str = "",
    ) -> "ActionOutcome":
        """Create an outcome without copying sensitive plan payloads."""

        return cls(plan.command_id, plan.action, status, code)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A non-secret explanation of why an input definition was disabled."""

    index: Optional[int]
    code: str
    field: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Validation and safety metadata for one supported action."""

    name: str
    operation: str
    value_required: bool
    value_max_length: int
    value_kind: str
    allowed_match_modes: frozenset[str]
    confirmation: str


# All action-level capabilities live in this registry.  Adding an action
# therefore requires an explicit decision about validation, prefix matching
# and confirmation rather than falling through permissive defaults.
ACTION_REGISTRY: Final[Mapping[str, ActionSpec]] = MappingProxyType(
    {
        ACTION_PASTE_TEXT: ActionSpec(
            ACTION_PASTE_TEXT,
            OPERATION_PASTE,
            True,
            MAX_VALUE_LENGTH,
            "text",
            frozenset({MATCH_EXACT}),
            "never",
        ),
        ACTION_OPEN: ActionSpec(
            ACTION_OPEN,
            OPERATION_OPEN,
            True,
            MAX_OPEN_TARGET_LENGTH,
            "open_target",
            frozenset({MATCH_EXACT}),
            "always",
        ),
        ACTION_OPEN_TERMINAL: ActionSpec(
            ACTION_OPEN_TERMINAL,
            OPERATION_OPEN,
            False,
            MAX_COMMAND_LINE_LENGTH,
            "terminal_draft",
            frozenset({MATCH_EXACT, MATCH_PREFIX_TAIL}),
            "never",
        ),
    }
)


def _is_match_separator(character: str) -> bool:
    category = unicodedata.category(character)
    return character.isspace() or category[:1] in {"P", "Z", "C"}


def normalize_command_phrase(value: Any) -> str:
    """Return the punctuation-insensitive key used for exact matching.

    There is intentionally no fuzzy, phonetic or substring matching.  Unicode
    normalization and case folding only make equivalent text compare equal;
    punctuation/control runs act as word separators.
    """

    text = unicodedata.normalize(
        "NFKC",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )
    normalized: list[str] = []
    for character in text:
        normalized.append(" " if _is_match_separator(character) else character)
    return " ".join("".join(normalized).split())


def _tokens_with_raw_spans(text: str) -> list[tuple[str, int, int]]:
    """Tokenize normalized speech while retaining raw suffix boundaries."""

    tokens: list[tuple[str, int, int]] = []
    run_start: Optional[int] = None
    for offset, character in enumerate(text):
        if _is_match_separator(character):
            if run_start is not None:
                raw_run = text[run_start:offset]
                for token in normalize_command_phrase(raw_run).split():
                    tokens.append((token, run_start, offset))
                run_start = None
        elif run_start is None:
            run_start = offset
    if run_start is not None:
        raw_run = text[run_start:]
        for token in normalize_command_phrase(raw_run).split():
            tokens.append((token, run_start, len(text)))
    return tokens


def sanitize_terminal_draft(value: str) -> str:
    """Validate and trim a terminal draft without changing its punctuation.

    A draft is exactly one visible Unicode line. CR, LF, Unicode line/paragraph
    separators, NUL and every other control/format/surrogate/private-use code
    point are rejected rather than removed, so unsafe input can never become a
    different command through sanitization. The executor may only copy the
    result to the clipboard.
    """

    if not isinstance(value, str):
        raise CommandValidationError("terminal_draft_not_string")
    if len(value) > MAX_COMMAND_LINE_LENGTH:
        raise CommandValidationError("terminal_draft_too_long")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    ):
        raise CommandValidationError("terminal_draft_contains_control")
    return value.strip()


def _validate_single_line(value: str, *, maximum: int) -> str:
    if len(value) > maximum:
        raise CommandValidationError("value_too_long")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    ):
        raise CommandValidationError("value_contains_control")
    result = value.strip()
    if not result:
        raise CommandValidationError("value_empty")
    return result


def validate_open_target_syntax(
    value: str,
    *,
    maximum: int = MAX_OPEN_TARGET_LENGTH,
) -> str:
    """Validate an HTTPS URL or an absolute local Windows path.

    This is deliberately syntax-only so the pure command engine never touches
    the filesystem. The application executor must additionally resolve the
    path and require an existing local file or directory immediately before
    opening it.
    """

    if not isinstance(value, str):
        raise CommandValidationError("value_not_string")
    if value != value.strip():
        raise CommandValidationError("open_outer_whitespace_denied")
    target = _validate_single_line(value, maximum=maximum)
    drive, tail = ntpath.splitdrive(target)
    scheme_match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)
    if scheme_match and not drive:
        if "\\" in target or any(character.isspace() for character in target):
            raise CommandValidationError("open_url_not_allowed")
        try:
            parsed = urllib.parse.urlsplit(target)
            _ = parsed.port
        except ValueError as exc:
            raise CommandValidationError("open_url_invalid") from exc
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or "%" in parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise CommandValidationError("open_url_not_allowed")
        return target

    if (
        not drive
        or not tail.startswith(("\\", "/"))
        or target.startswith(("\\\\", "//"))
    ):
        raise CommandValidationError("open_absolute_local_path_required")
    # A colon after the drive designator selects an NTFS alternate data stream.
    # It must never be hidden behind a harmless-looking filename in the UI.
    if ":" in tail:
        raise CommandValidationError("open_alternate_stream_denied")
    components = [part for part in re.split(r"[\\/]", tail) if part]
    if any(part.endswith((" ", ".")) for part in components):
        raise CommandValidationError("open_ambiguous_windows_name_denied")
    if ntpath.splitext(target.rstrip("\\/"))[1].casefold() in BLOCKED_OPEN_SUFFIXES:
        raise CommandValidationError("open_active_content_denied")
    return target


def _validate_value(value: Any, spec: ActionSpec) -> str:
    if not isinstance(value, str):
        raise CommandValidationError("value_not_string")
    if spec.value_kind == "text":
        if len(value) > spec.value_max_length:
            raise CommandValidationError("value_too_long")
        if "\x00" in value or not value.strip():
            raise CommandValidationError("value_empty_or_nul")
        if any(
            (
                unicodedata.category(character).startswith("C")
                and character not in {"\r", "\n"}
            )
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        ):
            raise CommandValidationError("value_contains_control")
        if (
            any(marker in value for marker in ("\r", "\n"))
            and len(value) > MAX_CONFIRMED_PASTE_LENGTH
        ):
            raise CommandValidationError("multiline_value_too_long")
        return value
    if spec.value_kind == "single_line":
        return _validate_single_line(value, maximum=spec.value_max_length)
    if spec.value_kind == "open_target":
        return validate_open_target_syntax(value, maximum=spec.value_max_length)
    if spec.value_kind == "terminal_draft":
        draft = sanitize_terminal_draft(value)
        if spec.value_required and not draft:
            raise CommandValidationError("value_empty")
        return draft
    raise CommandValidationError("unknown_value_kind")


def _generated_id(
    *,
    normalized_phrase: str,
    action: str,
    value: str,
    confirm: bool,
    match_mode: str,
    terminal_options: Optional[TerminalOptions],
) -> str:
    """Build a deterministic content ID without mutating user config."""

    canonical = json.dumps(
        {
            "action": action,
            "confirm": confirm,
            "match": match_mode,
            "phrase": normalized_phrase,
            "terminal_options": (
                {
                    "cwd_source": terminal_options.cwd_source,
                    "fixed_cwd": terminal_options.fixed_cwd,
                    "host": terminal_options.host,
                    "shell": terminal_options.shell,
                    "draft_delivery": terminal_options.draft_delivery,
                }
                if terminal_options is not None
                else None
            ),
            "value": value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "cc_" + hashlib.sha256(canonical).hexdigest()[:24]


def _validated_id(raw_id: Any) -> Optional[str]:
    if raw_id is None:
        return None
    if not isinstance(raw_id, str):
        raise CommandValidationError("id_not_string")
    command_id = raw_id.strip()
    if (
        not command_id
        or len(command_id) > MAX_COMMAND_ID_LENGTH
        or _VALID_ID_RE.fullmatch(command_id) is None
    ):
        raise CommandValidationError("id_invalid")
    return command_id


def _parse_terminal_options(raw_options: Any) -> TerminalOptions:
    if raw_options is None:
        return TerminalOptions()
    if not isinstance(raw_options, Mapping):
        raise CommandValidationError("options_not_object")
    known_keys = {
        "cwd_source",
        "fixed_cwd",
        "host",
        "shell",
        "draft_delivery",
    }
    if any(key not in known_keys for key in raw_options):
        raise CommandValidationError("options_unknown_field")

    cwd_source = raw_options.get("cwd_source", "active_explorer")
    host = raw_options.get("host", "auto")
    shell = raw_options.get("shell", "default")
    draft_delivery = raw_options.get("draft_delivery", "clipboard")
    if not isinstance(cwd_source, str) or cwd_source not in _VALID_CWD_SOURCES:
        raise CommandValidationError("options_cwd_source_invalid")
    if not isinstance(host, str) or host not in _VALID_TERMINAL_HOSTS:
        raise CommandValidationError("options_host_invalid")
    if not isinstance(shell, str) or shell not in _VALID_TERMINAL_SHELLS:
        raise CommandValidationError("options_shell_invalid")
    if (
        not isinstance(draft_delivery, str)
        or draft_delivery not in _VALID_DRAFT_DELIVERY
    ):
        raise CommandValidationError("options_draft_delivery_invalid")

    raw_fixed_cwd = raw_options.get("fixed_cwd")
    fixed_cwd: Optional[str] = None
    if raw_fixed_cwd is not None:
        if not isinstance(raw_fixed_cwd, str):
            raise CommandValidationError("options_fixed_cwd_not_string")
        fixed_cwd = _validate_single_line(
            raw_fixed_cwd,
            maximum=MAX_VALUE_LENGTH,
        )
    if cwd_source == "fixed" and fixed_cwd is None:
        raise CommandValidationError("options_fixed_cwd_required")
    if cwd_source != "fixed" and fixed_cwd is not None:
        raise CommandValidationError("options_fixed_cwd_unexpected")
    return TerminalOptions(cwd_source, fixed_cwd, host, shell, draft_delivery)


def _parse_definition(raw: Any, index: int) -> CommandDefinition:
    if not isinstance(raw, Mapping):
        raise CommandValidationError("definition_not_object")

    phrase = raw.get("phrase")
    if not isinstance(phrase, str):
        raise CommandValidationError("phrase_not_string")
    phrase = phrase.strip()
    normalized_phrase = normalize_command_phrase(phrase)
    if (
        not normalized_phrase
        or len(phrase) > MAX_PHRASE_LENGTH
        or "\x00" in phrase
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in phrase
        )
    ):
        raise CommandValidationError("phrase_invalid")

    raw_action = raw.get("action", ACTION_PASTE_TEXT)
    if not isinstance(raw_action, str):
        raise CommandValidationError("action_not_string")
    action = raw_action.strip().lower()
    spec = ACTION_REGISTRY.get(action)
    if spec is None:
        raise CommandValidationError("action_unknown")

    raw_match = raw.get("match", MATCH_EXACT)
    if not isinstance(raw_match, str):
        raise CommandValidationError("match_not_string")
    match_mode = raw_match.strip().lower()
    if match_mode not in _VALID_MATCH_MODES:
        raise CommandValidationError("match_unknown")
    if match_mode not in spec.allowed_match_modes:
        raise CommandValidationError("match_not_allowed_for_action")

    if "value" in raw:
        raw_value = raw.get("value")
    elif "text" in raw:
        # Compatibility with the original paste-text prototype.
        raw_value = raw.get("text")
    else:
        raw_value = ""
    value = _validate_value(raw_value, spec)
    if action == ACTION_OPEN_TERMINAL and match_mode == MATCH_PREFIX_TAIL and value:
        raise CommandValidationError("terminal_prefix_has_fixed_draft")

    if action == ACTION_OPEN_TERMINAL:
        terminal_options = _parse_terminal_options(raw.get("options"))
    else:
        if "options" in raw:
            raise CommandValidationError("options_not_allowed_for_action")
        terminal_options = None

    default_confirm = spec.confirmation in {"requested", "always"}
    raw_confirm = raw.get("confirm", default_confirm)
    # Stare ręcznie edytowane konfiguracje mogły zawierać inną wartość.
    # Zamiast wyłączać całą bezpieczną komendę wracamy do jej domyślnego
    # zachowania. Nieznane starsze akcje są wcześniej odrzucane przez rejestr
    # możliwości i nigdy nie trafiają do planowania.
    confirm = raw_confirm if isinstance(raw_confirm, bool) else default_confirm
    if action == ACTION_OPEN:
        confirm = True
    elif action in {ACTION_PASTE_TEXT, ACTION_OPEN_TERMINAL}:
        confirm = False

    command_id = _validated_id(raw.get("id"))
    if command_id is None:
        command_id = _generated_id(
            normalized_phrase=normalized_phrase,
            action=action,
            value=value,
            confirm=confirm,
            match_mode=match_mode,
            terminal_options=terminal_options,
        )

    return CommandDefinition(
        id=command_id,
        phrase=phrase,
        normalized_phrase=normalized_phrase,
        action=action,
        value=value,
        confirm=confirm,
        match_mode=match_mode,
        terminal_options=terminal_options,
    )


def _field_for_error(code: str) -> Optional[str]:
    for field in (
        "phrase",
        "action",
        "match",
        "value",
        "confirm",
        "options",
        "id",
    ):
        if code.startswith(field) or f"_{field}" in code:
            return field
    return None


class CommandRegistry:
    """Validated command set with deterministic, non-fuzzy matching.

    Invalid records and ambiguous duplicate IDs/match keys are retained only as
    non-secret ``issues``.  They never participate in matching.  The registry
    itself is immutable from a caller's perspective.
    """

    __slots__ = ("_definitions", "_exact", "_issues", "_prefix")

    def __init__(
        self,
        definitions: Iterable[CommandDefinition] = (),
        issues: Iterable[ValidationIssue] = (),
    ) -> None:
        candidates = tuple(definitions)
        issue_list = list(issues)

        id_counts = Counter(item.id for item in candidates)
        match_counts = Counter(
            (item.match_mode, item.normalized_phrase) for item in candidates
        )
        accepted: list[CommandDefinition] = []
        for item in candidates:
            if id_counts[item.id] != 1:
                issue_list.append(ValidationIssue(None, "duplicate_id", "id"))
                continue
            if match_counts[(item.match_mode, item.normalized_phrase)] != 1:
                issue_list.append(
                    ValidationIssue(None, "duplicate_match", "phrase")
                )
                continue
            accepted.append(item)

        self._definitions = tuple(accepted)
        self._issues = tuple(issue_list)
        self._exact = MappingProxyType(
            {
                item.normalized_phrase: item
                for item in accepted
                if item.match_mode == MATCH_EXACT
            }
        )
        # Stable secondary ID ordering guarantees deterministic behaviour even
        # when equally long prefixes normalize to unexpected Unicode forms.
        self._prefix = tuple(
            sorted(
                (
                    item
                    for item in accepted
                    if item.match_mode == MATCH_PREFIX_TAIL
                ),
                key=lambda item: (-len(item.normalized_phrase), item.id),
            )
        )

    @property
    def definitions(self) -> tuple[CommandDefinition, ...]:
        return self._definitions

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        return self._issues

    @classmethod
    def from_items(cls, items: Any) -> "CommandRegistry":
        """Parse the current flat ``phrase/action/value/confirm`` records."""

        if not isinstance(items, Sequence) or isinstance(
            items, (str, bytes, bytearray)
        ):
            return cls(issues=(ValidationIssue(None, "items_not_array", "items"),))

        issues: list[ValidationIssue] = []
        definitions: list[CommandDefinition] = []
        if len(items) > MAX_COMMANDS:
            issues.append(ValidationIssue(None, "too_many_commands", "items"))
        for index, raw in enumerate(items[:MAX_COMMANDS]):
            try:
                definitions.append(_parse_definition(raw, index))
            except CommandValidationError as exc:
                code = str(exc) or "definition_invalid"
                issues.append(ValidationIssue(index, code, _field_for_error(code)))
        return cls(definitions, issues)

    @classmethod
    def from_config(cls, config: Any) -> "CommandRegistry":
        """Parse ``config['custom_commands']['items']`` without modifying it."""

        if not isinstance(config, Mapping):
            return cls(issues=(ValidationIssue(None, "config_not_object"),))
        settings = config.get("custom_commands", {})
        if not isinstance(settings, Mapping):
            return cls(
                issues=(
                    ValidationIssue(None, "custom_commands_not_object", "custom_commands"),
                )
            )
        return cls.from_items(settings.get("items", ()))

    def match(self, transcript: Any) -> Optional[MatchResult]:
        """Match exact commands first, then the longest prefix-tail command."""

        if not isinstance(transcript, str):
            return None
        normalized = normalize_command_phrase(transcript)
        if not normalized:
            return None

        exact = self._exact.get(normalized)
        if exact is not None:
            return MatchResult(exact, "")

        for definition in self._prefix:
            prefix = definition.normalized_phrase
            if normalized != prefix and not normalized.startswith(prefix + " "):
                continue
            if normalized == prefix:
                return MatchResult(definition, "")

            # Retain shell punctuation in the spoken tail.  Only whitespace
            # separating the invocation phrase from its tail is removed.
            phrase_token_count = len(prefix.split())
            raw_tokens = _tokens_with_raw_spans(transcript)
            if len(raw_tokens) < phrase_token_count:
                return None
            raw_end = raw_tokens[phrase_token_count - 1][2]
            return MatchResult(definition, transcript[raw_end:].lstrip())
        return None


class SafetyPolicy:
    """Central, fail-closed policy for all command actions.

    Arbitrary process/command execution is not a capability of this engine.
    ``open_terminal`` may open a terminal and copy a one-line draft to the
    clipboard without confirmation, but the draft is never typed or submitted.
    """

    def evaluate(self, match: MatchResult) -> SafetyDecision:
        definition = match.definition
        spec = ACTION_REGISTRY.get(definition.action)
        if spec is None:
            return SafetyDecision(False, False, "action_unknown")

        if definition.action == ACTION_OPEN_TERMINAL:
            return SafetyDecision(True, False)

        if definition.action == ACTION_PASTE_TEXT and any(
            marker in definition.value for marker in ("\r", "\n")
        ):
            # Wielowierszowe wklejenie do terminala może samo wykonać kod.
            return SafetyDecision(True, True)

        if match.spoken_tail:
            return SafetyDecision(False, False, "unexpected_spoken_tail")

        if spec.confirmation == "always":
            return SafetyDecision(True, True)
        if spec.confirmation == "never":
            return SafetyDecision(True, False)
        if spec.confirmation == "requested":
            return SafetyDecision(True, bool(definition.confirm))
        return SafetyDecision(False, False, "confirmation_policy_unknown")


DEFAULT_SAFETY_POLICY: Final = SafetyPolicy()


def _safe_explorer_path(path: Optional[str]) -> Optional[str]:
    if not isinstance(path, str):
        return None
    candidate = path.strip()
    if not candidate:
        return None
    if any(unicodedata.category(character).startswith("C") for character in candidate):
        return None
    return candidate


def build_action_plan(
    match: MatchResult,
    context: ExecutionContext,
    policy: SafetyPolicy = DEFAULT_SAFETY_POLICY,
) -> ActionPlan:
    """Convert a match and captured foreground context into a safe plan.

    Planning does not touch the filesystem, focus windows, start processes or
    show confirmation UI.  Denials are returned as plans so the caller can
    produce a precise ``ActionOutcome`` without exceptions or secret logging.
    """

    definition = match.definition
    spec = ACTION_REGISTRY.get(definition.action)
    if spec is None:
        return ActionPlan(
            command_id=definition.id,
            action=definition.action,
            operation="deny",
            payload="",
            working_directory=None,
            context=context,
            allowed=False,
            requires_confirmation=False,
            denial_reason="action_unknown",
        )

    decision = policy.evaluate(match)
    requires_confirmation = decision.requires_confirmation
    payload = definition.value
    working_directory: Optional[str] = None
    operation = spec.operation
    denial_reason = decision.reason
    allowed = decision.allowed

    # A terminal or opened target would inherit Mówik's elevated token.  The
    # desktop app is intentionally manifested as asInvoker, so an elevated
    # process is exceptional and these actions fail closed instead of silently
    # becoming administrator-level launchers.
    if context.process_elevated and definition.action in {
        ACTION_OPEN,
        ACTION_OPEN_TERMINAL,
    }:
        allowed = False
        requires_confirmation = False
        denial_reason = "elevated_process_denied"

    if definition.action == ACTION_OPEN_TERMINAL:
        options = definition.terminal_options or TerminalOptions()
        if options.cwd_source == "active_explorer":
            working_directory = _safe_explorer_path(context.explorer_path)
            if working_directory is None:
                allowed = False
                denial_reason = "explorer_path_unavailable"
        elif options.cwd_source == "fixed":
            working_directory = options.fixed_cwd
        elif options.cwd_source == "home":
            # Home-directory resolution belongs to the OS executor.
            working_directory = None
        else:
            allowed = False
            denial_reason = "terminal_cwd_source_unknown"
        raw_draft = match.spoken_tail if match.spoken_tail else definition.value
        try:
            payload = sanitize_terminal_draft(raw_draft)
        except CommandValidationError:
            payload = ""
            allowed = False
            denial_reason = "unsafe_terminal_draft"
        operation = OPERATION_DRAFT if payload else OPERATION_OPEN

    if not allowed:
        # Never carry executable or attacker-controlled dynamic text in a
        # denied plan.  This also keeps accidental logs free of that content.
        payload = ""
        working_directory = None
        operation = "deny"

    return ActionPlan(
        command_id=definition.id,
        action=definition.action,
        operation=operation,
        payload=payload,
        working_directory=working_directory,
        context=context,
        allowed=allowed,
        requires_confirmation=requires_confirmation,
        denial_reason=denial_reason,
        terminal_options=definition.terminal_options,
    )


def parse_custom_commands(config: Any) -> CommandRegistry:
    """Convenience wrapper for ``CommandRegistry.from_config``."""

    return CommandRegistry.from_config(config)


__all__ = [
    "ACTION_OPEN",
    "ACTION_OPEN_TERMINAL",
    "ACTION_PASTE_TEXT",
    "ACTION_REGISTRY",
    "BLOCKED_OPEN_SUFFIXES",
    "ActionOutcome",
    "ActionPlan",
    "ActionSpec",
    "CommandDefinition",
    "CommandRegistry",
    "CommandValidationError",
    "DEFAULT_SAFETY_POLICY",
    "ExecutionContext",
    "MATCH_EXACT",
    "MATCH_PREFIX_TAIL",
    "MAX_OPEN_TARGET_LENGTH",
    "MatchResult",
    "SafetyDecision",
    "SafetyPolicy",
    "TerminalOptions",
    "ValidationIssue",
    "build_action_plan",
    "normalize_command_phrase",
    "parse_custom_commands",
    "sanitize_terminal_draft",
    "validate_open_target_syntax",
]
