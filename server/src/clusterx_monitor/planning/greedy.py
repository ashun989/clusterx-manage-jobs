from __future__ import annotations

import time

from .domain import PlanningProblem
from .verify import fit_nodes


def greedy_seed(
    problem: PlanningProblem, strategy: str, deadline: float | None = None,
) -> tuple[str, ...]:
    """Produce a verified inclusion-minimal solution for CP-SAT hints/fallback."""
    workloads = problem.workload_by_id
    selected = {item.workload_id for item in problem.workloads}
    if len(fit_nodes(problem, selected)) < problem.required_new_nodes:
        return ()

    if strategy == "min-users":
        users: dict[str, set[str]] = {}
        for item in problem.workloads:
            users.setdefault(item.user_id, set()).add(item.workload_id)
        for _, user_ids in sorted(
            users.items(),
            key=lambda item: (
                -sum(workloads[wid].total_gpu for wid in item[1]),
                -len(item[1]), item[0],
            ),
        ):
            if deadline is not None and time.monotonic() >= deadline:
                return tuple(sorted(selected))
            reduced = selected - user_ids
            if len(fit_nodes(problem, reduced)) >= problem.required_new_nodes:
                selected = reduced

    order = sorted(
        selected,
        key=lambda workload_id: (
            -workloads[workload_id].total_gpu,
            workload_id,
        ),
    )
    for workload_id in order:
        if deadline is not None and time.monotonic() >= deadline:
            break
        reduced = selected - {workload_id}
        if len(fit_nodes(problem, reduced)) >= problem.required_new_nodes:
            selected = reduced
    return tuple(sorted(selected))
