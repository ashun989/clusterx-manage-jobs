#!/usr/bin/env python3
"""Validate each independently released component's version metadata."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]


def text_version(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"invalid version in {path}: {value!r}")
    return value


def metadata_version(path: Path) -> str:
    match = re.search(r'^version\s*=\s*["\']([^"\']+)', path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ValueError(f"version is missing from {path}")
    return match.group(1)


def python_version(path: Path) -> str:
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)', path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"__version__ is missing from {path}")
    return match.group(1)


def main() -> int:
    web_package = json.loads((root / "web/package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((root / "web/package-lock.json").read_text(encoding="utf-8"))
    versions = {
        "server": {
            "server/VERSION": text_version(root / "server/VERSION"),
            "server/pyproject.toml": metadata_version(root / "server/pyproject.toml"),
            "__init__.py": python_version(root / "server/src/clusterx_monitor/__init__.py"),
        },
        "web": {
            "web/VERSION": text_version(root / "web/VERSION"),
            "package.json": web_package["version"],
            "package-lock.json": web_lock["version"],
            "package-lock root": web_lock["packages"][""]["version"],
        },
        "client": {
            "client/VERSION": text_version(root / "client/VERSION"),
            "client/pyproject.toml": metadata_version(root / "client/pyproject.toml"),
        },
        "skill": {
            "skills/clusterx-manage-jobs/VERSION": text_version(root / "skills/clusterx-manage-jobs/VERSION"),
        },
    }
    for component, entries in versions.items():
        if None in entries.values() or len(set(entries.values())) != 1:
            print(f"{component} version mismatch: {entries}", file=sys.stderr)
            return 1
    print(json.dumps({component: next(iter(entries.values())) for component, entries in versions.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
