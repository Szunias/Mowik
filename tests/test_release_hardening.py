from __future__ import annotations

import importlib.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


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

    def test_frozen_build_preserves_redistributed_license_metadata(self) -> None:
        spec = (ROOT / "packaging" / "Mowik.spec").read_text(encoding="utf-8")
        self.assertIn("copy_metadata", spec)
        for distribution in (
            "pynput",
            "pywin32",
            "nvidia-cublas-cu12",
            "nvidia-cuda-nvrtc-cu12",
            "nvidia-cudnn-cu12",
        ):
            with self.subTest(distribution=distribution):
                self.assertIn(f'"{distribution}"', spec)
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
        self.assertNotRegex(workflow, r"uses: actions/[^\s]+@v\d+")
        for pinned_action in (
            "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        ):
            with self.subTest(action=pinned_action):
                self.assertIn(pinned_action, workflow)

        upload_step = workflow.split(
            "      - name: Upload verified unsigned build artifact", 1
        )[1].split("\n  publish-release:", 1)[0]
        self.assertEqual(upload_step.count("release/"), 2)
        self.assertIn(
            "release/Mowik-${{ env.RELEASE_VERSION }}-Setup-UNSIGNED.exe",
            upload_step,
        )
        self.assertIn("release/SHA256SUMS.txt", upload_step)

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
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE", workflow)

    def test_release_is_verified_as_draft_before_publication(self) -> None:
        workflow = self._workflow()
        create_draft = workflow.index("gh release create $tag")
        upload = workflow.index("gh release upload $tag")
        verify_assets = workflow.index("Assert-ExactReleaseAssets -Release $draft")
        publish = workflow.index("gh release edit $tag --draft=false")
        verify_published = workflow.index(
            "Assert-ExactReleaseAssets -Release $published"
        )
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
        self.assertIn("unexpected asset set", workflow)
        self.assertIn("$asset[0].digest -cne $expectedDigest", workflow)
        self.assertIn("$asset[0].state -cne 'uploaded'", workflow)
        self.assertIn("unexpected size, digest", workflow)
        self.assertIn("moved while draft assets were being uploaded", workflow)
        self.assertNotIn("gh release delete", workflow)

    def test_release_payload_gate_checks_exact_names_and_canonical_hash(self) -> None:
        script = (ROOT / "scripts" / "test-release-artifacts.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("unexpected payload", script)
        self.assertIn("SHA256SUMS.txt is non-canonical", script)
        self.assertIn("-UNSIGNED.exe", script)
        self.assertIn("Assert-AuthenticodeSignature", script)

    def test_unsigned_installer_qa_requires_explicit_name_and_not_signed_state(self) -> None:
        script = (ROOT / "scripts" / "test-installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("[string]$InstallerFileName", script)
        self.assertIn("Setup(?:-UNSIGNED)?\\.exe", script)
        self.assertIn("requires the explicit -UNSIGNED.exe file name", script)
        self.assertGreaterEqual(script.count("SignatureStatus]::NotSigned"), 2)

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
        )
        command = "& $env:MOWIK_TEST_VERSION_SCRIPT -Version 2.7.1"
        for scenario in ("stale-comment", "duplicate"):
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

                source_file = project / "mowik.py"
                content = source_file.read_text(encoding="utf-8")
                if scenario == "stale-comment":
                    content = content.replace(
                        'APP_VERSION = "2.7.1"',
                        'APP_VERSION = "9.9.9"\n# APP_VERSION = "2.7.1"',
                        1,
                    )
                else:
                    content += '\nAPP_VERSION = "2.7.1"\n'
                source_file.write_text(content, encoding="utf-8")
                self.run_powershell(
                    command, environment=environment, expect_success=False
                )


if __name__ == "__main__":
    unittest.main()
