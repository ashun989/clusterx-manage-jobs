from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import math
import json
from pathlib import Path
import sqlite3
import time
from threading import RLock
from typing import Any, Callable


HISTORY_NUMERIC_FIELDS = (
    "bound_gpu", "planning_eligible_gpu", "allocated_gpu", "free_gpu",
    "pending_workloads", "pending_eligible_jobs", "alert_count",
    "critical_alert_count", "gpu_compute_util_avg_pct",
    "gpu_memory_util_avg_pct", "gpu_power_total_w",
)


def _timestamp(value: str | None) -> float:
    if not value:
        return datetime.now(timezone.utc).timestamp()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class SQLiteHistoryStore:
    """Structured, aggregate-only trend history with bounded local retention."""

    def __init__(
        self, path: str | Path, *, retention_days: int = 30,
        max_points: int = 100_000, max_db_mib: int = 256,
    ) -> None:
        if retention_days < 1 or max_points < 2 or max_db_mib < 1:
            raise ValueError("history limits must be positive")
        self.path = Path(path).expanduser().resolve()
        self.retention_days = retention_days
        self.max_points = max_points
        self.max_db_mib = max_db_mib
        self._lock = RLock()
        self.last_error: str | None = None
        self._failure_cooldown_until = 0.0
        self._checkpoint_at = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(f"PRAGMA wal_autocheckpoint = {max(1, min(1000, self.max_db_mib * 64))}")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS history_points (
                    snapshot_id TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL,
                    generated_ts REAL NOT NULL,
                    bound_gpu REAL,
                    planning_eligible_gpu REAL,
                    allocated_gpu REAL,
                    free_gpu REAL,
                    pending_workloads INTEGER NOT NULL,
                    pending_eligible_jobs INTEGER NOT NULL,
                    alert_count INTEGER NOT NULL,
                    critical_alert_count INTEGER NOT NULL,
                    gpu_compute_util_avg_pct REAL,
                    gpu_memory_util_avg_pct REAL,
                    gpu_power_total_w REAL
                );
                CREATE INDEX IF NOT EXISTS history_points_generated_ts
                    ON history_points(generated_ts);
                CREATE TABLE IF NOT EXISTS history_node_classifications (
                    snapshot_id TEXT NOT NULL REFERENCES history_points(snapshot_id) ON DELETE CASCADE,
                    classification TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(snapshot_id, classification)
                );
                CREATE TABLE IF NOT EXISTS history_alert_severities (
                    snapshot_id TEXT NOT NULL REFERENCES history_points(snapshot_id) ON DELETE CASCADE,
                    severity TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(snapshot_id, severity)
                );
            """)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > 1:
                raise RuntimeError(f"unsupported history schema version {version}")
            # Version 1 is the initial schema.  Keep this explicit so future
            # migrations can be added without silently accepting old layouts.
            if version < 1:
                connection.execute("PRAGMA user_version = 1")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"history database integrity check failed: {integrity}")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def publish(self, point: dict[str, Any], alerts: list[dict[str, Any]]) -> None:
        if time.monotonic() < self._failure_cooldown_until:
            return
        severities: dict[str, int] = {}
        for alert in alerts:
            severity = str(alert.get("severity") or "unknown").lower()
            severities[severity] = severities.get(severity, 0) + 1
        values = [point.get(field) for field in HISTORY_NUMERIC_FIELDS]
        try:
            checkpoint_due = False
            with self._lock, closing(self._connect()) as connection, connection:
                connection.execute(
                    f"INSERT OR REPLACE INTO history_points (snapshot_id, generated_at, generated_ts, {', '.join(HISTORY_NUMERIC_FIELDS)}) VALUES ({', '.join('?' for _ in range(3 + len(HISTORY_NUMERIC_FIELDS)))})",
                    [point["snapshot_id"], point.get("generated_at") or datetime.now(timezone.utc).isoformat(), _timestamp(point.get("generated_at")), *values],
                )
                connection.execute("DELETE FROM history_node_classifications WHERE snapshot_id = ?", (point["snapshot_id"],))
                connection.executemany(
                    "INSERT INTO history_node_classifications(snapshot_id, classification, count) VALUES (?, ?, ?)",
                    [(point["snapshot_id"], key, int(value)) for key, value in (point.get("node_classifications") or {}).items()],
                )
                connection.execute("DELETE FROM history_alert_severities WHERE snapshot_id = ?", (point["snapshot_id"],))
                connection.executemany(
                    "INSERT INTO history_alert_severities(snapshot_id, severity, count) VALUES (?, ?, ?)",
                    [(point["snapshot_id"], key, value) for key, value in severities.items()],
                )
                cutoff = datetime.now(timezone.utc).timestamp() - self.retention_days * 86_400
                connection.execute("DELETE FROM history_points WHERE generated_ts < ?", (cutoff,))
                connection.execute(
                    "DELETE FROM history_points WHERE snapshot_id IN (SELECT snapshot_id FROM history_points ORDER BY generated_ts DESC, snapshot_id DESC LIMIT -1 OFFSET ?)",
                    (self.max_points,),
                )
                self._enforce_size(connection)
                checkpoint_due = time.monotonic() >= self._checkpoint_at
            if checkpoint_due:
                with self._lock, closing(self._connect()) as checkpoint_connection:
                    checkpoint_connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                self._checkpoint_at = time.monotonic() + 60
            self.last_error = None
            self._failure_cooldown_until = 0.0
        except Exception as error:
            self.last_error = str(error)
            self._failure_cooldown_until = time.monotonic() + 30
            raise

    def _enforce_size(self, connection: sqlite3.Connection) -> None:
        max_bytes = self.max_db_mib * 1024 * 1024
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        for _ in range(32):
            pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
            free = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            wal_size = Path(f"{self.path}-wal").stat().st_size if Path(f"{self.path}-wal").exists() else 0
            shm_size = Path(f"{self.path}-shm").stat().st_size if Path(f"{self.path}-shm").exists() else 0
            if (pages - free) * page_size + wal_size + shm_size <= max_bytes:
                return
            count = int(connection.execute("SELECT COUNT(*) FROM history_points").fetchone()[0])
            if count <= 1:
                return
            remove = max(1, count // 4)
            connection.execute(
                "DELETE FROM history_points WHERE snapshot_id IN (SELECT snapshot_id FROM history_points ORDER BY generated_ts, snapshot_id LIMIT ?)",
                (remove,),
            )

    @staticmethod
    def _resolution(span_seconds: float, max_result_points: int, requested: int | None) -> int:
        if requested is not None:
            return max(1, requested)
        needed = max(1, math.ceil(span_seconds / max_result_points))
        for step in (1, 60, 300, 900, 3600, 21_600, 86_400, 604_800):
            if step >= needed:
                return step
        return needed

    def history(
        self, *, limit: int = 240, since: str | None = None,
        until: str | None = None, resolution_seconds: int | None = None,
        max_result_points: int = 800,
    ) -> dict[str, Any]:
        try:
            with self._lock, closing(self._connect()) as connection:
                if since is None and until is None:
                    rows = list(reversed(connection.execute(
                        "SELECT * FROM history_points ORDER BY generated_ts DESC, snapshot_id DESC LIMIT ?", (limit,),
                    ).fetchall()))
                else:
                    lower = _timestamp(since) if since else 0
                    upper = _timestamp(until) if until else datetime.now(timezone.utc).timestamp()
                    rows = connection.execute(
                        "SELECT * FROM history_points WHERE generated_ts BETWEEN ? AND ? ORDER BY generated_ts, snapshot_id", (lower, upper),
                    ).fetchall()
                ids = [str(row["snapshot_id"]) for row in rows]
                classifications: dict[str, dict[str, int]] = {item: {} for item in ids}
                for offset in range(0, len(ids), 500):
                    chunk = ids[offset:offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    for row in connection.execute(f"SELECT snapshot_id, classification, count FROM history_node_classifications WHERE snapshot_id IN ({placeholders})", chunk):
                        classifications[str(row["snapshot_id"])][str(row["classification"])] = int(row["count"])
                points = [{
                    "snapshot_id": str(row["snapshot_id"]), "generated_at": str(row["generated_at"]),
                    **{field: row[field] for field in HISTORY_NUMERIC_FIELDS},
                    "node_classifications": classifications[str(row["snapshot_id"])],
                } for row in rows]
                resolution = 1
                if points and (since is not None or until is not None):
                    span = max(1, _timestamp(points[-1]["generated_at"]) - _timestamp(points[0]["generated_at"]))
                    resolution = self._resolution(span, max_result_points, resolution_seconds)
                    points = self._downsample(points, resolution)
                total = int(connection.execute("SELECT COUNT(*) FROM history_points").fetchone()[0])
            self.last_error = None
            return {
                "points": points[-max_result_points:], "retained_snapshots": total,
                "history_capacity": self.max_points,
                "window_started_at": points[0].get("generated_at") if points else None,
                "newest_at": points[-1].get("generated_at") if points else None,
                "storage": "sqlite", "resolution_seconds": resolution,
                "retention_days": self.retention_days,
            }
        except Exception as error:
            self.last_error = str(error)
            raise

    @staticmethod
    def _downsample(points: list[dict[str, Any]], resolution: int) -> list[dict[str, Any]]:
        buckets: dict[int, list[dict[str, Any]]] = {}
        for point in points:
            bucket = int(_timestamp(point.get("generated_at")) // resolution)
            buckets.setdefault(bucket, []).append(point)
        result: list[dict[str, Any]] = []
        for items in buckets.values():
            latest = items[-1]
            merged: dict[str, Any] = {"snapshot_id": latest["snapshot_id"], "generated_at": latest["generated_at"]}
            for field in HISTORY_NUMERIC_FIELDS:
                values = [float(item[field]) for item in items if isinstance(item.get(field), (int, float))]
                merged[field] = round(sum(values) / len(values), 6) if values else None
            keys = {key for item in items for key in item.get("node_classifications", {})}
            merged["node_classifications"] = {
                key: round(sum(float(item.get("node_classifications", {}).get(key, 0)) for item in items) / len(items), 3)
                for key in sorted(keys)
            }
            result.append(merged)
        return result

    def status(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            count = int(connection.execute("SELECT COUNT(*) FROM history_points").fetchone()[0])
            oldest = connection.execute("SELECT generated_at FROM history_points ORDER BY generated_ts LIMIT 1").fetchone()
            newest = connection.execute("SELECT generated_at FROM history_points ORDER BY generated_ts DESC LIMIT 1").fetchone()
        size = sum(
            candidate.stat().st_size for candidate in (
                self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"),
            ) if candidate.exists()
        )
        return {"enabled": True, "storage": "sqlite", "points": count, "oldest_at": oldest[0] if oldest else None, "newest_at": newest[0] if newest else None, "retention_days": self.retention_days, "max_points": self.max_points, "max_db_mib": self.max_db_mib, "database_bytes": size, "integrity": integrity, "last_error": self.last_error}


class SnapshotStore:
    """Thread-safe bounded store for complete immutable-by-convention snapshots."""

    def __init__(self, capacity: int = 5, history_capacity: int = 2880, persistent_history: SQLiteHistoryStore | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if history_capacity < capacity:
            raise ValueError("history_capacity must be at least snapshot capacity")
        self.capacity = capacity
        self.history_capacity = history_capacity
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._history: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.persistent_history = persistent_history
        self.history_error: str | None = None
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
            point = self._history_point(snapshot)
            self._history[snapshot_id] = point
            self._history.move_to_end(snapshot_id)
            while len(self._history) > self.history_capacity:
                self._history.popitem(last=False)
            self.last_error = None
            self.last_attempt_at = datetime.now(timezone.utc).isoformat()
        if self.persistent_history is not None and time.monotonic() >= self.persistent_history._failure_cooldown_until:
            try:
                self.persistent_history.publish(point, list(snapshot.get("alerts") or []))
                self.history_error = None
            except Exception as error:
                self.history_error = str(error)

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

    def history(
        self, limit: int = 240, *, since: str | None = None,
        until: str | None = None, resolution_seconds: int | None = None,
    ) -> dict[str, Any]:
        persistent = self.persistent_history
        if persistent is not None and time.monotonic() >= persistent._failure_cooldown_until:
            try:
                result = persistent.history(
                    limit=limit, since=since, until=until,
                    resolution_seconds=resolution_seconds,
                )
                self.history_error = None
                return result
            except Exception as error:
                self.history_error = str(error)
                persistent._failure_cooldown_until = time.monotonic() + 30
        with self._lock:
            points = list(self._history.values())[-limit:]
            return {
                "points": deepcopy(points),
                "retained_snapshots": len(self._items),
                "history_capacity": self.history_capacity,
                "window_started_at": points[0].get("generated_at") if points else None,
                "newest_at": points[-1].get("generated_at") if points else None,
                "storage": "memory", "resolution_seconds": 1,
                "error": self.history_error,
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
        result = {
            "ready": latest is not None,
            "snapshot_id": latest.get("snapshot_id") if latest else None,
            "generated_at": latest.get("generated_at") if latest else None,
            "age_seconds": age,
            "stale": age is None or age > stale_after_seconds,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "retained_snapshots": len(self._items),
            "history_points": len(self._history),
            "history": {"enabled": False, "storage": "memory", "points": len(self._history), "last_error": self.history_error},
        }
        if self.persistent_history is not None and time.monotonic() >= self.persistent_history._failure_cooldown_until:
            try:
                result["history"] = self.persistent_history.status()
                result["history"]["last_error"] = self.history_error or result["history"].get("last_error")
            except Exception as error:
                self.history_error = str(error)
                result["history"] = {"enabled": True, "storage": "sqlite", "points": 0, "last_error": self.history_error}
        elif self.persistent_history is not None:
            result["history"] = {
                "enabled": True, "storage": "memory", "points": len(self._history),
                "last_error": self.history_error, "degraded": True,
            }
        return result


class PlanCache:
    def __init__(self, capacity: int = 128, max_bytes: int = 32 * 1024 * 1024) -> None:
        if capacity < 1 or max_bytes < 1024:
            raise ValueError("plan cache limits must be positive")
        self.capacity = capacity
        self.max_bytes = max_bytes
        self._bytes = 0
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
            self._put_locked(key, value)
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._put_locked(key, value)

    def _put_locked(self, key: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        previous = self._items.pop(key, None)
        if previous is not None:
            self._bytes -= len(json.dumps(previous, ensure_ascii=False, separators=(",", ":"), default=str).encode())
        if len(encoded) > self.max_bytes:
            return
        self._items[key] = deepcopy(value)
        self._items.move_to_end(key)
        self._bytes += len(encoded)
        while len(self._items) > self.capacity or self._bytes > self.max_bytes:
            old_key, old_value = self._items.popitem(last=False)
            self._bytes -= len(json.dumps(old_value, ensure_ascii=False, separators=(",", ":"), default=str).encode())
