from __future__ import annotations

from itertools import combinations
import time
from typing import Any, Iterable

from .models import PlanRequest


SCHEDULABLE_STATES = {"RUNNING", "IDLE", "MIXED", "running", "idle", "mixed"}


def _workload_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["workload_id"]): item for item in snapshot.get("workloads", [])}


def _node_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["node"]): item for item in snapshot.get("nodes", [])}


def _released(workload_ids: Iterable[str], workloads: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for workload_id in workload_ids:
        for placement in workloads[workload_id].get("placements", []):
            row = result.setdefault(str(placement["node"]), {"gpu": 0, "cpu": 0, "memory_gib": 0})
            for key in row:
                row[key] += float(placement.get(key) or 0)
    return result


def _fits(node: dict[str, Any], released: dict[str, float], target: dict[str, float | int]) -> bool:
    free_gpu = float(node.get("total_gpu") or 0) - float(node.get("allocated_gpu") or 0) + released.get("gpu", 0)
    if free_gpu < float(target["gpus_per_node"]):
        return False
    free_cpu = float(node.get("total_cpu") or 0) - float(node.get("allocated_cpu") or 0) + released.get("cpu", 0)
    if free_cpu < float(target["cpus_per_node"]):
        return False
    free_memory = float(node.get("total_memory_gib") or 0) - float(node.get("allocated_memory_gib") or 0) + released.get("memory_gib", 0)
    if free_memory < float(target["memory_per_node_gib"]):
        return False
    return True


def _eligible_node(
    node: dict[str, Any], scope: str, target: dict[str, float | int],
) -> bool:
    allocated = float(node.get("allocated_gpu") or 0)
    total = float(node.get("total_gpu") or 0)
    full = total > 0 and allocated >= total
    fragmented = 0 < allocated < total
    if not fragmented and allocated < total:
        fragmented = not _fits(node, {}, target) and (
            float(node.get("allocated_cpu") or 0) > 0
            or float(node.get("allocated_memory_gib") or 0) > 0
        )
    return fragmented if scope == "fragmented" else full if scope == "full" else fragmented or full


def _filter_workloads(
    snapshot: dict[str, Any], request: PlanRequest, eligible_nodes: set[str]
) -> dict[str, dict[str, Any]]:
    filters = request.filters
    group_findings = {
        str(item.get("group")): item.get("policy_findings") or []
        for item in snapshot.get("groups", [])
    }
    result: dict[str, dict[str, Any]] = {}
    for workload_id, workload in _workload_map(snapshot).items():
        user = str(workload.get("user") or "")
        group = str(workload.get("group") or "")
        kind = str(workload.get("type") or "unknown")
        placements = workload.get("placements") or []
        if not any(str(item.get("node")) in eligible_nodes for item in placements):
            continue
        if not bool(workload.get("planning_eligible", True)):
            continue
        if user in {"", "unknown"} or group == "unattributed":
            continue
        if filters.workload_types and kind not in filters.workload_types:
            continue
        if filters.groups and group not in filters.groups:
            continue
        if filters.users and user not in filters.users:
            continue
        if filters.workloads and workload_id not in filters.workloads:
            continue
        if workload_id in filters.exclude_workloads or user in filters.exclude_users:
            continue
        if filters.over_quota_only:
            group_state = next(
                (item.get("status") for item in snapshot.get("groups", []) if item.get("group") == group),
                None,
            )
            if group_state not in {"burst", "violation"}:
                continue
        findings = [
            item for item in [
                *(workload.get("policy_findings") or []),
                *group_findings.get(group, []),
            ]
            if item.get("status") == "violation"
        ]
        categories = {str(item.get("category")) for item in findings}
        codes = {str(item.get("code")) for item in findings}
        tags = {str(tag) for item in findings for tag in item.get("tags", [])}
        if filters.violation_categories and not categories.intersection(filters.violation_categories):
            continue
        if filters.violation_codes and not codes.intersection(filters.violation_codes):
            continue
        if filters.violation_tags and not tags.intersection(filters.violation_tags):
            continue
        result[workload_id] = workload
    return result


def _candidate(
    selected: tuple[str, ...], nodes: dict[str, dict[str, Any]],
    workloads: dict[str, dict[str, Any]], request: PlanRequest,
    target: dict[str, float | int],
) -> dict[str, Any] | None:
    released = _released(selected, workloads)
    freed = [
        name for name, node in nodes.items()
        if _fits(node, released.get(name, {}), target)
    ]
    if len(freed) < request.target.nodes:
        return None
    details = [workloads[item] for item in selected]
    return {
        "workloads": list(selected),
        "workload_count": len(selected),
        "users": len({str(item.get("user")) for item in details}),
        "groups": len({str(item.get("group")) for item in details}),
        "gpus": sum(float(item.get("total_gpu") or 0) for item in details),
        "cpus": sum(float(p.get("cpu") or 0) for item in details for p in item.get("placements", [])),
        "memory_gib": sum(float(p.get("memory_gib") or 0) for item in details for p in item.get("placements", [])),
        "freed_nodes": sorted(freed)[: request.target.nodes],
        "workload_details": details,
    }


def _rank_key(strategy: str, candidate: dict[str, Any]) -> tuple[Any, ...]:
    if strategy == "min-workloads":
        return (candidate["workload_count"], candidate["gpus"], candidate["users"], candidate["workloads"])
    if strategy == "min-users":
        return (candidate["users"], candidate["gpus"], candidate["workload_count"], candidate["workloads"])
    return (candidate["gpus"], candidate["workload_count"], candidate["users"], candidate["workloads"])


def solve_plan(snapshot: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
    """Pure process-safe solver entrypoint used by the API process pool."""
    request = PlanRequest.model_validate(request_payload)
    requested_target = request.target.model_dump(mode="json")
    profile = snapshot.get("planning_profile") or {}
    defaults_applied: list[str] = []
    cpus_per_node = request.target.cpus_per_node
    if cpus_per_node is None:
        cpus_per_node = request.target.gpus_per_node * float(profile["default_cpu_per_gpu"])
        defaults_applied.append("cpus_per_node")
    memory_per_node_gib = request.target.memory_per_node_gib
    if memory_per_node_gib is None:
        memory_per_node_gib = request.target.gpus_per_node * float(profile["default_memory_gib_per_gpu"])
        defaults_applied.append("memory_per_node_gib")
    resolved_target: dict[str, float | int] = {
        "nodes": request.target.nodes,
        "gpus_per_node": request.target.gpus_per_node,
        "cpus_per_node": cpus_per_node,
        "memory_per_node_gib": memory_per_node_gib,
    }
    started = time.monotonic()
    deadline = started + request.search_seconds
    nodes = {
        name: node for name, node in _node_map(snapshot).items()
        if node.get("state") in SCHEDULABLE_STATES
        and bool(node.get("planning_eligible", True))
    }
    excluded_node_details = [
        {
            "node": str(node.get("node")),
            "reasons": list(node.get("planning_exclusion_reasons") or ["attribution.resource_excess"]),
        }
        for node in snapshot.get("nodes", [])
        if node.get("state") in SCHEDULABLE_STATES
        and not bool(node.get("planning_eligible", True))
    ]
    excluded_workload_details = [
        {
            "workload_id": str(item.get("workload_id")),
            "reasons": list(item.get("planning_exclusion_reasons") or ["attribution.resource_excess"]),
            "nodes": list(item.get("planning_excluded_nodes") or []),
        }
        for item in snapshot.get("workloads", [])
        if not bool(item.get("planning_eligible", True))
    ]
    exclusion_reasons = sorted({
        str(reason)
        for item in [*excluded_node_details, *excluded_workload_details]
        for reason in item.get("reasons", [])
    })
    exclusions = {
        "node_count": len(excluded_node_details),
        "workload_count": len(excluded_workload_details),
        "reasons": exclusion_reasons,
        "nodes": excluded_node_details,
        "workloads": excluded_workload_details,
    }
    common = {
        "requested_target": requested_target,
        "resolved_target": resolved_target,
        "defaults_applied": defaults_applied,
        "planning_profile": profile,
        "planning_exclusions": exclusions,
    }
    already_free = [name for name, node in nodes.items() if _fits(node, {}, resolved_target)]
    if len(already_free) >= request.target.nodes:
        return {
            "snapshot_id": request.snapshot_id,
            "snapshot_generated_at": snapshot["generated_at"],
            "optimality": "not-needed",
            "search_elapsed_seconds": round(time.monotonic() - started, 4),
            "plans": [],
            "currently_schedulable_nodes": sorted(already_free),
            "no_plan_reason": None,
            **common,
        }

    eligible_nodes = {
        name for name, node in nodes.items()
        if _eligible_node(node, request.candidate_scope, resolved_target)
    }
    excluded_scope_nodes = {
        str(node.get("node")) for node in snapshot.get("nodes", [])
        if node.get("state") in SCHEDULABLE_STATES
        and not bool(node.get("planning_eligible", True))
        and _eligible_node(node, request.candidate_scope, resolved_target)
    }
    attribution_relevant = bool(excluded_scope_nodes) or any(
        any(
            str(placement.get("node")) in eligible_nodes | excluded_scope_nodes
            for placement in item.get("placements", [])
        )
        for item in snapshot.get("workloads", [])
        if not bool(item.get("planning_eligible", True))
    )
    workloads = _filter_workloads(snapshot, request, eligible_nodes)
    unfiltered_candidate_ids = {
        str(item.get("workload_id"))
        for item in snapshot.get("workloads", [])
        if bool(item.get("planning_eligible", True))
        and str(item.get("user") or "") not in {"", "unknown"}
        and str(item.get("group") or "") != "unattributed"
        and any(str(placement.get("node")) in eligible_nodes for placement in item.get("placements", []))
    }
    ids = sorted(workloads)
    candidates: list[dict[str, Any]] = []
    optimality = "exact"
    candidate_cap = 10_000

    # Exact enumeration is intentionally bounded. Larger searches retain the
    # same semantics but use deterministic greedy prefixes before the deadline.
    if len(ids) <= 20:
        for size in range(1, len(ids) + 1):
            for selected in combinations(ids, size):
                if time.monotonic() >= deadline:
                    optimality = "heuristic"
                    break
                item = _candidate(selected, nodes, workloads, request, resolved_target)
                if item is not None:
                    candidates.append(item)
                    if len(candidates) >= candidate_cap:
                        optimality = "heuristic"
                        break
            if optimality == "heuristic":
                break
    else:
        optimality = "heuristic"

    if not candidates:
        for strategy in request.strategies:
            if strategy == "min-gpu":
                ordered = sorted(ids, key=lambda item: (
                    float(workloads[item].get("total_gpu") or 0),
                    -len(workloads[item].get("placements") or []), item,
                ))
            elif strategy == "min-workloads":
                ordered = sorted(ids, key=lambda item: (
                    -len(workloads[item].get("placements") or []),
                    -float(workloads[item].get("total_gpu") or 0), item,
                ))
            else:
                ordered = sorted(ids, key=lambda item: (
                    str(workloads[item].get("user") or ""),
                    -len(workloads[item].get("placements") or []), item,
                ))
            selected: list[str] = []
            for workload_id in ordered:
                selected.append(workload_id)
                item = _candidate(tuple(selected), nodes, workloads, request, resolved_target)
                if item is not None:
                    candidates.append(item)
                    break

    plans: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for strategy in request.strategies:
        ranked = sorted(candidates, key=lambda item: _rank_key(strategy, item))
        rank = 0
        for item in ranked:
            key = (strategy, tuple(item["workloads"]))
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            plans.append({"strategy": strategy, "rank": rank, **item})
            if rank >= request.alternatives:
                break
    return {
        "snapshot_id": request.snapshot_id,
        "snapshot_generated_at": snapshot["generated_at"],
        "optimality": optimality,
        "search_elapsed_seconds": round(time.monotonic() - started, 4),
        "plans": plans,
        "currently_schedulable_nodes": sorted(already_free),
        "no_plan_reason": (
            None if plans else
            "no-candidates-after-filters" if unfiltered_candidate_ids and not ids else
            "attribution-excluded" if attribution_relevant else
            "insufficient-releasable-resources"
        ),
        **common,
    }
