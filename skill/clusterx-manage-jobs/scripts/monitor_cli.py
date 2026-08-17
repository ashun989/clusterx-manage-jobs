#!/usr/bin/env python3
"""Thin read-only client for the local Clusterx monitoring service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import requests


DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_FAIL_ON = 4


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _url(endpoint: str, path: str) -> str:
    return endpoint.rstrip("/") + path


def _request(endpoint: str, method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = requests.request(method, _url(endpoint, path), timeout=35, **kwargs)
    except requests.RequestException as error:
        raise ApiError(str(error)) from error
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = response.text
        raise ApiError(f"HTTP {response.status_code}: {detail}", status=response.status_code)
    try:
        return response.json()
    except ValueError as error:
        raise ApiError("monitor returned invalid JSON") from error


def _snapshot(endpoint: str, snapshot_id: str) -> dict[str, Any]:
    suffix = "latest" if snapshot_id == "latest" else snapshot_id
    return _request(endpoint, "GET", f"/api/v1/snapshots/{suffix}")


def _filter_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    result = rows
    for field in ("user", "group", "status", "type", "classification"):
        value = getattr(args, field, None)
        if value:
            allowed = {item.strip().lower() for item in value.split(",")}
            result = [row for row in result if str(row.get(field)).lower() in allowed]
    node = getattr(args, "node", None)
    if node:
        allowed = {item.strip().lower() for item in node.split(",")}
        result = [row for row in result if str(row.get("node")).lower() in allowed]
    workload = getattr(args, "workload", None)
    if workload:
        allowed = {item.strip().lower() for item in workload.split(",")}
        result = [
            row for row in result
            if str(row.get("workload_id")).lower() in allowed
            or str(row.get("workload_name")).lower() in allowed
        ]
    if getattr(args, "violations_only", False):
        result = [
            row for row in result
            if any(item.get("status") == "violation" for item in row.get("policy_findings", []))
            or row.get("status") == "violation"
            or row.get("policy_status") == "violation"
            or row.get("severity") == "error"
        ]
    facets = {
        "finding_category": "finding_categories",
        "finding_code": "finding_codes",
        "tag": "finding_tags",
    }
    for argument, field in facets.items():
        value = getattr(args, argument, None)
        if not value:
            continue
        allowed = {item.strip().lower() for item in value.split(",") if item.strip()}
        result = [
            row for row in result
            if allowed.intersection(str(item).lower() for item in row.get(field, []))
        ]
    return result


def _select_view(snapshot: dict[str, Any], command: str, args: argparse.Namespace) -> Any:
    if command == "overview":
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "generated_at": snapshot["generated_at"],
            "cluster": snapshot["cluster"], "queue": snapshot["queue"],
            "capacity": snapshot.get("capacity"),
            "planning_profile": snapshot.get("planning_profile"),
            "pending_pressure": snapshot.get("pending_pressure"),
            "telemetry": snapshot.get("telemetry"),
            "telemetry_status": snapshot.get("telemetry_status"),
            "historical_telemetry_status": snapshot.get("historical_telemetry_status"),
            "alerts": snapshot.get("alerts"),
            "freshness": snapshot.get("freshness"),
            "warnings": snapshot.get("warnings"),
        }
    key = {
        "users": "users", "groups": "groups", "nodes": "nodes",
        "workloads": "workloads", "alerts": "alerts",
    }[command]
    rows = list(snapshot.get(key) or [])
    if command == "workloads":
        rows.extend(snapshot.get("pending_workloads") or [])
    return _filter_rows(rows, args)


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        if "gpu_power_total_w" in value:
            power = value.get("gpu_power_total_w")
            util = value.get("gpu_compute_util_avg_pct")
            allocated = value.get("allocated_gpu_count", 0)
            return (
                f"util={util if util is not None else '-'}% "
                f"power={power if power is not None else '-'}W "
                f"coverage compute={value.get('compute_reported_gpu_count', 0)}/{allocated} "
                f"memory={value.get('memory_reported_gpu_count', 0)}/{allocated} "
                f"power={value.get('power_reported_gpu_count', 0)}/{allocated}"
            )
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _render_table(payload: Any, *, no_color: bool = False) -> str:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    console = Console(record=True, force_terminal=not no_color, color_system=None if no_color else "auto")
    if isinstance(payload, dict):
        table = Table(show_header=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", overflow="fold")
        for key, value in payload.items():
            table.add_row(str(key), _scalar(value))
    else:
        rows = list(payload or [])
        if not rows:
            return "No matching rows.\n"
        preferred = [
            "user", "group", "node", "workload_name", "type", "classification",
            "status", "policy_status", "gpu_quota", "allocated_gpu", "allocated_cpu",
            "allocated_memory_gib", "total_gpu", "total_cpu", "total_memory_gib",
            "resource_basis", "start_time", "runtime_hours", "runtime_quality",
            "runtime_source", "effective_free_gpu", "stranded_gpu", "unattributed",
            "planning_eligible", "planning_exclusion_reasons", "attribution_excess",
            "finding_categories", "finding_codes", "finding_tags", "historical_telemetry",
            "telemetry", "message",
        ]
        keys = [key for key in preferred if any(key in row for row in rows)]
        table = Table()
        for key in keys:
            table.add_column(key, overflow="fold")
        for row in rows:
            table.add_row(*[_scalar(row.get(key)) for key in keys])
    console.print(table)
    return console.export_text(styles=not no_color)


def _emit(payload: Any, args: argparse.Namespace, *, jsonl: bool = False) -> None:
    if args.format == "json" or jsonl:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":") if jsonl else None, indent=None if jsonl else 2) + "\n"
    else:
        text = _render_table(payload, no_color=args.no_color)
    if args.out:
        if jsonl:
            with Path(args.out).open("a", encoding="utf-8") as output:
                output.write(text)
        else:
            Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _has_failure(payload: Any, condition: str | None, *, snapshot: dict[str, Any] | None = None) -> bool:
    if not condition:
        return False
    if condition == "stale":
        if snapshot is not None:
            return bool((snapshot.get("freshness") or {}).get("stale"))
        if isinstance(payload, dict):
            freshness = payload.get("freshness") or payload.get("snapshot") or {}
            return bool(freshness.get("stale"))
        return False
    rows = payload if isinstance(payload, list) else payload.get("alerts", []) if isinstance(payload, dict) else []
    return any(
        any(item.get("status") == "violation" for item in row.get("policy_findings", []))
        or row.get("status") == "violation"
        or row.get("policy_status") == "violation"
        or row.get("severity") == "error"
        for row in rows
    )


def _add_output_options(
    parser: argparse.ArgumentParser, *, snapshot: bool = True,
    fail_on: tuple[str, ...] = (),
) -> None:
    if snapshot:
        parser.add_argument("--snapshot", default="latest")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--out")
    parser.add_argument("--no-color", action="store_true")
    if fail_on:
        parser.add_argument("--fail-on", choices=fail_on)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("CLUSTERX_MONITOR_URL", DEFAULT_ENDPOINT))
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    _add_output_options(status, snapshot=False, fail_on=("stale",))
    policy = sub.add_parser("policy")
    _add_output_options(policy, snapshot=False)
    overview = sub.add_parser("overview")
    _add_output_options(overview, fail_on=("violation", "stale"))
    for command in ("users", "groups", "nodes", "workloads", "alerts"):
        child = sub.add_parser(command)
        _add_output_options(
            child,
            fail_on=("stale",) if command == "nodes" else ("violation", "stale"),
        )
        child.add_argument("--user")
        child.add_argument("--group")
        child.add_argument("--status")
        child.add_argument("--type")
        child.add_argument("--classification")
        child.add_argument("--node")
        child.add_argument("--workload")
        if command != "nodes":
            child.add_argument("--violations-only", action="store_true")
            child.add_argument("--finding-category", help="comma-separated structured finding categories")
            child.add_argument("--finding-code", help="comma-separated structured finding codes")
            child.add_argument("--tag", help="comma-separated structured finding tags")
    plan = sub.add_parser("plan")
    _add_output_options(plan)
    plan.add_argument("--nodes", type=int, required=True)
    plan.add_argument("--gpus-per-node", type=int, default=8)
    plan.add_argument("--cpus-per-node", type=float)
    plan.add_argument("--memory-per-node-gib", type=float)
    plan.add_argument("--strategy", action="append", choices=("min-gpu", "min-workloads", "min-users"), default=[])
    plan.add_argument("--candidate-scope", choices=("fragmented", "full", "all"), default="fragmented")
    plan.add_argument("--alternatives", type=int, default=1)
    plan.add_argument("--search-seconds", type=float, default=10)
    for option in ("type", "group", "user", "workload", "exclude-workload", "exclude-user"):
        plan.add_argument(f"--{option}", action="append", default=[])
    plan.add_argument("--over-quota-only", action="store_true")
    plan.add_argument("--violation-category", action="append", default=[])
    plan.add_argument("--violation-code", action="append", default=[])
    plan.add_argument("--violation-tag", action="append", default=[])
    watch = sub.add_parser("watch")
    watch.add_argument("--view", choices=("overview", "users", "groups", "nodes", "workloads", "alerts"), default="alerts")
    watch.add_argument("--count", type=int)
    watch.add_argument("--format", choices=("table", "json", "jsonl"), default="jsonl")
    watch.add_argument("--out")
    watch.add_argument("--no-color", action="store_true")
    watch.add_argument("--fail-on", choices=("violation", "stale"))
    return parser


def _run_watch(args: argparse.Namespace) -> int:
    emitted = 0
    try:
        with requests.get(_url(args.endpoint, "/api/v1/events"), stream=True, timeout=(5, None)) as response:
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code}")
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                snapshot = _snapshot(args.endpoint, event["snapshot_id"])
                payload = _select_view(snapshot, args.view, args)
                _emit(payload, args, jsonl=args.format == "jsonl")
                emitted += 1
                if _has_failure(payload, args.fail_on, snapshot=snapshot):
                    return EXIT_FAIL_ON
                if args.count is not None and emitted >= args.count:
                    return 0
    except requests.RequestException as error:
        raise ApiError(str(error)) from error
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    source_snapshot: dict[str, Any] | None = None
    try:
        if args.command == "watch":
            if args.count is not None and args.count <= 0:
                parser.error("--count must be positive")
            return _run_watch(args)
        if args.command == "status":
            payload = _request(args.endpoint, "GET", "/api/v1/status")
        elif args.command == "policy":
            payload = _request(args.endpoint, "GET", "/api/v1/policy")
        elif args.command == "plan":
            if not 1 <= args.nodes <= 1024 or not 1 <= args.gpus_per_node <= 1024:
                parser.error("target nodes and GPUs must be between 1 and 1024")
            if args.cpus_per_node is not None and not 0 < args.cpus_per_node <= 1_000_000:
                parser.error("CPUs per node must be between 0 and 1000000")
            if args.memory_per_node_gib is not None and not 0 < args.memory_per_node_gib <= 10_000_000:
                parser.error("memory per node must be between 0 and 10000000 GiB")
            if not 1 <= args.alternatives <= 10:
                parser.error("alternatives must be between 1 and 10")
            if not 1 <= args.search_seconds <= 30:
                parser.error("search seconds must be between 1 and 30")
            body = {
                "snapshot_id": args.snapshot,
                "target": {
                    "nodes": args.nodes, "gpus_per_node": args.gpus_per_node,
                    "cpus_per_node": args.cpus_per_node,
                    "memory_per_node_gib": args.memory_per_node_gib,
                },
                "strategies": args.strategy or ["min-gpu"],
                "candidate_scope": args.candidate_scope,
                "alternatives": args.alternatives,
                "search_seconds": args.search_seconds,
                "filters": {
                    "workload_types": args.type, "groups": args.group,
                    "users": args.user, "workloads": args.workload,
                    "exclude_workloads": args.exclude_workload,
                    "exclude_users": args.exclude_user,
                    "over_quota_only": args.over_quota_only,
                    "violation_categories": args.violation_category,
                    "violation_codes": args.violation_code,
                    "violation_tags": args.violation_tag,
                },
            }
            if body["snapshot_id"] == "latest":
                body["snapshot_id"] = _snapshot(args.endpoint, "latest")["snapshot_id"]
            payload = _request(args.endpoint, "POST", "/api/v1/plans", json=body)
        else:
            source_snapshot = _snapshot(args.endpoint, args.snapshot)
            payload = _select_view(source_snapshot, args.command, args)
        _emit(payload, args)
        return EXIT_FAIL_ON if _has_failure(payload, getattr(args, "fail_on", None), snapshot=source_snapshot) else 0
    except KeyboardInterrupt:
        return 130
    except ApiError as error:
        if error.status == 422:
            print(f"monitor input rejected: {error}", file=sys.stderr)
            return EXIT_USAGE
        print(f"monitor service unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    raise SystemExit(main())
