from __future__ import annotations

"""Cold-start node ownership suggestions.

This planner is deliberately separate from the scheduling planner.  The
existing planner selects workloads to release; this one assigns every current
queue node to exactly one configured group while preserving finite GPU quota
capacity.  It is a preview-only operation: applying a proposal still goes
through the normal revision-checked group configuration update.
"""

import time
from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from .models import PolicyConfig
from .planning.cp_sat import configured_solver_workers

MODEL_VERSION = 2
STRATEGIES = ("workload-first", "gpu-first", "user-first")


@dataclass(frozen=True)
class _Workload:
    workload_id: str
    user: str
    group: str
    placements: tuple[tuple[str, int], ...]


def _gpu(value: Any) -> int:
    try:
        return max(0, round(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nodes(snapshot: dict[str, Any]) -> tuple[tuple[str, int], ...]:
    values: dict[str, int] = {}
    for item in snapshot.get("nodes") or []:
        name = str(item.get("node") or "").strip()
        if not name or name in values:
            continue
        values[name] = _gpu(item.get("total_gpu"))
    return tuple(sorted(values.items()))


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
            (str(placement.get("node") or "").strip(), _gpu(placement.get("gpu")))
            for placement in item.get("placements") or []
            if str(placement.get("node") or "").strip() in known_nodes
            and _gpu(placement.get("gpu")) > 0
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


def _objective(
    strategy: str,
    affected_workloads: cp_model.LinearExpr,
    affected_gpu: cp_model.LinearExpr,
    affected_users: cp_model.LinearExpr,
    tie_break: cp_model.LinearExpr,
    max_workloads: int,
    max_gpu: int,
    max_users: int,
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
    third_multiplier = max_tie + 1
    second_multiplier = (third_max + 1) * third_multiplier
    first_multiplier = (second_max + 1) * second_multiplier
    maximum = (
        first_max * first_multiplier
        + second_max * second_multiplier
        + third_max * third_multiplier
        + max_tie
    )
    if maximum >= 2**63:
        raise ValueError("node allocation objective exceeds CP-SAT int64 range")
    return (
        first * first_multiplier
        + second * second_multiplier
        + third * third_multiplier
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
        "capacity_coverage": coverage,
        "capacity_deficits": coverage,
        "diagnostics": [message],
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
    for workload_index, workload in enumerate(workloads):
        affected = model.new_bool_var(f"affected-workload:{workload_index}")
        affected_flags[workload_index] = affected
        for node_name, gpu in workload.placements:
            node_index = next(index for index, (name, _) in enumerate(nodes) if name == node_name)
            for group_index, group_name in enumerate(group_names):
                if group_name != workload.group:
                    model.add(affected >= assignment[node_index, group_index])
                    affected_gpu_terms.append(gpu * assignment[node_index, group_index])
        user_flag = user_flags.setdefault(
            workload.user,
            model.new_bool_var(f"affected-user:{len(user_flags)}"),
        )
        model.add(user_flag >= affected)

    affected_workloads = sum(affected_flags.values())
    affected_gpu = sum(affected_gpu_terms)
    affected_users = sum(user_flags.values())
    tie_break = sum(
        (node_index * len(group_names) + group_index + 1) * variable
        for (node_index, group_index), variable in assignment.items()
    )
    model.minimize(_objective(
        strategy, affected_workloads, affected_gpu, affected_users, tie_break,
        len(workloads), sum(gpu for _, gpu in nodes), len(user_flags),
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
    diagnostics = []
    if status_name == "INFEASIBLE":
        diagnostics.append("finite GPU quotas cannot all be covered by a mutually exclusive node assignment")
    elif status_name not in {"OPTIMAL", "FEASIBLE"}:
        diagnostics.append("solver did not return a complete node assignment within the time limit")
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
