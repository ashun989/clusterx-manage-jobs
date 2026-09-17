from __future__ import annotations

import time
from typing import Any

from ..models import PlanRequest
from .cp_sat import solve_once
from .domain import MODEL_VERSION, PlanningProblem, PreparedPlan, SolveAttempt, clean_number
from .greedy import greedy_seed
from .verify import verify_selection


def _optional_sum(items: list[object]) -> float | int | None:
    if any(value is None for value in items):
        return None
    return clean_number(sum(float(value) for value in items))


def _plan(
    problem: PlanningProblem,
    strategy: str,
    rank: int,
    attempt: SolveAttempt,
) -> dict[str, Any]:
    verified = verify_selection(problem, set(attempt.selected))
    workloads = problem.workload_by_id
    details = [workloads[item] for item in verified.selected]
    return {
        "strategy": strategy,
        "rank": rank,
        "rank_status": attempt.status,
        "rank_backend": attempt.backend,
        "objective_value": attempt.objective_value,
        "best_objective_bound": attempt.best_objective_bound,
        "workloads": list(verified.selected),
        "workload_count": len(verified.selected),
        "users": len({item.user_id for item in details}),
        "groups": len({item.group_id for item in details}),
        "gpus": sum(item.total_gpu for item in details),
        "cpus": _optional_sum([item.total_cpu for item in details]),
        "memory_gib": _optional_sum([item.total_memory_gib for item in details]),
        "freed_nodes": list(verified.target_nodes),
        "target_nodes": list(verified.target_nodes),
        "newly_schedulable_nodes": list(verified.newly_schedulable_nodes),
        "workload_details": [dict(item.raw) for item in details],
    }


def _strategy_result(
    problem: PlanningProblem,
    request: PlanRequest,
    strategy: str,
    deadline: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seed = greedy_seed(problem, strategy, deadline)
    excluded: list[tuple[str, ...]] = []
    attempts: list[SolveAttempt] = []
    plans: list[dict[str, Any]] = []
    termination = "alternatives-complete"
    exhausted = False
    for rank in range(1, request.alternatives + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.01:
            termination = "time-limit"
            break
        attempt = solve_once(problem, strategy, excluded, seed, remaining)
        attempts.append(attempt)
        if attempt.status in {"OPTIMAL", "FEASIBLE"}:
            plans.append(_plan(problem, strategy, rank, attempt))
            if attempt.status != "OPTIMAL":
                termination = "time-limit"
                break
            excluded.append(attempt.selected)
            seed = attempt.selected
            continue
        if attempt.status == "INFEASIBLE":
            termination = "search-exhausted"
            exhausted = True
            break
        termination = "time-limit"
        if not plans and seed:
            fallback = SolveAttempt(
                status="HEURISTIC",
                selected=seed,
                objective_value=None,
                best_objective_bound=attempt.best_objective_bound,
                wall_time_seconds=attempt.wall_time_seconds,
                deterministic_time_seconds=attempt.deterministic_time_seconds,
                branches=attempt.branches,
                conflicts=attempt.conflicts,
                backend="greedy-fallback",
            )
            plans.append(_plan(problem, strategy, 1, fallback))
        break
    if not plans and seed:
        fallback = SolveAttempt(
            status="HEURISTIC",
            selected=seed,
            objective_value=None,
            best_objective_bound=None,
            wall_time_seconds=0,
            deterministic_time_seconds=0,
            branches=0,
            conflicts=0,
            backend="greedy-fallback",
        )
        plans.append(_plan(problem, strategy, 1, fallback))
    primary_status = plans[0]["rank_status"] if plans else (
        attempts[-1].status if attempts else "UNKNOWN"
    )
    top_k_complete = (
        len(plans) >= request.alternatives
        and all(item["rank_status"] == "OPTIMAL" for item in plans)
    ) or exhausted
    return ({
        "strategy": strategy,
        "status": primary_status,
        "termination_reason": termination,
        "top_k_complete": top_k_complete,
        "requested_alternatives": request.alternatives,
        "returned_alternatives": len(plans),
        "wall_time_seconds": round(sum(item.wall_time_seconds for item in attempts), 6),
        "deterministic_time_seconds": round(
            sum(item.deterministic_time_seconds for item in attempts), 6,
        ),
        "branches": sum(item.branches for item in attempts),
        "conflicts": sum(item.conflicts for item in attempts),
        "plans": plans,
    }, plans)


def solve_prepared_plan(
    snapshot: dict[str, Any],
    request: PlanRequest,
    prepared: PreparedPlan,
    started: float,
) -> dict[str, Any]:
    base = {
        "snapshot_id": request.snapshot_id,
        "snapshot_generated_at": snapshot["generated_at"],
        "currently_schedulable_nodes": list(prepared.already_free_nodes),
        "no_plan_reason": prepared.no_plan_reason,
        **prepared.common,
    }
    if prepared.problem is None:
        not_needed = prepared.no_plan_reason is None
        strategy_results = [] if not_needed else [
            {
                "strategy": strategy,
                "status": "INFEASIBLE",
                "termination_reason": "precheck-infeasible",
                "top_k_complete": True,
                "requested_alternatives": request.alternatives,
                "returned_alternatives": 0,
                "wall_time_seconds": 0,
                "deterministic_time_seconds": 0,
                "branches": 0,
                "conflicts": 0,
                "plans": [],
            }
            for strategy in request.strategies
        ]
        return {
            **base,
            "optimality": "not-needed" if not_needed else "exact",
            "search_elapsed_seconds": round(time.monotonic() - started, 4),
            "plans": [],
            "strategy_results": strategy_results,
            "solver": {
                "backend": "cp-sat",
                "model_version": MODEL_VERSION,
                "status": "NOT_NEEDED" if not_needed else "INFEASIBLE",
                "time_limit_seconds": request.search_seconds,
                "wall_time_seconds": 0,
            },
        }

    problem = prepared.problem
    deadline = started + request.search_seconds
    strategy_results: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    for index, strategy in enumerate(request.strategies):
        remaining_strategies = len(request.strategies) - index
        remaining = max(0.01, deadline - time.monotonic())
        strategy_deadline = min(
            deadline, time.monotonic() + remaining / remaining_strategies,
        )
        result, items = _strategy_result(
            problem, request, strategy, strategy_deadline,
        )
        strategy_results.append(result)
        plans.extend(items)
    primary_exact = all(result["status"] == "OPTIMAL" for result in strategy_results)
    request_exact = primary_exact and all(
        result["top_k_complete"] for result in strategy_results
    )
    elapsed = round(time.monotonic() - started, 4)
    solver_status = (
        "OPTIMAL" if request_exact else
        "PARTIAL" if primary_exact else
        "FEASIBLE" if plans else
        "UNKNOWN"
    )
    return {
        **base,
        "no_plan_reason": None if plans else "solver-time-limit",
        "optimality": "exact" if request_exact else "heuristic",
        "search_elapsed_seconds": elapsed,
        "plans": plans,
        "strategy_results": strategy_results,
        "solver": {
            "backend": "cp-sat",
            "model_version": MODEL_VERSION,
            "status": solver_status,
            "time_limit_seconds": request.search_seconds,
            "wall_time_seconds": elapsed,
            "candidate_node_count": len(problem.candidate_nodes),
            "candidate_workload_count": len(problem.workloads),
        },
    }
