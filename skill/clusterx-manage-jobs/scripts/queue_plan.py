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
import re
import sys
import time
from typing import Any, Callable, Iterable

from config_resolver import inspect_config, resolve_config
from redact import redact


SCHEMA_VERSION = 2
CALIBRATION_STATES = 1_000
GPU_COMPUTE_UTIL = "gpu-compute-util"
GPU_MEMORY_UTIL = "gpu-memory-util"


@dataclass(frozen=True)
class Target:
    nodes: int
    gpus: int
    cpus: int | None = None
    memory_gib: int | None = None


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _runtime_fields(workload: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    created_at = _parse_utc_timestamp(workload.get("create_time"))
    if created_at is None or created_at > generated_at:
        return {"create_time": None, "runtime_seconds": None}
    return {
        "create_time": str(workload["create_time"]),
        "runtime_seconds": int((generated_at - created_at).total_seconds()),
    }


def _format_runtime(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        return "-"
    total_minutes = int(seconds) // 60
    if total_minutes < 1:
        return "<1m"
    if total_minutes < 60:
        return f"{total_minutes}m"
    total_hours, minutes = divmod(total_minutes, 60)
    if total_hours < 24:
        return f"{total_hours}h {minutes:02d}m"
    days, hours = divmod(total_hours, 24)
    return f"{days}d {hours:02d}h"


def _percent_summary(values: Iterable[Any]) -> dict[str, float | None]:
    finite = [float(value) for value in values
              if isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(float(value))]
    if not finite:
        return {"avg_pct": None, "min_pct": None, "max_pct": None}
    return {
        "avg_pct": round(sum(finite) / len(finite), 2),
        "min_pct": round(min(finite), 2),
        "max_pct": round(max(finite), 2),
    }


def _summarize_gpu_utilization(
    gpus: Iterable[dict[str, Any]], allocated_gpu_count: int | float
) -> dict[str, Any]:
    rows = list(gpus)
    compute = _percent_summary(row.get("gpu_compute_util_pct") for row in rows)
    memory = _percent_summary(row.get("gpu_memory_util_pct") for row in rows)
    return {
        "allocated_gpu_count": int(allocated_gpu_count),
        "reported_gpu_count": sum(
            row.get("gpu_compute_util_pct") is not None
            and row.get("gpu_memory_util_pct") is not None
            for row in rows
        ),
        "gpu_compute_util_avg_pct": compute["avg_pct"],
        "gpu_compute_util_min_pct": compute["min_pct"],
        "gpu_compute_util_max_pct": compute["max_pct"],
        "gpu_memory_util_avg_pct": memory["avg_pct"],
        "gpu_memory_util_min_pct": memory["min_pct"],
        "gpu_memory_util_max_pct": memory["max_pct"],
    }


def _format_utilization(summary: dict[str, Any] | None, prefix: str) -> str:
    summary = summary or {}
    average = summary.get(f"{prefix}_avg_pct")
    minimum = summary.get(f"{prefix}_min_pct")
    maximum = summary.get(f"{prefix}_max_pct")
    if average is None:
        return "-"
    result = f"{float(average):.1f}%"
    if minimum is not None and maximum is not None and not math.isclose(
        float(minimum), float(maximum), abs_tol=0.05
    ):
        result += f" [{float(minimum):.1f}–{float(maximum):.1f}]"
    allocated = int(summary.get("allocated_gpu_count") or 0)
    reported = int(summary.get("reported_gpu_count") or 0)
    if allocated and reported < allocated:
        result += f" ({reported}/{allocated})"
    return result


def _gpu_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    device = str(row.get("device_index") or "")
    device_key: tuple[int, int | str] = (
        (0, int(device)) if device.isdigit() else (1, device)
    )
    return (
        str(row.get("node") or ""), device_key,
        str(row.get("gpu_uuid") or ""),
    )


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
            "id": str(raw.get("id") or ""),
            "host_ip": str(raw.get("host_ip") or ""),
            "jobs": {},
            "unattributed": {"gpu": 0, "cpu": 0, "memory_gib": 0},
            "attribution_excess": {"gpu": 0, "cpu": 0, "memory_gib": 0},
        }
    return nodes


def _fits(node: dict[str, Any], target: Target, stopped: set[str]) -> bool:
    excess = node.get("attribution_excess") or {}
    if float(excess.get("gpu", 0)) > 0:
        return False
    if target.cpus is not None and float(excess.get("cpu", 0)) > 0:
        return False
    if target.memory_gib is not None and float(excess.get("memory_gib", 0)) > 0:
        return False
    released = {"gpu": 0.0, "cpu": 0.0, "memory_gib": 0.0}
    for job_id, placement in node["jobs"].items():
        if job_id in stopped:
            for key in released:
                released[key] += float(placement.get(key, 0))
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
        "workload_count": len(job_ids),
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
    deadline: float,
    clock: Callable[[], float],
    stats: dict[str, Any],
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
        for seed in unique_seeds.values():
            selected = set(seed)
            while clock() < deadline:
                candidate = _candidate_from_jobs(selected, nodes, jobs, target)
                stats["states_examined"] += 1
                if len(candidate["freed_nodes"]) >= target.nodes:
                    results.append(_prune_candidate(candidate, nodes, jobs, target, key))
                    break
                expansions = []
                for addition in unique_additions:
                    expanded = selected | addition
                    if expanded == selected:
                        continue
                    proposal = _candidate_from_jobs(expanded, nodes, jobs, target)
                    stats["states_examined"] += 1
                    gained = len(proposal["freed_nodes"]) - len(candidate["freed_nodes"])
                    expansions.append((-gained, key(proposal), tuple(sorted(expanded)), expanded))
                    if clock() >= deadline:
                        break
                if not expansions:
                    break
                selected = set(min(expansions)[3])
            if clock() >= deadline:
                break
        if clock() >= deadline:
            break
    return results


def _rank_candidates(
    candidates: Iterable[dict[str, Any]],
    strategy: str,
    key: Callable[[dict[str, Any]], tuple[Any, ...]],
    alternatives: int,
    optimality: str,
) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for candidate in candidates:
        signature = tuple(candidate["jobs"])
        previous = unique.get(signature)
        if previous is None or key(candidate) < key(previous):
            unique[signature] = candidate
    ranked = sorted(unique.values(), key=key)[:alternatives]
    if not ranked:
        return []
    primary_field = {
        "min-gpu": "gpus", "min-workloads": "workload_count", "min-users": "users"
    }[strategy]
    best_cost = int(ranked[0][primary_field])
    return [
        {
            "strategy": strategy,
            "rank": rank,
            "primary_cost": int(candidate[primary_field]),
            "delta_from_best": int(candidate[primary_field]) - best_cost,
            "optimality": optimality,
            **candidate,
        }
        for rank, candidate in enumerate(ranked, 1)
    ]


def solve_candidates(
    nodes: dict[str, dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    target: Target,
    *,
    candidate_scope: str = "fragmented",
    alternatives: int = 3,
    search_seconds: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    search_stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    started = clock()
    stats = search_stats if search_stats is not None else {}
    stats.update({
        "search_budget_seconds": search_seconds,
        "search_elapsed_seconds": 0.0,
        "estimated_states": 0,
        "states_examined": 0,
        "switch_reason": "not-needed",
    })
    already = [name for name, node in nodes.items() if _fits(node, target, set())]
    if len(already) >= target.nodes:
        stats["search_elapsed_seconds"] = round(clock() - started, 6)
        return [], "not-needed"

    fragmented = [
        name
        for name, node in nodes.items()
        if node["jobs"] and node["allocated_gpu"] < node["total_gpu"]
        and (
            node["allocated_gpu"] > 0
            or (target.cpus is not None and node["allocated_cpu"] > 0 and not _fits(node, target, set()))
            or (target.memory_gib is not None and node["allocated_memory_gib"] > 0 and not _fits(node, target, set()))
        )
    ]
    full = [
        name
        for name, node in nodes.items()
        if node["jobs"] and node["allocated_gpu"] > 0 and node["total_gpu"] > 0
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
        stats["switch_reason"] = "no-eligible-candidates"
        stats["search_elapsed_seconds"] = round(clock() - started, 6)
        return [], "exact"

    max_group_size = min(need, len(eligible))
    state_count = sum(
        math.comb(len(eligible), size) for size in range(1, max_group_size + 1)
    )
    stats["estimated_states"] = state_count

    keys: list[tuple[str, Callable[[dict[str, Any]], tuple[Any, ...]]]] = [
        ("min-gpu", lambda c: (c["gpus"], c["job_count"], c["users"], c["jobs"])),
        ("min-workloads", lambda c: (c["workload_count"], c["gpus"], c["users"], c["jobs"])),
        ("min-users", lambda c: (c["users"], c["gpus"], c["job_count"], c["jobs"])),
    ]
    top_by_strategy: dict[str, dict[tuple[str, ...], dict[str, Any]]] = {
        strategy: {} for strategy, _ in keys
    }

    def retain(candidate: dict[str, Any]) -> None:
        for strategy, key in keys:
            pruned = _prune_candidate(candidate, nodes, jobs, target, key)
            bucket = top_by_strategy[strategy]
            bucket[tuple(pruned["jobs"])] = pruned
            if len(bucket) > alternatives:
                keep = sorted(bucket.values(), key=key)[:alternatives]
                top_by_strategy[strategy] = {tuple(item["jobs"]): item for item in keep}

    exact_deadline = started + search_seconds * 0.8
    final_deadline = started + search_seconds
    optimality = "exact"
    stats["switch_reason"] = "completed"
    stop_exact = False
    for size in range(1, max_group_size + 1):
        for group in itertools.combinations(eligible, size):
            candidate = _candidate(group, nodes, jobs, target)
            stats["states_examined"] += 1
            if len(candidate["freed_nodes"]) >= target.nodes:
                retain(candidate)
            elapsed = clock() - started
            if state_count > CALIBRATION_STATES and stats["states_examined"] == CALIBRATION_STATES:
                throughput = stats["states_examined"] / max(elapsed, 1e-9)
                estimated_total = state_count / throughput
                if estimated_total > search_seconds * 0.8:
                    stats["switch_reason"] = "estimated-time"
                    stop_exact = True
                    break
            if clock() >= exact_deadline:
                stats["switch_reason"] = "exact-deadline"
                stop_exact = True
                break
        if stop_exact:
            break
    if stop_exact:
        optimality = "heuristic"
        for candidate in _heuristic_candidates(
            eligible, nodes, jobs, target, [key for _, key in keys],
            final_deadline, clock, stats,
        ):
            retain(candidate)

    if not any(top_by_strategy.values()):
        stats["search_elapsed_seconds"] = round(clock() - started, 6)
        return [], optimality
    selected: list[dict[str, Any]] = []
    for strategy, key in keys:
        selected.extend(_rank_candidates(
            top_by_strategy[strategy].values(), strategy, key, alternatives, optimality
        ))
    stats["search_elapsed_seconds"] = round(clock() - started, 6)
    return selected, optimality


def _resource_number(value: Any, *, memory: bool = False) -> float:
    """Parse SSP pod CPU/memory values, including mCPU and binary units."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(m|[KMGTPE]i?B?)?\s*", str(value), re.I)
    if not match:
        raise ValueError(f"unsupported resource value: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if not memory:
        return number / 1000 if unit == "m" else number
    factors = {
        "": 1 / 1024**3, "b": 1 / 1024**3,
        "k": 1000 / 1024**3, "kb": 1000 / 1024**3,
        "m": 1000**2 / 1024**3, "mb": 1000**2 / 1024**3,
        "g": 1000**3 / 1024**3, "gb": 1000**3 / 1024**3,
        "t": 1000**4 / 1024**3, "tb": 1000**4 / 1024**3,
        "ki": 1024 / 1024**3, "kib": 1024 / 1024**3,
        "mi": 1024**2 / 1024**3, "mib": 1024**2 / 1024**3,
        "gi": 1, "gib": 1, "ti": 1024, "tib": 1024,
    }
    if unit not in factors:
        raise ValueError(f"unsupported memory unit: {value!r}")
    return number * factors[unit]


def _clean_number(value: float) -> int | float:
    return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else round(value, 6)


def _node_signature(raw_nodes: Iterable[dict[str, Any]]) -> tuple[Any, ...]:
    normalized = normalize_nodes(raw_nodes)
    return tuple(
        (name, node["state"], node["allocated_gpu"], node["allocated_cpu"],
         node["allocated_memory_gib"])
        for name, node in sorted(normalized.items())
    )


def _list_node_pods(cluster: Any, cluster_name: str, queue: str, node_id: str) -> list[dict[str, Any]]:
    path = (
        f"/subscriptions/{cluster.client.subscription}/"
        f"resourceGroups/{cluster.client.resource_group}/"
        f"regions/{cluster.client.region}/clusters/{cluster_name}/pods"
    )
    pods: list[dict[str, Any]] = []
    skip = 0
    while True:
        response = cluster.client._make_management_request(
            "GET", path,
            params={"node_id": node_id, "queue_name": queue,
                    "page_size": 100, "skip": skip},
        )
        page = response.get("pods") or []
        pods.extend(page)
        total = int(response.get("total_size") or len(pods))
        if not page or len(pods) >= total:
            return pods
        skip += len(page)


def _escape_prometheus_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _query_gpu_utilization(
    cluster: Any, queue: str, cluster_name: str, minutes: int
) -> list[dict[str, Any]]:
    """Fetch per-GPU compute and memory utilization in one Prometheus request."""
    from clusterx.launcher.ssp.prometheus_stats import build_cache_path, get_query_token

    labels = {
        "label_resource_compute_sensecore_cn_workspace_name": cluster.cfg["workspace"],
        "label_resource_compute_sensecore_cn_cluster_name": cluster_name,
        "label_resource_compute_sensecore_cn_queue_name": queue,
    }
    selector = ",".join(
        f'{key}="{_escape_prometheus_label(value)}"' for key, value in labels.items()
    )
    compute = (
        "label_replace("
        f"avg_over_time(lepton__ssp__gpu_util{{{selector}}}[{minutes}m]),"
        f'"queue_plan_metric","{GPU_COMPUTE_UTIL}","__name__",".*")'
    )
    memory = (
        "label_replace(100 * "
        f"avg_over_time(lepton__ssp__gpu_memory_used__MiB{{{selector}}}[{minutes}m]) / "
        f"avg_over_time(lepton__ssp__gpu_memory_total__MiB{{{selector}}}[{minutes}m]),"
        f'"queue_plan_metric","{GPU_MEMORY_UTIL}","__name__",".*")'
    )
    cache_path = build_cache_path(
        cluster.cfg["subscription"], cluster.cfg["resource_group"],
        cluster.cfg["region"], cluster.cfg["workspace"],
    )
    token = get_query_token(cluster.client, cache_path=cache_path)["query_token"]
    promql = f"({compute}) or ({memory})"
    try:
        response = cluster.client.query_prometheus(token, promql)
    except Exception as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code not in (401, 403):
            raise
        cache_path.unlink(missing_ok=True)
        token = get_query_token(cluster.client, cache_path=cache_path)["query_token"]
        response = cluster.client.query_prometheus(token, promql)
    if response.get("status") not in (None, "success"):
        error = response.get("error") or response.get("errorType") or response
        raise RuntimeError(f"Prometheus utilization query failed: {error}")
    return list((response.get("data") or {}).get("result") or [])


def _attach_gpu_utilization(
    jobs: dict[str, dict[str, Any]],
    series: Iterable[dict[str, Any]],
    hostname_to_node: dict[str, str],
) -> list[str]:
    """Attach current, unambiguous per-GPU telemetry to workload records."""
    expected: dict[tuple[str, str, str], int] = {}
    for workload_id, workload in jobs.items():
        workload["gpus"] = []
        for placement in workload.get("placements") or []:
            gpu_count = int(placement.get("gpu") or 0)
            if gpu_count <= 0:
                continue
            key = (
                workload_id,
                str(placement.get("node") or ""),
                str(placement.get("pod") or ""),
            )
            expected[key] = expected.get(key, 0) + gpu_count

    records: dict[tuple[tuple[str, str, str], str], dict[str, Any]] = {}
    ambiguous_pods: set[tuple[str, str, str]] = set()
    for item in series:
        labels = item.get("metric") or {}
        workload_id = str(
            labels.get("label_resource_compute_sensecore_cn_workload_uid") or ""
        )
        node_name = hostname_to_node.get(str(labels.get("Hostname") or ""), "")
        pod_name = str(labels.get("exported_pod") or labels.get("pod") or "")
        pod_key = (workload_id, node_name, pod_name)
        if pod_key not in expected:
            continue
        gpu_uuid = str(labels.get("UUID") or "")
        device_index = str(labels.get("gpu") or labels.get("device") or "")
        identity = gpu_uuid or f"device:{device_index}"
        if not identity or identity == "device:":
            ambiguous_pods.add(pod_key)
            continue
        metric_name = str(labels.get("queue_plan_metric") or "")
        field = {
            GPU_COMPUTE_UTIL: "gpu_compute_util_pct",
            GPU_MEMORY_UTIL: "gpu_memory_util_pct",
        }.get(metric_name)
        if field is None:
            continue
        value = item.get("value") or []
        try:
            parsed = float(value[1])
        except (IndexError, TypeError, ValueError):
            parsed = math.nan
        if not math.isfinite(parsed):
            continue
        record = records.setdefault((pod_key, identity), {
            "node": node_name,
            "pod": pod_name,
            "device_index": device_index or None,
            "gpu_uuid": gpu_uuid or None,
            "gpu_compute_util_pct": None,
            "gpu_memory_util_pct": None,
        })
        if record[field] is not None:
            ambiguous_pods.add(pod_key)
            continue
        record[field] = round(parsed, 2)

    by_pod: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (pod_key, _), record in records.items():
        by_pod.setdefault(pod_key, []).append(record)
    for pod_key, rows in by_pod.items():
        if len(rows) > expected[pod_key]:
            ambiguous_pods.add(pod_key)

    for pod_key, rows in by_pod.items():
        if pod_key in ambiguous_pods:
            continue
        workload_id = pod_key[0]
        jobs[workload_id]["gpus"].extend(sorted(rows, key=_gpu_sort_key))

    allocated = 0
    reported = 0
    for workload in jobs.values():
        workload["gpus"].sort(key=_gpu_sort_key)
        allocated_count = int(workload.get("total_gpu") or 0)
        summary = _summarize_gpu_utilization(workload["gpus"], allocated_count)
        workload["gpu_utilization"] = summary
        allocated += allocated_count
        reported += int(summary["reported_gpu_count"])

    warnings: list[str] = []
    if ambiguous_pods:
        warnings.append(
            f"Per-GPU utilization ignored {len(ambiguous_pods)} ambiguous Pod mappings"
        )
    if reported < allocated:
        warnings.append(
            f"Per-GPU utilization covered {reported}/{allocated} attributed GPUs; "
            "missing values are shown as unavailable"
        )
    return warnings


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

    pod_map: dict[str, list[dict[str, Any]]] = {}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(_list_node_pods, cluster, cluster_name, queue, node["id"]): name
            for name, node in nodes.items()
            if (node["allocated_gpu"] or node["allocated_cpu"] or node["allocated_memory_gib"])
        }
        for future in as_completed(futures):
            node_name = futures[future]
            try:
                pod_map[node_name] = future.result()
            except Exception:
                failures.append(node_name)
    if failures:
        raise RuntimeError(f"pod workload mapping failed for {len(failures)} occupied nodes")

    after_nodes = cluster.client.list_queue_nodes(
        cluster=cluster_name, queue=queue, page_size=100, is_bound=True
    ).get("nodes") or []
    if _node_signature(nodes_list) != _node_signature(after_nodes):
        raise RuntimeError("queue node allocation changed during collection; retry the command")

    jobs: dict[str, dict[str, Any]] = {}
    hostname_to_node: dict[str, str] = {}
    for node_name, node in nodes.items():
        if node["host_ip"]:
            hostname_to_node["host-" + node["host_ip"].replace(".", "-")] = node_name
        attributed = {"gpu": 0.0, "cpu": 0.0, "memory_gib": 0.0}
        for pod in pod_map.get(node_name, []):
            workload = pod.get("workload") or {}
            workload_id = str(workload.get("uid") or workload.get("id") or "")
            if not workload_id:
                raise RuntimeError(f"pod workload identity is missing on node {node_name}")
            resource = pod.get("resource") or {}
            placement = {
                "gpu": _clean_number(float(resource.get("accelerate_device_count") or 0)),
                "cpu": _clean_number(_resource_number(resource.get("cpu"))),
                "memory_gib": _clean_number(_resource_number(resource.get("memory"), memory=True)),
            }
            for key in attributed:
                attributed[key] += float(placement[key])
            existing = node["jobs"].setdefault(
                workload_id, {"gpu": 0, "cpu": 0, "memory_gib": 0}
            )
            for key in existing:
                existing[key] = _clean_number(float(existing[key]) + float(placement[key]))
            ownership = pod.get("ownership") or {}
            workspace = pod.get("workspace") or {}
            item = jobs.setdefault(workload_id, {
                "job_id": workload_id,
                "job_name": str(workload.get("display_name") or workload.get("name") or workload_id),
                "user": str(ownership.get("creator_name") or "unknown"),
                "type": str(workload.get("type") or "unknown"),
                "workspace": str(workspace.get("name") or ""),
                "create_time": None,
                "actionable": True,
                "total_gpu": 0,
                "placements": [],
            })
            pod_create_time = pod.get("create_time")
            parsed_pod_time = _parse_utc_timestamp(pod_create_time)
            parsed_item_time = _parse_utc_timestamp(item.get("create_time"))
            if parsed_pod_time is not None and (
                parsed_item_time is None or parsed_pod_time < parsed_item_time
            ):
                item["create_time"] = str(pod_create_time)
            item["placements"].append({"node": node_name, "pod": str(pod.get("name") or ""), **placement})

        allocated = {
            "gpu": float(node["allocated_gpu"]), "cpu": float(node["allocated_cpu"]),
            "memory_gib": float(node["allocated_memory_gib"]),
        }
        for key in allocated:
            delta = allocated[key] - attributed[key]
            if delta > 1e-6:
                node["unattributed"][key] = _clean_number(delta)
            elif delta < -1e-6:
                node["attribution_excess"][key] = _clean_number(-delta)
                warnings.append(
                    f"Pod-attributed {key} exceeds node allocation on {node_name}; "
                    "the node is excluded when that resource is required"
                )
    for item in jobs.values():
        item["total_gpu"] = _clean_number(sum(float(p["gpu"]) for p in item["placements"]))

    if any(float(item["total_gpu"]) > 0 for item in jobs.values()):
        try:
            utilization_series = _query_gpu_utilization(
                cluster, queue, cluster_name, minutes
            )
            warnings.extend(_attach_gpu_utilization(
                jobs, utilization_series, hostname_to_node
            ))
        except Exception:
            _attach_gpu_utilization(jobs, [], hostname_to_node)
            warnings.append(
                "Per-GPU utilization was unavailable; allocation analysis is still valid"
            )
    else:
        _attach_gpu_utilization(jobs, [], hostname_to_node)

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

    return {
        "nodes": nodes,
        "jobs": jobs,
        "gpu_utilization_window_minutes": minutes,
    }, warnings


def build_report(
    snapshot: dict[str, Any], target: Target, queue: str, cluster_name: str,
    candidate_scope: str = "fragmented",
    alternatives: int = 3,
    search_seconds: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    nodes = snapshot["nodes"]
    jobs = snapshot["jobs"]
    generated_at = (now or (lambda: datetime.now(timezone.utc)))()
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)
    utilization_window = int(snapshot.get("gpu_utilization_window_minutes") or 5)
    for workload in jobs.values():
        workload.setdefault("gpus", [])
        workload.setdefault(
            "gpu_utilization",
            _summarize_gpu_utilization(
                workload["gpus"], workload.get("total_gpu") or 0
            ),
        )
    search_stats: dict[str, Any] = {}
    suggestions, optimality = solve_candidates(
        nodes, jobs, target, candidate_scope=candidate_scope,
        alternatives=alternatives, search_seconds=search_seconds,
        clock=clock, search_stats=search_stats,
    )
    for suggestion in suggestions:
        suggestion["workloads"] = suggestion.pop("jobs")
        suggestion.pop("job_count", None)
        suggestion["workload_details"] = [
            {
                "workload_id": workload_id,
                "workload_name": jobs[workload_id]["job_name"],
                "type": jobs[workload_id].get("type", "trainingJob"),
                "actionable": jobs[workload_id].get("actionable", True),
                "user": jobs[workload_id]["user"],
                "workspace": jobs[workload_id].get("workspace", ""),
                **_runtime_fields(jobs[workload_id], generated_at),
                "gpu_utilization": jobs[workload_id]["gpu_utilization"],
                "total_gpu": jobs[workload_id]["total_gpu"],
                "placements": jobs[workload_id]["placements"],
            }
            for workload_id in suggestion["workloads"]
        ]
    free_nodes = sum(_fits(node, target, set()) for node in nodes.values())
    fragmented = [
        {
            "node": name,
            "allocated_gpu": node["allocated_gpu"],
            "total_gpu": node["total_gpu"],
            "free_gpu": node["total_gpu"] - node["allocated_gpu"],
            "workloads": [
                {
                    "workload_id": workload_id,
                    "workload_name": jobs[workload_id]["job_name"],
                    "type": jobs[workload_id].get("type", "trainingJob"),
                    "actionable": jobs[workload_id].get("actionable", True),
                    "user": jobs[workload_id]["user"],
                    "workspace": jobs[workload_id].get("workspace", ""),
                    **_runtime_fields(jobs[workload_id], generated_at),
                    "gpu_utilization": _summarize_gpu_utilization(
                        [
                            gpu for gpu in jobs[workload_id]["gpus"]
                            if gpu.get("node") == name
                        ],
                        node["jobs"][workload_id].get("gpu") or 0,
                    ),
                    **node["jobs"][workload_id],
                }
                for workload_id in sorted(node["jobs"])
            ],
            "unattributed": node.get("unattributed", {}),
            "attribution_excess": node.get("attribution_excess", {}),
            "metrics": node.get("metrics", {}),
        }
        for name, node in sorted(nodes.items())
        if (
            0 < node["allocated_gpu"] < node["total_gpu"]
            or (
                node["allocated_gpu"] < node["total_gpu"]
                and not _fits(node, target, set())
                and (
                    (target.cpus is not None and node["allocated_cpu"] > 0)
                    or (target.memory_gib is not None and node["allocated_memory_gib"] > 0)
                )
            )
        )
    ]
    full_nodes = sum(
        node["total_gpu"] > 0 and node["allocated_gpu"] >= node["total_gpu"]
        for node in nodes.values()
    )
    type_counts: dict[str, int] = {}
    for workload in jobs.values():
        kind = str(workload.get("type", "unknown"))
        type_counts[kind] = type_counts.get(kind, 0) + 1
    utilization_workloads = [
        {
            "workload_id": workload_id,
            "workload_name": jobs[workload_id]["job_name"],
            "type": jobs[workload_id].get("type", "unknown"),
            "user": jobs[workload_id]["user"],
            "workspace": jobs[workload_id].get("workspace", ""),
            **_runtime_fields(jobs[workload_id], generated_at),
            "total_gpu": jobs[workload_id]["total_gpu"],
            **jobs[workload_id]["gpu_utilization"],
            "gpus": jobs[workload_id]["gpus"],
        }
        for workload_id in sorted(
            jobs,
            key=lambda item: (
                str(jobs[item].get("user") or ""),
                str(jobs[item].get("job_name") or ""),
                item,
            ),
        )
        if float(jobs[workload_id].get("total_gpu") or 0) > 0
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
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
            "running_workloads": len(jobs),
            "workload_counts": dict(sorted(type_counts.items())),
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
            **search_stats,
        },
        "gpu_utilization": {
            "window_minutes": utilization_window,
            "allocated_gpu_count": sum(
                int(item["allocated_gpu_count"]) for item in utilization_workloads
            ),
            "reported_gpu_count": sum(
                int(item["reported_gpu_count"]) for item in utilization_workloads
            ),
            "workloads": utilization_workloads,
        },
        "fragmented_nodes": fragmented,
        "suggestions": suggestions,
        "warnings": [],
    }


def render_text(report: dict[str, Any], *, show_gpu_details: bool = False) -> str:
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
        (
            "GPU telemetry: "
            f"{report['gpu_utilization']['reported_gpu_count']}/"
            f"{report['gpu_utilization']['allocated_gpu_count']} attributed GPUs; "
            f"window={report['gpu_utilization']['window_minutes']}m"
        ),
        (
            "Search: "
            f"{report['analysis']['optimality']}; "
            f"{report['analysis']['search_elapsed_seconds']:.3f}s / "
            f"{report['analysis']['search_budget_seconds']:g}s; "
            f"{report['analysis']['states_examined']} / "
            f"{report['analysis']['estimated_states']} states; "
            f"switch={report['analysis']['switch_reason']}"
        ),
    ]
    lines.extend(f"Warning: {warning}" for warning in report.get("warnings", []))
    fragments = report.get("fragmented_nodes") or []
    if fragments:
        lines.append("")
        lines.append("Fragmented workload utilization:")
        for fragment in fragments:
            for workload in fragment.get("workloads") or []:
                utilization = workload.get("gpu_utilization") or {}
                lines.append(
                    f"  - {fragment['node']} · {workload['user']}: "
                    f"{workload['workload_name']} [{workload['type']}] · "
                    f"GPU util {_format_utilization(utilization, 'gpu_compute_util')} · "
                    f"GPU mem {_format_utilization(utilization, 'gpu_memory_util')}"
                )
    if show_gpu_details:
        lines.append("")
        lines.append(
            f"Per-GPU utilization ({report['gpu_utilization']['window_minutes']}m):"
        )
        gpu_rows = 0
        for workload in report["gpu_utilization"].get("workloads") or []:
            for gpu in workload.get("gpus") or []:
                gpu_rows += 1
                lines.append(
                    f"  - {workload['user']}: {workload['workload_name']} "
                    f"[{workload['type']}] · {gpu['node']} GPU "
                    f"{gpu.get('device_index') or '?'} · "
                    f"GPU util {_format_utilization({'gpu_compute_util_avg_pct': gpu.get('gpu_compute_util_pct')}, 'gpu_compute_util')} · "
                    f"GPU mem {_format_utilization({'gpu_memory_util_avg_pct': gpu.get('gpu_memory_util_pct')}, 'gpu_memory_util')}"
                )
        if not gpu_rows:
            lines.append("  No per-GPU telemetry is available.")
    if not report["analysis"]["needs_repacking"]:
        lines.append("Result: enough nodes are already schedulable; no workload coordination is needed.")
        return "\n".join(lines) + "\n"
    if not report["suggestions"]:
        lines.append("Result: no eligible candidate suggestion satisfies the target.")
        return "\n".join(lines) + "\n"
    labels = {"min-gpu": "coordinated GPU", "min-workloads": "workloads", "min-users": "users"}
    for index, suggestion in enumerate(report["suggestions"], 1):
        display_strategy = suggestion.get("display_strategy", suggestion["strategy"])
        if suggestion["rank"] == 1:
            qualifier = "minimum" if suggestion["optimality"] == "exact" else "lowest found"
        else:
            qualifier = "alternative" if suggestion["optimality"] == "exact" else "alternative found"
        lines.extend(
            [
                "",
                f"Plan {index} ({display_strategy} rank {suggestion['rank']}, {qualifier}, {suggestion['optimality']}):",
                f"  Coordination candidate: {suggestion['gpus']} GPU, {_count_label(suggestion['workload_count'], 'workload')}, {_count_label(suggestion['users'], 'user')}",
                f"  Freed nodes ({len(suggestion['freed_nodes'])}): {', '.join(suggestion['freed_nodes'])}",
            ]
        )
        if suggestion["rank"] > 1:
            delta = suggestion["delta_from_best"]
            comparison = (
                f"tied on {labels[display_strategy]}" if delta == 0
                else f"+{delta} {labels[display_strategy]} vs best"
            )
            lines.append(f"  Comparison: {comparison}")
        for workload in suggestion["workload_details"]:
            utilization = workload.get("gpu_utilization") or {}
            lines.append(
                f"  - {workload['user']}: {workload['workload_name']} "
                f"[{workload['type']}] ({workload['total_gpu']} GPU total, "
                f"running {_format_runtime(workload.get('runtime_seconds'))}, "
                f"GPU util {_format_utilization(utilization, 'gpu_compute_util')}, "
                f"GPU mem {_format_utilization(utilization, 'gpu_memory_util')})"
            )
            for placement in workload["placements"]:
                lines.append(
                    f"      {placement['node']}: {placement.get('gpu', 0)} GPU, "
                    f"{placement.get('cpu', 0)} CPU, "
                    f"{placement.get('memory_gib', 0)} GiB memory"
                )
    lines.append("\nRead-only report: no workload was stopped or modified.")
    return "\n".join(lines) + "\n"


def render_rich(
    report: dict[str, Any], *, console: Any | None = None,
    show_gpu_details: bool = False,
) -> bool:
    """Render a colored terminal report; return False when Rich is unavailable."""
    try:
        from rich import box
        from rich.console import Console
        from rich.console import Group
        from rich.panel import Panel
        from rich.rule import Rule
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
    overview = Table.grid(expand=True, padding=(0, 2))
    for _ in range(4):
        overview.add_column(ratio=1, overflow="fold")
    overview.add_row(
        "[bold cyan]Queue[/]", str(report["queue"]),
        "[bold cyan]Target[/]", f"{target['nodes']} × {target['gpus_per_node']} GPU",
    )
    overview.add_row(
        "[bold cyan]Candidates[/]", str(report["analysis"]["candidate_scope"]),
        "[bold cyan]Status[/]", status,
    )
    overview.add_row(
        "[bold cyan]GPU[/]",
        f"[red]{summary['allocated_gpu']} allocated[/] · [green]{summary['free_gpu']} free[/] · {summary['total_gpu']} total",
        "[bold cyan]Nodes[/]",
        f"{summary['total_nodes']} total · [green]{schedulable} schedulable[/] · [yellow]{summary['fragmented_nodes']} fragmented[/] · {summary['full_nodes']} full",
    )
    gpu_telemetry = report["gpu_utilization"]
    overview.add_row(
        "[bold cyan]Telemetry[/]",
        f"{gpu_telemetry['reported_gpu_count']}/{gpu_telemetry['allocated_gpu_count']} attributed GPUs",
        "[bold cyan]Window[/]",
        f"{gpu_telemetry['window_minutes']}m",
    )
    console.print(Panel(overview, title="[bold]ClusterX Queue Packing[/]", border_style="cyan"))
    for warning in report.get("warnings", []):
        console.print(f"[bold yellow]Warning:[/] {warning}")

    analysis = report["analysis"]
    search = Table.grid(expand=True, padding=(0, 2))
    search.add_column(style="bold cyan")
    search.add_column(ratio=1)
    search.add_column(style="bold cyan")
    search.add_column(ratio=1)
    search.add_row(
        "Mode", f"[yellow]{analysis['optimality']}[/]" if analysis["optimality"] == "heuristic" else f"[green]{analysis['optimality']}[/]",
        "Switch", str(analysis["switch_reason"]),
    )
    search.add_row(
        "Time", f"{analysis['search_elapsed_seconds']:.3f}s / {analysis['search_budget_seconds']:g}s",
        "States", f"{analysis['states_examined']} examined / {analysis['estimated_states']} estimated",
    )
    console.print(Panel(search, title="[bold]Search diagnostics[/]", border_style="blue"))

    fragments = report.get("fragmented_nodes") or []
    if fragments:
        table = Table(
            title="Fragmented nodes",
            box=box.ROUNDED,
            header_style="bold magenta",
            row_styles=("", "dim"),
        )
        table.add_column("Node", style="cyan", overflow="fold")
        table.add_column("GPU", justify="right")
        table.add_column("Free", justify="right", style="green")
        table.add_column("Node util", justify="right")
        table.add_column("User", overflow="fold")
        table.add_column("Workload", overflow="fold")
        table.add_column("Running", justify="right")
        table.add_column("GPU util", justify="right")
        table.add_column("GPU mem", justify="right")
        table.add_column("GPU", justify="right")
        for fragment in fragments:
            jobs = list(fragment.get("workloads") or [])
            unattributed = fragment.get("unattributed") or {}
            if any(float(unattributed.get(key, 0)) > 0 for key in ("gpu", "cpu", "memory_gib")):
                jobs.append({
                    "user": "-", "workload_name": "unattributed", "type": "unknown",
                    **unattributed,
                })
            gpu_util = (fragment.get("metrics") or {}).get("gpu-util")
            for row, item in enumerate(jobs or [{}]):
                table.add_row(
                    str(fragment["node"]) if row == 0 else "",
                    f"{fragment['allocated_gpu']}/{fragment['total_gpu']}" if row == 0 else "",
                    str(fragment["free_gpu"]) if row == 0 else "",
                    ("-" if gpu_util is None else f"{gpu_util:.1f}%") if row == 0 else "",
                    str(item.get("user", "-")),
                    f"{item.get('workload_name', '-')} [{item.get('type', 'unknown')}]",
                    _format_runtime(item.get("runtime_seconds")),
                    _format_utilization(item.get("gpu_utilization"), "gpu_compute_util"),
                    _format_utilization(item.get("gpu_utilization"), "gpu_memory_util"),
                    str(item.get("gpu", 0)),
                )
        console.print(table)

    if show_gpu_details:
        gpu_table = Table(
            title=f"Per-GPU utilization · {gpu_telemetry['window_minutes']}m",
            box=box.ROUNDED,
            header_style="bold cyan",
            row_styles=("", "dim"),
        )
        gpu_table.add_column("User", overflow="fold")
        gpu_table.add_column("Workload", overflow="fold", ratio=2)
        gpu_table.add_column("Type", overflow="fold")
        gpu_table.add_column("Node", overflow="fold")
        gpu_table.add_column("GPU", justify="right")
        gpu_table.add_column("GPU util", justify="right")
        gpu_table.add_column("GPU mem", justify="right")
        detail_count = 0
        for workload in gpu_telemetry.get("workloads") or []:
            for gpu in workload.get("gpus") or []:
                detail_count += 1
                gpu_table.add_row(
                    str(workload["user"]), str(workload["workload_name"]),
                    str(workload["type"]), str(gpu["node"]),
                    str(gpu.get("device_index") or "?"),
                    _format_utilization(
                        {"gpu_compute_util_avg_pct": gpu.get("gpu_compute_util_pct")},
                        "gpu_compute_util",
                    ),
                    _format_utilization(
                        {"gpu_memory_util_avg_pct": gpu.get("gpu_memory_util_pct")},
                        "gpu_memory_util",
                    ),
                )
        if detail_count:
            console.print(gpu_table)
        else:
            console.print("[yellow]No per-GPU telemetry is available.[/]")

    if target_met:
        console.print("[bold green]✓ Enough nodes are already schedulable; no pause is needed.[/]")
        return True
    suggestions = report.get("suggestions") or []
    if not suggestions:
        console.print("[bold red]No eligible candidate suggestion satisfies the target.[/]")
        return True

    labels = {"min-gpu": "Coordinated GPU", "min-workloads": "Workloads", "min-users": "Users"}
    colors = {"min-gpu": "cyan", "min-workloads": "blue", "min-users": "magenta"}
    plan_index = 0
    for strategy in ("min-gpu", "min-workloads", "min-users"):
        group = [item for item in suggestions if item["strategy"] == strategy]
        if not group:
            continue
        color = colors[strategy]
        console.print(Rule(f"[bold {color}]{strategy} · {labels[strategy]}[/]", style=color))
        for suggestion in group:
            plan_index += 1
            display_strategy = suggestion["strategy"]
            if suggestion["rank"] == 1:
                qualifier = "Minimum" if suggestion["optimality"] == "exact" else "Lowest found"
            else:
                qualifier = "Alternative" if suggestion["optimality"] == "exact" else "Alternative found"
            summary_grid = Table.grid(expand=True, padding=(0, 2))
            summary_grid.add_column(style="bold")
            summary_grid.add_column(ratio=1)
            summary_grid.add_row(
                "Candidate",
                f"[yellow]{suggestion['gpus']} GPU[/] · {_count_label(suggestion['workload_count'], 'workload')} · {_count_label(suggestion['users'], 'user')} · {len(suggestion['freed_nodes'])} nodes releasable",
            )
            if suggestion["rank"] > 1:
                delta = suggestion["delta_from_best"]
                comparison = f"Tied on {labels[display_strategy]}" if delta == 0 else f"+{delta} {labels[display_strategy]} versus best"
                summary_grid.add_row("Comparison", f"[magenta]{comparison}[/]")
            summary_grid.add_row("Freed nodes", "[green]" + ", ".join(suggestion["freed_nodes"]) + "[/]")

            workloads_table = Table(
                box=box.SIMPLE, expand=True, header_style=f"bold {color}"
            )
            workloads_table.add_column("Workload", overflow="fold", ratio=3)
            workloads_table.add_column("Utilization", overflow="fold", ratio=2)
            for workload in suggestion["workload_details"]:
                workloads_table.add_row(
                    f"{workload['user']} · {workload['workload_name']}\n"
                    f"{workload['type']} · Running "
                    f"{_format_runtime(workload.get('runtime_seconds'))} · "
                    f"{workload['total_gpu']} GPU",
                    "GPU " + _format_utilization(
                        workload.get("gpu_utilization"), "gpu_compute_util"
                    ) + "\nMem " + _format_utilization(
                        workload.get("gpu_utilization"), "gpu_memory_util"
                    ),
                )
            console.print(Panel(
                Group(summary_grid, workloads_table),
                title=f"[bold {color}]Plan {plan_index} · Rank {suggestion['rank']} · {qualifier} · {suggestion['optimality']}[/]",
                border_style=color,
            ))
    console.print(Panel("[bold green]Read-only report:[/] no workload was stopped or modified.", border_style="green"))
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
        choices=("all", "min-gpu", "min-workloads", "min-jobs", "min-users"),
        default="all",
        help="Suggestion strategy; min-jobs is a deprecated alias for min-workloads",
    )
    parser.add_argument(
        "--candidate-scope",
        choices=("fragmented", "full", "all"),
        default="fragmented",
        help="Nodes whose workloads may be coordination candidates (default: fragmented)",
    )
    parser.add_argument(
        "--alternatives", type=int, default=3,
        help="Maximum plans per strategy, including rank 1 (default: 3)",
    )
    parser.add_argument(
        "--search-seconds", type=float, default=10.0,
        help="Local solver time budget in seconds (default: 10)",
    )
    parser.add_argument(
        "--show-gpu-details", action="store_true",
        help="Expand per-GPU utilization in terminal output; JSON always includes it",
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
    if not 1 <= args.alternatives <= 10:
        parser.error("--alternatives must be between 1 and 10")
    if args.search_seconds <= 0:
        parser.error("--search-seconds must be positive")
    if args.strategy == "min-jobs":
        args.strategy = "min-workloads"
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
            if "queue node allocation changed" not in str(error):
                raise
            snapshot, warnings = collect_snapshot(
                cluster, str(queue), str(cluster_name), minutes=args.minutes
            )
            warnings.append("Node allocation changed during the first collection; report uses one retry")
        target = Target(args.nodes, args.gpus_per_node, args.cpus_per_node, args.memory_per_node_gib)
        report = build_report(
            snapshot, target, str(queue), str(cluster_name), args.candidate_scope,
            args.alternatives, args.search_seconds,
        )
        report["warnings"].extend(warnings)
        report["analysis"]["requested_strategy"] = args.strategy
        if args.strategy != "all":
            report["suggestions"] = [
                suggestion for suggestion in report["suggestions"]
                if args.strategy == suggestion["strategy"]
            ]
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload + "\n", encoding="utf-8")
        if args.as_json:
            sys.stdout.write(payload + "\n")
        elif not render_rich(report, show_gpu_details=args.show_gpu_details):
            sys.stdout.write(render_text(report, show_gpu_details=args.show_gpu_details))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(redact(f"queue analysis failed: {error}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
