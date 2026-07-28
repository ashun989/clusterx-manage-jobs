#!/usr/bin/env python3
"""Run a secret-safe Clusterx development-machine preflight."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from config_resolver import inspect_config, resolve_config
from redact import redact


TESTED_CLUSTERX_VERSION = "2026.7.1"
MINIMUM_PYTHON = (3, 10)
REQUIRED_KEYS = {
    "default",
    "ssp",
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
        help="Explicit Clusterx YAML path; values are never printed",
    )
    parser.add_argument("--cwd", help="Project directory used for local discovery")
    parser.add_argument("--tmpdir", help="Shared Clusterx command-script directory")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    result: dict[str, object] = {"ok": True, "checks": {}}
    checks: dict[str, object] = result["checks"]  # type: ignore[assignment]
    python_supported = sys.version_info >= MINIMUM_PYTHON
    checks["python"] = {
        "version": platform.python_version(),
        "minimum": ".".join(str(part) for part in MINIMUM_PYTHON),
        "supported": python_supported,
    }
    if not python_supported:
        result["ok"] = False

    selection = resolve_config(explicit=args.config, cwd=args.cwd)
    config = selection.path
    config_check = inspect_config(selection)
    checks["config"] = config_check
    config_ok = bool(
        config_check["exists"] and config_check["permissions_safe"]
    )
    if config_ok:
        keys = config_keys(config)
        config_check["missing_keys"] = sorted(REQUIRED_KEYS - keys)
        if config_check["missing_keys"]:
            config_ok = False
            result["ok"] = False
    else:
        result["ok"] = False

    binary = shutil.which("clusterx")
    checks["clusterx"] = {"found": bool(binary), "path": binary}
    if binary and config_ok:
        env = os.environ.copy()
        env["CLUSTERX_CFG_PATH"] = str(config)
        try:
            version = subprocess.run(
                [binary, "--version"],
                text=True,
                capture_output=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks["clusterx"]["version_ok"] = False  # type: ignore[index]
            checks["clusterx"]["version"] = [redact(str(exc))]  # type: ignore[index]
            result["ok"] = False
        else:
            checks["clusterx"]["version_ok"] = version.returncode == 0  # type: ignore[index]
            version_lines = redact(version.stdout or version.stderr).strip().splitlines()[:1]
            checks["clusterx"]["version"] = version_lines  # type: ignore[index]
            version_text = version_lines[0] if version_lines else ""
            checks["clusterx"]["tested_version"] = TESTED_CLUSTERX_VERSION  # type: ignore[index]
            checks["clusterx"]["compatibility"] = (  # type: ignore[index]
                "tested"
                if TESTED_CLUSTERX_VERSION in version_text
                else "unverified; inspect live help"
            )
            if version.returncode != 0:
                result["ok"] = False
    elif not binary:
        result["ok"] = False
    else:
        checks["clusterx"]["version_skipped"] = "configuration is unavailable or unsafe"  # type: ignore[index]

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
