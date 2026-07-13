from __future__ import annotations

from pathlib import Path
import sys

from PyInstaller.utils.win32.winmanifest import read_manifest_from_executable


REQUIRED_MANIFEST_VALUES = (
    ">true</dpiAware>",
    ">System</dpiAwareness>",
    ">true</longPathAware>",
)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: test-exe-manifest.py PATH_TO_EXE")
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise SystemExit(f"Executable not found: {executable}")

    manifest = read_manifest_from_executable(str(executable)).decode("utf-8")
    missing = [
        value for value in REQUIRED_MANIFEST_VALUES if value not in manifest
    ]
    if missing:
        raise SystemExit(
            "Mowik.exe manifest is missing: " + ", ".join(missing)
        )
    print("Mowik.exe DPI manifest: SystemAware")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
