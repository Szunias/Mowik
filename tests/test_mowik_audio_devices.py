from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mowik_audio_devices as audio_devices


def device(
    name: str,
    hostapi: int,
    *,
    inputs: int = 1,
    outputs: int = 0,
    sample_rate: float = 48_000.0,
) -> dict[str, object]:
    return {
        "name": name,
        "hostapi": hostapi,
        "max_input_channels": inputs,
        "max_output_channels": outputs,
        "default_samplerate": sample_rate,
    }


class StableMicrophoneSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host_apis = [
            {"name": "MME"},
            {"name": "Windows WASAPI"},
        ]
        self.devices = [
            device("Speakers", 0, inputs=0, outputs=2, sample_rate=44_100),
            device("Studio Microphone", 1, inputs=1),
            device("Studio Microphone", 1, inputs=2, outputs=2),
        ]

    def assert_error(
        self, code: str, callback
    ) -> audio_devices.MicrophoneSelectionError:
        with self.assertRaises(audio_devices.MicrophoneSelectionError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        return raised.exception

    def test_json_roundtrip_preserves_exact_schema_one_fingerprint(self) -> None:
        selector = audio_devices.build_microphone_selector(
            2,
            self.devices,
            self.host_apis,
        )

        self.assertEqual(
            selector,
            {
                "schema_version": 1,
                "name": "Studio Microphone",
                "host_api_name": "Windows WASAPI",
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate_hz": 48_000,
            },
        )
        roundtripped = json.loads(json.dumps(selector, ensure_ascii=False))
        parsed = audio_devices.parse_microphone_selector(roundtripped)

        self.assertEqual(parsed.to_json_dict(), selector)
        self.assertEqual(
            audio_devices.resolve_microphone_device(
                roundtripped,
                self.devices,
                self.host_apis,
            ),
            2,
        )

    def test_reorder_of_devices_and_host_apis_resolves_new_index(self) -> None:
        selector = audio_devices.build_microphone_selector(
            2,
            self.devices,
            self.host_apis,
        )
        reordered_host_apis = [
            {"name": "Windows WASAPI"},
            {"name": "MME"},
        ]
        reordered_devices = [
            device("Studio Microphone", 0, inputs=2, outputs=2),
            device("Speakers", 1, inputs=0, outputs=2, sample_rate=44_100),
            device("Studio Microphone", 0, inputs=1),
        ]

        resolved = audio_devices.resolve_microphone_device(
            selector,
            lambda: reordered_devices,
            lambda: reordered_host_apis,
        )

        self.assertEqual(resolved, 0)

    def test_missing_device_fails_closed_without_echoing_its_name(self) -> None:
        selector = audio_devices.build_microphone_selector(
            2,
            self.devices,
            self.host_apis,
        )
        current_devices = [
            device("Speakers", 0, inputs=0, outputs=2, sample_rate=44_100),
            device("Another Microphone", 1, inputs=2, outputs=2),
        ]

        error = self.assert_error(
            audio_devices.ERROR_DEVICE_MISSING,
            lambda: audio_devices.resolve_microphone_device(
                selector,
                current_devices,
                self.host_apis,
            ),
        )

        self.assertNotIn("Studio Microphone", str(error))

    def test_exact_duplicate_fingerprints_are_ambiguous(self) -> None:
        selector = audio_devices.build_microphone_selector(
            2,
            self.devices,
            self.host_apis,
        )
        duplicate = copy.deepcopy(self.devices[2])
        current_devices = [self.devices[2], duplicate]

        self.assert_error(
            audio_devices.ERROR_DEVICE_AMBIGUOUS,
            lambda: audio_devices.resolve_microphone_device(
                selector,
                current_devices,
                self.host_apis,
            ),
        )

    def test_output_only_device_is_never_accepted_as_a_microphone(self) -> None:
        self.assert_error(
            audio_devices.ERROR_DEVICE_NOT_INPUT,
            lambda: audio_devices.build_microphone_selector(
                0,
                self.devices,
                self.host_apis,
            ),
        )
        self.assert_error(
            audio_devices.ERROR_DEVICE_NOT_INPUT,
            lambda: audio_devices.resolve_microphone_device(
                0,
                self.devices,
                self.host_apis,
            ),
        )

        selector = audio_devices.build_microphone_selector(
            1,
            self.devices,
            self.host_apis,
        )
        output_with_same_name = [
            device("Studio Microphone", 1, inputs=0, outputs=2),
        ]
        self.assert_error(
            audio_devices.ERROR_DEVICE_MISSING,
            lambda: audio_devices.resolve_microphone_device(
                selector,
                output_with_same_name,
                self.host_apis,
            ),
        )

    def test_malformed_schema_bool_and_nonlegacy_numbers_are_rejected(self) -> None:
        valid = audio_devices.build_microphone_selector(
            1,
            self.devices,
            self.host_apis,
        )
        malformed_values = [
            {},
            True,
            1.0,
            "1",
            {**valid, "schema_version": True},
            {**valid, "schema_version": 1.0},
            {**valid, "max_input_channels": True},
            {**valid, "default_samplerate_hz": 48_000.0},
            {**valid, "unknown": "field"},
        ]
        for malformed in malformed_values:
            with self.subTest(value=malformed):
                self.assert_error(
                    audio_devices.ERROR_SELECTOR_MALFORMED,
                    lambda malformed=malformed: audio_devices.resolve_microphone_device(
                        malformed,
                        self.devices,
                        self.host_apis,
                    ),
                )

        self.assert_error(
            audio_devices.ERROR_SELECTOR_SCHEMA_UNSUPPORTED,
            lambda: audio_devices.resolve_microphone_device(
                {**valid, "schema_version": 2},
                self.devices,
                self.host_apis,
            ),
        )

    def test_legacy_index_is_validated_in_place_and_never_falls_back(self) -> None:
        self.assertEqual(
            audio_devices.resolve_microphone_device(
                1,
                self.devices,
                self.host_apis,
            ),
            1,
        )
        for invalid in (-1, 99):
            with self.subTest(index=invalid):
                self.assert_error(
                    audio_devices.ERROR_LEGACY_INDEX_INVALID,
                    lambda invalid=invalid: audio_devices.resolve_microphone_device(
                        invalid,
                        self.devices,
                        self.host_apis,
                    ),
                )

        current_after_reorder = [
            device("Different Microphone", 1, inputs=1),
            self.devices[0],
        ]
        self.assert_error(
            audio_devices.ERROR_DEVICE_NOT_INPUT,
            lambda: audio_devices.resolve_microphone_device(
                1,
                current_after_reorder,
                self.host_apis,
            ),
        )

    def test_none_means_default_without_enumerating_hardware(self) -> None:
        devices = mock.Mock(side_effect=AssertionError("devices queried"))
        host_apis = mock.Mock(side_effect=AssertionError("host APIs queried"))

        self.assertIsNone(
            audio_devices.resolve_microphone_device(None, devices, host_apis)
        )
        devices.assert_not_called()
        host_apis.assert_not_called()

    def test_snapshot_callbacks_are_bounded_and_fail_with_nonsecret_code(self) -> None:
        devices = mock.Mock(return_value=self.devices)
        host_apis = mock.Mock(return_value=self.host_apis)
        selector = audio_devices.build_microphone_selector(1, devices, host_apis)

        self.assertEqual(
            audio_devices.resolve_microphone_device(
                selector,
                devices,
                host_apis,
            ),
            1,
        )
        self.assertEqual(devices.call_count, 2)
        self.assertEqual(host_apis.call_count, 2)

        def unavailable():
            raise RuntimeError("secret hardware details")

        error = self.assert_error(
            audio_devices.ERROR_SNAPSHOT_UNAVAILABLE,
            lambda: audio_devices.resolve_microphone_device(
                selector,
                unavailable,
                self.host_apis,
            ),
        )
        self.assertNotIn("secret hardware details", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_malformed_snapshot_fails_closed_instead_of_skipping_records(self) -> None:
        selector = audio_devices.build_microphone_selector(
            1,
            self.devices,
            self.host_apis,
        )
        malformed_devices = [
            self.devices[1],
            {"name": "broken record"},
        ]

        self.assert_error(
            audio_devices.ERROR_SNAPSHOT_MALFORMED,
            lambda: audio_devices.resolve_microphone_device(
                selector,
                malformed_devices,
                self.host_apis,
            ),
        )


if __name__ == "__main__":
    unittest.main()
