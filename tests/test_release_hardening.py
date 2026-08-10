from __future__ import annotations

import importlib.util
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def current_app_version() -> str:
    """Read APP_VERSION without importing Mówik's heavy runtime dependencies."""

    source = (ROOT / "mowik.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"\r\n]+)"$', source, flags=re.MULTILINE)
    if match is None:
        raise RuntimeError("mowik.py does not declare a single APP_VERSION")
    return match.group(1)


def load_manifest_validator():
    path = ROOT / "scripts" / "test-exe-manifest.py"
    spec = importlib.util.spec_from_file_location("mowik_manifest_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManifestHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = load_manifest_validator()
        cls.source = (ROOT / "packaging" / "Mowik.manifest").read_text(
            encoding="utf-8"
        )

    def test_source_manifest_passes_structural_validation(self) -> None:
        self.validator.validate_manifest(self.source)

    def test_elevation_is_rejected(self) -> None:
        elevated = self.source.replace('level="asInvoker"', 'level="requireAdministrator"')
        with self.assertRaisesRegex(
            self.validator.ManifestValidationError, "asInvoker"
        ):
            self.validator.validate_manifest(elevated)

    def test_ui_access_is_rejected(self) -> None:
        ui_access = self.source.replace('uiAccess="false"', 'uiAccess="true"')
        with self.assertRaisesRegex(
            self.validator.ManifestValidationError, "uiAccess=false"
        ):
            self.validator.validate_manifest(ui_access)

    def test_missing_long_path_setting_is_rejected(self) -> None:
        missing = self.source.replace(
            '      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>\n',
            "",
        )
        with self.assertRaisesRegex(
            self.validator.ManifestValidationError, "longPathAware"
        ):
            self.validator.validate_manifest(missing)

    def test_supported_os_duplicates_are_rejected(self) -> None:
        marker = (
            '      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}" />'
        )
        duplicated = self.source.replace(marker, f"{marker}\n{marker}")
        with self.assertRaisesRegex(
            self.validator.ManifestValidationError, "duplicated"
        ):
            self.validator.validate_manifest(duplicated)


class ReleasePipelineHardeningTests(unittest.TestCase):
    @staticmethod
    def _workflow() -> str:
        return (
            ROOT / ".github" / "workflows" / "windows-release.yml"
        ).read_text(encoding="utf-8")

    def test_pyinstaller_build_keeps_manifest_and_disables_upx(self) -> None:
        spec = (ROOT / "packaging" / "Mowik.spec").read_text(encoding="utf-8")
        self.assertIn('manifest=str(ROOT / "packaging" / "Mowik.manifest")', spec)
        self.assertGreaterEqual(spec.count("upx=False"), 2)
        self.assertIn("console=False", spec)

    def test_pyinstaller_has_a_fail_closed_tcl_tk_payload_fallback(self) -> None:
        spec = (ROOT / "packaging" / "Mowik.spec").read_text(encoding="utf-8")
        tkinter_hook = (
            ROOT
            / "packaging"
            / "hooks"
            / "pre_find_module_path"
            / "hook-tkinter.py"
        ).read_text(encoding="utf-8")
        self.assertIn("if not tcltk_info.available:", spec)
        self.assertIn('Path(sys.base_prefix) / "tcl"', spec)
        self.assertIn('tcl_data_dir / "init.tcl"', spec)
        self.assertIn('tk_data_dir / "tk.tcl"', spec)
        self.assertIn("tcltk_info.TCL_ROOTNAME", spec)
        self.assertIn("tcltk_info.TK_ROOTNAME", spec)
        self.assertIn("(str(tcl_major_dir), tcl_major_dir.name)", spec)
        self.assertIn("raise FileNotFoundError", spec)
        self.assertIn(
            'hookspath=[str(ROOT / "packaging" / "hooks")]',
            spec,
        )
        self.assertIn("if tcltk_info.available:", tkinter_hook)
        self.assertIn('Path(sys.base_prefix) / "Lib"', tkinter_hook)
        self.assertIn('"tkinter" / "__init__.py"', tkinter_hook)
        self.assertIn("hook_api.search_dirs = [str(stdlib_dir)]", tkinter_hook)
        self.assertIn("raise FileNotFoundError", tkinter_hook)

    def test_builds_validate_tcl_tk_payload_before_frozen_gui_smoke(self) -> None:
        local_build = (ROOT / "BUDUJ_EXE.cmd").read_text(encoding="utf-8")
        self.assertIn("scripts\\build-release.ps1", local_build)
        self.assertIn("-PrepareApplicationOnly", local_build)
        self.assertIn("-PreparedAppManifestPath", local_build)

        release_build = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        invocation = "& $TkPayloadValidator -ApplicationDirectory $AppDirectory"
        self.assertEqual(release_build.count(invocation), 2)
        release_pyinstaller = release_build.index("'-m', 'PyInstaller'")
        release_payload_gate = release_build.index(invocation, release_pyinstaller)
        release_gui_smoke = release_build.index(
            "$GuiSmokeProcess = Start-Process", release_payload_gate
        )
        self.assertLess(release_pyinstaller, release_payload_gate)
        self.assertLess(release_payload_gate, release_gui_smoke)

        prepared_branch = release_build.index(
            'Write-Host "[1-3/7] Weryfikuję wcześniej zbudowaną aplikację..."'
        )
        prepared_payload_gate = release_build.index(invocation, prepared_branch)
        signing_branch = release_build.index(
            'Write-Host "[4/7] Podpisuję i weryfikuję Mowik.exe..."'
        )
        self.assertNotIn(
            "$GuiSmokeProcess = Start-Process",
            release_build[prepared_payload_gate:signing_branch],
        )

    def test_python_candidate_probe_checks_tk_without_requiring_a_display(self) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("import _tkinter,struct,sys,tkinter", installer)
        self.assertIn("_tkinter.TK_VERSION", installer)
        self.assertIn("tkinter.Tcl()", installer)
        self.assertNotIn("tkinter.Tk()", installer)
        self.assertNotIn("root.withdraw()", installer)

    def test_installer_discovers_only_trusted_python_and_winget_binaries(
        self,
    ) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Get-Command py.exe", installer)
        self.assertNotIn("Get-Command python.exe", installer)
        self.assertNotIn("Get-Command winget.exe", installer)
        self.assertIn("Get-AuthenticodeSignature", installer)
        self.assertIn("Python Software Foundation", installer)
        self.assertIn("Microsoft.DesktopAppInstaller", installer)
        self.assertIn("Microsoft Corporation", installer)
        self.assertIn("function Resolve-TrustedLauncherPython", installer)
        self.assertIn("pathlib.Path(sys.executable).resolve()", installer)
        self.assertIn('-ExpectedFileName "python.exe"', installer)
        self.assertGreaterEqual(installer.count("-I -S"), 3)

    def test_installer_rejects_elevation_before_logging_or_mutation(self) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        guard = installer.index("\n    Assert-NonElevatedInstaller\n")
        transcript = installer.index("Start-Transcript")
        removal = installer.index("if ($RemovePrivateEnvironmentOnly)")

        self.assertIn("WindowsBuiltInRole]::Administrator", installer)
        self.assertLess(guard, transcript)
        self.assertLess(guard, removal)

    def test_application_preflight_runs_before_project_and_native_imports(self) -> None:
        source = (ROOT / "mowik.py").read_text(encoding="utf-8")
        probe = source.index("_run_early_read_only_probe()")
        elevation = source.index("_reject_elevated_runtime_before_native_imports()")
        project_import = source.index("import mowik_commands as command_engine")
        native_import = source.index("import numpy as np")

        self.assertLess(probe, elevation)
        self.assertLess(elevation, project_import)
        self.assertLess(project_import, native_import)

    def test_install_ensures_cached_model_while_manual_refresh_stays_explicit(self) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        refresh = (ROOT / "POBIERZ_MODEL_PONOWNIE.cmd").read_text(encoding="utf-8")
        application = (ROOT / "mowik.py").read_text(encoding="utf-8")

        self.assertIn("--ensure-model --console-log", installer)
        self.assertNotIn("--download-model --console-log", installer)
        self.assertIn("--download-model --console-log", refresh)
        self.assertIn('"--ensure-model"', application)
        self.assertIn("force_download=False", application)
        self.assertIn("force_download=True", application)

    def test_installer_falls_back_to_runtime_cuda_probe_when_wmi_is_unavailable(
        self,
    ) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        wmi_probe = installer.index("Get-CimInstance Win32_VideoController")
        runtime_probe = installer.index("ctranslate2.get_cuda_device_count() > 0")
        gpu_install = installer.index("requirements-gpu-hashed.txt", runtime_probe)

        self.assertLess(wmi_probe, runtime_probe)
        self.assertLess(runtime_probe, gpu_install)

    def test_reused_environments_remove_only_the_orphaned_cudnn_distribution(
        self,
    ) -> None:
        sources = {
            "install.ps1": (ROOT / "install.ps1").read_text(encoding="utf-8"),
            "scripts/build-release.ps1": (
                ROOT / "scripts" / "build-release.ps1"
            ).read_text(encoding="utf-8"),
        }
        for name, source in sources.items():
            with self.subTest(source=name):
                self.assertIn("nvidia-cudnn-cu12", source)
                self.assertIn("uninstall", source)
                self.assertIn("--yes", source)
                self.assertNotIn("pip freeze", source.casefold())

        local_build = (ROOT / "BUDUJ_EXE.cmd").read_text(encoding="utf-8")
        self.assertIn("scripts\\build-release.ps1", local_build)
        self.assertNotIn("pip install", local_build)

    def test_frozen_build_preserves_redistributed_license_metadata(self) -> None:
        spec = (ROOT / "packaging" / "Mowik.spec").read_text(encoding="utf-8")
        self.assertIn("copy_metadata", spec)
        for distribution in (
            "pynput",
            "pywin32",
            "nvidia-cublas-cu12",
            "nvidia-cuda-nvrtc-cu12",
        ):
            with self.subTest(distribution=distribution):
                self.assertIn(f'"{distribution}"', spec)
        self.assertNotIn("nvidia-cudnn-cu12", spec)
        self.assertNotIn("nvidia.cudnn", spec)
        ctranslate2_license = (
            ROOT / "THIRD_PARTY_LICENSES" / "CTranslate2-LICENSE.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2019-     The OpenNMT Authors", ctranslate2_license)
        self.assertIn("Permission is hereby granted", ctranslate2_license)
        apache_license = (
            ROOT / "THIRD_PARTY_LICENSES" / "Apache-2.0.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("Apache License", apache_license)
        self.assertIn("Version 2.0, January 2004", apache_license)
        onnxruntime_license = (
            ROOT / "THIRD_PARTY_LICENSES" / "ONNXRuntime-LICENSE.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("Copyright (c) Microsoft Corporation", onnxruntime_license)
        self.assertIn("Permission is hereby granted", onnxruntime_license)
        notices = (ROOT / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        self.assertIn("THIRD-PARTY COMPONENT NOTICES", notices)
        self.assertIn("INFORMACJE O KOMPONENTACH ZEWNĘTRZNYCH", notices)

    def test_direct_release_dependencies_are_exactly_pinned(self) -> None:
        for requirement_file in ("requirements.txt", "requirements-gpu.txt"):
            lines = (
                ROOT / requirement_file
            ).read_text(encoding="utf-8").splitlines()
            requirements = [
                line.strip()
                for line in lines
                if line.strip() and not line.lstrip().startswith("#")
            ]
            self.assertTrue(requirements)
            for requirement in requirements:
                with self.subTest(file=requirement_file, requirement=requirement):
                    self.assertRegex(
                        requirement,
                        r"^[A-Za-z0-9_.-]+==[^=<>!~\s]+$",
                    )

    def test_frozen_build_uses_complete_exact_release_constraints(self) -> None:
        constraint_lines = (
            ROOT / "constraints-release.txt"
        ).read_text(encoding="utf-8").splitlines()
        constraints = [
            line.strip()
            for line in constraint_lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertGreater(len(constraints), 30)
        by_name: dict[str, str] = {}
        for constraint in constraints:
            self.assertRegex(
                constraint,
                r"^[A-Za-z0-9_.-]+==[^=<>!~\s]+$",
            )
            name, version = constraint.split("==", 1)
            normalized = name.casefold().replace("_", "-")
            self.assertNotIn(normalized, by_name)
            by_name[normalized] = version

        for requirement_file in ("requirements.txt", "requirements-gpu.txt"):
            for line in (ROOT / requirement_file).read_text(encoding="utf-8").splitlines():
                requirement = line.strip()
                if not requirement or requirement.startswith("#"):
                    continue
                name, version = requirement.split("==", 1)
                normalized = name.casefold().replace("_", "-")
                self.assertEqual(by_name.get(normalized), version)

        build_script = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("'--require-hashes', '--only-binary=:all:', '--no-deps'", build_script)
        self.assertIn("'constraints-release-hashed.txt'", build_script)
        self.assertIn("'mowik.py', '--runtime-gui-smoke-test'", build_script)
        self.assertIn("-ArgumentList '--runtime-gui-smoke-test'", build_script)
        self.assertIn("@('-m', 'pip', 'check')", build_script)
        self.assertIn("$UseCleanReleaseEnvironment = $true", build_script)
        self.assertIn("pathlib.Path(sys.argv[1])", build_script)
        self.assertNotIn("pathlib.Path(r'$ReleaseVenv')", build_script)
        local_build = (ROOT / "BUDUJ_EXE.cmd").read_text(encoding="utf-8")
        self.assertIn("scripts\\build-release.ps1", local_build)
        self.assertIn("-BuildMode UnsignedLocal", local_build)
        self.assertIn("-PrepareApplicationOnly", local_build)
        self.assertNotIn(".venv\\Scripts\\python.exe\" -m", local_build)
        self.assertIn("constraints-release-hashed.txt", self._workflow())

    def test_private_environment_removal_is_fail_closed(self) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        repair = (ROOT / "NAPRAW_INSTALACJE.cmd").read_text(encoding="utf-8")

        self.assertIn("function Remove-PrivateEnvironment", installer)
        self.assertIn("[IO.Path]::GetFullPath", installer)
        self.assertIn("[StringComparison]::OrdinalIgnoreCase", installer)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", installer)
        self.assertIn("Stack[System.IO.DirectoryInfo]", installer)
        self.assertIn("$Pending.Push($Child)", installer)
        self.assertIn("Remove-PrivateEnvironment -Path $Venv", installer)
        self.assertIn("-RemovePrivateEnvironmentOnly", repair)
        self.assertNotIn("rmdir /S /Q", repair)

    def test_source_installer_recreates_and_exactly_validates_runtime_venv(
        self,
    ) -> None:
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn("Odtwarzam prywatne srodowisko .venv od zera", installer)
        self.assertNotIn("$VenvOk =", installer)
        self.assertIn("scripts\\test-release-environment.py", installer)
        self.assertIn("$EnvironmentLocks", installer)
        self.assertIn("@EnvironmentLocks", installer)

    def test_build_removal_and_environment_reuse_are_fail_closed(self) -> None:
        build = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("$AllowedRemovalDirectories", build)
        self.assertIn("Stack[System.IO.DirectoryInfo]", build)
        self.assertIn("$Pending.Push($Child)", build)
        self.assertIn(
            "$ReuseVerifiedReleaseEnvironment -and (-not $IsReleaseBuild)",
            build,
        )

    def test_local_build_names_and_unsupported_elevation_are_documented(self) -> None:
        polish = (ROOT / "README.pl.md").read_text(encoding="utf-8")
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        local_installer = (ROOT / "BUDUJ_INSTALATOR.cmd").read_text(
            encoding="utf-8"
        )

        self.assertIn("Mowik-x.y.z-Setup-LOCAL-UNSIGNED.exe", polish)
        self.assertIn("Mowik-x.y.z-Setup-LOCAL-UNSIGNED.exe", english)
        self.assertIn("Uruchom jako administrator", polish)
        self.assertIn("jest niewspierane", polish)
        self.assertIn("Run as administrator", english)
        self.assertIn("is unsupported", english)
        self.assertIn("-BuildMode UnsignedLocal", local_installer)

    def test_install_and_release_locks_require_wheel_hashes(self) -> None:
        parsed_locks: dict[str, dict[str, str]] = {}
        for lock_name in (
            "constraints-release-hashed.txt",
            "requirements-bootstrap-hashed.txt",
            "requirements-runtime-hashed.txt",
            "requirements-gpu-hashed.txt",
        ):
            lock = (ROOT / lock_name).read_text(encoding="utf-8")
            requirements = [
                line for line in lock.splitlines()
                if line and not line.startswith(("#", " "))
            ]
            self.assertTrue(requirements, lock_name)
            self.assertEqual(lock.count("=="), len(requirements), lock_name)
            self.assertGreaterEqual(
                lock.count("--hash=sha256:"), len(requirements), lock_name
            )
            for hash_line in (
                line.strip() for line in lock.splitlines()
                if "--hash=sha256:" in line
            ):
                self.assertRegex(
                    hash_line, r"^--hash=sha256:[0-9a-f]{64}(?: \\)?$"
                )

            by_name: dict[str, str] = {}
            for requirement in requirements:
                pinned = requirement.removesuffix(" \\")
                self.assertRegex(pinned, r"^[A-Za-z0-9_.-]+==[^=<>!~\s]+$")
                name, version = pinned.split("==", 1)
                normalized = name.casefold().replace("_", "-")
                self.assertNotIn(normalized, by_name, lock_name)
                by_name[normalized] = version
            parsed_locks[lock_name] = by_name

        release_constraints = {}
        for line in (ROOT / "constraints-release.txt").read_text(
            encoding="utf-8"
        ).splitlines():
            if not line or line.startswith("#"):
                continue
            name, version = line.split("==", 1)
            release_constraints[name.casefold().replace("_", "-")] = version
        self.assertEqual(
            parsed_locks["constraints-release-hashed.txt"], release_constraints
        )

        for requirement_file, lock_name in (
            ("requirements.txt", "requirements-runtime-hashed.txt"),
            ("requirements-gpu.txt", "requirements-gpu-hashed.txt"),
        ):
            for line in (ROOT / requirement_file).read_text(
                encoding="utf-8"
            ).splitlines():
                if not line or line.startswith("#"):
                    continue
                name, version = line.split("==", 1)
                normalized = name.casefold().replace("_", "-")
                self.assertEqual(parsed_locks[lock_name].get(normalized), version)

        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("--require-hashes", installer)
        self.assertIn("--only-binary=:all:", installer)
        self.assertIn("requirements-bootstrap-hashed.txt", installer)
        self.assertIn("requirements-runtime-hashed.txt", installer)
        self.assertIn("requirements-gpu-hashed.txt", installer)
        self.assertNotIn("pip install --upgrade pip setuptools wheel", installer)

    def test_inno_has_fail_closed_signed_and_explicit_unsigned_modes(self) -> None:
        script = (ROOT / "packaging" / "Mowik.iss").read_text(encoding="utf-8")
        self.assertIn("#ifdef SignedRelease", script)
        self.assertIn("SignTool=MowikAuthenticode", script)
        self.assertIn("SignedUninstaller=yes", script)
        self.assertIn("SignedUninstaller=no", script)
        self.assertIn("Setup-UNSIGNED", script)
        self.assertNotIn("DisablePrecompiledFileVerifications", script)
        self.assertRegex(
            script,
            r'Name: "autostart";[^\n]*Flags: unchecked',
        )

    def test_release_path_never_weakens_antivirus_or_hides_shells(self) -> None:
        release_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / ".github" / "workflows" / "windows-release.yml",
                ROOT / "packaging" / "Mowik.iss",
                ROOT / "scripts" / "build-release.ps1",
                ROOT / "scripts" / "WindowsReleaseTools.psm1",
            )
        ).casefold()
        forbidden = (
            "add-mppreference",
            "set-mppreference",
            "exclusionpath",
            "exclusionprocess",
            "disablerealtimemonitoring",
            "encodedcommand",
            "windowstyle hidden",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, release_sources)

    def test_build_signs_app_before_inno_and_verifies_installer_afterward(self) -> None:
        script = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        sign_app = script.index("Invoke-AuthenticodeSign")
        compile_inno = script.index("Invoke-Checked $Iscc")
        verify_installer = script.index(
            "Assert-AuthenticodeSignature", compile_inno
        )
        self.assertLess(sign_app, compile_inno)
        self.assertLess(compile_inno, verify_installer)
        self.assertIn("FileMode]::CreateNew", script)
        self.assertIn("SignedRelease refuses to replace", script)
        self.assertIn("cannot be built with -SkipTests", script)
        self.assertIn("requires -UsePreparedApplication", script)
        self.assertIn("-PrepareApplicationOnly", script)
        self.assertIn("-UsePreparedApplication", script)
        self.assertIn("Write-DirectoryIntegrityManifest", script)
        self.assertIn("Assert-DirectoryIntegrityManifest", script)
        self.assertIn("Assert-DirectoryIntegrityManifestTransition", script)
        self.assertIn("-PreparedAppManifestPath", script)
        self.assertIn("must be unsigned", script)
        expected_hash = script.index("$ActualPreparedManifestHash -cne")
        manifest_gate = script.index("Assert-DirectoryIntegrityManifest")
        prepared_execution = script.index("Invoke-AuthenticodeSign")
        self.assertLess(expected_hash, manifest_gate)
        self.assertLess(manifest_gate, prepared_execution)
        self.assertIn("ExpectedPreparedAppManifestSha256", script)
        self.assertIn("-PrepareApplicationOnly cannot be combined with -SkipTests", script)
        self.assertIn(".release-venv", script)
        self.assertIn("test-release-environment.py", script)

    def test_release_version_checks_are_exact_not_substring_or_prefix_based(self) -> None:
        build = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
        preflight = (ROOT / "scripts" / "test-release-version.ps1").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('-notlike "$Version*"', build)
        self.assertGreaterEqual(build.count("$BuiltVersion -cne $Version"), 2)
        self.assertNotIn(".Contains($Expected)", preflight)
        self.assertIn("$Matches.Count -ne 1", preflight)

    def test_signing_uses_sha256_rfc3161_and_timestamp_verification(self) -> None:
        module = (ROOT / "scripts" / "WindowsReleaseTools.psm1").read_text(
            encoding="utf-8"
        )
        self.assertIn("'/fd', 'SHA256'", module)
        self.assertIn("'/tr', $ValidatedTimestampServer", module)
        self.assertIn("'/td', 'SHA256'", module)
        self.assertIn("'verify', '/pa', '/all', '/tw', '/v'", module)
        self.assertIn("TimeStamperCertificate", module)
        self.assertNotIn("Get-Command signtool.exe", module)
        self.assertIn("Windows Kits\\10\\bin", module)

    def test_workflow_builds_explicit_unsigned_release_and_never_clobbers(self) -> None:
        workflow = self._workflow()
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", workflow)
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD", workflow)
        self.assertNotIn("-BuildMode SignedRelease", workflow)
        self.assertNotIn("-RequireAuthenticode", workflow)
        self.assertIn("-BuildMode UnsignedRelease", workflow)
        self.assertIn("-SkipToolInstall", workflow)
        self.assertIn("Setup-UNSIGNED.exe", workflow)
        self.assertNotIn("Setup.exe", workflow)
        self.assertIn("SignatureStatus]::NotSigned", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn("Refusing to mutate or clobber published assets", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertRegex(workflow, r"uses: actions/checkout@[0-9a-f]{40}")
        self.assertRegex(workflow, r"uses: actions/setup-python@[0-9a-f]{40}")
        self.assertRegex(workflow, r"uses: actions/upload-artifact@[0-9a-f]{40}")
        self.assertRegex(workflow, r"uses: actions/download-artifact@[0-9a-f]{40}")
        self.assertRegex(workflow, r"uses: actions/attest@[0-9a-f]{40}")
        self.assertNotRegex(workflow, r"uses: actions/[^\s]+@v\d+")
        for pinned_action in (
            "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26",
        ):
            with self.subTest(action=pinned_action):
                self.assertIn(pinned_action, workflow)

        upload_step = workflow.split(
            "      - name: Upload verified unsigned build artifact", 1
        )[1].split("\n  publish-release:", 1)[0]
        self.assertIn(
            "release/Mowik-${{ env.RELEASE_VERSION }}-Setup-UNSIGNED.exe",
            upload_step,
        )
        self.assertIn("release/BUILD-INFO.txt", upload_step)
        self.assertIn("release/SHA256SUMS.txt", upload_step)

    def test_release_payload_has_signed_github_provenance(self) -> None:
        workflow = self._workflow()
        build_job = workflow.split("  publish-release:", 1)[0]
        self.assertIn("id-token: write", build_job)
        self.assertIn("attestations: write", build_job)
        self.assertIn("Attest verified unsigned release payload", build_job)
        self.assertIn(
            "release/Mowik-${{ env.RELEASE_VERSION }}-Setup-UNSIGNED.exe",
            build_job,
        )
        self.assertIn("release/BUILD-INFO.txt", build_job)
        self.assertIn("release/SHA256SUMS.txt", build_job)

    def test_publish_job_has_write_permission_and_rechecks_unsigned_hashes(self) -> None:
        workflow = self._workflow()
        build_job, publish_job = workflow.split("  publish-release:", 1)
        self.assertIn("contents: read", build_job)
        self.assertNotIn("contents: write", build_job)
        self.assertIn("contents: write", publish_job)
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", publish_job)
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD", publish_job)
        self.assertIn("actions/download-artifact@", publish_job)
        self.assertNotIn("MOWIK_EXPECTED_SIGNER_THUMBPRINT", publish_job)
        self.assertIn("MOWIK_EXPECTED_INSTALLER_SHA256", publish_job)
        self.assertIn("MOWIK_EXPECTED_CHECKSUM_SHA256", publish_job)
        self.assertIn("SignatureStatus]::NotSigned", publish_job)
        self.assertIn("differs from the verified build output", publish_job)

    def test_pinned_inno_is_verified_before_unsigned_release_build(self) -> None:
        workflow = self._workflow()
        self.assertIn('MOWIK_INNO_SETUP_VERSION: "6.7.1"', workflow)
        self.assertIn(
            'MOWIK_INNO_SETUP_SHA256: "4D11E8050B6185E0D49BD9E8CC661A7A59F44959A621D31D11033124C4E8A7B0"',
            workflow,
        )
        self.assertIn("github.com/jrsoftware/issrc/releases/download/is-6_7_1", workflow)
        download = workflow.index("Invoke-WebRequest")
        verify_hash = workflow.index("does not match the pinned release asset")
        verify_signature = workflow.index("not validly signed by Pyrsys B.V.")
        install = workflow.index("$innoInstall = Start-Process")
        self.assertLess(download, verify_hash)
        self.assertLess(verify_hash, verify_signature)
        self.assertLess(verify_signature, install)
        self.assertIn("Resolve-InnoCompiler", workflow)
        self.assertLess(
            workflow.index("Resolve-InnoCompiler"),
            workflow.index("Build and verify explicitly unsigned release installer"),
        )
        module = (ROOT / "scripts" / "WindowsReleaseTools.psm1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Assert-TrustedInnoCompiler", module)
        self.assertIn("O=Pyrsys B\\.V\\.", module)

    def test_workflow_authorizes_exact_remote_tag_before_release_build(self) -> None:
        workflow = self._workflow()
        trigger = workflow.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger)
        self.assertNotIn("inputs:", trigger)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("$env:GITHUB_REF_TYPE -ne 'tag'", workflow)
        self.assertIn("^v(?<version>\\d+\\.\\d+\\.\\d+)$", workflow)
        self.assertIn('git rev-parse "$tag^{commit}"', workflow)
        self.assertIn('git rev-parse "$env:GITHUB_SHA^{commit}"', workflow)
        self.assertGreaterEqual(
            workflow.count("refs/remotes/origin/main^{commit}"), 2
        )
        self.assertGreaterEqual(workflow.count("git merge-base --is-ancestor"), 2)
        self.assertGreaterEqual(workflow.count("git ls-remote origin"), 2)
        self.assertIn("MOWIK_RELEASE_TAG_COMMIT", workflow)
        self.assertNotIn("MOWIK_SIGNING_TAG_COMMIT", workflow)
        self.assertLess(
            workflow.index("Authorize immutable release tag"),
            workflow.index("Build and verify explicitly unsigned release installer"),
        )

        version_test = (
            ROOT / "scripts" / "test-release-version.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("the workflow default", version_test)

    def test_unsigned_release_mode_is_distinct_and_fail_closed(self) -> None:
        workflow = self._workflow()
        build_script = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("'UnsignedLocal', 'UnsignedRelease', 'SignedRelease'", build_script)
        self.assertIn("UnsignedRelease cannot be built with -SkipTests", build_script)
        self.assertIn("UnsignedRelease requires a preinstalled, verified", build_script)
        self.assertIn("UnsignedRelease requires -SkipToolInstall", build_script)
        self.assertIn("UnsignedRelease refuses to replace", build_script)
        self.assertGreaterEqual(build_script.count("SignatureStatus]::NotSigned"), 3)
        self.assertIn("UNSIGNED RELEASE BUILD", build_script)
        self.assertIn("UNSIGNED LOCAL DEVELOPER BUILD - do not publish", build_script)
        self.assertIn("Setup-LOCAL-UNSIGNED", build_script)
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE", workflow)

    def test_release_is_verified_as_draft_before_publication(self) -> None:
        workflow = self._workflow()
        create_draft = workflow.index("gh release create $tag")
        upload = workflow.index("gh release upload $tag")
        verify_assets = workflow.index("Assert-ExactGitHubReleaseAssets `")
        publish = workflow.index("gh release edit $tag --draft=false")
        verify_published = workflow.rindex("Assert-ExactGitHubReleaseAssets `")
        self.assertLess(create_draft, upload)
        self.assertLess(upload, verify_assets)
        self.assertLess(verify_assets, publish)
        self.assertLess(publish, verify_published)
        self.assertIn("--draft", workflow[create_draft:upload])
        self.assertIn("--verify-tag", workflow[create_draft:upload])
        self.assertIn("--generate-notes", workflow[create_draft:upload])
        self.assertIn("--notes $releaseWarning", workflow[create_draft:upload])
        self.assertIn("not digitally signed", workflow)
        self.assertIn("Unknown publisher", workflow)
        self.assertIn("SmartScreen", workflow)
        self.assertIn("SHA256SUMS.txt", workflow)
        self.assertIn("--json tagName,isDraft,assets", workflow)
        release_tools = (
            ROOT / "scripts" / "WindowsReleaseTools.psm1"
        ).read_text(encoding="utf-8")
        self.assertIn("unexpected asset set", release_tools)
        self.assertIn("$Asset[0].digest -cne $ExpectedDigest", release_tools)
        self.assertIn("$Asset[0].state -cne 'uploaded'", release_tools)
        self.assertIn("unexpected size, digest", release_tools)
        self.assertIn("moved while draft assets were being uploaded", workflow)
        self.assertNotIn("gh release delete", workflow)

    def test_release_payload_gate_checks_exact_names_and_canonical_hash(self) -> None:
        script = (ROOT / "scripts" / "test-release-artifacts.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("unexpected payload", script)
        self.assertIn("BUILD-INFO.txt", script)
        self.assertIn("Get-ReleaseSourceIdentity", script)
        self.assertIn("does not match the current release source", script)
        self.assertIn("SHA256SUMS.txt is non-canonical", script)
        self.assertIn("-UNSIGNED.exe", script)
        self.assertIn("Assert-AuthenticodeSignature", script)

    def test_release_build_binds_artifacts_to_the_current_source_identity(self) -> None:
        build = (ROOT / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
        module = (ROOT / "scripts" / "WindowsReleaseTools.psm1").read_text(
            encoding="utf-8"
        )
        workflow = self._workflow()

        self.assertIn("Get-ReleaseSourceIdentity -ProjectRoot $Root", build)
        self.assertIn("MOWIK-RELEASE-BUILD-INFO-V2", build)
        self.assertIn('"build-mode`t$BuildMode"', build)
        self.assertIn("BUILD-INFO.txt", build)
        self.assertIn("function Get-ReleaseSourceIdentity", module)
        self.assertIn("'assets'", module)
        self.assertGreaterEqual(workflow.count("release/BUILD-INFO.txt"), 3)

    def test_checksum_writer_uses_supported_ascii_constructor(self) -> None:
        script = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("[Text.ASCIIEncoding]::new()", script)
        self.assertNotIn("[Text.ASCIIEncoding]::new($false)", script)

    def test_artifact_gate_uses_named_powershell_parameters(self) -> None:
        script = (ROOT / "scripts" / "build-release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$ArtifactTestArguments = @{", script)
        self.assertIn("& $ArtifactTestScript @ArtifactTestArguments", script)
        self.assertNotIn(
            "Invoke-Checked (Join-Path $PSScriptRoot 'test-release-artifacts.ps1')",
            script,
        )

    def test_unsigned_installer_qa_requires_explicit_name_and_not_signed_state(self) -> None:
        script = (ROOT / "scripts" / "test-installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("[string]$InstallerFileName", script)
        self.assertIn("Setup(?:-UNSIGNED)?\\.exe", script)
        self.assertIn("requires the explicit -UNSIGNED.exe file name", script)
        self.assertGreaterEqual(script.count("SignatureStatus]::NotSigned"), 2)
        self.assertIn("$RepairedAppHash -cne $ExpectedAppHash", script)
        # Runnery Windows są podniesione, a Mówik odmawia wtedy startu modalnym
        # okienkiem, którego nikt tam nie zamknie. Smoke GUI musi omijać tę
        # ścieżkę, inaczej zadanie wisi do limitu czasu.
        self.assertIn("-ArgumentList '--runtime-gui-smoke-test'", script)
        self.assertNotIn("-ArgumentList '--settings'", script)
        self.assertIn("WaitForExit($SettingsStartupTimeoutSeconds * 1000)", script)
        self.assertIn('"/DIR=`"$TestDir`""', script)
        self.assertIn('"/LOG=`"$InstallLog`""', script)
        self.assertIn('"/LOG=`"$RepairLog`""', script)

    def test_diagnostics_propagates_command_failures(self) -> None:
        script = (ROOT / "DIAGNOSTYKA.cmd").read_text(encoding="utf-8")
        self.assertIn('set "RC=0"', script)
        self.assertGreaterEqual(script.count('if errorlevel 1 set "RC=1"'), 5)
        self.assertIn("exit /b %RC%", script)
        self.assertNotIn("exit /b 0", script)

    def test_signtool_must_be_authenticode_signed_by_microsoft(self) -> None:
        module = (ROOT / "scripts" / "WindowsReleaseTools.psm1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Get-AuthenticodeSignature -LiteralPath $Tool.FullName", module)
        self.assertIn("O=Microsoft Corporation", module)

    def test_settings_exposes_and_resets_maximum_recording_time(self) -> None:
        source = (ROOT / "mowik.py").read_text(encoding="utf-8")
        self.assertIn("maximum_recording_var = tk.StringVar(", source)
        self.assertIn('updated["maximum_recording_seconds"] = parse_int(', source)
        self.assertIn(
            'maximum_recording_var.set(\n'
            '            str(DEFAULT_CONFIG["maximum_recording_seconds"])',
            source,
        )

    def test_settings_does_not_offer_removed_legacy_models(self) -> None:
        source = (ROOT / "mowik.py").read_text(encoding="utf-8")
        self.assertNotIn('t("base — bardzo lekki"', source)
        self.assertNotIn('t("medium — dokładniejszy"', source)
        self.assertIn('LEGACY_REMOVED_MODELS = frozenset({"base", "medium"})', source)

    def test_settings_rejects_impossible_recording_duration_range(self) -> None:
        source = (ROOT / "mowik.py").read_text(encoding="utf-8")
        self.assertIn(
            'updated["minimum_recording_ms"]\n'
            '            > updated["maximum_recording_seconds"] * 1_000',
            source,
        )

    def test_windows_ci_covers_supported_source_python_versions(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "windows-ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('- "3.11"', workflow)
        self.assertIn('- "3.12"', workflow)
        self.assertNotIn('"3.10"', workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("python -m unittest discover", workflow)
        self.assertIn("mowik_audio_devices.py", workflow)
        self.assertIn("Get-ChildItem -LiteralPath . -Filter '*.ps1'", workflow)


class ReleasePowerShellBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("pwsh") or shutil.which("powershell")
        if cls.shell is None:
            raise unittest.SkipTest("PowerShell is required for Windows release behavior tests")

    def run_powershell(
        self,
        command: str,
        *,
        environment: dict[str, str],
        expect_success: bool,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(environment)
        result = subprocess.run(
            [
                self.shell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        if expect_success and result.returncode != 0:
            self.fail(f"PowerShell failed unexpectedly:\n{result.stdout}")
        if not expect_success and result.returncode == 0:
            self.fail(f"PowerShell unexpectedly succeeded:\n{result.stdout}")
        return result

    def test_exact_github_release_asset_gate_executes_for_multiple_files(self) -> None:
        module = ROOT / "scripts" / "WindowsReleaseTools.psm1"
        command = (
            "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
            "$paths = @($env:MOWIK_TEST_INSTALLER, $env:MOWIK_TEST_CHECKSUM); "
            "$assets = @(foreach ($path in $paths) { "
            "$file = Get-Item -LiteralPath $path; "
            "[pscustomobject]@{ "
            "name = $file.Name; size = $file.Length; state = 'uploaded'; "
            "digest = 'sha256:' + "
            "(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() "
            "} }); "
            "$release = [pscustomobject]@{ assets = $assets }; "
            "Assert-ExactGitHubReleaseAssets -Release $release "
            "-ReleaseTag 'v-test' -ExpectedPath $paths"
        )
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            installer = temp_path / "Mowik-test-Setup-UNSIGNED.exe"
            checksum = temp_path / "SHA256SUMS.txt"
            installer.write_bytes(b"installer")
            checksum.write_bytes(b"checksum")
            environment = {
                "MOWIK_TEST_MODULE": str(module),
                "MOWIK_TEST_INSTALLER": str(installer),
                "MOWIK_TEST_CHECKSUM": str(checksum),
            }
            self.run_powershell(
                command,
                environment=environment,
                expect_success=True,
            )
            bad_digest = command.replace(
                "$release = [pscustomobject]@{ assets = $assets }; ",
                "$assets[0].digest = 'sha256:bad'; "
                "$release = [pscustomobject]@{ assets = $assets }; ",
            )
            result = self.run_powershell(
                bad_digest,
                environment=environment,
                expect_success=False,
            )
            self.assertIn("unexpected size, digest", result.stdout)

    def test_release_artifact_gate_rejects_source_changes_after_build(self) -> None:
        identity_command = (
            "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
            "Get-ReleaseSourceIdentity -ProjectRoot $env:MOWIK_TEST_ROOT"
        )
        gate_command = (
            "& $env:MOWIK_TEST_ARTIFACT_SCRIPT "
            "-Version '2.7.4' "
            "-ExpectedBuildMode 'UnsignedRelease' "
            "-InstallerFileName 'Mowik-2.7.4-Setup-UNSIGNED.exe'"
        )

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            for relative_directory in (
                ".github/workflows",
                "assets",
                "packaging",
                "release",
                "scripts",
                "tests",
                "THIRD_PARTY_LICENSES",
            ):
                (project / relative_directory).mkdir(parents=True)

            module = project / "scripts" / "WindowsReleaseTools.psm1"
            artifact_script = project / "scripts" / "test-release-artifacts.ps1"
            shutil.copy2(ROOT / "scripts" / module.name, module)
            shutil.copy2(ROOT / "scripts" / artifact_script.name, artifact_script)
            source = project / "mowik.py"
            source.write_text("BUILD_MARKER = 'initial'\n", encoding="utf-8")

            installer_name = "Mowik-2.7.4-Setup-UNSIGNED.exe"
            installer = project / "release" / installer_name
            installer.write_bytes(b"test installer payload")
            environment = {
                "MOWIK_TEST_MODULE": str(module),
                "MOWIK_TEST_ROOT": str(project),
                "MOWIK_TEST_ARTIFACT_SCRIPT": str(artifact_script),
            }
            identity_result = self.run_powershell(
                identity_command,
                environment=environment,
                expect_success=True,
            )
            source_identity = identity_result.stdout.strip().splitlines()[-1]
            installer_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
            build_info = (
                "MOWIK-RELEASE-BUILD-INFO-V2\n"
                "version\t2.7.4\n"
                "build-mode\tUnsignedRelease\n"
                f"installer\t{installer_name}\n"
                f"installer-sha256\t{installer_hash}\n"
                f"source-sha256\t{source_identity}\n"
            )
            build_info_path = project / "release" / "BUILD-INFO.txt"
            build_info_path.write_bytes(build_info.encode("ascii"))
            build_info_hash = hashlib.sha256(build_info_path.read_bytes()).hexdigest()
            checksum = (
                f"{installer_hash}  {installer_name}\n"
                f"{build_info_hash}  BUILD-INFO.txt\n"
            )
            (project / "release" / "SHA256SUMS.txt").write_bytes(
                checksum.encode("ascii")
            )

            self.run_powershell(
                gate_command,
                environment=environment,
                expect_success=True,
            )
            source.write_text("BUILD_MARKER = 'changed'\n", encoding="utf-8")
            result = self.run_powershell(
                gate_command,
                environment=environment,
                expect_success=False,
            )
            self.assertIn(
                "does not match the current release source",
                result.stdout,
            )

    def test_tcl_tk_payload_gate_rejects_incomplete_frozen_layouts(self) -> None:
        script = ROOT / "scripts" / "test-tk-payload.ps1"
        command = (
            "& $env:MOWIK_TEST_TK_PAYLOAD_SCRIPT "
            "-ApplicationDirectory $env:MOWIK_TEST_APP_DIRECTORY"
        )
        for scenario in ("valid", "missing-init", "empty-tk", "empty-modules"):
            with (
                self.subTest(scenario=scenario),
                tempfile.TemporaryDirectory() as temp,
            ):
                application = Path(temp) / "Mowik"
                tcl_data = application / "_internal" / "_tcl_data"
                tk_data = application / "_internal" / "_tk_data"
                tcl_modules = application / "_internal" / "tcl8" / "8.6"
                tcl_data.mkdir(parents=True)
                tk_data.mkdir(parents=True)
                tcl_modules.mkdir(parents=True)
                (tcl_data / "init.tcl").write_text("package require Tcl", encoding="utf-8")
                (tk_data / "tk.tcl").write_text("package require Tk", encoding="utf-8")
                (tcl_modules / "http.tm").write_text("package provide http", encoding="utf-8")

                if scenario == "missing-init":
                    (tcl_data / "init.tcl").unlink()
                elif scenario == "empty-tk":
                    (tk_data / "tk.tcl").write_bytes(b"")
                elif scenario == "empty-modules":
                    (tcl_modules / "http.tm").unlink()

                result = self.run_powershell(
                    command,
                    environment={
                        "MOWIK_TEST_TK_PAYLOAD_SCRIPT": str(script),
                        "MOWIK_TEST_APP_DIRECTORY": str(application),
                    },
                    expect_success=scenario == "valid",
                )
                if scenario == "missing-init":
                    self.assertIn("init.tcl", result.stdout)
                elif scenario == "empty-tk":
                    self.assertIn("empty", result.stdout)
                elif scenario == "empty-modules":
                    self.assertIn("contains no non-empty files", result.stdout)

    def test_directory_manifest_rejects_mutation_addition_and_removal(self) -> None:
        module = ROOT / "scripts" / "WindowsReleaseTools.psm1"
        write_command = (
            "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
            "Write-DirectoryIntegrityManifest "
            "-Directory $env:MOWIK_TEST_DIRECTORY "
            "-ManifestPath $env:MOWIK_TEST_MANIFEST"
        )
        assert_command = (
            "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
            "Assert-DirectoryIntegrityManifest "
            "-Directory $env:MOWIK_TEST_DIRECTORY "
            "-ManifestPath $env:MOWIK_TEST_MANIFEST"
        )

        for scenario in ("mutation", "addition", "removal"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                temp_path = Path(temp)
                app = temp_path / "app"
                nested = app / "_internal"
                nested.mkdir(parents=True)
                executable = app / "Mowik.exe"
                library = nested / "runtime.dll"
                executable.write_bytes(b"prepared executable")
                library.write_bytes(b"trusted runtime")
                manifest = temp_path / "prepared.manifest"
                environment = {
                    "MOWIK_TEST_MODULE": str(module),
                    "MOWIK_TEST_DIRECTORY": str(app),
                    "MOWIK_TEST_MANIFEST": str(manifest),
                }
                self.run_powershell(
                    write_command, environment=environment, expect_success=True
                )
                self.run_powershell(
                    assert_command, environment=environment, expect_success=True
                )

                if scenario == "mutation":
                    library.write_bytes(b"tampered runtime")
                elif scenario == "addition":
                    (nested / "injected.dll").write_bytes(b"unexpected")
                else:
                    library.unlink()
                result = self.run_powershell(
                    assert_command, environment=environment, expect_success=False
                )
                self.assertIn("application directory changed", result.stdout.casefold())

    def test_directory_manifest_is_canonical_and_records_size_and_sha256(self) -> None:
        module = ROOT / "scripts" / "WindowsReleaseTools.psm1"
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            app = temp_path / "app"
            app.mkdir()
            payload = b"abc"
            (app / "Mowik.exe").write_bytes(payload)
            manifest = temp_path / "prepared.manifest"
            self.run_powershell(
                "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
                "Write-DirectoryIntegrityManifest "
                "-Directory $env:MOWIK_TEST_DIRECTORY "
                "-ManifestPath $env:MOWIK_TEST_MANIFEST",
                environment={
                    "MOWIK_TEST_MODULE": str(module),
                    "MOWIK_TEST_DIRECTORY": str(app),
                    "MOWIK_TEST_MANIFEST": str(manifest),
                },
                expect_success=True,
            )
            self.assertEqual(
                manifest.read_text(encoding="utf-8"),
                "MOWIK-DIRECTORY-MANIFEST-V1\n"
                f"Mowik.exe\t3\t{hashlib.sha256(payload).hexdigest()}\n",
            )

    def test_directory_manifest_transition_allows_only_signed_executable(self) -> None:
        module = ROOT / "scripts" / "WindowsReleaseTools.psm1"
        write_before = (
            "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
            "Write-DirectoryIntegrityManifest "
            "-Directory $env:MOWIK_TEST_DIRECTORY "
            "-ManifestPath $env:MOWIK_TEST_BEFORE"
        )
        write_after_and_compare = (
            "Import-Module $env:MOWIK_TEST_MODULE -Force -DisableNameChecking; "
            "Write-DirectoryIntegrityManifest "
            "-Directory $env:MOWIK_TEST_DIRECTORY "
            "-ManifestPath $env:MOWIK_TEST_AFTER; "
            "Assert-DirectoryIntegrityManifestTransition "
            "-BeforeManifestPath $env:MOWIK_TEST_BEFORE "
            "-AfterManifestPath $env:MOWIK_TEST_AFTER "
            "-AllowedChangedPath 'Mowik.exe'"
        )

        for tamper_runtime in (False, True):
            with (
                self.subTest(tamper_runtime=tamper_runtime),
                tempfile.TemporaryDirectory() as temp,
            ):
                temp_path = Path(temp)
                app = temp_path / "app"
                runtime = app / "_internal" / "runtime.dll"
                runtime.parent.mkdir(parents=True)
                executable = app / "Mowik.exe"
                executable.write_bytes(b"unsigned")
                runtime.write_bytes(b"trusted runtime")
                environment = {
                    "MOWIK_TEST_MODULE": str(module),
                    "MOWIK_TEST_DIRECTORY": str(app),
                    "MOWIK_TEST_BEFORE": str(temp_path / "before.manifest"),
                    "MOWIK_TEST_AFTER": str(temp_path / "after.manifest"),
                }
                self.run_powershell(
                    write_before, environment=environment, expect_success=True
                )
                executable.write_bytes(b"signed executable")
                if tamper_runtime:
                    runtime.write_bytes(b"tampered runtime")
                result = self.run_powershell(
                    write_after_and_compare,
                    environment=environment,
                    expect_success=not tamper_runtime,
                )
                if tamper_runtime:
                    self.assertIn(
                        "changed an unexpected application file",
                        result.stdout.casefold(),
                    )

    def test_version_preflight_rejects_stale_comment_and_duplicate_assignment(self) -> None:
        relative_files = (
            "mowik.py",
            "packaging/version_info.txt",
            "packaging/Mowik.iss",
            "scripts/build-release.ps1",
            "scripts/test-installer.ps1",
            "scripts/test-release-artifacts.ps1",
            "scripts/test-release-version.ps1",
            "BUDUJ_INSTALATOR.cmd",
            "WERSJA.txt",
            "install.ps1",
            "README.md",
            "README.pl.md",
        )
        # Wersję czytamy z mowik.py, żeby ten test nie wymagał ręcznej edycji
        # przy każdym wydaniu i nie zaczął cicho sprawdzać starego numeru.
        version = current_app_version()
        command = f"& $env:MOWIK_TEST_VERSION_SCRIPT -Version {version}"
        for scenario in ("stale-comment", "duplicate", "stale-readme"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                project = Path(temp)
                for relative in relative_files:
                    source = ROOT / relative
                    destination = project / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                script = project / "scripts" / "test-release-version.ps1"
                environment = {"MOWIK_TEST_VERSION_SCRIPT": str(script)}
                self.run_powershell(
                    command, environment=environment, expect_success=True
                )

                source_file = (
                    project / "README.md"
                    if scenario == "stale-readme"
                    else project / "mowik.py"
                )
                content = source_file.read_text(encoding="utf-8")
                if scenario == "stale-comment":
                    content = content.replace(
                        f'APP_VERSION = "{version}"',
                        f'APP_VERSION = "9.9.9"\n# APP_VERSION = "{version}"',
                        1,
                    )
                elif scenario == "duplicate":
                    content += f'\nAPP_VERSION = "{version}"\n'
                else:
                    content = content.replace(version, "9.9.9", 1)
                source_file.write_text(content, encoding="utf-8")
                self.run_powershell(
                    command, environment=environment, expect_success=False
                )


if __name__ == "__main__":
    unittest.main()
