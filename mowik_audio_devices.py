"""Pure helpers for stable, fail-closed PortAudio microphone selection.

PortAudio's numeric device indices are enumeration positions and may change
after a reboot or after connecting another audio device.  Schema 1 stores a
small, non-secret fingerprint made only from values returned by PortAudio:
the exact device and host-API names, channel counts, and normalized default
sample rate.  Resolution succeeds only when exactly one current input device
has the same fingerprint.

The module deliberately does not import ``sounddevice``.  Callers provide
already captured snapshots or zero-argument callbacks, which keeps both the
runtime boundary and tests deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any, Optional, TypeAlias


MICROPHONE_SELECTOR_SCHEMA_VERSION = 1
MAX_DEVICE_NAME_LENGTH = 512
MAX_HOST_API_NAME_LENGTH = 256
MAX_CHANNEL_COUNT = 1_024
MIN_SAMPLE_RATE_HZ = 1_000
MAX_SAMPLE_RATE_HZ = 1_536_000

ERROR_SELECTOR_MALFORMED = "microphone_selector_malformed"
ERROR_SELECTOR_SCHEMA_UNSUPPORTED = "microphone_selector_schema_unsupported"
ERROR_DEVICE_MISSING = "microphone_device_missing"
ERROR_DEVICE_AMBIGUOUS = "microphone_device_ambiguous"
ERROR_SNAPSHOT_UNAVAILABLE = "microphone_snapshot_unavailable"
ERROR_SNAPSHOT_MALFORMED = "microphone_snapshot_malformed"
ERROR_LEGACY_INDEX_INVALID = "microphone_legacy_index_invalid"
ERROR_DEVICE_NOT_INPUT = "microphone_device_not_input"


SnapshotProvider: TypeAlias = (
    Iterable[Mapping[str, Any]] | Callable[[], Iterable[Mapping[str, Any]]]
)


class MicrophoneSelectionError(ValueError):
    """Fail-closed selection failure whose message never contains device data."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class MicrophoneSelector:
    schema_version: int
    name: str
    host_api_name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate_hz: int

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "host_api_name": self.host_api_name,
            "max_input_channels": self.max_input_channels,
            "max_output_channels": self.max_output_channels,
            "default_samplerate_hz": self.default_samplerate_hz,
        }


_SELECTOR_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "host_api_name",
        "max_input_channels",
        "max_output_channels",
        "default_samplerate_hz",
    }
)


def _materialize_snapshot(source: SnapshotProvider) -> tuple[Mapping[str, Any], ...]:
    items: Optional[tuple[Any, ...]] = None
    try:
        raw = source() if callable(source) else source
        if isinstance(raw, (str, bytes, bytearray, Mapping)):
            raise TypeError("snapshot must be an iterable of mappings")
        items = tuple(raw)
    except Exception:
        pass
    if items is None:
        # Błąd sterownika/callbacku może zawierać nazwę urządzenia.
        # Wyjście z bloku except przed zgłoszeniem usuwa również niejawny
        # exception context, a publiczna granica zwraca tylko stabilny kod.
        raise MicrophoneSelectionError(ERROR_SNAPSHOT_UNAVAILABLE)
    if any(not isinstance(item, Mapping) for item in items):
        raise MicrophoneSelectionError(ERROR_SNAPSHOT_MALFORMED)
    return items


def _bounded_name(value: Any, maximum: int, error_code: str) -> str:
    if not isinstance(value, str):
        raise MicrophoneSelectionError(error_code)
    if not value.strip() or len(value) > maximum:
        raise MicrophoneSelectionError(error_code)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise MicrophoneSelectionError(error_code)
    return value


def _bounded_integer(
    value: Any,
    minimum: int,
    maximum: int,
    error_code: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise MicrophoneSelectionError(error_code)
    normalized = int(value)
    if not minimum <= normalized <= maximum:
        raise MicrophoneSelectionError(error_code)
    return normalized


def _sample_rate_hz(value: Any, error_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MicrophoneSelectionError(error_code)
    numeric = float(value)
    if not math.isfinite(numeric):
        raise MicrophoneSelectionError(error_code)
    normalized = int(round(numeric))
    if not MIN_SAMPLE_RATE_HZ <= normalized <= MAX_SAMPLE_RATE_HZ:
        raise MicrophoneSelectionError(error_code)
    return normalized


def parse_microphone_selector(value: Any) -> MicrophoneSelector:
    """Validate a schema-1 JSON value without exposing its contents in errors."""

    if not isinstance(value, Mapping) or set(value) != _SELECTOR_KEYS:
        raise MicrophoneSelectionError(ERROR_SELECTOR_MALFORMED)
    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, Integral):
        raise MicrophoneSelectionError(ERROR_SELECTOR_MALFORMED)
    if int(schema_version) != MICROPHONE_SELECTOR_SCHEMA_VERSION:
        raise MicrophoneSelectionError(ERROR_SELECTOR_SCHEMA_UNSUPPORTED)
    return MicrophoneSelector(
        schema_version=MICROPHONE_SELECTOR_SCHEMA_VERSION,
        name=_bounded_name(
            value.get("name"),
            MAX_DEVICE_NAME_LENGTH,
            ERROR_SELECTOR_MALFORMED,
        ),
        host_api_name=_bounded_name(
            value.get("host_api_name"),
            MAX_HOST_API_NAME_LENGTH,
            ERROR_SELECTOR_MALFORMED,
        ),
        max_input_channels=_bounded_integer(
            value.get("max_input_channels"),
            1,
            MAX_CHANNEL_COUNT,
            ERROR_SELECTOR_MALFORMED,
        ),
        max_output_channels=_bounded_integer(
            value.get("max_output_channels"),
            0,
            MAX_CHANNEL_COUNT,
            ERROR_SELECTOR_MALFORMED,
        ),
        default_samplerate_hz=_bounded_integer(
            value.get("default_samplerate_hz"),
            MIN_SAMPLE_RATE_HZ,
            MAX_SAMPLE_RATE_HZ,
            ERROR_SELECTOR_MALFORMED,
        ),
    )


def _host_api_name(
    device: Mapping[str, Any], host_apis: tuple[Mapping[str, Any], ...]
) -> str:
    host_api_index = _bounded_integer(
        device.get("hostapi"),
        0,
        max(0, len(host_apis) - 1),
        ERROR_SNAPSHOT_MALFORMED,
    )
    if not host_apis:
        raise MicrophoneSelectionError(ERROR_SNAPSHOT_MALFORMED)
    return _bounded_name(
        host_apis[host_api_index].get("name"),
        MAX_HOST_API_NAME_LENGTH,
        ERROR_SNAPSHOT_MALFORMED,
    )


def _selector_from_device(
    device: Mapping[str, Any],
    host_apis: tuple[Mapping[str, Any], ...],
    *,
    not_input_error: str,
) -> MicrophoneSelector:
    input_channels = _bounded_integer(
        device.get("max_input_channels"),
        0,
        MAX_CHANNEL_COUNT,
        ERROR_SNAPSHOT_MALFORMED,
    )
    if input_channels == 0:
        raise MicrophoneSelectionError(not_input_error)
    return MicrophoneSelector(
        schema_version=MICROPHONE_SELECTOR_SCHEMA_VERSION,
        name=_bounded_name(
            device.get("name"),
            MAX_DEVICE_NAME_LENGTH,
            ERROR_SNAPSHOT_MALFORMED,
        ),
        host_api_name=_host_api_name(device, host_apis),
        max_input_channels=input_channels,
        max_output_channels=_bounded_integer(
            device.get("max_output_channels"),
            0,
            MAX_CHANNEL_COUNT,
            ERROR_SNAPSHOT_MALFORMED,
        ),
        default_samplerate_hz=_sample_rate_hz(
            device.get("default_samplerate"),
            ERROR_SNAPSHOT_MALFORMED,
        ),
    )


def build_microphone_selector(
    device_index: int,
    devices: SnapshotProvider,
    host_apis: SnapshotProvider,
) -> dict[str, Any]:
    """Capture a stable schema-1 selector for one current input device."""

    if isinstance(device_index, bool) or not isinstance(device_index, Integral):
        raise MicrophoneSelectionError(ERROR_LEGACY_INDEX_INVALID)
    index = int(device_index)
    if index < 0:
        raise MicrophoneSelectionError(ERROR_LEGACY_INDEX_INVALID)
    device_snapshot = _materialize_snapshot(devices)
    if index >= len(device_snapshot):
        raise MicrophoneSelectionError(ERROR_LEGACY_INDEX_INVALID)
    host_api_snapshot = _materialize_snapshot(host_apis)
    selector = _selector_from_device(
        device_snapshot[index],
        host_api_snapshot,
        not_input_error=ERROR_DEVICE_NOT_INPUT,
    )
    return selector.to_json_dict()


def _resolve_legacy_index(
    value: int,
    devices: tuple[Mapping[str, Any], ...],
    host_apis: tuple[Mapping[str, Any], ...],
) -> int:
    if value < 0 or value >= len(devices):
        raise MicrophoneSelectionError(ERROR_LEGACY_INDEX_INVALID)
    _selector_from_device(
        devices[value],
        host_apis,
        not_input_error=ERROR_DEVICE_NOT_INPUT,
    )
    return value


def resolve_microphone_device(
    value: Any,
    devices: SnapshotProvider,
    host_apis: SnapshotProvider,
) -> Optional[int]:
    """Resolve ``None``, a legacy index, or a schema-1 selector fail-closed.

    ``None`` deliberately means the current Windows/PortAudio default.  Every
    other value either resolves to one explicit current input index or raises
    :class:`MicrophoneSelectionError` with a non-secret ``code``.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise MicrophoneSelectionError(ERROR_SELECTOR_MALFORMED)

    if isinstance(value, Integral):
        legacy_index = int(value)
        if legacy_index < 0:
            raise MicrophoneSelectionError(ERROR_LEGACY_INDEX_INVALID)
        device_snapshot = _materialize_snapshot(devices)
        if legacy_index >= len(device_snapshot):
            raise MicrophoneSelectionError(ERROR_LEGACY_INDEX_INVALID)
        host_api_snapshot = _materialize_snapshot(host_apis)
        return _resolve_legacy_index(legacy_index, device_snapshot, host_api_snapshot)

    selector = parse_microphone_selector(value)
    device_snapshot = _materialize_snapshot(devices)
    host_api_snapshot = _materialize_snapshot(host_apis)
    matches: list[int] = []
    for index, device in enumerate(device_snapshot):
        try:
            candidate = _selector_from_device(
                device,
                host_api_snapshot,
                not_input_error=ERROR_DEVICE_NOT_INPUT,
            )
        except MicrophoneSelectionError as exc:
            if exc.code == ERROR_DEVICE_NOT_INPUT:
                continue
            raise
        if candidate == selector:
            matches.append(index)
    if not matches:
        raise MicrophoneSelectionError(ERROR_DEVICE_MISSING)
    if len(matches) != 1:
        raise MicrophoneSelectionError(ERROR_DEVICE_AMBIGUOUS)
    return matches[0]


__all__ = [
    "ERROR_DEVICE_AMBIGUOUS",
    "ERROR_DEVICE_MISSING",
    "ERROR_DEVICE_NOT_INPUT",
    "ERROR_LEGACY_INDEX_INVALID",
    "ERROR_SELECTOR_MALFORMED",
    "ERROR_SELECTOR_SCHEMA_UNSUPPORTED",
    "ERROR_SNAPSHOT_MALFORMED",
    "ERROR_SNAPSHOT_UNAVAILABLE",
    "MICROPHONE_SELECTOR_SCHEMA_VERSION",
    "MicrophoneSelectionError",
    "MicrophoneSelector",
    "build_microphone_selector",
    "parse_microphone_selector",
    "resolve_microphone_device",
]
