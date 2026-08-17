#!/usr/bin/env python3
"""Run Clusterx with the resolved global or project configuration."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from config_resolver import inspect_config, resolve_config
from redact import redact


SHELL_INTERPRETERS = {"bash", "dash", "ksh", "sh", "zsh"}
DEFAULT_RESOURCE_POLICY = Path(__file__).resolve().parents[1] / "assets" / "resource-policy.json"


def _resource_option(clusterx_args: list[str], name: str, default: str) -> str:
    value = default
    index = 1
    while index < len(clusterx_args):
        argument = clusterx_args[index]
        if argument == "--":
            break
        if argument == name:
            if index + 1 >= len(clusterx_args):
                raise ValueError(f"{name} requires a value")
            value = clusterx_args[index + 1]
            index += 2
            continue
        prefix = name + "="
        if argument.startswith(prefix):
            value = argument[len(prefix):]
        index += 1
    return value


def _load_training_policy(path: Path) -> tuple[Decimal, Decimal]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        training = payload["training"]
        cpu_per_gpu = Decimal(str(training["cpu_per_gpu"]))
        zero_gpu_max_cpu = Decimal(str(training["zero_gpu_max_cpu_per_node"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, InvalidOperation) as error:
        raise ValueError(f"cannot load resource policy: {error}") from error
    if not cpu_per_gpu.is_finite() or cpu_per_gpu <= 0:
        raise ValueError("resource policy cpu_per_gpu must be positive")
    if not zero_gpu_max_cpu.is_finite() or zero_gpu_max_cpu <= 0:
        raise ValueError("resource policy zero_gpu_max_cpu_per_node must be positive")
    return cpu_per_gpu, zero_gpu_max_cpu


def _validate_training_cpu(clusterx_args: list[str], policy_path: Path) -> str | None:
    if not clusterx_args or clusterx_args[0] != "run":
        return None
    try:
        gpu_text = _resource_option(clusterx_args, "--gpus-per-task", "0")
        cpu_text = _resource_option(clusterx_args, "--cpus-per-task", "4")
        gpus = Decimal(gpu_text)
        cpus = Decimal(cpu_text)
    except (ValueError, InvalidOperation) as error:
        return f"invalid Clusterx training resources: {error}"
    if not gpus.is_finite() or gpus < 0 or gpus != gpus.to_integral_value():
        return "invalid Clusterx training resources: --gpus-per-task must be a non-negative integer"
    if not cpus.is_finite() or cpus < 0:
        return "invalid Clusterx training resources: --cpus-per-task must be non-negative"
    try:
        cpu_per_gpu, zero_gpu_max_cpu = _load_training_policy(policy_path)
    except ValueError as error:
        return str(error)
    maximum = zero_gpu_max_cpu if gpus == 0 else gpus * cpu_per_gpu
    if cpus > maximum:
        rule = "zero_gpu_max_cpu_per_node" if gpus == 0 else "gpus_per_task × cpu_per_gpu"
        return (
            f"refusing Clusterx run: {cpus:g} CPU exceeds the per-task limit "
            f"{maximum:g} for {gpus:g} GPU ({rule})"
        )
    return None


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
    parser.add_argument(
        "--resource-policy",
        default=os.environ.get("CLUSTERX_RESOURCE_POLICY", str(DEFAULT_RESOURCE_POLICY)),
        help="Public resource policy used for Clusterx run validation",
    )
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

    resource_error = _validate_training_cpu(clusterx_args, Path(args.resource_policy))
    if resource_error is not None:
        print(resource_error, file=sys.stderr)
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
