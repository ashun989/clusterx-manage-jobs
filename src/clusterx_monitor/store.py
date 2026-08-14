from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable


class SnapshotStore:
    """Thread-safe bounded store for complete immutable-by-convention snapshots."""

    def __init__(self, capacity: int = 5) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
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
