from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable


class SnapshotStore:
    """Thread-safe bounded store for complete immutable-by-convention snapshots."""

    def __init__(self, capacity: int = 5, history_capacity: int = 2880) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if history_capacity < capacity:
            raise ValueError("history_capacity must be at least snapshot capacity")
        self.capacity = capacity
        self.history_capacity = history_capacity
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._history: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = RLock()
        self.last_error: str | None = None
        self.last_attempt_at: str | None = None

    def publish(self, snapshot: dict[str, Any]) -> None:
        snapshot_id = str(snapshot["snapshot_id"])
        with self._lock:
            self._items[snapshot_id] = deepcopy(snapshot)
            self._items.move_to_end(snapshot_id)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
            self._history[snapshot_id] = self._history_point(snapshot)
            self._history.move_to_end(snapshot_id)
            while len(self._history) > self.history_capacity:
                self._history.popitem(last=False)
            self.last_error = None
            self.last_attempt_at = datetime.now(timezone.utc).isoformat()

    def record_failure(self, error: Exception | str) -> None:
        with self._lock:
            self.last_error = str(error)
            self.last_attempt_at = datetime.now(timezone.utc).isoformat()

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._items:
                return None
            return deepcopy(next(reversed(self._items.values())))

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(snapshot_id)
            return deepcopy(item) if item is not None else None

    @staticmethod
    def _history_point(snapshot: dict[str, Any]) -> dict[str, Any]:
        capacity = snapshot.get("capacity") or {}
        pressure = snapshot.get("pending_pressure") or {}
        telemetry = snapshot.get("telemetry") or {}
        nodes = snapshot.get("nodes") or []
        classifications: dict[str, int] = {}
        for node in nodes:
            classification = str(node.get("classification") or "unknown")
            classifications[classification] = classifications.get(classification, 0) + 1
        alerts = snapshot.get("alerts") or []
        return {
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "generated_at": snapshot.get("generated_at"),
            "bound_gpu": capacity.get("bound_gpu"),
            "planning_eligible_gpu": capacity.get("planning_eligible_gpu"),
            "allocated_gpu": capacity.get("allocated_gpu"),
            "free_gpu": capacity.get("free_gpu"),
            "pending_workloads": len(snapshot.get("pending_workloads") or []),
            "pending_eligible_jobs": int(pressure.get("eligible_jobs") or 0),
            "alert_count": len(alerts),
            "critical_alert_count": sum(
                str(alert.get("severity") or "").lower() in {"critical", "error"}
                for alert in alerts
            ),
            "gpu_compute_util_avg_pct": telemetry.get("gpu_compute_util_avg_pct"),
            "gpu_memory_util_avg_pct": telemetry.get("gpu_memory_util_avg_pct"),
            "gpu_power_total_w": telemetry.get("gpu_power_total_w"),
            "node_classifications": classifications,
        }

    def history(self, limit: int = 240) -> dict[str, Any]:
        with self._lock:
            points = list(self._history.values())[-limit:]
            return {
                "points": deepcopy(points),
                "retained_snapshots": len(self._items),
                "history_capacity": self.history_capacity,
                "window_started_at": points[0].get("generated_at") if points else None,
                "newest_at": points[-1].get("generated_at") if points else None,
            }

    def index(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(self._history_point(item)) for item in reversed(self._items.values())]

    def compare(self, from_snapshot_id: str, to_snapshot_id: str) -> dict[str, Any] | None:
        with self._lock:
            before = self._items.get(from_snapshot_id)
            after = self._items.get(to_snapshot_id)
            if before is None or after is None:
                return None
            before_point = self._history_point(before)
            after_point = self._history_point(after)
            numeric_fields = (
                "bound_gpu", "planning_eligible_gpu", "allocated_gpu", "free_gpu",
                "pending_workloads", "pending_eligible_jobs", "alert_count",
                "critical_alert_count", "gpu_compute_util_avg_pct",
                "gpu_memory_util_avg_pct", "gpu_power_total_w",
            )
            deltas = {
                field: (
                    after_point[field] - before_point[field]
                    if isinstance(after_point.get(field), (int, float))
                    and isinstance(before_point.get(field), (int, float)) else None
                )
                for field in numeric_fields
            }
            before_workloads = {
                str(item.get("workload_id"))
                for item in [*(before.get("workloads") or []), *(before.get("pending_workloads") or [])]
            }
            after_workloads = {
                str(item.get("workload_id"))
                for item in [*(after.get("workloads") or []), *(after.get("pending_workloads") or [])]
            }
            return {
                "from": before_point,
                "to": after_point,
                "deltas": deltas,
                "workloads_added": sorted(after_workloads - before_workloads),
                "workloads_removed": sorted(before_workloads - after_workloads),
            }

    def status(self, stale_after_seconds: float) -> dict[str, Any]:
        latest = self.latest()
        age: float | None = None
        if latest:
            generated = datetime.fromisoformat(latest["generated_at"])
            age = max(0.0, (datetime.now(timezone.utc) - generated).total_seconds())
        return {
            "ready": latest is not None,
            "snapshot_id": latest.get("snapshot_id") if latest else None,
            "generated_at": latest.get("generated_at") if latest else None,
            "age_seconds": age,
            "stale": age is None or age > stale_after_seconds,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "retained_snapshots": len(self._items),
            "history_points": len(self._history),
        }


class PlanCache:
    def __init__(self, capacity: int = 128) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = RLock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
                return deepcopy(value)
            return None

    def get_or_compute(
        self, key: str, compute: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        cached = self.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached
        value = compute()
        value["cache_hit"] = False
        with self._lock:
            self._items[key] = deepcopy(value)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._items[key] = deepcopy(value)
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
