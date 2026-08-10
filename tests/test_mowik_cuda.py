from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mowik_cuda as cuda


def wheel_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def package_for(archive: bytes, prefix: str = "nvidia/cublas/bin/") -> cuda.CudaPackage:
    return cuda.CudaPackage(
        project="nvidia-cublas-cu12",
        version="12.9.2.10",
        filename="nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
        sha256=hashlib.sha256(archive).hexdigest(),
        member_prefix=prefix,
    )


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(payload)
        self.headers = headers or {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class PinnedPackageTests(unittest.TestCase):
    def test_pinned_versions_match_the_release_constraints(self) -> None:
        constraints = (
            Path(__file__).resolve().parents[1] / "constraints-release-hashed.txt"
        ).read_text(encoding="utf-8")

        for package in cuda.CUDA_PACKAGES:
            with self.subTest(project=package.project):
                self.assertIn(f"{package.project}=={package.version}", constraints)
                self.assertIn(f"--hash=sha256:{package.sha256}", constraints)

    def test_required_libraries_are_the_ones_the_app_preloads(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "mowik.py"
        ).read_text(encoding="utf-8")

        for name in cuda.REQUIRED_LIBRARIES:
            with self.subTest(library=name):
                self.assertIn(Path(name).name, source)


class DownloadUrlTests(unittest.TestCase):
    def metadata(self, **overrides) -> bytes:
        package = cuda.CUDA_PACKAGES[0]
        entry = {
            "filename": package.filename,
            "url": f"https://files.pythonhosted.org/packages/aa/bb/{package.filename}",
            "digests": {"sha256": package.sha256},
        }
        entry.update(overrides)
        return json.dumps({"urls": [entry]}).encode("utf-8")

    def resolve(self, payload: bytes) -> str:
        with mock.patch.object(cuda, "_open_url", return_value=FakeResponse(payload)):
            return cuda.resolve_download_url(cuda.CUDA_PACKAGES[0], mock.Mock())

    def test_returns_the_published_url_for_the_pinned_file(self) -> None:
        url = self.resolve(self.metadata())

        self.assertTrue(url.startswith("https://files.pythonhosted.org/"))

    def test_rejects_a_digest_that_differs_from_the_pinned_one(self) -> None:
        payload = self.metadata(digests={"sha256": "0" * 64})

        with self.assertRaises(cuda.CudaRuntimeError) as raised:
            self.resolve(payload)

        self.assertIn("suma kontrolna", str(raised.exception).lower())

    def test_rejects_a_download_host_the_index_did_not_promise(self) -> None:
        payload = self.metadata(url="https://example.invalid/evil.whl")

        with self.assertRaises(cuda.CudaRuntimeError):
            self.resolve(payload)

    def test_rejects_plain_http(self) -> None:
        payload = self.metadata(url="http://files.pythonhosted.org/a.whl")

        with self.assertRaises(cuda.CudaRuntimeError):
            self.resolve(payload)


class DownloadArchiveTests(unittest.TestCase):
    def test_verified_download_reports_progress(self) -> None:
        archive = wheel_bytes({"nvidia/cublas/bin/cublas64_12.dll": b"x" * 4096})
        package = package_for(archive)
        seen: list[tuple[int, int | None]] = []

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / package.filename
            with mock.patch.object(
                cuda,
                "_open_url",
                return_value=FakeResponse(
                    archive, {"Content-Length": str(len(archive))}
                ),
            ):
                cuda.download_archive(
                    "https://files.pythonhosted.org/x.whl",
                    package,
                    destination,
                    mock.Mock(),
                    progress=lambda done, total: seen.append((done, total)),
                )

            self.assertEqual(destination.read_bytes(), archive)

        self.assertEqual(seen[0], (0, len(archive)))
        self.assertEqual(seen[-1][0], len(archive))

    def test_mismatched_checksum_is_rejected(self) -> None:
        archive = wheel_bytes({"nvidia/cublas/bin/cublas64_12.dll": b"x"})
        package = package_for(archive)
        tampered = archive + b"tampered"

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cuda, "_open_url", return_value=FakeResponse(tampered)
        ):
            with self.assertRaises(cuda.CudaRuntimeError) as raised:
                cuda.download_archive(
                    "https://files.pythonhosted.org/x.whl",
                    package,
                    Path(directory) / package.filename,
                    mock.Mock(),
                )

        self.assertIn("sumę kontrolną", str(raised.exception))

    def test_oversized_response_is_abandoned(self) -> None:
        archive = b"x" * 32
        package = package_for(archive)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cuda, "MAX_ARCHIVE_BYTES", 8
        ), mock.patch.object(cuda, "_open_url", return_value=FakeResponse(archive)):
            with self.assertRaises(cuda.CudaRuntimeError):
                cuda.download_archive(
                    "https://files.pythonhosted.org/x.whl",
                    package,
                    Path(directory) / package.filename,
                    mock.Mock(),
                )


class ExtractionTests(unittest.TestCase):
    def extract(self, members: dict[str, bytes], prefix: str = "nvidia/cublas/bin/"):
        archive = wheel_bytes(members)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "cuda"
        root.mkdir()
        archive_path = Path(directory.name) / "wheel.whl"
        archive_path.write_bytes(archive)
        return root, archive_path, prefix

    def test_only_libraries_from_the_expected_folder_are_written(self) -> None:
        root, archive_path, prefix = self.extract(
            {
                "nvidia/cublas/bin/cublas64_12.dll": b"real",
                "nvidia/cublas/bin/notes.txt": b"skip",
                "nvidia/other/bin/other.dll": b"skip",
            }
        )

        extracted = cuda.extract_libraries(archive_path, root, prefix)

        self.assertEqual(extracted, 1)
        self.assertTrue((root / "cublas" / "bin" / "cublas64_12.dll").is_file())
        self.assertFalse((root / "cublas" / "bin" / "notes.txt").exists())
        self.assertFalse((root / "other").exists())

    def test_path_traversal_entries_never_escape_the_target(self) -> None:
        root, archive_path, prefix = self.extract(
            {
                "nvidia/cublas/bin/../../../escaped.dll": b"evil",
                "nvidia/cublas/bin/cublas64_12.dll": b"real",
            }
        )

        cuda.extract_libraries(archive_path, root, prefix)

        self.assertFalse((root.parent.parent / "escaped.dll").exists())
        self.assertTrue((root / "cublas" / "bin" / "cublas64_12.dll").is_file())

    def test_archive_without_libraries_is_an_error(self) -> None:
        root, archive_path, prefix = self.extract(
            {"nvidia/cublas/bin/readme.txt": b"nothing"}
        )

        with self.assertRaises(cuda.CudaRuntimeError):
            cuda.extract_libraries(archive_path, root, prefix)


class EnsureRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "cuda-12.9"

    def complete_packages(self) -> tuple[tuple[cuda.CudaPackage, bytes], ...]:
        first = wheel_bytes(
            {
                "nvidia/cublas/bin/cublasLt64_12.dll": b"lt",
                "nvidia/cublas/bin/cublas64_12.dll": b"blas",
            }
        )
        second = wheel_bytes({"nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll": b"rtc"})
        return (
            (package_for(first), first),
            (
                cuda.CudaPackage(
                    project="nvidia-cuda-nvrtc-cu12",
                    version="12.9.86",
                    filename="nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
                    sha256=hashlib.sha256(second).hexdigest(),
                    member_prefix="nvidia/cuda_nvrtc/bin/",
                ),
                second,
            ),
        )

    def run_ensure(self, prepared) -> Path:
        archives = {package.filename: payload for package, payload in prepared}

        def open_url(url, context):
            if url.startswith("https://pypi.org/"):
                filename = url.rstrip("/json").split("/")[-2]
                del filename
                project = url.split("/")[4]
                package = next(p for p, _ in prepared if p.project == project)
                return FakeResponse(
                    json.dumps(
                        {
                            "urls": [
                                {
                                    "filename": package.filename,
                                    "url": (
                                        "https://files.pythonhosted.org/packages/"
                                        f"aa/bb/{package.filename}"
                                    ),
                                    "digests": {"sha256": package.sha256},
                                }
                            ]
                        }
                    ).encode("utf-8")
                )
            payload = archives[url.rsplit("/", 1)[-1]]
            return FakeResponse(payload, {"Content-Length": str(len(payload))})

        with mock.patch.object(cuda, "_open_url", side_effect=open_url):
            return cuda.ensure_runtime(
                self.root,
                packages=[package for package, _ in prepared],
                ssl_context=mock.Mock(),
            )

    def test_complete_download_publishes_the_runtime(self) -> None:
        self.run_ensure(self.complete_packages())

        self.assertTrue(cuda.is_runtime_complete(self.root))
        self.assertTrue(
            (self.root / "cuda_nvrtc" / "bin" / "nvrtc64_120_0.dll").is_file()
        )

    def test_a_second_call_does_not_download_again(self) -> None:
        prepared = self.complete_packages()
        self.run_ensure(prepared)

        with mock.patch.object(cuda, "_open_url") as open_url:
            cuda.ensure_runtime(self.root, packages=[p for p, _ in prepared])

        open_url.assert_not_called()

    def test_incomplete_download_leaves_nothing_behind(self) -> None:
        partial = wheel_bytes({"nvidia/cublas/bin/cublas64_12.dll": b"blas"})
        prepared = ((package_for(partial), partial),)

        with self.assertRaises(cuda.CudaRuntimeError):
            self.run_ensure(prepared)

        self.assertFalse(self.root.exists())
        leftovers = list(self.root.parent.glob(f"{self.root.name}.*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
