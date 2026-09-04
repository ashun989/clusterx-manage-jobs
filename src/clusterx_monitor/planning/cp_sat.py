from __future__ import annotations

import os
from typing import Iterable

from ortools.sat.python import cp_model

from .domain import PlanningProblem, SolveAttempt


class InvalidSolverModelError(RuntimeError):
    pass


def configured_solver_workers() -> int:
    raw = os.environ.get("CLUSTERX_PLANNER_WORKERS", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise InvalidSolverModelError("CLUSTERX_PLANNER_WORKERS must be an integer from 1 to 4") from error
    if not 1 <= value <= 4:
        raise InvalidSolverModelError("CLUSTERX_PLANNER_WORKERS must be between 1 and 4")
    return value


def _objective(
    problem: PlanningProblem,
    strategy: str,
    x: dict[str, cp_model.IntVar],
    z: dict[str, cp_model.IntVar],
) -> cp_model.LinearExpr:
    gpu = sum(item.total_gpu * x[item.workload_id] for item in problem.workloads)
    count = sum(x.values())
    users = sum(z.values())
    max_gpu = sum(item.total_gpu for item in problem.workloads)
    max_count = len(problem.workloads)
    max_users = len(z)
    if strategy == "min-workloads":
        first, second, third = count, gpu, users
        first_max, second_max, third_max = max_count, max_gpu, max_users
    elif strategy == "min-users":
        first, second, third = users, gpu, count
        first_max, second_max, third_max = max_users, max_gpu, max_count
    else:
        first, second, third = gpu, count, users
        first_max, second_max, third_max = max_gpu, max_count, max_users
    multiplier = (second_max + 1) * (third_max + 1)
    maximum = (
        first_max * multiplier
        + second_max * (third_max + 1)
        + third_max
    )
    if maximum >= 2**63:
        raise InvalidSolverModelError("planner objective exceeds CP-SAT int64 range")
    return first * multiplier + second * (third_max + 1) + third


def _build_model(
    problem: PlanningProblem,
    strategy: str,
    excluded: Iterable[tuple[str, ...]],
    hint: tuple[str, ...],
) -> tuple[cp_model.CpModel, dict[str, cp_model.IntVar]]:
    model = cp_model.CpModel()
    x = {
        item.workload_id: model.new_bool_var(f"workload:{index}")
        for index, item in enumerate(problem.workloads)
    }
    y = {
        item.node_id: model.new_bool_var(f"node:{index}")
        for index, item in enumerate(problem.candidate_nodes)
    }
    users: dict[str, list[cp_model.IntVar]] = {}
    for item in problem.workloads:
        users.setdefault(item.user_id, []).append(x[item.workload_id])
    z = {
        user: model.new_bool_var(f"user:{index}")
        for index, user in enumerate(sorted(users))
    }
    for user, variables in users.items():
        for variable in variables:
            model.add(variable <= z[user])
        model.add(z[user] <= sum(variables))

    by_id = problem.workload_by_id
    for node in problem.candidate_nodes:
        touching = [
            (workload_id, by_id[workload_id].releases[node.node_id])
            for workload_id in x
            if node.node_id in by_id[workload_id].releases
        ]
        model.add(
            sum(release.gpu * x[workload_id] for workload_id, release in touching)
            >= node.deficit.gpu * y[node.node_id]
        )
        model.add(
            sum(release.cpu_millis * x[workload_id] for workload_id, release in touching)
            >= node.deficit.cpu_millis * y[node.node_id]
        )
        model.add(
            sum(release.memory_mib * x[workload_id] for workload_id, release in touching)
            >= node.deficit.memory_mib * y[node.node_id]
        )
    model.add(sum(y.values()) >= problem.required_new_nodes)

    all_ids = tuple(x)
    for selection in excluded:
        selected = set(selection)
        same = [x[workload_id] if workload_id in selected else 1 - x[workload_id]
                for workload_id in all_ids]
        model.add(sum(same) <= len(all_ids) - 1)

    model.minimize(_objective(problem, strategy, x, z))
    hinted = set(hint)
    for workload_id, variable in x.items():
        model.add_hint(variable, int(workload_id in hinted))
    return model, x


def solve_once(
    problem: PlanningProblem,
    strategy: str,
    excluded: Iterable[tuple[str, ...]],
    hint: tuple[str, ...],
    time_limit_seconds: float,
) -> SolveAttempt:
    model, x = _build_model(problem, strategy, excluded, hint)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.01, time_limit_seconds)
    solver.parameters.num_search_workers = configured_solver_workers()
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    name = solver.status_name(status).upper()
    if status == cp_model.MODEL_INVALID:
        raise InvalidSolverModelError(solver.solution_info())
    has_solution = status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
    selected = tuple(sorted(
        workload_id for workload_id, variable in x.items()
        if has_solution and solver.boolean_value(variable)
    ))
    return SolveAttempt(
        status=name,
        selected=selected,
        objective_value=int(round(solver.objective_value)) if has_solution else None,
        best_objective_bound=(
            int(round(solver.best_objective_bound))
            if status in {cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN}
            else None
        ),
        wall_time_seconds=round(solver.wall_time, 6),
        deterministic_time_seconds=round(solver.deterministic_time, 6),
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
    )
