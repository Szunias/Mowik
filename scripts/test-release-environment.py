"""Fail closed unless a release venv exactly matches its hashed lock files."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
from pathlib import Path
import re
import site


REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)==")


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def locked_names(paths: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = REQUIREMENT_RE.match(line)
            if match:
                names.add(canonical_name(match.group(1)))
    return names


def verify_record_hashes(distributions: list[importlib.metadata.Distribution]) -> None:
    failures: list[str] = []
    for distribution in distributions:
        name = canonical_name(distribution.metadata.get("Name", "unknown"))
        for entry in distribution.files or ():
            recorded_hash = entry.hash
            if recorded_hash is None:
                continue
            if recorded_hash.mode != "sha256":
                failures.append(f"{name}:{entry}:unsupported-{recorded_hash.mode}")
                continue
            path = Path(distribution.locate_file(entry))
            try:
                digest = hashlib.sha256(path.read_bytes()).digest()
            except OSError:
                failures.append(f"{name}:{entry}:missing")
                continue
            encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            if encoded != recorded_hash.value:
                failures.append(f"{name}:{entry}:hash-mismatch")
    if failures:
        preview = ", ".join(failures[:10])
        raise SystemExit(f"Installed package RECORD verification failed: {preview}")


def verify_no_unowned_package_files(
    distributions: list[importlib.metadata.Distribution],
) -> None:
    owned = {
        Path(distribution.locate_file(entry)).resolve()
        for distribution in distributions
        for entry in (distribution.files or ())
    }
    unowned: list[str] = []
    package_roots = (
        Path(value).resolve()
        for value in site.getsitepackages()
        if Path(value).name.casefold() == "site-packages"
    )
    for root in package_roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() == ".pyc":
                continue
            if path.resolve() not in owned:
                unowned.append(str(path.relative_to(root)))
    if unowned:
        preview = ", ".join(sorted(unowned)[:10])
        raise SystemExit(f"Release environment has unowned package files: {preview}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock", nargs="+", type=Path)
    args = parser.parse_args()

    expected = locked_names(args.lock)
    distributions = list(importlib.metadata.distributions())
    installed = {
        canonical_name(distribution.metadata.get("Name", ""))
        for distribution in distributions
        if distribution.metadata.get("Name")
    }
    missing = sorted(expected - installed)
    unexpected = sorted(installed - expected)
    if missing or unexpected:
        raise SystemExit(
            "Release environment distribution mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )
    verify_record_hashes(distributions)
    verify_no_unowned_package_files(distributions)
    print(
        f"Release environment verified: {len(installed)} exact distributions "
        "and RECORD hashes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
