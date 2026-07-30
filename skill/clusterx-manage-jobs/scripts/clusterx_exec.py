#!/usr/bin/env python3
"""Run Clusterx with the resolved global or project configuration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from config_resolver import inspect_config, resolve_config
from redact import redact


SHELL_INTERPRETERS = {"bash", "dash", "ksh", "sh", "zsh"}


def _unsafe_shell_command(clusterx_args: list[str]) -> tuple[str, str] | None:
    """Return a shell and command-string option that Clusterx cannot preserve."""
    if not clusterx_args or clusterx_args[0] != "run":
        return None

    for index, argument in enumerate(clusterx_args[:-1]):
        shell = Path(argument).name
        if shell not in SHELL_INTERPRETERS:
            continue
        for option in clusterx_args[index + 1 :]:
            if not option.startswith("-") or option == "--":
                break
            flags = option.lstrip("-")
            if "c" in flags:
                return shell, option
    return None


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

    unsafe_shell = _unsafe_shell_command(clusterx_args)
    if unsafe_shell is not None:
        shell, option = unsafe_shell
        print(
            "refusing unsafe Clusterx run command: "
            f"{shell} {option} loses command-string argument boundaries because "
            "Clusterx joins command arguments without shell quoting; invoke an "
            "absolute runner script instead (for example, "
            "'bash /absolute/path/runner.sh') and pass environment variables "
            "with repeated '-e KEY=VALUE' options",
            file=sys.stderr,
        )
        return 2

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
    completed = subprocess.run(
        [binary, *clusterx_args],
        env=env,
        text=True,
        capture_output=True,
        errors="replace",
    )
    sys.stdout.write(redact(completed.stdout))
    sys.stderr.write(redact(completed.stderr))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
