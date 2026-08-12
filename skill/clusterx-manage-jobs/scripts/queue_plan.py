#!/usr/bin/env python3
"""Read-only SSP queue packing report and node-release suggestions."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import itertools
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

from config_resolver import inspect_config, resolve_config
from redact import redact


SCHEMA_VERSION = 1
MAX_EXACT_STATES = 100_000


@dataclass(frozen=True)
class Target:
    nodes: int
    gpus: int
    cpus: int | None = None
    memory_gib: int | None = None


def _resource_map(node: dict[str, Any]) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for item in node.get("summary_data") or []:
        kind = str(item.get("resource_type", "")).upper()
        total = int(item.get("total") or 0)
        allocated = int(item.get("allocated") or 0)
        if kind == "MEMORY" and item.get("unit") != "GiB":
            total //= 1024**3
            allocated //= 1024**3
        result[kind] = (allocated, total)
    return result


def normalize_nodes(raw_nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for raw in raw_nodes:
        resources = _resource_map(raw)
        nodes[str(raw.get("name"))] = {
            "node": str(raw.get("name")),
            "state": str(raw.get("state", "UNKNOWN")),
            "allocated_gpu": resources.get("DEVICE", (0, 0))[0],
            "total_gpu": resources.get("DEVICE", (0, 0))[1],
            "allocated_cpu": resources.get("CPU", (0, 0))[0],
            "total_cpu": resources.get("CPU", (0, 0))[1],
            "allocated_memory_gib": resources.get("MEMORY", (0, 0))[0],
            "total_memory_gib": resources.get("MEMORY", (0, 0))[1],
            "jobs": {},
        }
    return nodes


def _fits(node: dict[str, Any], target: Target, stopped: set[str]) -> bool:
    released = {"gpu": 0, "cpu": 0, "memory_gib": 0}
    for job_id, placement in node["jobs"].items():
        if job_id in stopped:
            for key in released:
                released[key] += int(placement.get(key, 0))
    free_gpu = node["total_gpu"] - node["allocated_gpu"] + released["gpu"]
    if free_gpu < target.gpus:
        return False
    if target.cpus is not None:
        free_cpu = node["total_cpu"] - node["allocated_cpu"] + released["cpu"]
        if free_cpu < target.cpus:
            return False
    if target.memory_gib is not None:
        free_memory = (
            node["total_memory_gib"]
            - node["allocated_memory_gib"]
            + released["memory_gib"]
        )
        if free_memory < target.memory_gib:
            return False
    return node["state"].upper() not in {"DRAIN", "FAILED", "UNHEALTHY"}


def _job_cost(job_ids: set[str], jobs: dict[str, dict[str, Any]]) -> dict[str, int]:
    return {
        "gpus": sum(int(jobs[j]["total_gpu"]) for j in job_ids),
        "job_count": len(job_ids),
        "users": len({str(jobs[j]["user"]) for j in job_ids}),
    }


def _count_label(value: int, singular: str) -> str:
    return f"{value} {singular if value == 1 else singular + 's'}"


def _candidate(
    targets: Iterable[str],
    nodes: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    target: Target,
) -> dict[str, Any]:
    selected = list(targets)
    job_ids = {
        job_id
        for node_name in selected
        for job_id, placement in nodes[node_name]["jobs"].items()
        if int(placement.get("gpu", 0)) > 0
    }
    if target.cpus is not None or target.memory_gib is not None:
        for node_name in selected:
            if _fits(nodes[node_name], target, job_ids):
                continue
            job_ids.update(nodes[node_name]["jobs"])
    freed = sorted(node for node, data in nodes.items() if _fits(data, target, job_ids))
    costs = _job_cost(job_ids, jobs)
    return {
        "target_nodes": sorted(selected),
        "freed_nodes": freed,
        "jobs": sorted(job_ids),
        **costs,
    }


def _candidate_from_jobs(
    job_ids: Iterable[str],
    nodes: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    target: Target,
) -> dict[str, Any]:
    selected = set(job_ids)
    freed = sorted(node for node, data in nodes.items() if _fits(data, target, selected))
    target_nodes = sorted({
        node_name
        for job_id in selected
        for placement in jobs[job_id].get("placements", [])
        if (node_name := str(placement.get("node", "")))
    })
    return {
        "target_nodes": target_nodes,
        "freed_nodes": freed,
        "jobs": sorted(selected),
        **_job_cost(selected, jobs),
    }


def _prune_candidate(
    candidate: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    target: Target,
    key: Callable[[dict[str, Any]], tuple[Any, ...]],
) -> dict[str, Any]:
    """Remove redundant jobs until no single removal still satisfies the target."""
    current = candidate
    while True:
        reduced = [
            _candidate_from_jobs(set(current["jobs"]) - {job_id}, nodes, jobs, target)
            for job_id in current["jobs"]
        ]
        feasible = [item for item in reduced if len(item["freed_nodes"]) >= target.nodes]
        if not feasible:
            return current
        current = min(feasible, key=key)


def _heuristic_candidates(
    eligible: list[str],
    nodes: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    target: Target,
    keys: list[Callable[[dict[str, Any]], tuple[Any, ...]]],
    max_states: int,
) -> list[dict[str, Any]]:
    eligible_jobs = sorted({job_id for name in eligible for job_id in nodes[name]["jobs"]})
    seeds = [{job_id} for job_id in eligible_jobs]
    seeds.extend(set(nodes[name]["jobs"]) for name in eligible)
    additions = [{job_id} for job_id in eligible_jobs]
    additions.extend(set(nodes[name]["jobs"]) for name in eligible)
    unique_seeds = {tuple(sorted(seed)): seed for seed in seeds if seed}
    unique_additions = list({tuple(sorted(item)): item for item in additions if item}.values())
    results: list[dict[str, Any]] = []
    for key in keys:
        examined = 0
        for seed in unique_seeds.values():
            selected = set(seed)
            while examined < max_states:
                candidate = _candidate_from_jobs(selected, nodes, jobs, target)
                examined += 1
                if len(candidate["freed_nodes"]) >= target.nodes:
                    results.append(_prune_candidate(candidate, nodes, jobs, target, key))
                    break
                expansions = []
                for addition in unique_additions:
                    expanded = selected | addition
                    if expanded == selected:
                        continue
                    proposal = _candidate_from_jobs(expanded, nodes, jobs, target)
                    examined += 1
                    gained = len(proposal["freed_nodes"]) - len(candidate["freed_nodes"])
                    expansions.append((-gained, key(proposal), tuple(sorted(expanded)), expanded))
                    if examined >= max_states:
                        break
                if not expansions:
                    break
                selected = set(min(expansions)[3])
            if examined >= max_states:
                break
        if examined >= max_states:
            break
    return results


def solve_candidates(
    nodes: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    target: Target,
    *,
    candidate_scope: str = "fragmented",
    max_states: int = MAX_EXACT_STATES,
) -> tuple[list[dict[str, Any]], str]:
    already = [name for name, node in nodes.items() if _fits(node, target, set())]
    if len(already) >= target.nodes:
        return [], "not-needed"

    fragmented = [
        name
        for name, node in nodes.items()
        if node["jobs"] and 0 < node["allocated_gpu"] < node["total_gpu"]
    ]
    full = [
        name
        for name, node in nodes.items()
        if node["jobs"] and node["total_gpu"] > 0
        and node["allocated_gpu"] >= node["total_gpu"]
    ]
    if candidate_scope == "fragmented":
        eligible = fragmented
    elif candidate_scope == "full":
        eligible = full
    elif candidate_scope == "all":
        eligible = sorted(set(fragmented + full))
    else:
        raise ValueError(f"unsupported candidate scope: {candidate_scope}")
    eligible = [name for name in eligible if name not in already]
    need = target.nodes - len(already)
    if need <= 0 or not eligible:
        return [], "exact"

    max_group_size = min(need, len(eligible))
    state_count = sum(
        math.comb(len(eligible), size) for size in range(1, max_group_size + 1)
    )

    keys: list[tuple[str, Callable[[dict[str, Any]], tuple[Any, ...]]]] = [
        ("min-gpu", lambda c: (c["gpus"], c["job_count"], c["users"], c["jobs"])),
        ("min-jobs", lambda c: (c["job_count"], c["gpus"], c["users"], c["jobs"])),
        ("min-users", lambda c: (c["users"], c["gpus"], c["job_count"], c["jobs"])),
    ]
    candidates: list[dict[str, Any]] = []
    optimality = "exact" if state_count <= max_states else "heuristic"
    if optimality == "exact":
        examined = 0
        for size in range(1, max_group_size + 1):
            for group in itertools.combinations(eligible, size):
                examined += 1
                if examined > max_states:
                    optimality = "heuristic"
                    candidates.clear()
                    break
                candidate = _candidate(group, nodes, jobs, target)
                if len(candidate["freed_nodes"]) >= target.nodes:
                    candidates.append(candidate)
            if optimality == "heuristic":
                break

    if optimality == "heuristic":
        candidates = _heuristic_candidates(
            eligible, nodes, jobs, target, [key for _, key in keys], max_states
        )

    if not candidates:
        return [], optimality
    selected: list[dict[str, Any]] = []
    seen: dict[tuple[str, ...], dict[str, Any]] = {}
    for strategy, key in keys:
        best = min(candidates, key=key)
        signature = tuple(best["jobs"])
        if signature in seen:
            seen[signature]["also_strategies"].append(strategy)
            continue
        plan = {
            "strategy": strategy,
            "also_strategies": [],
            "optimality": optimality,
            **best,
        }
        seen[signature] = plan
        selected.append(plan)
    return selected, optimality


def _convert_job(cluster: Any, raw: dict[str, Any]) -> dict[str, Any] | None:
    schema = cluster._convert_to_job_schema(raw)
    if str(schema.status.value).lower() != "running":
        return None
    return {
        "job_id": str(schema.job_id),
        "job_name": schema.job_name or str(schema.job_id),
        "user": str(schema.user or "unknown"),
        "queue": str(schema.partition or ""),
        "gpus_per_node": int(schema.gpus_per_node),
        "cpus_per_node": int(schema.cpus_per_node),
        "memory_per_node_gib": int(str(schema.memory).removesuffix("Gi") or 0),
        "num_nodes": int(schema.num_nodes),
        "total_gpu": int(schema.gpus_per_node) * int(schema.num_nodes),
    }


def _list_workers(cluster: Any, job_id: str) -> list[dict[str, Any]]:
    workers: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        response = cluster.list_job_workers(job_id, page_size=100, page_token=token)
        workers.extend(response.get("workers") or [])
        token = response.get("next_page_token")
        if not token:
            return workers


def collect_snapshot(
    cluster: Any, queue: str, cluster_name: str, *, minutes: int = 5
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    raw_nodes = cluster.client.list_queue_nodes(
        cluster=cluster_name, queue=queue, page_size=100, is_bound=True
    )
    nodes_list = raw_nodes.get("nodes") or []
    total_nodes = raw_nodes.get("total_size") or raw_nodes.get("total")
    if total_nodes is not None and int(total_nodes) > len(nodes_list):
        raise RuntimeError("queue node response is truncated; refusing partial analysis")
    nodes = normalize_nodes(nodes_list)

    def read_jobs() -> list[dict[str, Any]]:
        response = cluster.client.list_training_jobs(
            filter_str='state="RUNNING"', page_size=1000
        )
        result = []
        for raw in response.get("training_jobs") or response.get("trainingJobs") or []:
            job = _convert_job(cluster, raw)
            if job and job["queue"] == queue:
                result.append(job)
        return result

    before = read_jobs()
    jobs = {job["job_id"]: job for job in before}
    worker_map: dict[str, list[dict[str, Any]]] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(_list_workers, cluster, job): job for job in jobs}
        for future in as_completed(futures):
            job_id = futures[future]
            try:
                worker_map[job_id] = future.result()
            except Exception:
                failures.append(job_id)
    if failures:
        raise RuntimeError(f"worker mapping failed for {len(failures)} running jobs")
    after = read_jobs()
    if {j["job_id"] for j in before} != {j["job_id"] for j in after}:
        raise RuntimeError("running job set changed during collection; retry the command")

    acn_to_node = {name: name for name in nodes}
    hostname_to_node: dict[str, str] = {}
    for job_id, workers in worker_map.items():
        job = jobs[job_id]
        observed = 0
        placements: list[dict[str, Any]] = []
        for worker in workers:
            if str(worker.get("phase", "")).upper() != "RUNNING":
                continue
            node_name = str((worker.get("acn") or {}).get("name") or "")
            if node_name not in acn_to_node:
                raise RuntimeError(f"worker node is absent from queue inventory for job {job_id}")
            host_ip = str(worker.get("host_ip") or "")
            if host_ip:
                hostname_to_node["host-" + host_ip.replace(".", "-")] = node_name
            resource = worker.get("resource") or {}
            placement = {
                "gpu": int(resource.get("accelerate_device_count") or 0),
                "cpu": int(resource.get("cpu_count") or 0),
                "memory_gib": int(resource.get("memory_gib") or 0),
            }
            nodes[node_name]["jobs"][job_id] = placement
            placements.append({"node": node_name, **placement})
            observed += placement["gpu"]
        job["placements"] = placements
        if observed != job["total_gpu"]:
            raise RuntimeError(f"GPU allocation mismatch for job {job_id}")

    try:
        metric_rows = cluster.stats_prometheus(
            scope="queue", metric="all", minutes=minutes, queue=queue,
            cluster=cluster_name, verbose=False,
        )
        for row in metric_rows:
            node_name = hostname_to_node.get(str(row.get("hostname") or ""))
            if not node_name:
                continue
            nodes[node_name]["metrics"] = {
                key: value for key, value in row.items()
                if key not in {"hostname", "scope", "granularity", "window"}
            }
    except Exception:
        warnings.append("Prometheus node metrics were unavailable; allocation analysis is still valid")

    return {"nodes": nodes, "jobs": jobs}, warnings


def build_report(
    snapshot: dict[str, Any], target: Target, queue: str, cluster_name: str,
    candidate_scope: str = "fragmented",
) -> dict[str, Any]:
    nodes = snapshot["nodes"]
    jobs = snapshot["jobs"]
    suggestions, optimality = solve_candidates(
        nodes, jobs, target, candidate_scope=candidate_scope
    )
    for suggestion in suggestions:
        suggestion["job_details"] = [
            {
                "job_id": job_id,
                "job_name": jobs[job_id]["job_name"],
                "user": jobs[job_id]["user"],
                "total_gpu": jobs[job_id]["total_gpu"],
                "placements": jobs[job_id]["placements"],
            }
            for job_id in suggestion["jobs"]
        ]
    free_nodes = sum(_fits(node, target, set()) for node in nodes.values())
    fragmented = [
        {
            "node": name,
            "allocated_gpu": node["allocated_gpu"],
            "total_gpu": node["total_gpu"],
            "free_gpu": node["total_gpu"] - node["allocated_gpu"],
            "jobs": [
                {
                    "job_id": job_id,
                    "job_name": jobs[job_id]["job_name"],
                    "user": jobs[job_id]["user"],
                    **node["jobs"][job_id],
                }
                for job_id in sorted(node["jobs"])
            ],
            "metrics": node.get("metrics", {}),
        }
        for name, node in sorted(nodes.items())
        if node["jobs"] and node["allocated_gpu"] < node["total_gpu"]
    ]
    full_nodes = sum(
        bool(node["jobs"]) and node["total_gpu"] > 0
        and node["allocated_gpu"] >= node["total_gpu"]
        for node in nodes.values()
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue": queue,
        "cluster": cluster_name,
        "target": {
            "nodes": target.nodes,
            "gpus_per_node": target.gpus,
            "cpus_per_node": target.cpus,
            "memory_per_node_gib": target.memory_gib,
        },
        "summary": {
            "total_nodes": len(nodes),
            "total_gpu": sum(n["total_gpu"] for n in nodes.values()),
            "allocated_gpu": sum(n["allocated_gpu"] for n in nodes.values()),
            "free_gpu": sum(n["total_gpu"] - n["allocated_gpu"] for n in nodes.values()),
            "currently_schedulable_nodes": free_nodes,
            "fragmented_nodes": len(fragmented),
            "full_nodes": full_nodes,
            "running_jobs": len(jobs),
        },
        "analysis": {
            "needs_repacking": free_nodes < target.nodes,
            "resource_scope": "+".join(
                ["gpu"]
                + (["cpu"] if target.cpus is not None else [])
                + (["memory"] if target.memory_gib is not None else [])
            ),
            "optimality": optimality,
            "candidate_scope": candidate_scope,
        },
        "fragmented_nodes": fragmented,
        "suggestions": suggestions,
        "warnings": [],
    }


def render_text(report: dict[str, Any]) -> str:
    target = report["target"]
    summary = report["summary"]
    lines = [
        f"Queue: {report['queue']}",
        f"Target: {target['nodes']} x {target['gpus_per_node']} GPU",
        (
            f"Nodes: {summary['total_nodes']}; GPU allocated/free/total: "
            f"{summary['allocated_gpu']}/{summary['free_gpu']}/{summary['total_gpu']}"
        ),
        (
            f"Currently schedulable nodes: {summary['currently_schedulable_nodes']}; "
            f"fragmented nodes: {summary['fragmented_nodes']}; "
            f"full nodes: {summary['full_nodes']}"
        ),
        f"Candidate scope: {report['analysis']['candidate_scope']}",
    ]
    lines.extend(f"Warning: {warning}" for warning in report.get("warnings", []))
    if not report["analysis"]["needs_repacking"]:
        lines.append("Result: enough nodes are already schedulable; no job coordination is needed.")
        return "\n".join(lines) + "\n"
    if not report["suggestions"]:
        lines.append("Result: no eligible candidate suggestion satisfies the target.")
        return "\n".join(lines) + "\n"
    labels = {"min-gpu": "coordinated GPU", "min-jobs": "jobs", "min-users": "users"}
    for index, suggestion in enumerate(report["suggestions"], 1):
        display_strategy = suggestion.get("display_strategy", suggestion["strategy"])
        equivalent = [
            strategy for strategy in (
                suggestion["strategy"], *suggestion.get("also_strategies", [])
            )
            if strategy != display_strategy
        ]
        qualifier = "minimum" if suggestion["optimality"] == "exact" else "lowest found"
        lines.extend(
            [
                "",
                f"Plan {index} ({qualifier} {labels[display_strategy]}, {suggestion['optimality']}):",
                f"  Coordination candidate: {suggestion['gpus']} GPU, {_count_label(suggestion['job_count'], 'job')}, {_count_label(suggestion['users'], 'user')}",
                f"  Freed nodes: {', '.join(suggestion['freed_nodes'][:target['nodes']])}",
            ]
        )
        if equivalent:
            lines.append(
                ("  Also optimal for: " if suggestion["optimality"] == "exact" else "  Also best found for: ")
                + ", ".join(labels[strategy] for strategy in equivalent)
            )
        for job in suggestion["job_details"]:
            lines.append(f"  - {job['user']}: {job['job_name']} ({job['total_gpu']} GPU total)")
    lines.append("\nRead-only report: no job was stopped or modified.")
    return "\n".join(lines) + "\n"


def render_rich(report: dict[str, Any], *, console: Any | None = None) -> bool:
    """Render a colored terminal report; return False when Rich is unavailable."""
    try:
        from rich import box
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        return False

    console = console or Console()
    target = report["target"]
    summary = report["summary"]
    schedulable = summary["currently_schedulable_nodes"]
    target_met = schedulable >= target["nodes"]
    status = Text(
        "READY" if target_met else "FRAGMENTED",
        style="bold green" if target_met else "bold yellow",
    )
    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold cyan")
    overview.add_column()
    overview.add_row("Queue", str(report["queue"]))
    overview.add_row("Target", f"{target['nodes']} × {target['gpus_per_node']} GPU")
    overview.add_row(
        "GPU",
        f"[red]{summary['allocated_gpu']} allocated[/] / "
        f"[green]{summary['free_gpu']} free[/] / {summary['total_gpu']} total",
    )
    overview.add_row(
        "Nodes",
        f"{summary['total_nodes']} total · "
        f"[green]{schedulable} schedulable[/] · "
        f"[yellow]{summary['fragmented_nodes']} fragmented[/] · "
        f"[red]{summary['full_nodes']} full[/]",
    )
    overview.add_row("Candidates", str(report["analysis"]["candidate_scope"]))
    overview.add_row("Status", status)
    console.print(Panel(overview, title="[bold]ClusterX Queue Packing[/]", border_style="cyan"))
    for warning in report.get("warnings", []):
        console.print(f"[bold yellow]Warning:[/] {warning}")

    fragments = report.get("fragmented_nodes") or []
    if fragments:
        table = Table(
            title="Fragmented nodes",
            box=box.ROUNDED,
            header_style="bold magenta",
            row_styles=("", "dim"),
        )
        table.add_column("Node", style="cyan", no_wrap=True)
        table.add_column("GPU", justify="right")
        table.add_column("Free", justify="right", style="green")
        table.add_column("GPU util", justify="right")
        table.add_column("Users / jobs", overflow="fold")
        for fragment in fragments:
            jobs = fragment.get("jobs") or []
            occupants = ", ".join(
                f"{item['user']}/{item['job_name']}:{item['gpu']}G" for item in jobs
            )
            gpu_util = (fragment.get("metrics") or {}).get("gpu-util")
            table.add_row(
                str(fragment["node"]),
                f"{fragment['allocated_gpu']}/{fragment['total_gpu']}",
                str(fragment["free_gpu"]),
                "-" if gpu_util is None else f"{gpu_util:.1f}%",
                occupants or "-",
            )
        console.print(table)

    if target_met:
        console.print("[bold green]✓ Enough nodes are already schedulable; no pause is needed.[/]")
        return True
    suggestions = report.get("suggestions") or []
    if not suggestions:
        console.print("[bold red]No eligible candidate suggestion satisfies the target.[/]")
        return True

    labels = {
        "min-gpu": "coordinated GPU",
        "min-jobs": "jobs",
        "min-users": "users",
    }
    for index, suggestion in enumerate(suggestions, 1):
        display_strategy = suggestion.get("display_strategy", suggestion["strategy"])
        equivalent = [
            strategy for strategy in (
                suggestion["strategy"], *suggestion.get("also_strategies", [])
            )
            if strategy != display_strategy
        ]
        qualifier = "Minimum" if suggestion["optimality"] == "exact" else "Lowest found"
        plan = Table(
            title=f"Plan {index} · {qualifier} {labels[display_strategy]} · {suggestion['optimality']}",
            box=box.SIMPLE_HEAVY,
            header_style="bold blue",
        )
        plan.add_column("User", style="cyan")
        plan.add_column("Job")
        plan.add_column("GPU", justify="right", style="yellow")
        plan.add_column("Placement", style="dim")
        for job in suggestion["job_details"]:
            placement = ", ".join(
                f"{item['node']}:{item['gpu']}G" for item in job["placements"]
            )
            plan.add_row(
                str(job["user"]), str(job["job_name"]),
                str(job["total_gpu"]), placement,
            )
        console.print(
            f"[bold]Coordination candidate:[/] [yellow]{suggestion['gpus']} GPU[/], "
            f"{_count_label(suggestion['job_count'], 'job')}, "
            f"{_count_label(suggestion['users'], 'user')}\n"
            f"[bold]Freed nodes:[/] [green]"
            + ", ".join(suggestion["freed_nodes"][:target["nodes"]])
            + "[/]"
        )
        if equivalent:
            console.print(
                ("[bold]Also optimal for:[/] [magenta]" if suggestion["optimality"] == "exact" else "[bold]Also best found for:[/] [magenta]")
                + ", ".join(labels[strategy] for strategy in equivalent)
                + "[/]"
            )
        console.print(plan)
    console.print("[dim]Read-only report: no job was stopped or modified.[/]")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=int, required=True, help="Number of schedulable nodes required")
    parser.add_argument(
        "--gpus-per-node", type=int, default=8,
        help="GPUs required per node (default: 8)",
    )
    parser.add_argument("--cpus-per-node", type=int, help="Optional CPUs required per node")
    parser.add_argument("--memory-per-node-gib", type=int, help="Optional memory required per node in GiB")
    parser.add_argument("--queue", "-q", help="Queue override; default comes from Clusterx config")
    parser.add_argument("--cluster-name", help="Cluster override; default comes from Clusterx config")
    parser.add_argument("--config", help="Explicit protected Clusterx YAML path")
    parser.add_argument("--cwd", help="Configuration discovery directory (default: current directory)")
    parser.add_argument("--minutes", type=int, default=5, help="Prometheus lookback only (default: 5)")
    parser.add_argument(
        "--strategy",
        choices=("all", "min-gpu", "min-jobs", "min-users"),
        default="all",
        help="Suggestion strategy to display (default: all)",
    )
    parser.add_argument(
        "--candidate-scope",
        choices=("fragmented", "full", "all"),
        default="fragmented",
        help="Nodes whose jobs may be coordination candidates (default: fragmented)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print schema-versioned JSON instead of a terminal report")
    parser.add_argument("--out", type=Path, help="Also save the complete JSON report to this path")
    args = parser.parse_args()
    for name in ("nodes", "gpus_per_node"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("cpus_per_node", "memory_per_node_gib"):
        if getattr(args, name) is not None and getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.minutes <= 0:
        parser.error("--minutes must be positive")
    return args


def main() -> int:
    args = parse_args()
    selection = resolve_config(explicit=args.config, cwd=args.cwd)
    inspection = inspect_config(selection)
    if not inspection.get("exists") or not inspection.get("permissions_safe"):
        print("Clusterx config is missing or permissions are unsafe; require mode 600", file=sys.stderr)
        return 2
    os.environ["CLUSTERX_CFG_PATH"] = str(selection.path)
    try:
        from clusterx.launcher.ssp.ssp import SSPCluster

        cluster = SSPCluster()
        queue = args.queue or cluster.cfg.get("queue") or cluster.cfg.get("partition")
        cluster_name = args.cluster_name or cluster.cfg.get("cluster")
        if not queue or not cluster_name:
            raise ValueError("queue and cluster name are required")
        try:
            snapshot, warnings = collect_snapshot(
                cluster, str(queue), str(cluster_name), minutes=args.minutes
            )
        except RuntimeError as error:
            if "running job set changed" not in str(error):
                raise
            snapshot, warnings = collect_snapshot(
                cluster, str(queue), str(cluster_name), minutes=args.minutes
            )
            warnings.append("Job set changed during the first collection; report uses one retry")
        target = Target(args.nodes, args.gpus_per_node, args.cpus_per_node, args.memory_per_node_gib)
        report = build_report(
            snapshot, target, str(queue), str(cluster_name), args.candidate_scope
        )
        report["warnings"].extend(warnings)
        report["analysis"]["requested_strategy"] = args.strategy
        if args.strategy != "all":
            filtered = []
            for suggestion in report["suggestions"]:
                if (
                    args.strategy == suggestion["strategy"]
                    or args.strategy in suggestion.get("also_strategies", [])
                ):
                    suggestion["display_strategy"] = args.strategy
                    filtered.append(suggestion)
            report["suggestions"] = filtered
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload + "\n", encoding="utf-8")
        if args.as_json:
            sys.stdout.write(payload + "\n")
        elif not render_rich(report):
            sys.stdout.write(render_text(report))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(redact(f"queue analysis failed: {error}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
