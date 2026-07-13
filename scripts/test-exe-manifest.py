from __future__ import annotations

from pathlib import Path
import sys
import xml.etree.ElementTree as ET


ASM_V1 = "urn:schemas-microsoft-com:asm.v1"
ASM_V3 = "urn:schemas-microsoft-com:asm.v3"
COMPAT_V1 = "urn:schemas-microsoft-com:compatibility.v1"
SETTINGS_2005 = "http://schemas.microsoft.com/SMI/2005/WindowsSettings"
SETTINGS_2016 = "http://schemas.microsoft.com/SMI/2016/WindowsSettings"

SUPPORTED_WINDOWS_IDS = {
    "{e2011457-1546-43c5-a5fe-008deee3d3f0}",  # Windows Vista / Server 2008
    "{35138b9a-5d96-4fbd-8e2d-a2440225f93a}",  # Windows 7 / Server 2008 R2
    "{4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38}",  # Windows 8 / Server 2012
    "{1f676c76-80e1-4239-95bb-83d0f6d0da78}",  # Windows 8.1 / Server 2012 R2
    "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}",  # Windows 10 and Windows 11
}


class ManifestValidationError(ValueError):
    pass


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _require_one(root: ET.Element, path: str, description: str) -> ET.Element:
    matches = root.findall(path)
    if len(matches) != 1:
        raise ManifestValidationError(
            f"expected exactly one {description}, found {len(matches)}"
        )
    return matches[0]


def validate_manifest(manifest: bytes | str) -> None:
    try:
        root = ET.fromstring(manifest)
    except (ET.ParseError, UnicodeError) as exc:
        raise ManifestValidationError(f"manifest is not valid XML: {exc}") from exc

    if root.tag != f"{{{ASM_V1}}}assembly":
        raise ManifestValidationError("root element must be asm.v1 assembly")
    if root.attrib != {"manifestVersion": "1.0"}:
        raise ManifestValidationError("assembly must declare only manifestVersion=1.0")

    requested_level = _require_one(
        root,
        f".//{{{ASM_V3}}}requestedExecutionLevel",
        "asm.v3 requestedExecutionLevel",
    )
    all_requested_levels = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "requestedExecutionLevel"
    ]
    if all_requested_levels != [requested_level]:
        raise ManifestValidationError(
            "requestedExecutionLevel must not be duplicated or hidden in another namespace"
        )
    if requested_level.attrib != {"level": "asInvoker", "uiAccess": "false"}:
        raise ManifestValidationError(
            "requestedExecutionLevel must be exactly asInvoker with uiAccess=false"
        )

    auto_elevate = [
        element
        for element in root.iter()
        if _local_name(element.tag) == "autoElevate"
    ]
    if auto_elevate:
        raise ManifestValidationError("autoElevate must not be present")

    supported_os_nodes = root.findall(f".//{{{COMPAT_V1}}}supportedOS")
    supported_os_ids = [node.attrib.get("Id") for node in supported_os_nodes]
    if len(supported_os_ids) != len(set(supported_os_ids)):
        raise ManifestValidationError("supportedOS entries must not be duplicated")
    if set(supported_os_ids) != SUPPORTED_WINDOWS_IDS:
        missing = sorted(SUPPORTED_WINDOWS_IDS - set(supported_os_ids))
        unexpected = sorted(set(supported_os_ids) - SUPPORTED_WINDOWS_IDS)
        raise ManifestValidationError(
            f"supportedOS set mismatch; missing={missing}, unexpected={unexpected}"
        )
    if any(set(node.attrib) != {"Id"} for node in supported_os_nodes):
        raise ManifestValidationError("supportedOS may contain only the Id attribute")

    dpi_aware = _require_one(
        root, f".//{{{SETTINGS_2005}}}dpiAware", "dpiAware setting"
    )
    dpi_awareness = _require_one(
        root, f".//{{{SETTINGS_2016}}}dpiAwareness", "dpiAwareness setting"
    )
    long_path_aware = _require_one(
        root, f".//{{{SETTINGS_2016}}}longPathAware", "longPathAware setting"
    )
    if (dpi_aware.text or "").strip() != "true":
        raise ManifestValidationError("dpiAware must be true")
    if (dpi_awareness.text or "").strip() != "System":
        raise ManifestValidationError("dpiAwareness must be System")
    if (long_path_aware.text or "").strip() != "true":
        raise ManifestValidationError("longPathAware must be true")
    for node, description in (
        (dpi_aware, "dpiAware"),
        (dpi_awareness, "dpiAwareness"),
        (long_path_aware, "longPathAware"),
    ):
        if node.attrib:
            raise ManifestValidationError(f"{description} must not have attributes")

    common_controls = [
        node
        for node in root.findall(f".//{{{ASM_V1}}}assemblyIdentity")
        if node.attrib.get("name") == "Microsoft.Windows.Common-Controls"
    ]
    if len(common_controls) != 1:
        raise ManifestValidationError(
            "expected exactly one Microsoft.Windows.Common-Controls dependency"
        )
    expected_common_controls = {
        "type": "win32",
        "name": "Microsoft.Windows.Common-Controls",
        "version": "6.0.0.0",
        "processorArchitecture": "*",
        "publicKeyToken": "6595b64144ccf1df",
        "language": "*",
    }
    if common_controls[0].attrib != expected_common_controls:
        raise ManifestValidationError(
            "Microsoft.Windows.Common-Controls dependency metadata is not exact"
        )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: test-exe-manifest.py PATH_TO_EXE")
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise SystemExit(f"Executable not found: {executable}")

    from PyInstaller.utils.win32.winmanifest import read_manifest_from_executable

    manifest = read_manifest_from_executable(str(executable))
    try:
        validate_manifest(manifest)
    except ManifestValidationError as exc:
        raise SystemExit(f"Mowik.exe manifest validation failed: {exc}") from exc

    print(
        "Mowik.exe manifest: asInvoker, uiAccess=false, System DPI, "
        "long paths, supported Windows IDs and Common Controls v6 verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
