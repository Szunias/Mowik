"""Runtime CUDA pobierany na żądanie, poza instalatorem.

Biblioteki cuBLAS i NVRTC zajmują w rozpakowanej postaci prawie gigabajt, a
korzystają z nich wyłącznie posiadacze karty NVIDIA. Dołączanie ich do
instalatora kazało wszystkim pobierać pół giga danych, których większość nigdy
nie użyje, więc pobieramy je dopiero przy pierwszym uruchomieniu modelu na GPU
— tak samo jak model mowy.

Moduł celowo korzysta wyłącznie z biblioteki standardowej: ładuje się przed
pakietami natywnymi, zanim ustawimy ścieżki wyszukiwania bibliotek DLL.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Wersja katalogu, nie samej CUDA: zmieniamy ją, gdy zmienia się zestaw
# pobieranych pakietów, żeby stary komplet nie mieszał się z nowym.
CUDA_RUNTIME_LAYOUT = "cuda-12.9"

PYPI_METADATA_URL = "https://pypi.org/pypi/{project}/{version}/json"
DOWNLOAD_HOST = "files.pythonhosted.org"
DOWNLOAD_TIMEOUT_SECONDS = 60.0
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
# Wheel cuBLAS ma ~553 MiB; limit chroni przed pobieraniem bez końca, gdy
# serwer poda złą długość albo strumień nigdy się nie zamknie.
MAX_ARCHIVE_BYTES = 1200 * 1024 * 1024
MAX_MEMBER_BYTES = 1200 * 1024 * 1024


class CudaRuntimeError(RuntimeError):
    """Powód, dla którego GPU nie ruszy; treść trafia wprost do użytkownika."""


@dataclass(frozen=True)
class CudaPackage:
    project: str
    version: str
    filename: str
    sha256: str
    member_prefix: str


# Wersje i sumy kontrolne pochodzą z constraints-release-hashed.txt; muszą się
# zgadzać, bo instalator buduje ten sam zestaw dla wariantu z bibliotekami.
CUDA_PACKAGES: tuple[CudaPackage, ...] = (
    CudaPackage(
        project="nvidia-cublas-cu12",
        version="12.9.2.10",
        filename="nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
        sha256="623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
        member_prefix="nvidia/cublas/bin/",
    ),
    CudaPackage(
        project="nvidia-cuda-nvrtc-cu12",
        version="12.9.86",
        filename="nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
        sha256="72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
        member_prefix="nvidia/cuda_nvrtc/bin/",
    ),
)

# Te dwie biblioteki decydują, czy komplet nadaje się do użycia; ładuje je
# jawnie configure_cuda_dll_search_paths().
REQUIRED_LIBRARIES: tuple[str, ...] = (
    "cublas/bin/cublasLt64_12.dll",
    "cublas/bin/cublas64_12.dll",
)

ProgressCallback = Callable[[int, Optional[int]], None]


def user_runtime_root(local_appdata: Optional[str] = None) -> Optional[Path]:
    """Katalog na pobrane biblioteki; None, gdy nie znamy profilu użytkownika."""

    raw = local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA")
    if not raw:
        return None
    try:
        return Path(raw) / "Mowik" / CUDA_RUNTIME_LAYOUT
    except (OSError, ValueError):
        return None


def is_runtime_complete(root: Optional[Path]) -> bool:
    """Sprawdź komplet po plikach, bo przerwane pobranie zostawia część."""

    if root is None:
        return False
    try:
        return all((root / name).is_file() for name in REQUIRED_LIBRARIES)
    except OSError:
        return False


def _create_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _open_url(url: str, context: ssl.SSLContext):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mowik", "Accept": "application/json"},
        method="GET",
    )
    return urllib.request.urlopen(  # noqa: S310 - schemat sprawdzony niżej
        request,
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        context=context,
    )


def _require_trusted_download_url(url: str) -> str:
    """Nie pobieraj z adresu podanego przez API, jeśli nie jest to PyPI po TLS."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != DOWNLOAD_HOST:
        raise CudaRuntimeError(
            f"Nieoczekiwany adres pobierania bibliotek GPU: {parsed.scheme}://"
            f"{parsed.hostname or '?'}"
        )
    return url


def resolve_download_url(package: CudaPackage, context: ssl.SSLContext) -> str:
    """Znajdź adres pliku; nazwy katalogów PyPI nie da się wyliczyć z sumy."""

    url = PYPI_METADATA_URL.format(project=package.project, version=package.version)
    try:
        with _open_url(url, context) as response:
            payload = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise CudaRuntimeError(
            f"Nie udało się sprawdzić pakietu {package.project}: {type(exc).__name__}"
        ) from exc

    entries = payload.get("urls")
    if not isinstance(entries, list):
        raise CudaRuntimeError(f"Nieoczekiwana odpowiedź PyPI dla {package.project}.")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("filename") != package.filename:
            continue
        published = entry.get("digests", {}).get("sha256")
        if published != package.sha256:
            # Suma z indeksu musi się zgadzać z przypiętą; rozjazd oznacza, że
            # pobralibyśmy inny plik niż ten sprawdzony przy wydaniu.
            raise CudaRuntimeError(
                f"Suma kontrolna {package.filename} w PyPI nie zgadza się "
                "z wersją przypiętą w Mówiku."
            )
        candidate = entry.get("url")
        if isinstance(candidate, str):
            return _require_trusted_download_url(candidate)
    raise CudaRuntimeError(f"PyPI nie udostępnia pliku {package.filename}.")


def download_archive(
    url: str,
    package: CudaPackage,
    destination: Path,
    context: ssl.SSLContext,
    progress: Optional[ProgressCallback] = None,
) -> None:
    """Pobierz wheel i odrzuć go, jeśli suma kontrolna się nie zgadza."""

    digest = hashlib.sha256()
    downloaded = 0
    try:
        with _open_url(url, context) as response:
            raw_length = response.headers.get("Content-Length")
            try:
                total = int(raw_length) if raw_length is not None else None
            except (TypeError, ValueError):
                total = None
            if total is not None and total > MAX_ARCHIVE_BYTES:
                raise CudaRuntimeError(
                    f"Plik {package.filename} jest większy, niż Mówik dopuszcza."
                )
            with destination.open("wb") as archive:
                if progress is not None:
                    progress(0, total)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise CudaRuntimeError(
                            f"Pobieranie {package.filename} przekroczyło limit rozmiaru."
                        )
                    digest.update(chunk)
                    archive.write(chunk)
                    if progress is not None:
                        progress(downloaded, total)
    except (urllib.error.URLError, OSError) as exc:
        raise CudaRuntimeError(
            f"Pobieranie {package.filename} nie powiodło się: {type(exc).__name__}"
        ) from exc

    if digest.hexdigest() != package.sha256:
        raise CudaRuntimeError(
            f"Pobrany plik {package.filename} ma inną sumę kontrolną niż oczekiwana."
        )


def _safe_member_target(root: Path, name: str, prefix: str) -> Optional[Path]:
    """Wypakuj tylko biblioteki z oczekiwanego katalogu i nigdy poza katalog."""

    if not name.startswith(prefix) or name.endswith("/"):
        return None
    relative = name[len("nvidia/") :] if name.startswith("nvidia/") else name
    if not relative.lower().endswith(".dll"):
        return None
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate == resolved_root or resolved_root not in candidate.parents:
        return None
    return candidate


def extract_libraries(archive_path: Path, root: Path, prefix: str) -> int:
    """Rozpakuj biblioteki z wheela; wheel to zwykły ZIP."""

    extracted = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                target = _safe_member_target(root, info.filename, prefix)
                if target is None:
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise CudaRuntimeError(
                        f"Biblioteka {info.filename} jest większa, niż Mówik dopuszcza."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, DOWNLOAD_CHUNK_BYTES)
                extracted += 1
    except (zipfile.BadZipFile, OSError) as exc:
        raise CudaRuntimeError(
            f"Nie udało się rozpakować bibliotek GPU: {type(exc).__name__}"
        ) from exc
    if extracted == 0:
        raise CudaRuntimeError("Pobrany pakiet nie zawiera bibliotek GPU.")
    return extracted


def ensure_runtime(
    root: Path,
    *,
    packages: Iterable[CudaPackage] = CUDA_PACKAGES,
    status_callback: Optional[Callable[[CudaPackage, int, Optional[int]], None]] = None,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> Path:
    """Pobierz komplet do katalogu tymczasowego i dopiero potem go udostępnij."""

    if is_runtime_complete(root):
        return root

    context = ssl_context if ssl_context is not None else _create_ssl_context()
    staging = root.with_name(f"{root.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise CudaRuntimeError(
            f"Nie udało się przygotować katalogu na biblioteki GPU: {type(exc).__name__}"
        ) from exc

    try:
        for package in packages:
            url = resolve_download_url(package, context)
            with tempfile.TemporaryDirectory(dir=str(staging)) as download_dir:
                archive_path = Path(download_dir) / package.filename
                download_archive(
                    url,
                    package,
                    archive_path,
                    context,
                    progress=(
                        None
                        if status_callback is None
                        else lambda done, total, current=package: status_callback(
                            current, done, total
                        )
                    ),
                )
                extract_libraries(archive_path, staging, package.member_prefix)

        if not is_runtime_complete(staging):
            raise CudaRuntimeError(
                "Pobrany komplet bibliotek GPU jest niekompletny."
            )

        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, root)
        except OSError:
            # Inna instancja mogła w międzyczasie ukończyć to samo pobieranie.
            if not is_runtime_complete(root):
                raise
            shutil.rmtree(staging, ignore_errors=True)
        return root
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
