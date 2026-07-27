#!/usr/bin/env python3
"""Run Clusterx with the resolved global or project configuration."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from config_resolver import inspect_config, resolve_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Explicit Clusterx YAML path")
    parser.add_argument("--cwd", help="Project directory used for local discovery")
    parser.add_argument("clusterx_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    clusterx_args = args.clusterx_args
    if clusterx_args[:1] == ["--"]:
        clusterx_args = clusterx_args[1:]
    if not clusterx_args:
        parser.error("pass Clusterx arguments after --")

    binary = shutil.which("clusterx")
    if not binary:
        print("clusterx is not installed or not on PATH", file=sys.stderr)
        return 2

    selection = resolve_config(explicit=args.config, cwd=args.cwd)
    inspection = inspect_config(selection)
    if not inspection["exists"]:
        print(
            f"Clusterx config not found ({selection.source}): {selection.path}",
            file=sys.stderr,
        )
        return 2
    if not inspection["permissions_safe"]:
        print(
            f"Clusterx config permissions are unsafe: {selection.path}; require 600",
            file=sys.stderr,
        )
        return 2

    print(
        f"Clusterx config: {selection.source} ({selection.path})",
        file=sys.stderr,
    )
    env = os.environ.copy()
    env["CLUSTERX_CFG_PATH"] = str(selection.path)
    return subprocess.run([binary, *clusterx_args], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
