#!/usr/bin/env python3
"""Run Clusterx with the resolved global or project configuration."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from importlib.resources import files as resource_files
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import requests
import yaml

try:
    from .config_resolver import inspect_config, resolve_config
    from .redact import redact
except ImportError:  # pragma: no cover - supports direct local script execution
    from config_resolver import inspect_config, resolve_config
    from redact import redact


SHELL_INTERPRETERS = {"bash", "dash", "ksh", "sh", "zsh"}
try:
    DEFAULT_RESOURCE_POLICY = Path(
        resource_files("clusterx_monitor_cli_assets").joinpath("resource-policy.json")
    )
except (ModuleNotFoundError, FileNotFoundError):
    DEFAULT_RESOURCE_POLICY = (
        Path(__file__).resolve().parents[1]
        / "clusterx_monitor_cli_assets"
        / "resource-policy.json"
    )
DEFAULT_IDENTITY_CONFIG = Path("~/.config/clusterx-manage-jobs/identity.yaml").expanduser()
MONITOR_URL_ENV = "CLUSTERX_MONITOR_URL"
CLUSTERX_USER_ENV = "CLUSTERX_USER"


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


def _configured_cluster_user(endpoint: str, explicit: str | None, path: Path) -> str | None:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    configured = os.environ.get(CLUSTERX_USER_ENV, "").strip()
    if configured:
        return configured
    if not path.is_file():
        return None
    if path.is_symlink():
        raise ValueError(f"cluster user identity configuration must not be a symlink: {path}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(f"cluster user identity configuration permissions are unsafe: {path}; require 600")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("cluster user identity configuration must have schema_version 1")
    users = payload.get("cluster_users")
    if not isinstance(users, dict):
        raise ValueError("cluster user identity configuration must contain cluster_users")
    value = users.get(endpoint.rstrip("/"))
    return str(value).strip() if value is not None and str(value).strip() else None


def _monitor_request(endpoint: str, user: str | None = None) -> dict[str, object]:
    params = {} if user is None else {"user": user}
    response = requests.get(
        endpoint.rstrip("/") + "/api/v1/access/nodes",
        params=params, timeout=(5, 20),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"monitor returned HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("monitor returned invalid access response")
    return payload


def _monitor_snapshot(endpoint: str) -> dict[str, object]:
    response = requests.get(
        endpoint.rstrip("/") + "/api/v1/snapshots/latest", timeout=(5, 20),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"monitor returned HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("monitor returned invalid snapshot response")
    return payload


def _placement_values(clusterx_args: list[str], name: str) -> set[str]:
    raw = _resource_option(clusterx_args, name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _check_node_policy(
    clusterx_args: list[str], *, explicit_user: str | None,
    identity_path: Path,
) -> str | None:
    if not clusterx_args or clusterx_args[0] != "run":
        return None
    endpoint = os.environ.get(MONITOR_URL_ENV, "").strip()
    if not endpoint:
        return None
    try:
        state = _monitor_snapshot(endpoint)
        allocation = state.get("node_allocation") or {}
        if not isinstance(allocation, dict) or not allocation.get("enabled"):
            return None
        user = _configured_cluster_user(endpoint, explicit_user, identity_path)
        if not user:
            return "node allocation is enabled but no cluster user was provided; pass --cluster-user or configure identity.yaml"
        access = _monitor_request(endpoint, user)
        owned = {
            str(node.get("node")) for node in (access.get("nodes") or [])
            if isinstance(node, dict) and node.get("node")
        }
        include = _placement_values(clusterx_args, "--include")
        exclude = _placement_values(clusterx_args, "--exclude")
        if include and not include.intersection(owned):
            print(
                f"warning: explicit --include does not contain any node in the {access.get('identity', {}).get('group', 'resolved')} owned pool; owned nodes: {', '.join(sorted(owned)) or '-'}",
                file=sys.stderr,
            )
        elif exclude and owned.intersection(exclude):
            print(
                "warning: explicit --exclude removes nodes from the resolved group-owned pool: "
                + ", ".join(sorted(owned.intersection(exclude))),
                file=sys.stderr,
            )
        elif not include:
            print(
                "node policy recommendation: owned nodes="
                + (", ".join(sorted(owned)) or "-"),
                file=sys.stderr,
            )
    except (OSError, ValueError, RuntimeError, requests.RequestException, yaml.YAMLError) as error:
        print(f"warning: node policy check unavailable; continuing without placement constraint: {error}", file=sys.stderr)
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
    parser.add_argument("--cluster-user", help="explicit Clusterx workload owner used for node policy checks")
    parser.add_argument("--identity-config", default=str(DEFAULT_IDENTITY_CONFIG), help="local cluster user identity mapping")
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

    node_policy_error = _check_node_policy(
        clusterx_args, explicit_user=args.cluster_user,
        identity_path=Path(args.identity_config).expanduser(),
    )
    if node_policy_error is not None:
        print(node_policy_error, file=sys.stderr)
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
