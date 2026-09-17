from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping


CPU_SCALE = 1_000
MEMORY_SCALE = 1_024
MODEL_VERSION = 2


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def floor_units(value: Any, scale: int = 1) -> int:
    """Conservatively convert available or releasable capacity to integers."""
    return max(0, int((_decimal(value) * scale).to_integral_value(rounding=ROUND_FLOOR)))


def ceil_units(value: Any, scale: int = 1) -> int:
    """Conservatively convert a requested resource quantity to integers."""
    return max(0, int((_decimal(value) * scale).to_integral_value(rounding=ROUND_CEILING)))


def clean_number(value: float) -> float | int:
    return int(value) if value.is_integer() else round(value, 6)


@dataclass(frozen=True)
class ResourceVector:
    gpu: int = 0
    cpu_millis: int = 0
    memory_mib: int = 0

    def plus(self, other: "ResourceVector") -> "ResourceVector":
        return ResourceVector(
            self.gpu + other.gpu,
            self.cpu_millis + other.cpu_millis,
            self.memory_mib + other.memory_mib,
        )

    def deficit_from(self, available: "ResourceVector") -> "ResourceVector":
        return ResourceVector(
            max(0, self.gpu - available.gpu),
            max(0, self.cpu_millis - available.cpu_millis),
            max(0, self.memory_mib - available.memory_mib),
        )

    def covers(self, required: "ResourceVector") -> bool:
        return (
            self.gpu >= required.gpu
            and self.cpu_millis >= required.cpu_millis
            and self.memory_mib >= required.memory_mib
        )


@dataclass(frozen=True)
class CandidateNode:
    node_id: str
    deficit: ResourceVector


@dataclass(frozen=True)
class CandidateWorkload:
    workload_id: str
    user_id: str
    group_id: str
    total_gpu: int
    total_cpu: float | int | None
    total_memory_gib: float | int | None
    releases: Mapping[str, ResourceVector]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class PlanningProblem:
    snapshot_id: str
    requested_nodes: int
    required_new_nodes: int
    already_free_nodes: tuple[str, ...]
    candidate_nodes: tuple[CandidateNode, ...]
    workloads: tuple[CandidateWorkload, ...]

    @property
    def workload_by_id(self) -> dict[str, CandidateWorkload]:
        return {item.workload_id: item for item in self.workloads}


@dataclass(frozen=True)
class PreparedPlan:
    problem: PlanningProblem | None
    common: Mapping[str, Any]
    already_free_nodes: tuple[str, ...]
    no_plan_reason: str | None = None


@dataclass(frozen=True)
class SolveAttempt:
    status: str
    selected: tuple[str, ...]
    objective_value: int | None
    best_objective_bound: int | None
    wall_time_seconds: float
    deterministic_time_seconds: float
    branches: int
    conflicts: int
    backend: str = "cp-sat"


@dataclass(frozen=True)
class VerifiedSelection:
    selected: tuple[str, ...]
    target_nodes: tuple[str, ...]
    newly_schedulable_nodes: tuple[str, ...]
