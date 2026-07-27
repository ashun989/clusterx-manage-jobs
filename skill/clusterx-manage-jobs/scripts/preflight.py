#!/usr/bin/env python3
"""Run a secret-safe Clusterx development-machine preflight."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from redact import redact


REQUIRED_KEYS = {
    "cluster_type",
    "subscription",
    "resource_group",
    "region",
    "workspace",
    "cluster",
    "ak_id",
    "ak_secret",
}


def config_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    key_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = key_re.match(line)
            if match:
                keys.add(match.group(1))
    except (OSError, UnicodeError):
        pass
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".config" / "clusterx.yaml"),
        help="Clusterx YAML path; values are never printed",
    )
    parser.add_argument("--tmpdir", help="Shared Clusterx command-script directory")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    result: dict[str, object] = {"ok": True, "checks": {}}
    checks: dict[str, object] = result["checks"]  # type: ignore[assignment]

    binary = shutil.which("clusterx")
    checks["clusterx"] = {"found": bool(binary), "path": binary}
    if binary:
        version = subprocess.run(
            [binary, "--version"], text=True, capture_output=True, timeout=10
        )
        checks["clusterx"]["version_ok"] = version.returncode == 0  # type: ignore[index]
        checks["clusterx"]["version"] = (  # type: ignore[index]
            redact(version.stdout or version.stderr)
        ).strip().splitlines()[:1]
    else:
        result["ok"] = False

    config = Path(args.config).expanduser()
    config_check: dict[str, object] = {"exists": config.is_file(), "path": str(config)}
    checks["config"] = config_check
    if config.is_file():
        mode = stat.S_IMODE(config.stat().st_mode)
        keys = config_keys(config)
        config_check["mode"] = oct(mode)
        config_check["permissions_safe"] = mode & 0o077 == 0
        config_check["missing_keys"] = sorted(REQUIRED_KEYS - keys)
        if not config_check["permissions_safe"] or config_check["missing_keys"]:
            result["ok"] = False
    else:
        result["ok"] = False

    if args.tmpdir:
        tmpdir = Path(args.tmpdir).expanduser()
        tmp_check = {
            "path": str(tmpdir),
            "exists": tmpdir.is_dir(),
            "writable": tmpdir.is_dir() and os.access(tmpdir, os.W_OK),
        }
        checks["tmpdir"] = tmp_check
        if not tmp_check["exists"] or not tmp_check["writable"]:
            result["ok"] = False

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Clusterx preflight:", "PASS" if result["ok"] else "NEEDS ATTENTION")
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
