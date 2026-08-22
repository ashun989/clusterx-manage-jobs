from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..models import PlanRequest
from .domain import (
    CPU_SCALE,
    MEMORY_SCALE,
    CandidateNode,
    CandidateWorkload,
    PlanningProblem,
    PreparedPlan,
    ResourceVector,
    ceil_units,
    clean_number,
    floor_units,
)


SCHEDULABLE_STATES = {"RUNNING", "IDLE", "MIXED", "running", "idle", "mixed"}


def _resource_total(item: dict[str, Any], key: str) -> float | int | None:
    value = item.get(key)
    if value is None:
        return None
    return clean_number(float(value))


def _target(
    request: PlanRequest, snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], ResourceVector]:
    requested = request.target.model_dump(mode="json")
    profile = snapshot.get("planning_profile") or {}
    defaults: list[str] = []
    cpus = request.target.cpus_per_node
    if cpus is None:
        cpus = request.target.gpus_per_node * float(profile["default_cpu_per_gpu"])
        defaults.append("cpus_per_node")
    memory = request.target.memory_per_node_gib
    if memory is None:
        memory = request.target.gpus_per_node * float(profile["default_memory_gib_per_gpu"])
        defaults.append("memory_per_node_gib")
    resolved = {
        "nodes": request.target.nodes,
        "gpus_per_node": request.target.gpus_per_node,
        "cpus_per_node": clean_number(float(cpus)),
        "memory_per_node_gib": clean_number(float(memory)),
    }
    units = ResourceVector(
        gpu=ceil_units(request.target.gpus_per_node),
        cpu_millis=ceil_units(cpus, CPU_SCALE),
        memory_mib=ceil_units(memory, MEMORY_SCALE),
    )
    return requested, resolved, defaults, units


def _node_free(node: dict[str, Any]) -> ResourceVector:
    return ResourceVector(
        gpu=floor_units(
            float(node.get("total_gpu") or 0) - float(node.get("allocated_gpu") or 0)
        ),
        cpu_millis=floor_units(
            float(node.get("total_cpu") or 0) - float(node.get("allocated_cpu") or 0),
            CPU_SCALE,
        ),
        memory_mib=floor_units(
            float(node.get("total_memory_gib") or 0)
            - float(node.get("allocated_memory_gib") or 0),
            MEMORY_SCALE,
        ),
    )


def _eligible_node(node: dict[str, Any], scope: str, target: ResourceVector) -> bool:
    allocated = float(node.get("allocated_gpu") or 0)
    total = float(node.get("total_gpu") or 0)
    full = total > 0 and allocated >= total
    fragmented = 0 < allocated < total
    if not fragmented and allocated < total:
        fragmented = not _node_free(node).covers(target) and (
            float(node.get("allocated_cpu") or 0) > 0
            or float(node.get("allocated_memory_gib") or 0) > 0
        )
    return fragmented if scope == "fragmented" else full if scope == "full" else fragmented or full


def _findings(
    snapshot: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    groups = {
        str(item.get("group")): list(item.get("policy_findings") or [])
        for item in snapshot.get("groups", [])
    }
    users = {
        str(item.get("user")): [
            finding for finding in item.get("policy_findings") or []
            if finding.get("code") == "quota.development.instances_per_user"
        ]
        for item in snapshot.get("users", [])
    }
    return groups, users


def _passes_filters(
    workload_id: str,
    workload: dict[str, Any],
    request: PlanRequest,
    group_findings: dict[str, list[dict[str, Any]]],
    user_findings: dict[str, list[dict[str, Any]]],
    snapshot: dict[str, Any],
) -> bool:
    filters = request.filters
    user = str(workload.get("user") or "")
    group = str(workload.get("group") or "")
    kind = str(workload.get("type") or "unknown")
    if filters.workload_types and kind not in filters.workload_types:
        return False
    if filters.groups and group not in filters.groups:
        return False
    if filters.users and user not in filters.users:
        return False
    if filters.workloads and workload_id not in filters.workloads:
        return False
    if workload_id in filters.exclude_workloads or user in filters.exclude_users:
        return False
    if filters.over_quota_only:
        state = next(
            (item.get("status") for item in snapshot.get("groups", []) if item.get("group") == group),
            None,
        )
        if state not in {"burst", "violation"}:
            return False
    findings = [
        item for item in [
            *(workload.get("policy_findings") or []),
            *group_findings.get(group, []),
            *(user_findings.get(user, []) if kind == "aid" else []),
        ]
        if item.get("status") == "violation"
    ]
    categories = {str(item.get("category")) for item in findings}
    codes = {str(item.get("code")) for item in findings}
    tags = {str(tag) for item in findings for tag in item.get("tags", [])}
    return not (
        (filters.violation_categories and not categories.intersection(filters.violation_categories))
        or (filters.violation_codes and not codes.intersection(filters.violation_codes))
        or (filters.violation_tags and not tags.intersection(filters.violation_tags))
    )


def _release_map(
    workload: dict[str, Any], schedulable_nodes: set[str],
) -> dict[str, ResourceVector]:
    totals: dict[str, ResourceVector] = defaultdict(ResourceVector)
    for placement in workload.get("placements") or []:
        node_id = str(placement.get("node") or "")
        if node_id not in schedulable_nodes:
            continue
        totals[node_id] = totals[node_id].plus(ResourceVector(
            gpu=floor_units(placement.get("gpu")),
            cpu_millis=floor_units(placement.get("cpu"), CPU_SCALE),
            memory_mib=floor_units(placement.get("memory_gib"), MEMORY_SCALE),
        ))
    return dict(totals)


def _exclusions(snapshot: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    excluded_nodes = [
        {
            "node": str(node.get("node")),
            "reasons": list(
                node.get("planning_exclusion_reasons") or ["attribution.resource_excess"]
            ),
        }
        for node in snapshot.get("nodes", [])
        if node.get("state") in SCHEDULABLE_STATES
        and not bool(node.get("planning_eligible", True))
    ]
    excluded_workloads = [
        {
            "workload_id": str(item.get("workload_id")),
            "reasons": list(
                item.get("planning_exclusion_reasons") or ["attribution.resource_excess"]
            ),
            "nodes": list(item.get("planning_excluded_nodes") or []),
        }
        for item in snapshot.get("workloads", [])
        if not bool(item.get("planning_eligible", True))
    ]
    reasons = sorted({
        str(reason)
        for item in [*excluded_nodes, *excluded_workloads]
        for reason in item.get("reasons", [])
    })
    return ({
        "node_count": len(excluded_nodes),
        "workload_count": len(excluded_workloads),
        "reasons": reasons,
        "nodes": excluded_nodes,
        "workloads": excluded_workloads,
    }, {str(item["node"]) for item in excluded_nodes})


def _fit_nodes(problem: PlanningProblem, selected: set[str]) -> set[str]:
    workloads = problem.workload_by_id
    result: set[str] = set()
    for node in problem.candidate_nodes:
        released = ResourceVector()
        for workload_id in selected:
            released = released.plus(
                workloads[workload_id].releases.get(node.node_id, ResourceVector())
            )
        if released.covers(node.deficit):
            result.add(node.node_id)
    return result


def prepare_plan(snapshot: dict[str, Any], request: PlanRequest) -> PreparedPlan:
    requested, resolved, defaults, target = _target(request, snapshot)
    exclusions, excluded_node_ids = _exclusions(snapshot)
    common = {
        "requested_target": requested,
        "resolved_target": resolved,
        "defaults_applied": defaults,
        "planning_profile": snapshot.get("planning_profile") or {},
        "planning_exclusions": exclusions,
    }
    nodes = {
        str(item["node"]): item for item in snapshot.get("nodes", [])
        if item.get("state") in SCHEDULABLE_STATES
        and bool(item.get("planning_eligible", True))
    }
    already_free = tuple(sorted(
        node_id for node_id, node in nodes.items() if _node_free(node).covers(target)
    ))
    required_new = max(0, request.target.nodes - len(already_free))
    if required_new == 0:
        return PreparedPlan(None, common, already_free)

    candidate_node_ids = {
        node_id for node_id, node in nodes.items()
        if node_id not in already_free and _eligible_node(node, request.candidate_scope, target)
    }
    candidate_nodes = tuple(
        CandidateNode(node_id, target.deficit_from(_node_free(nodes[node_id])))
        for node_id in sorted(candidate_node_ids)
    )
    group_findings, user_findings = _findings(snapshot)
    schedulable_node_ids = set(nodes)
    unfiltered_ids: set[str] = set()
    candidates: list[CandidateWorkload] = []
    for raw in snapshot.get("workloads", []):
        workload_id = str(raw.get("workload_id"))
        user = str(raw.get("user") or "")
        group = str(raw.get("group") or "")
        releases = _release_map(raw, schedulable_node_ids)
        if not candidate_node_ids.intersection(releases):
            continue
        if not bool(raw.get("planning_eligible", True)):
            continue
        if user in {"", "unknown"} or group == "unattributed":
            continue
        unfiltered_ids.add(workload_id)
        if not _passes_filters(
            workload_id, raw, request, group_findings, user_findings, snapshot,
        ):
            continue
        placement_gpu = sum(item.gpu for item in releases.values())
        raw_gpu = raw.get("total_gpu")
        candidates.append(CandidateWorkload(
            workload_id=workload_id,
            user_id=user,
            group_id=group,
            total_gpu=max(0, int(round(float(raw_gpu)))) if raw_gpu is not None else placement_gpu,
            total_cpu=_resource_total(raw, "total_cpu"),
            total_memory_gib=_resource_total(raw, "total_memory_gib"),
            releases=releases,
            raw=raw,
        ))
    problem = PlanningProblem(
        snapshot_id=request.snapshot_id,
        requested_nodes=request.target.nodes,
        required_new_nodes=required_new,
        already_free_nodes=already_free,
        candidate_nodes=candidate_nodes,
        workloads=tuple(sorted(candidates, key=lambda item: item.workload_id)),
    )
    all_selected = {item.workload_id for item in problem.workloads}
    if len(_fit_nodes(problem, all_selected)) < required_new:
        snapshot_nodes = {
            str(item.get("node")): item for item in snapshot.get("nodes", [])
            if item.get("state") in SCHEDULABLE_STATES
        }
        excluded_scope = any(
            node_id in excluded_node_ids
            and _eligible_node(node, request.candidate_scope, target)
            for node_id, node in snapshot_nodes.items()
        )
        reason = (
            "no-candidates-after-filters" if unfiltered_ids and not candidates else
            "attribution-excluded" if excluded_scope else
            "insufficient-releasable-resources"
        )
        return PreparedPlan(None, common, already_free, reason)
    return PreparedPlan(problem, common, already_free)
