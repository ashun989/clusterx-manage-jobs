from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PendingPressureConfig(FrozenModel):
    min_wait_minutes: int = Field(default=10, ge=0, le=1440)
    min_jobs: int = Field(default=1, ge=1, le=1000)


class DevelopmentConfig(FrozenModel):
    zero_gpu_max_cpu_per_node: float = Field(default=8, gt=0, le=4096)
    zero_gpu_max_memory_gib_per_node: float = Field(default=140, gt=0, le=65536)
    one_gpu_max_cpu_per_node: float = Field(default=14, gt=0, le=4096)
    one_gpu_max_memory_gib_per_node: float = Field(default=240, gt=0, le=65536)
    max_gpu: int = Field(default=1, ge=0, le=1024)
    max_instances_per_user: int = Field(default=1, ge=1, le=1000)
    one_gpu_max_runtime_hours: float = Field(default=72, ge=0, le=8760)


class TrainingConfig(FrozenModel):
    cpu_per_gpu: float = Field(default=14, gt=0, le=1024)
    memory_gib_per_gpu: float = Field(default=240, gt=0, le=16384)
    zero_gpu_max_cpu_per_node: float = Field(default=14, gt=0, le=4096)
    zero_gpu_max_memory_gib_per_node: float = Field(default=240, gt=0, le=65536)


class PlanningConfig(FrozenModel):
    default_cpu_per_gpu: float = Field(default=14, gt=0, le=1024)
    default_memory_gib_per_gpu: float = Field(default=240, gt=0, le=16384)


class LowUtilizationConfig(FrozenModel):
    window_hours: int = Field(default=24, ge=1, le=168)
    refresh_minutes: int = Field(default=5, ge=1, le=60)
    min_observation_minutes: int = Field(default=60, ge=0, le=10_080)
    gpu_compute_threshold_pct: float = Field(default=20, ge=0, le=100)
    gpu_memory_threshold_pct: float = Field(default=20, ge=0, le=100)


class GroupConfig(FrozenModel):
    gpu_quota: int | Literal["remainder"] | None = None
    cpu_quota: float | None = None
    memory_quota_gib: float | None = None
    members: tuple[str, ...] = ()

    @field_validator("members", mode="before")
    @classmethod
    def normalize_members(cls, value: object) -> tuple[str, ...]:
        return tuple(str(item).strip().lower() for item in (value or []))

    @field_validator("gpu_quota")
    @classmethod
    def validate_gpu_quota(cls, value: int | str | None) -> int | str | None:
        if isinstance(value, int) and not 0 <= value <= 100_000:
            raise ValueError("gpu_quota must be between 0 and 100000")
        return value

    @field_validator("cpu_quota", "memory_quota_gib")
    @classmethod
    def validate_scalar_quota(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("resource quota must be a finite non-negative number")
        return value


class PolicyConfig(FrozenModel):
    schema_version: int = 1
    refresh_seconds: float = Field(default=30, ge=10, le=3600)
    telemetry_lookback_minutes: int = Field(default=5, ge=1, le=60)
    pending_pressure: PendingPressureConfig = PendingPressureConfig()
    development: DevelopmentConfig = DevelopmentConfig()
    training: TrainingConfig = TrainingConfig()
    planning: PlanningConfig = PlanningConfig()
    low_utilization: LowUtilizationConfig = LowUtilizationConfig()
    groups: dict[str, GroupConfig]

    @model_validator(mode="after")
    def validate_groups(self) -> "PolicyConfig":
        if self.schema_version != 1:
            raise ValueError("unsupported policy schema_version")
        if "default" not in self.groups:
            raise ValueError("groups.default is required")
        if self.planning.default_cpu_per_gpu > self.training.cpu_per_gpu:
            raise ValueError("planning.default_cpu_per_gpu must not exceed training.cpu_per_gpu")
        if self.planning.default_memory_gib_per_gpu > self.training.memory_gib_per_gpu:
            raise ValueError(
                "planning.default_memory_gib_per_gpu must not exceed training.memory_gib_per_gpu"
            )
        remainder_groups = [
            name for name, group in self.groups.items()
            if group.gpu_quota == "remainder"
        ]
        if any(name != "default" for name in remainder_groups):
            raise ValueError("only the default group may use remainder quota")
        owners: dict[str, str] = {}
        for group_name, group in self.groups.items():
            for user in group.members:
                if not user:
                    raise ValueError(f"empty member in group {group_name}")
                previous = owners.get(user)
                if previous is not None:
                    raise ValueError(
                        f"user {user!r} appears in both {previous!r} and {group_name!r}"
                    )
                owners[user] = group_name
        return self


class PlanTarget(FrozenModel):
    nodes: int = Field(ge=1, le=1024)
    gpus_per_node: int = Field(default=8, ge=1, le=1024)
    cpus_per_node: float | None = Field(default=None, gt=0, le=1_000_000)
    memory_per_node_gib: float | None = Field(default=None, gt=0, le=10_000_000)


class PlanFilters(FrozenModel):
    workload_types: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    workloads: tuple[str, ...] = ()
    exclude_workloads: tuple[str, ...] = ()
    exclude_users: tuple[str, ...] = ()
    over_quota_only: bool = False
    violation_categories: tuple[str, ...] = ()
    violation_codes: tuple[str, ...] = ()
    violation_tags: tuple[str, ...] = ()


class PlanRequest(FrozenModel):
    snapshot_id: str = Field(min_length=1)
    target: PlanTarget
    strategies: tuple[Literal["min-gpu", "min-workloads", "min-users"], ...] = (
        "min-gpu",
    )
    candidate_scope: Literal["fragmented", "full", "all"] = "fragmented"
    alternatives: int = Field(default=1, ge=1, le=10)
    search_seconds: float = Field(default=10, ge=1, le=30)
    filters: PlanFilters = PlanFilters()

    @field_validator("strategies")
    @classmethod
    def unique_strategies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one strategy is required")
        return tuple(dict.fromkeys(value))
