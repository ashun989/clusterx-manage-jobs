from __future__ import annotations

from .domain import PlanningProblem, ResourceVector, VerifiedSelection


class InvalidPlanError(RuntimeError):
    pass


def fit_nodes(problem: PlanningProblem, selected: set[str]) -> tuple[str, ...]:
    workloads = problem.workload_by_id
    unknown = selected.difference(workloads)
    if unknown:
        raise InvalidPlanError(f"solver selected unknown workloads: {sorted(unknown)!r}")
    fitted: list[str] = []
    for node in problem.candidate_nodes:
        released = ResourceVector()
        for workload_id in selected:
            released = released.plus(workloads[workload_id].releases.get(node.node_id, ResourceVector()))
        if released.covers(node.deficit):
            fitted.append(node.node_id)
    return tuple(sorted(fitted))


def verify_selection(problem: PlanningProblem, selected: set[str]) -> VerifiedSelection:
    fitted = fit_nodes(problem, selected)
    if len(fitted) < problem.required_new_nodes:
        raise InvalidPlanError(
            f"solver plan frees {len(fitted)} nodes, expected {problem.required_new_nodes}"
        )
    chosen = tuple(sorted(selected))
    return VerifiedSelection(
        selected=chosen,
        target_nodes=fitted[:problem.required_new_nodes],
        newly_schedulable_nodes=fitted,
    )
