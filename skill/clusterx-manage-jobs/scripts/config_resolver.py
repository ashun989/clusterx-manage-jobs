#!/usr/bin/env python3
"""Resolve Clusterx configuration without reading or printing its values."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Mapping


LOCAL_CONFIG = Path(".clusterx") / "clusterx.yaml"


@dataclass(frozen=True)
class ConfigSelection:
    path: Path
    source: str


def _absolute(path: str | Path, cwd: Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else cwd / candidate


def _nearest_local_config(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for directory in (current, *current.parents):
        candidate = directory / LOCAL_CONFIG
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def resolve_config(
    *,
    explicit: str | Path | None = None,
    cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> ConfigSelection:
    env = os.environ if environ is None else environ
    working_dir = Path.cwd() if cwd is None else Path(cwd)
    working_dir = working_dir.expanduser().resolve()

    if explicit:
        return ConfigSelection(_absolute(explicit, working_dir), "explicit")

    if env.get("CLUSTERX_CFG_PATH"):
        return ConfigSelection(
            _absolute(env["CLUSTERX_CFG_PATH"], working_dir), "environment"
        )

    local = _nearest_local_config(working_dir)
    if local is not None:
        return ConfigSelection(local, "project")

    dev_env = Path(
        env.get("DEV_ENV", "/data/zengquansheng/.dev-env")
    ).expanduser()
    persistent = dev_env / "clusterx" / "clusterx.yaml"
    if persistent.exists() or persistent.is_symlink():
        return ConfigSelection(persistent, "global")

    home_dir = Path.home() if home is None else Path(home).expanduser()
    return ConfigSelection(home_dir / ".config" / "clusterx.yaml", "native")


def inspect_config(selection: ConfigSelection) -> dict[str, object]:
    path = selection.path
    exists = path.is_file()
    result: dict[str, object] = {
        "source": selection.source,
        "path": str(path),
        "exists": exists,
    }
    if not exists:
        result["permissions_safe"] = False
        return result

    try:
        resolved = path.resolve(strict=True)
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError:
        result["permissions_safe"] = False
        return result

    result["resolved_path"] = str(resolved)
    result["mode"] = oct(mode)
    result["permissions_safe"] = mode & 0o077 == 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Explicit Clusterx YAML path")
    parser.add_argument("--cwd", help="Project directory used for local discovery")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    selection = resolve_config(explicit=args.config, cwd=args.cwd)
    inspection = inspect_config(selection)
    if args.as_json:
        print(json.dumps(inspection, ensure_ascii=False, indent=2))
    else:
        print(f"{selection.source}: {selection.path}")
    return 0 if inspection["exists"] and inspection["permissions_safe"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
