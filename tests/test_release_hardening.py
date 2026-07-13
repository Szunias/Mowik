from __future__ import annotations

import importlib.util
from pathlib import Path
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
        self.assertIn("-PrepareApplicationOnly", script)
        self.assertIn("-UsePreparedApplication", script)
        self.assertIn("Prepared Mowik.exe changed", script)
        self.assertIn("must be unsigned", script)

    def test_signing_uses_sha256_rfc3161_and_timestamp_verification(self) -> None:
        module = (ROOT / "scripts" / "WindowsReleaseTools.psm1").read_text(
            encoding="utf-8"
        )
        self.assertIn("'/fd', 'SHA256'", module)
        self.assertIn("'/tr', $ValidatedTimestampServer", module)
        self.assertIn("'/td', 'SHA256'", module)
        self.assertIn("'verify', '/pa', '/all', '/tw', '/v'", module)
        self.assertIn("TimeStamperCertificate", module)

    def test_workflow_requires_certificate_and_never_clobbers_release(self) -> None:
        workflow = self._workflow()
        self.assertIn("WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", workflow)
        self.assertIn("WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD", workflow)
        self.assertIn("-BuildMode SignedRelease", workflow)
        self.assertIn("-RequireAuthenticode", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn("Refusing to mutate or clobber published assets", workflow)
        self.assertIn("if: always()", workflow)

    def test_workflow_authorizes_exact_remote_tag_before_certificate(self) -> None:
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
        self.assertIn("MOWIK_SIGNING_TAG_COMMIT", workflow)
        self.assertLess(
            workflow.index("Authorize immutable signing tag"),
            workflow.index("Import code-signing certificate"),
        )

        version_test = (
            ROOT / "scripts" / "test-release-version.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("the workflow default", version_test)

    def test_private_key_is_removed_immediately_after_signing(self) -> None:
        workflow = self._workflow()
        prepare = workflow.index(
            "Build and test unsigned application before exposing signing key"
        )
        import_key = workflow.index("Import code-signing certificate")
        build = workflow.index("Build, sign and verify installer")
        remove_key = workflow.index(
            "Remove signing key before installer QA and upload"
        )
        installer_qa = workflow.index(
            "Test English and Polish installation and uninstall"
        )
        self.assertLess(prepare, import_key)
        self.assertLess(import_key, build)
        self.assertLess(build, remove_key)
        self.assertLess(remove_key, installer_qa)
        self.assertIn("MOWIK_SIGNING_CERTIFICATE_CLEANUP_PATH", workflow)
        self.assertIn("MOWIK_PREPARED_APP_SHA256", workflow)
        self.assertIn("-UsePreparedApplication", workflow)
        self.assertIn("-PreparedAppSha256", workflow)

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


if __name__ == "__main__":
    unittest.main()
