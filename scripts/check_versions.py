#!/usr/bin/env python3
"""Fail the release build when package versions drift apart."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
source_version = (root / "VERSION").read_text(encoding="utf-8").strip()
python_version = re.search(r'__version__\s*=\s*["\']([^"\']+)', (root / "src/clusterx_monitor/__init__.py").read_text())
project_version = re.search(r'^version\s*=\s*["\']([^"\']+)', (root / "pyproject.toml").read_text(), re.MULTILINE)
web_version = json.loads((root / "web/package.json").read_text(encoding="utf-8"))["version"]
web_lock = json.loads((root / "web/package-lock.json").read_text(encoding="utf-8"))
lock_root = web_lock.get("packages", {}).get("", {})
versions = {
    source_version,
    python_version.group(1) if python_version else None,
    project_version.group(1) if project_version else None,
    web_version,
    web_lock.get("version"),
    lock_root.get("version"),
}
if len(versions) != 1 or None in versions:
    print(f"version mismatch: {sorted(str(item) for item in versions)}", file=sys.stderr)
    raise SystemExit(1)
print(next(iter(versions)))
