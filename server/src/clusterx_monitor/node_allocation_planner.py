from __future__ import annotations

"""Cold-start node ownership suggestions.

This planner is deliberately separate from the scheduling planner.  The
existing planner selects workloads to release; this one assigns every current
queue node to exactly one configured group while preserving finite GPU quota
capacity. It minimizes impact to attributed running placements and weighs
known aggregate pending resource demand. It is a preview-only operation:
applying a proposal still goes through the normal revision-checked group
configuration update.
"""

import time
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from .models import PolicyConfig
from .planning.cp_sat import configured_solver_workers

MODEL_VERSION = 3
STRATEGIES = ("workload-first", "gpu-first", "user-first")


@dataclass(frozen=True)
class _Workload:
    workload_id: str
    user: str
    group: str
    placements: tuple[tuple[str, int, int, int], ...]


def _gpu(value: Any) -> int:
    try:
        return max(0, round(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _units(value: Any, scale: int) -> int:
    try:
        return max(0, round(float(value or 0) * scale))
    except (TypeError, ValueError, OverflowError):
        return 0


def _known_units(value: Any, scale: int) -> int | None:
    if value is None:
        return None
    return _units(value, scale)


def _nodes(snapshot: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    values: dict[str, int] = {}
    for item in snapshot.get("nodes") or []:
        name = str(item.get("node") or "").strip()
        if not name or name in values:
            continue
        values[name] = _gpu(item.get("total_gpu"))
    return tuple(sorted(values.items()))


def _node_resources(snapshot: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    return {
        str(item.get("node") or "").strip(): (
            _gpu(item.get("total_gpu")),
            _units(item.get("total_cpu"), 1000),
            _units(item.get("total_memory_gib"), 1024),
        )
        for item in snapshot.get("nodes") or []
        if str(item.get("node") or "").strip()
    }


def _workloads(
    snapshot: dict[str, Any], group_names: set[str],
) -> tuple[tuple[_Workload, ...], dict[str, Any]]:
    usable: list[_Workload] = []
    excluded: list[dict[str, Any]] = []
    known_nodes = {name for name, _ in _nodes(snapshot)}
    for index, item in enumerate(snapshot.get("workloads") or []):
        workload_id = str(item.get("workload_id") or item.get("workload_name") or f"workload-{index}")
        user = str(item.get("user") or "unknown").strip().lower()
        group = str(item.get("group") or "").strip()
        placements = tuple(
            (
                str(placement.get("node") or "").strip(),
                _gpu(placement.get("gpu")),
                _units(placement.get("cpu"), 1000),
                _units(placement.get("memory_gib"), 1024),
            )
            for placement in item.get("placements") or []
            if str(placement.get("node") or "").strip() in known_nodes
            and any((
                _gpu(placement.get("gpu")), _units(placement.get("cpu"), 1000),
                _units(placement.get("memory_gib"), 1024),
            ))
        )
        # A workload without a known configured owner is still useful context
        # to an administrator, but it must not bias an ownership proposal.
        if user == "unknown" or group not in group_names or not placements:
            excluded.append({
                "workload_id": workload_id,
                "user": user,
                "group": group or "unattributed",
                "reason": (
                    "unknown-user-or-group" if user == "unknown" or group not in group_names
                    else "no-current-node-placement"
                ),
            })
            continue
        usable.append(_Workload(workload_id, user, group, placements))
    return tuple(usable), {"excluded_workloads": excluded}


def _pending_demands(
    snapshot: dict[str, Any], group_names: set[str],
) -> tuple[dict[str, dict[str, int]], list[dict[str, str]], list[dict[str, str]], bool]:
    totals: dict[str, dict[str, int]] = {}
    excluded: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    complete = bool(snapshot.get("pending_complete", True))
    scales = {"gpu": 1, "cpu_millis": 1000, "memory_mib": 1024}
    fields = {"gpu": "total_gpu", "cpu_millis": "total_cpu", "memory_mib": "total_memory_gib"}
    for item in snapshot.get("pending_workloads") or []:
        workload_id = str(item.get("workload_id") or item.get("workload_name") or "unknown")
        group = str(item.get("group") or "").strip()
        if group not in group_names:
            excluded.append({"workload_id": workload_id, "reason": "unknown-user-or-group"})
            complete = False
            continue
        known = False
        unknown_dimensions = []
        group_total = totals.setdefault(group, {key: 0 for key in fields})
        for dimension, field in fields.items():
            value = _known_units(item.get(field), scales[dimension])
            if value is None:
                unknown_dimensions.append(dimension)
                continue
            group_total[dimension] += value
            known = True
        if unknown_dimensions:
            partial.append({
                "workload_id": workload_id,
                "reason": "unknown-resource-fields:" + ",".join(unknown_dimensions),
            })
            complete = False
        if not known:
            excluded.append({"workload_id": workload_id, "reason": "resource-request-unknown"})
            complete = False
    return totals, excluded, partial, complete


def _objective(
    strategy: str,
    affected_workloads: cp_model.LinearExpr,
    affected_gpu: cp_model.LinearExpr,
    affected_users: cp_model.LinearExpr,
    tie_break: cp_model.LinearExpr,
    max_workloads: int,
    max_gpu: int,
    max_users: int,
    pending_shortfall: cp_model.LinearExpr,
    max_pending_shortfall: int,
    max_tie: int,
) -> cp_model.LinearExpr:
    if strategy == "workload-first":
        first, second, third = affected_workloads, affected_gpu, affected_users
        first_max, second_max, third_max = max_workloads, max_gpu, max_users
    elif strategy == "gpu-first":
        first, second, third = affected_gpu, affected_workloads, affected_users
        first_max, second_max, third_max = max_gpu, max_workloads, max_users
    else:
        first, second, third = affected_users, affected_workloads, affected_gpu
        first_max, second_max, third_max = max_users, max_workloads, max_gpu
    pending_multiplier = max_tie + 1
    third_multiplier = (max_pending_shortfall + 1) * pending_multiplier
    second_multiplier = (third_max + 1) * third_multiplier
    first_multiplier = (second_max + 1) * second_multiplier
    maximum = (
        first_max * first_multiplier
        + second_max * second_multiplier
        + third_max * third_multiplier
        + max_pending_shortfall * pending_multiplier
        + max_tie
    )
    if maximum >= 2**63:
        raise ValueError("node allocation objective exceeds CP-SAT int64 range")
    return (
        first * first_multiplier
        + second * second_multiplier
        + third * third_multiplier
        + pending_shortfall * pending_multiplier
        + tie_break
    )


def _proposal_template(policy: PolicyConfig) -> dict[str, Any]:
    return {
        name: {
            "gpu_quota": group.gpu_quota,
            "cpu_quota": group.cpu_quota,
            "memory_quota_gib": group.memory_quota_gib,
            "members": list(group.members),
            "nodes": [],
        }
        for name, group in policy.groups.items()
    }


def _effective_gpu_quotas(
    nodes: tuple[tuple[str, int], ...], policy: PolicyConfig,
) -> tuple[dict[str, int], str | None]:
    """Resolve finite quotas, including the dynamic default remainder."""
    bound_gpu = sum(total_gpu for _, total_gpu in nodes)
    quotas: dict[str, int] = {}
    explicit_gpu = 0
    for group_name, group in policy.groups.items():
        if isinstance(group.gpu_quota, int):
            quotas[group_name] = group.gpu_quota
            if group_name != "default":
                explicit_gpu += group.gpu_quota

    if policy.groups["default"].gpu_quota == "remainder":
        remainder = max(0, bound_gpu - explicit_gpu)
        if remainder % 8 != 0:
            return quotas, (
                "default remainder quota resolves to "
                f"{remainder} GPU, which is not a multiple of 8"
            )
        quotas["default"] = remainder
    return quotas, None


def _invalid_quota_proposal(
    policy: PolicyConfig, strategy: str, nodes: tuple[tuple[str, int], ...],
    context: dict[str, Any], message: str,
) -> dict[str, Any]:
    coverage = [
        {
            "group": group_name,
            "quota": quota,
            "assigned_gpu": 0,
            "deficit": quota,
            "covered": False,
        }
        for group_name, quota in _effective_gpu_quotas(nodes, policy)[0].items()
    ]
    return {
        "strategy": strategy,
        "status": "INVALID_QUOTA_ALIGNMENT",
        "feasible": False,
        "groups": _proposal_template(policy),
        "assignment": [],
        "metrics": {"affected_workloads": None, "affected_gpu": None, "affected_users": None},
        "pending_demand": [],
        "pending_demand_complete": bool(context.get("pending_demand_complete", False)),
        "excluded_pending_workloads": context.get("excluded_pending_workloads", []),
        "partial_pending_workloads": context.get("partial_pending_workloads", []),
        "capacity_coverage": coverage,
        "capacity_deficits": coverage,
        "diagnostics": [message] + ([] if context.get("pending_demand_complete") else [
            "pending inventory, request shape, or node capacity evidence is incomplete"
        ]),
        "excluded_workloads": context["excluded_workloads"],
        "solver": {
            "backend": "cp-sat",
            "model_version": MODEL_VERSION,
            "status": "INVALID_QUOTA_ALIGNMENT",
            "wall_time_seconds": 0,
            "deterministic_time_seconds": 0,
            "branches": 0,
            "conflicts": 0,
            "node_count": len(nodes),
            "group_count": len(policy.groups),
        },
        "computed_seconds": 0,
    }


def _solve_strategy(
    snapshot: dict[str, Any], policy: PolicyConfig, strategy: str,
    time_limit_seconds: float,
) -> dict[str, Any]:
    nodes = _nodes(snapshot)
    group_names = tuple(sorted(policy.groups))
    workloads, context = _workloads(snapshot, set(group_names))
    pending_demands, pending_excluded, pending_partial, pending_complete = _pending_demands(
        snapshot, set(group_names),
    )
    dimension_fields = {
        "gpu": "total_gpu", "cpu_millis": "total_cpu",
        "memory_mib": "total_memory_gib",
    }
    unknown_capacity_dimensions = {
        dimension for dimension, field in dimension_fields.items()
        if any(int(values.get(dimension) or 0) > 0 for values in pending_demands.values())
        and any(node.get(field) is None for node in snapshot.get("nodes") or [])
    }
    if unknown_capacity_dimensions:
        pending_complete = False
    context["excluded_pending_workloads"] = pending_excluded
    context["partial_pending_workloads"] = pending_partial
    context["pending_demand_complete"] = pending_complete
    template = _proposal_template(policy)
    started = time.monotonic()
    effective_quotas, quota_error = _effective_gpu_quotas(nodes, policy)
    if quota_error:
        return _invalid_quota_proposal(policy, strategy, nodes, context, quota_error)

    model = cp_model.CpModel()
    assignment = {
        (node_index, group_index): model.new_bool_var(f"node:{node_index}:group:{group_index}")
        for node_index, _ in enumerate(nodes)
        for group_index, _ in enumerate(group_names)
    }
    for node_index, _ in enumerate(nodes):
        model.add(sum(assignment[node_index, group_index] for group_index in range(len(group_names))) == 1)

    # A finite quota is an exact aggregate capacity target.  A remainder is
    # resolved against this snapshot before solving; an omitted quota has no
    # assignment constraint.
    coverage: list[tuple[str, int, cp_model.LinearExpr]] = []
    for group_index, group_name in enumerate(group_names):
        quota = effective_quotas.get(group_name)
        if quota is not None:
            capacity = sum(
                total_gpu * assignment[node_index, group_index]
                for node_index, (_, total_gpu) in enumerate(nodes)
            )
            model.add(capacity == quota)
            coverage.append((group_name, quota, capacity))

    affected_flags: dict[int, cp_model.IntVar] = {}
    affected_gpu_terms: list[cp_model.LinearExpr] = []
    user_flags: dict[str, cp_model.IntVar] = {}
    node_resources = _node_resources(snapshot)
    node_indexes = {name: index for index, (name, _) in enumerate(nodes)}
    for workload_index, workload in enumerate(workloads):
        affected = model.new_bool_var(f"affected-workload:{workload_index}")
        affected_flags[workload_index] = affected
        for node_name, gpu, cpu_millis, memory_mib in workload.placements:
            node_index = node_indexes[node_name]
            for group_index, group_name in enumerate(group_names):
                if group_name != workload.group:
                    if gpu or cpu_millis or memory_mib:
                        model.add(affected >= assignment[node_index, group_index])
                    if gpu:
                        affected_gpu_terms.append(gpu * assignment[node_index, group_index])
        user_flag = user_flags.setdefault(
            workload.user,
            model.new_bool_var(f"affected-user:{len(user_flags)}"),
        )
        model.add(user_flag >= affected)

    affected_workloads = sum(affected_flags.values())
    affected_gpu = sum(affected_gpu_terms)
    affected_users = sum(user_flags.values())

    pending_shortfall_terms: list[cp_model.LinearExpr] = []
    pending_capacity: list[tuple[str, str, int, cp_model.LinearExpr, int]] = []
    node_dimension_indexes = {"gpu": 0, "cpu_millis": 1, "memory_mib": 2}
    max_pending_shortfall = 0
    for group_index, group_name in enumerate(group_names):
        demands = pending_demands.get(group_name) or {}
        for dimension, node_dimension_index in node_dimension_indexes.items():
            demand = int(demands.get(dimension) or 0)
            if demand <= 0:
                continue
            if dimension in unknown_capacity_dimensions:
                continue
            capacity = sum(
                node_resources.get(node_name, (0, 0, 0))[node_dimension_index]
                * assignment[node_index, group_index]
                for node_index, (node_name, _) in enumerate(nodes)
            )
            gap = model.new_int_var(0, demand, f"pending-gap:{group_index}:{dimension}")
            model.add_max_equality(gap, [demand - capacity, 0])
            # Scale each resource shortfall to an approximate percentage of
            # the known group demand so GPU, CPU and memory can share one
            # lexicographic objective term.
            weight = max(1, 1_000_000 // demand)
            pending_shortfall_terms.append(gap * weight)
            max_pending_shortfall += demand * weight
            pending_capacity.append((group_name, dimension, demand, capacity, gap))
    pending_shortfall = sum(pending_shortfall_terms)
    tie_break = sum(
        (node_index * len(group_names) + group_index + 1) * variable
        for (node_index, group_index), variable in assignment.items()
    )
    model.minimize(_objective(
        strategy, affected_workloads, affected_gpu, affected_users, tie_break,
        len(workloads), sum(gpu for _, gpu in nodes), len(user_flags),
        pending_shortfall, max_pending_shortfall,
        max(1, len(nodes) * max(1, len(group_names)) * len(nodes)),
    ))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.01, time_limit_seconds)
    solver.parameters.num_search_workers = configured_solver_workers()
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    status_name = solver.status_name(status).upper()
    feasible = status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
    if status == cp_model.MODEL_INVALID:
        raise ValueError(solver.solution_info())

    if feasible:
        for node_index, (node_name, _) in enumerate(nodes):
            group_index = next(
                index for index in range(len(group_names))
                if solver.boolean_value(assignment[node_index, index])
            )
            template[group_names[group_index]]["nodes"].append(node_name)

    coverage_result = []
    deficits = []
    for group_name, quota, expression in coverage:
        assigned_gpu = round(solver.value(expression)) if feasible else 0
        deficit = abs(quota - assigned_gpu)
        item = {
            "group": group_name, "quota": quota,
            "assigned_gpu": assigned_gpu, "deficit": deficit,
            "covered": deficit == 0,
        }
        coverage_result.append(item)
        if deficit:
            deficits.append(item)

    metrics = {
        "affected_workloads": round(solver.value(affected_workloads)) if feasible else None,
        "affected_gpu": round(solver.value(affected_gpu)) if feasible else None,
        "affected_users": round(solver.value(affected_users)) if feasible else None,
    }
    dimension_scales = {"gpu": 1, "cpu_millis": 1000, "memory_mib": 1024}
    dimension_labels = {"gpu": "gpu", "cpu_millis": "cpu", "memory_mib": "memory"}
    pending_demand_result = []
    for group_name, demands in sorted(pending_demands.items()):
        for dimension in sorted(unknown_capacity_dimensions):
            demand = int(demands.get(dimension) or 0)
            if demand <= 0:
                continue
            pending_demand_result.append({
                "group": group_name,
                "resource": dimension_labels[dimension],
                "requested": round(demand / dimension_scales[dimension], 3),
                "assigned_capacity": None,
                "shortfall": None,
            })
    for group_name, dimension, demand, capacity, gap in pending_capacity:
        assigned = round(solver.value(capacity)) if feasible else 0
        shortfall = round(solver.value(gap)) if feasible else demand
        scale = dimension_scales[dimension]
        pending_demand_result.append({
            "group": group_name,
            "resource": dimension_labels[dimension],
            "requested": round(demand / scale, 3),
            "assigned_capacity": round(assigned / scale, 3),
            "shortfall": round(shortfall / scale, 3),
        })
    diagnostics = []
    if status_name == "INFEASIBLE":
        diagnostics.append("finite GPU quotas cannot all be covered by a mutually exclusive node assignment")
    elif status_name not in {"OPTIMAL", "FEASIBLE"}:
        diagnostics.append("solver did not return a complete node assignment within the time limit")
    if not pending_complete:
        diagnostics.append("pending inventory, request shape, or node capacity evidence is incomplete; only available evidence was optimized")
    return {
        "strategy": strategy,
        "status": status_name,
        "feasible": feasible,
        "groups": template,
        "assignment": [
            {"node": node, "group": group}
            for group, value in template.items()
            for node in value["nodes"]
        ],
        "metrics": metrics,
        "pending_demand": pending_demand_result,
        "pending_demand_complete": pending_complete,
        "excluded_pending_workloads": pending_excluded,
        "partial_pending_workloads": pending_partial,
        "capacity_coverage": coverage_result,
        "capacity_deficits": deficits,
        "diagnostics": diagnostics,
        "excluded_workloads": context["excluded_workloads"],
        "solver": {
            "backend": "cp-sat",
            "model_version": MODEL_VERSION,
            "status": status_name,
            "wall_time_seconds": round(solver.wall_time, 6),
            "deterministic_time_seconds": round(solver.deterministic_time, 6),
            "branches": solver.num_branches,
            "conflicts": solver.num_conflicts,
            "node_count": len(nodes),
            "group_count": len(group_names),
        },
        "computed_seconds": round(time.monotonic() - started, 6),
    }


def solve_node_allocation_plans(
    snapshot: dict[str, Any], policy_payload: dict[str, Any], search_seconds: float = 10,
) -> dict[str, Any]:
    """Return the three deterministic cold-start allocation alternatives."""
    policy = PolicyConfig.model_validate(policy_payload)
    started = time.monotonic()
    deadline = started + max(1.0, min(float(search_seconds), 30.0))
    proposals: list[dict[str, Any]] = []
    for index, strategy in enumerate(STRATEGIES):
        remaining = max(0.01, deadline - time.monotonic())
        slot = remaining / (len(STRATEGIES) - index)
        proposals.append(_solve_strategy(snapshot, policy, strategy, slot))
    return {
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "node_allocation_enabled": policy.node_allocation.enabled,
        "model_version": MODEL_VERSION,
        "search_seconds": round(time.monotonic() - started, 6),
        "proposals": proposals,
    }
