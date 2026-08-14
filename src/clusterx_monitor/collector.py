from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import math
import re
from typing import Any, Iterable
from uuid import uuid4

from .models import SCHEMA_VERSION


GPU_COMPUTE_UTIL = "gpu-compute-util"
GPU_MEMORY_UTIL = "gpu-memory-util"
GPU_POWER = "gpu-power"
HISTORY_COMPUTE_UTIL = "history-gpu-compute-util"
HISTORY_MEMORY_UTIL = "history-gpu-memory-util"
HISTORY_COMPUTE_SAMPLES = "history-gpu-compute-samples"
HISTORY_MEMORY_SAMPLES = "history-gpu-memory-samples"


def resource_number(value: Any, *, memory: bool = False) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(m|[KMGTPE]i?B?)?\s*", str(value), re.I)
    if not match:
        raise ValueError(f"unsupported resource value: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if not memory:
        return number / 1000 if unit == "m" else number
    factors = {
        "": 1 / 1024**3, "b": 1 / 1024**3,
        "k": 1000 / 1024**3, "kb": 1000 / 1024**3,
        "m": 1000**2 / 1024**3, "mb": 1000**2 / 1024**3,
        "g": 1000**3 / 1024**3, "gb": 1000**3 / 1024**3,
        "t": 1000**4 / 1024**3, "tb": 1000**4 / 1024**3,
        "ki": 1024 / 1024**3, "kib": 1024 / 1024**3,
        "mi": 1024**2 / 1024**3, "mib": 1024**2 / 1024**3,
        "gi": 1, "gib": 1, "ti": 1024, "tib": 1024,
    }
    if unit not in factors:
        raise ValueError(f"unsupported memory unit: {value!r}")
    return number * factors[unit]


def _clean(value: float) -> int | float:
    return int(round(value)) if math.isclose(value, round(value), abs_tol=1e-9) else round(value, 6)


def _optional_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return _clean(number) if math.isfinite(number) and number >= 0 else None


def _optional_resource_number(value: Any, *, memory: bool = False) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = resource_number(value, memory=memory)
    except (TypeError, ValueError):
        return None
    return _clean(number) if math.isfinite(number) and number >= 0 else None


def _resource_total(rows: Iterable[dict[str, Any]], key: str) -> int | float | None:
    values = [row.get(key) for row in rows]
    if not values or any(value is None for value in values):
        return None
    return _clean(sum(float(value) for value in values))


def _resource_map(node: dict[str, Any]) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for item in node.get("summary_data") or []:
        kind = str(item.get("resource_type") or "").upper()
        allocated = float(item.get("allocated") or 0)
        total = float(item.get("total") or 0)
        if kind == "MEMORY" and str(item.get("unit")) != "GiB":
            allocated /= 1024**3
            total /= 1024**3
        result[kind] = (allocated, total)
    return result


def normalize_nodes(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for raw in rows:
        resources = _resource_map(raw)
        nodes.append({
            "node": str(raw.get("name") or ""),
            "id": str(raw.get("id") or ""),
            "host_ip": str(raw.get("host_ip") or ""),
            "state": str(raw.get("state") or "UNKNOWN"),
            "allocated_gpu": _clean(resources.get("DEVICE", (0, 0))[0]),
            "total_gpu": _clean(resources.get("DEVICE", (0, 0))[1]),
            "allocated_cpu": _clean(resources.get("CPU", (0, 0))[0]),
            "total_cpu": _clean(resources.get("CPU", (0, 0))[1]),
            "allocated_memory_gib": _clean(resources.get("MEMORY", (0, 0))[0]),
            "total_memory_gib": _clean(resources.get("MEMORY", (0, 0))[1]),
            "workloads": {},
            "unattributed": {"gpu": 0, "cpu": 0, "memory_gib": 0},
            "attribution_excess": {"gpu": 0, "cpu": 0, "memory_gib": 0},
            "planning_eligible": True,
            "planning_exclusion_reasons": [],
        })
    return sorted(nodes, key=lambda item: item["node"])


def _node_signature(rows: Iterable[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            n["node"], n["id"], n["state"],
            n["allocated_gpu"], n["total_gpu"],
            n["allocated_cpu"], n["total_cpu"],
            n["allocated_memory_gib"], n["total_memory_gib"],
        )
        for n in normalize_nodes(rows)
    )


def _list_node_pods(cluster: Any, cluster_name: str, queue: str, node_id: str) -> list[dict[str, Any]]:
    client = cluster.client
    path = (
        f"/subscriptions/{client.subscription}/resourceGroups/{client.resource_group}/"
        f"regions/{client.region}/clusters/{cluster_name}/pods"
    )
    result: list[dict[str, Any]] = []
    skip = 0
    while True:
        response = client._make_management_request(
            "GET", path,
            params={"node_id": node_id, "queue_name": queue, "page_size": 100, "skip": skip},
        )
        page = response.get("pods") or []
        result.extend(page)
        total = int(response.get("total_size") or len(result))
        if not page or len(result) >= total:
            return result
        skip += len(page)


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _query_prometheus(cluster: Any, promql: str) -> list[dict[str, Any]]:
    from clusterx.launcher.ssp.prometheus_stats import build_cache_path, get_query_token
    cache_path = build_cache_path(
        cluster.cfg["subscription"], cluster.cfg["resource_group"],
        cluster.cfg["region"], cluster.cfg["workspace"],
    )
    token = get_query_token(cluster.client, cache_path=cache_path)["query_token"]
    try:
        response = cluster.client.query_prometheus(token, promql)
    except Exception as error:
        code = getattr(getattr(error, "response", None), "status_code", None)
        if code not in {401, 403}:
            raise
        cache_path.unlink(missing_ok=True)
        token = get_query_token(cluster.client, cache_path=cache_path)["query_token"]
        response = cluster.client.query_prometheus(token, promql)
    if response.get("status") not in {None, "success"}:
        raise RuntimeError(f"Prometheus query failed: {response.get('error') or response}")
    return list((response.get("data") or {}).get("result") or [])


def _prometheus_selector(cluster: Any, queue: str, cluster_name: str) -> str:
    labels = {
        "label_resource_compute_sensecore_cn_workspace_name": cluster.cfg["workspace"],
        "label_resource_compute_sensecore_cn_cluster_name": cluster_name,
        "label_resource_compute_sensecore_cn_queue_name": queue,
    }
    return ",".join(f'{key}="{_escape(value)}"' for key, value in labels.items())


def _query_gpu_telemetry(cluster: Any, queue: str, cluster_name: str, minutes: int) -> list[dict[str, Any]]:
    selector = _prometheus_selector(cluster, queue, cluster_name)
    expressions = [
        "label_replace("
        f"avg_over_time(lepton__ssp__gpu_util{{{selector}}}[{minutes}m]),"
        f'"monitor_metric","{GPU_COMPUTE_UTIL}","__name__",".*")',
        "label_replace(100 * "
        f"avg_over_time(lepton__ssp__gpu_memory_used__MiB{{{selector}}}[{minutes}m]) / "
        f"avg_over_time(lepton__ssp__gpu_memory_total__MiB{{{selector}}}[{minutes}m]),"
        f'"monitor_metric","{GPU_MEMORY_UTIL}","__name__",".*")',
        "label_replace("
        f"avg_over_time(lepton__ssp__gpu_power_usage{{{selector}}}[{minutes}m]),"
        f'"monitor_metric","{GPU_POWER}","__name__",".*")',
    ]
    return _query_prometheus(cluster, " or ".join(f"({item})" for item in expressions))


def _query_workload_history(
    cluster: Any, queue: str, cluster_name: str, window_hours: int,
) -> dict[str, dict[str, float]]:
    """Return sample-weighted historical GPU telemetry grouped by workload UID."""
    selector = _prometheus_selector(cluster, queue, cluster_name)
    uid = "label_resource_compute_sensecore_cn_workload_uid"
    window = f"{window_hours}h"
    util = f"lepton__ssp__gpu_util{{{selector}}}"
    used = f"lepton__ssp__gpu_memory_used__MiB{{{selector}}}"
    total = f"lepton__ssp__gpu_memory_total__MiB{{{selector}}}"
    compute_count = f"sum by ({uid}) (count_over_time({util}[{window}]))"
    memory_count = f"sum by ({uid}) (count_over_time({used}[{window}]))"
    expressions = [
        "label_replace("
        f"sum by ({uid}) (sum_over_time({util}[{window}])) / {compute_count},"
        f'"monitor_metric","{HISTORY_COMPUTE_UTIL}","__name__",".*")',
        "label_replace(100 * "
        f"sum by ({uid}) (sum_over_time({used}[{window}])) / "
        f"sum by ({uid}) (sum_over_time({total}[{window}])),"
        f'"monitor_metric","{HISTORY_MEMORY_UTIL}","__name__",".*")',
        "label_replace("
        f"{compute_count},"
        f'"monitor_metric","{HISTORY_COMPUTE_SAMPLES}","__name__",".*")',
        "label_replace("
        f"{memory_count},"
        f'"monitor_metric","{HISTORY_MEMORY_SAMPLES}","__name__",".*")',
    ]
    series = _query_prometheus(cluster, " or ".join(f"({item})" for item in expressions))
    fields = {
        HISTORY_COMPUTE_UTIL: "gpu_compute_util_avg_pct",
        HISTORY_MEMORY_UTIL: "gpu_memory_util_avg_pct",
        HISTORY_COMPUTE_SAMPLES: "compute_sample_count",
        HISTORY_MEMORY_SAMPLES: "memory_sample_count",
    }
    result: dict[str, dict[str, float]] = {}
    duplicates: set[tuple[str, str]] = set()
    for item in series:
        labels = item.get("metric") or {}
        workload_id = str(labels.get(uid) or "")
        field = fields.get(str(labels.get("monitor_metric") or ""))
        if not workload_id or field is None:
            continue
        key = (workload_id, field)
        if key in duplicates or field in result.get(workload_id, {}):
            duplicates.add(key)
            result.get(workload_id, {}).pop(field, None)
            continue
        try:
            value = float((item.get("value") or [None, None])[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(value):
            result.setdefault(workload_id, {})[field] = round(value, 2)
    return result


def _attach_telemetry(
    workloads: dict[str, dict[str, Any]], series: Iterable[dict[str, Any]],
    hostname_to_node: dict[str, str],
) -> list[str]:
    expected: dict[tuple[str, str, str], int] = {}
    for workload_id, workload in workloads.items():
        workload["gpus"] = []
        for placement in workload.get("placements") or []:
            count = int(placement.get("gpu") or 0)
            if count:
                key = (workload_id, str(placement["node"]), str(placement.get("pod") or ""))
                expected[key] = expected.get(key, 0) + count

    records: dict[tuple[tuple[str, str, str], str], dict[str, Any]] = {}
    ambiguous: set[tuple[str, str, str]] = set()
    fields = {
        GPU_COMPUTE_UTIL: "gpu_compute_util_pct",
        GPU_MEMORY_UTIL: "gpu_memory_util_pct",
        GPU_POWER: "gpu_power_w",
    }
    for item in series:
        labels = item.get("metric") or {}
        workload_id = str(labels.get("label_resource_compute_sensecore_cn_workload_uid") or "")
        node = hostname_to_node.get(str(labels.get("Hostname") or ""), "")
        pod = str(labels.get("exported_pod") or labels.get("pod") or "")
        pod_key = (workload_id, node, pod)
        if pod_key not in expected:
            continue
        uuid = str(labels.get("UUID") or "")
        device = str(labels.get("gpu") or labels.get("device") or "")
        identity = uuid or (f"device:{device}" if device else "")
        field = fields.get(str(labels.get("monitor_metric") or ""))
        if not identity or field is None:
            ambiguous.add(pod_key)
            continue
        try:
            value = float((item.get("value") or [None, None])[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(value):
            continue
        record = records.setdefault((pod_key, identity), {
            "node": node, "pod": pod, "device_index": device or None,
            "gpu_uuid": uuid or None, "gpu_compute_util_pct": None,
            "gpu_memory_util_pct": None, "gpu_power_w": None,
        })
        if record[field] is not None:
            ambiguous.add(pod_key)
        else:
            record[field] = round(value, 2)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (pod_key, _), value in records.items():
        grouped.setdefault(pod_key, []).append(value)
    for key, values in grouped.items():
        if len(values) > expected[key]:
            ambiguous.add(key)
    for key, values in grouped.items():
        if key not in ambiguous:
            workloads[key[0]]["gpus"].extend(sorted(values, key=lambda item: (item["node"], item.get("device_index") or "")))
    covered = sum(len(item["gpus"]) for item in workloads.values())
    allocated = sum(int(item.get("total_gpu") or 0) for item in workloads.values())
    warnings = []
    if ambiguous:
        warnings.append(f"ignored {len(ambiguous)} ambiguous Pod telemetry mappings")
    if covered < allocated:
        warnings.append(f"per-GPU telemetry covered {covered}/{allocated} attributed GPUs")
    return warnings


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _pending_workloads(cluster: Any, queue: str) -> tuple[list[dict[str, Any]], bool]:
    queue_id = cluster._get_queue_id(queue)
    response = cluster.client.list_training_jobs(
        filter_str=f'queue_id="{queue_id}" AND state="PENDING"', page_size=1000
    )
    jobs = response.get("training_jobs") or response.get("trainingJobs") or []
    total = response.get("total_size") or response.get("total")
    complete = total is None or int(total) <= len(jobs)
    now = datetime.now(timezone.utc)
    result = []
    for job in jobs:
        spec = job.get("spec") or {}
        tasks = ((spec.get("vc_job") or {}).get("tasks") or [])
        task_resources = []
        for index, task in enumerate(tasks):
            resource = task.get("resource_spec") or {}
            replicas = max(0, int(task.get("replicas") or 0))
            task_resources.append({
                "name": str(task.get("name") or f"task-{index + 1}"),
                "role": str(task.get("role") or ""),
                "replicas": replicas,
                "gpu_per_replica": _optional_number(resource.get("accelerate_device_count")) or 0,
                "cpu_per_replica": _optional_number(resource.get("cpu_count")),
                "memory_gib_per_replica": _optional_number(resource.get("memory_gib")),
            })
        resource_shapes = {
            (
                item["gpu_per_replica"],
                item["cpu_per_replica"],
                item["memory_gib_per_replica"],
            )
            for item in task_resources
        }
        homogeneous = len(resource_shapes) == 1
        per_replica = task_resources[0] if homogeneous and task_resources else {}
        active_tasks = [item for item in task_resources if item["replicas"] > 0]

        def requested_total(key: str) -> int | float | None:
            if not active_tasks or any(item[key] is None for item in active_tasks):
                return None
            return _clean(sum(float(item[key]) * item["replicas"] for item in active_tasks))

        status = job.get("status") or {}
        created = _parse_time(status.get("create_time"))
        result.append({
            "workload_id": str(job.get("name") or ""),
            "workload_name": str(job.get("display_name") or job.get("name") or ""),
            "user": str((job.get("ownership") or {}).get("creator_name") or "unknown").lower(),
            "status": "PENDING",
            "create_time": created.isoformat() if created else None,
            "queue_age_seconds": max(0, (now - created).total_seconds()) if created else None,
            "num_nodes": sum(item["replicas"] for item in task_resources),
            "gpus_per_node": per_replica.get("gpu_per_replica"),
            "cpus_per_node": per_replica.get("cpu_per_replica"),
            "memory_per_node_gib": per_replica.get("memory_gib_per_replica"),
            "total_gpu": requested_total("gpu_per_replica") or 0,
            "total_cpu": requested_total("cpu_per_replica"),
            "total_memory_gib": requested_total("memory_gib_per_replica"),
            "resource_basis": "requested",
            "task_resources": task_resources,
        })
    return result, complete


class ClusterCollector:
    def __init__(self, cluster: Any, queue: str, cluster_name: str) -> None:
        self.cluster = cluster
        self.queue = queue
        self.cluster_name = cluster_name
        self._history_cache: dict[str, dict[str, float]] = {}
        self._history_fetched_at: datetime | None = None
        self._history_last_attempt_at: datetime | None = None
        self._history_window_hours: int | None = None
        self._history_query_available = False

    def collect(
        self, *, telemetry_minutes: int = 5,
        historical_window_hours: int = 24,
        historical_refresh_minutes: int = 5,
    ) -> dict[str, Any]:
        raw = self.cluster.client.list_queue_nodes(
            cluster=self.cluster_name, queue=self.queue, page_size=100, is_bound=True
        )
        raw_nodes = raw.get("nodes") or []
        total = raw.get("total_size") or raw.get("total")
        if total is not None and int(total) > len(raw_nodes):
            raise RuntimeError("queue node response is truncated")
        nodes = normalize_nodes(raw_nodes)
        node_by_name = {item["node"]: item for item in nodes}
        occupied = [
            item for item in nodes
            if item["allocated_gpu"] or item["allocated_cpu"] or item["allocated_memory_gib"]
        ]
        pods_by_node: dict[str, list[dict[str, Any]]] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = {
                pool.submit(_list_node_pods, self.cluster, self.cluster_name, self.queue, item["id"]): item["node"]
                for item in occupied
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    pods_by_node[name] = future.result()
                except Exception:
                    failures.append(name)
        if failures:
            raise RuntimeError(f"pod workload mapping failed for {len(failures)} occupied nodes")

        workloads: dict[str, dict[str, Any]] = {}
        hostname_to_node: dict[str, str] = {}
        warnings: list[str] = []
        for node in nodes:
            if node["host_ip"]:
                hostname_to_node["host-" + node["host_ip"].replace(".", "-")] = node["node"]
            attributed = {"gpu": 0.0, "cpu": 0.0, "memory_gib": 0.0}
            for pod in pods_by_node.get(node["node"], []):
                raw_workload = pod.get("workload") or {}
                workload_id = str(raw_workload.get("uid") or raw_workload.get("id") or "")
                if not workload_id:
                    raise RuntimeError(f"pod workload identity is missing on {node['node']}")
                resource = pod.get("resource") or {}
                placement = {
                    "node": node["node"], "pod": str(pod.get("name") or ""),
                    "gpu": _clean(float(resource.get("accelerate_device_count") or 0)),
                    "cpu": _optional_resource_number(resource.get("cpu")),
                    "memory_gib": _optional_resource_number(resource.get("memory"), memory=True),
                }
                for key in attributed:
                    if placement[key] is not None:
                        attributed[key] += float(placement[key])
                node_row = node["workloads"].setdefault(workload_id, {"gpu": 0, "cpu": 0, "memory_gib": 0})
                for key in node_row:
                    if placement[key] is not None:
                        node_row[key] = _clean(float(node_row[key]) + float(placement[key]))
                ownership = pod.get("ownership") or {}
                workspace = pod.get("workspace") or {}
                workload = workloads.setdefault(workload_id, {
                    "workload_id": workload_id,
                    "workload_name": str(raw_workload.get("display_name") or raw_workload.get("name") or workload_id),
                    "user": str(ownership.get("creator_name") or "unknown").lower(),
                    "type": str(raw_workload.get("type") or "unknown"),
                    "workspace": str(workspace.get("name") or ""),
                    "create_time": None, "start_time": raw_workload.get("start_time"),
                    "placements": [], "gpus": [], "total_gpu": 0,
                    "total_cpu": None, "total_memory_gib": None,
                    "resource_basis": "attributed", "task_resources": [],
                })
                timestamp = _parse_time(pod.get("create_time"))
                current = _parse_time(workload.get("create_time"))
                if timestamp and (current is None or timestamp < current):
                    workload["create_time"] = timestamp.isoformat()
                workload["placements"].append(placement)
            allocated = {
                "gpu": float(node["allocated_gpu"]), "cpu": float(node["allocated_cpu"]),
                "memory_gib": float(node["allocated_memory_gib"]),
            }
            for key, value in allocated.items():
                delta = value - attributed[key]
                if delta > 1e-6:
                    node["unattributed"][key] = _clean(delta)
                elif delta < -1e-6:
                    node["attribution_excess"][key] = _clean(-delta)
                    warnings.append(f"Pod-attributed {key} exceeds node allocation on {node['node']}")
            excess = node["attribution_excess"]
            if any(float(value or 0) > 0 for value in excess.values()):
                node["planning_eligible"] = False
                node["planning_exclusion_reasons"] = ["attribution.resource_excess"]
        for workload in workloads.values():
            workload["total_gpu"] = _clean(sum(float(p["gpu"]) for p in workload["placements"]))
            workload["total_cpu"] = _resource_total(workload["placements"], "cpu")
            workload["total_memory_gib"] = _resource_total(workload["placements"], "memory_gib")

        try:
            telemetry = _query_gpu_telemetry(self.cluster, self.queue, self.cluster_name, telemetry_minutes)
            warnings.extend(_attach_telemetry(workloads, telemetry, hostname_to_node))
            telemetry_available = True
        except Exception:
            _attach_telemetry(workloads, [], hostname_to_node)
            telemetry_available = False
            warnings.append("per-GPU telemetry is unavailable")

        history_now = datetime.now(timezone.utc)
        history_due = (
            self._history_last_attempt_at is None
            or self._history_window_hours != historical_window_hours
            or (history_now - self._history_last_attempt_at).total_seconds()
            >= historical_refresh_minutes * 60
        )
        history_available = self._history_query_available and self._history_fetched_at is not None
        if history_due:
            self._history_last_attempt_at = history_now
            self._history_window_hours = historical_window_hours
            try:
                self._history_cache = _query_workload_history(
                    self.cluster, self.queue, self.cluster_name,
                    historical_window_hours,
                )
                self._history_fetched_at = history_now
                self._history_query_available = True
                history_available = True
            except Exception:
                self._history_query_available = False
                history_available = False
                warnings.append("historical workload GPU telemetry is unavailable")
        history_fetched_at = self._history_fetched_at.isoformat() if self._history_fetched_at else None
        for workload in workloads.values():
            values = self._history_cache.get(str(workload["workload_id"]), {}) if history_available else {}
            workload["historical_telemetry"] = {
                "window_hours": historical_window_hours,
                "fetched_at": history_fetched_at if history_available else None,
                "collection_status": "available" if values else "unavailable",
                "gpu_compute_util_avg_pct": values.get("gpu_compute_util_avg_pct"),
                "gpu_memory_util_avg_pct": values.get("gpu_memory_util_avg_pct"),
                "compute_sample_count": int(values.get("compute_sample_count", 0)),
                "memory_sample_count": int(values.get("memory_sample_count", 0)),
            }
        try:
            pending, pending_complete = _pending_workloads(self.cluster, self.queue)
        except Exception:
            pending, pending_complete = [], False
            warnings.append("pending workload inventory is unavailable")

        after_response = self.cluster.client.list_queue_nodes(
            cluster=self.cluster_name, queue=self.queue, page_size=100, is_bound=True
        )
        after = after_response.get("nodes") or []
        after_total = after_response.get("total_size") or after_response.get("total")
        if after_total is not None and int(after_total) > len(after):
            raise RuntimeError("queue node response is truncated")
        if _node_signature(raw_nodes) != _node_signature(after):
            raise RuntimeError("queue node allocation changed during collection")
        generated = datetime.now(timezone.utc)
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"{generated.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}",
            "generated_at": generated.isoformat(),
            "cluster": self.cluster_name,
            "queue": self.queue,
            "telemetry_window_minutes": telemetry_minutes,
            "telemetry_available": telemetry_available,
            "historical_telemetry_status": "available" if history_available else "unavailable",
            "nodes": nodes,
            "workloads": sorted(workloads.values(), key=lambda item: (item["user"], item["workload_name"])),
            "pending_workloads": pending,
            "pending_complete": pending_complete,
            "warnings": warnings,
        }
