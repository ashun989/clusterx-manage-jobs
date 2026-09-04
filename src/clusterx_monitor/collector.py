from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import math
import re
import time
from typing import Any, Iterable
from urllib.parse import quote, urlencode
from uuid import uuid4

from .models import SCHEMA_VERSION


GPU_COMPUTE_UTIL = "gpu-compute-util"
GPU_MEMORY_UTIL = "gpu-memory-util"
GPU_POWER = "gpu-power"
HISTORY_COMPUTE_UTIL = "history-gpu-compute-util"
HISTORY_MEMORY_UTIL = "history-gpu-memory-util"
HISTORY_COMPUTE_SAMPLES = "history-gpu-compute-samples"
HISTORY_MEMORY_SAMPLES = "history-gpu-memory-samples"
CONSOLE_ORIGIN = "https://console.d.pjlab.org.cn"
CONSOLE_ROUTES = {
    "trainingJob": ("training/detail/", "trainingJobs"),
    "aid": ("development/detail", "aids"),
    "air": ("air/detail/", "airs"),
}
NODE_PAGE_SIZE = 100
MAX_NODE_PAGES = 1000
MAX_PAGE_ITEMS = 100_000
RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/regions/([^/]+)/"
    r"workspaces/([^/]+)/(trainingJobs|aids|airs)/([^/]+)$"
)


def _response_total(response: dict[str, Any], context: str, *, default: int | None = None) -> int | None:
    """Parse the current Clusterx pagination field.

    Clusterx 2026.8.19 consistently names the field ``total_size``.  Older
    monitor code accepted the undocumented ``total`` alias, which made a
    response from a stale endpoint look valid and could silently publish an
    incomplete inventory.  Keep the parser strict so an API migration fails
    loudly instead of changing completeness semantics.
    """
    if "total" in response:
        raise RuntimeError(f"{context} response uses unsupported total field")
    if "total_size" not in response:
        return default
    raw_total = response["total_size"]
    if isinstance(raw_total, bool):
        raise RuntimeError(f"{context} total_size must be an integer")
    try:
        total = int(raw_total)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{context} total_size must be an integer") from error
    if isinstance(raw_total, float) and not raw_total.is_integer():
        raise RuntimeError(f"{context} total_size must be an integer")
    if total < 0:
        raise RuntimeError(f"{context} total_size must be non-negative")
    return total


def _training_job_page(response: dict[str, Any], context: str) -> list[dict[str, Any]]:
    """Return a typed TrainingJob page using the current snake-case key."""
    if "trainingJobs" in response:
        raise RuntimeError(f"{context} response uses unsupported trainingJobs field")
    if "training_jobs" not in response:
        return []
    page = response["training_jobs"]
    if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
        raise RuntimeError(f"{context} jobs must be a list of objects")
    return page


def _list_training_jobs_page(
    cluster: Any, queue_id: str, state: str, page_token: str | None,
) -> dict[str, Any]:
    """Fetch one TrainingJob page using the 2026.8.19 token contract.

    The installed SDK exposes ``page_token`` in the public API documentation
    but its convenience method has not yet added the keyword.  Use that
    method for the first request (preserving its authentication setup) and
    the same client's signed request primitive for subsequent pages.
    """
    filter_str = f'queue_id="{queue_id}" AND state="{state}"'
    client = cluster.client
    if page_token is None:
        return client.list_training_jobs(filter_str=filter_str, page_size=1000)
    path = f"{client._get_base_path()}trainingJobs"
    return client._make_request(
        "GET",
        path,
        params={"filter": filter_str, "page_size": 1000, "page_token": page_token},
    )


def _config_segment(cluster: Any, key: str) -> str | None:
    cfg = getattr(cluster, "cfg", None)
    value = cfg.get(key) if isinstance(cfg, dict) else None
    if not isinstance(value, str) or not value or any(char in value for char in "/?#"):
        return None
    return value


def _workload_console_url(
    cluster: Any, kind: str, *, resource_id: Any = None,
    resource_name: Any = None, workspace: Any = None,
) -> str | None:
    route = CONSOLE_ROUTES.get(kind)
    if route is None:
        return None
    detail_path, collection = route
    rid = str(resource_id or "")
    match = RESOURCE_ID_PATTERN.fullmatch(rid)
    if match and match.group(5) == collection:
        region = match.group(3)
    else:
        name = str(resource_name or "")
        workspace_name = str(workspace or "") or (_config_segment(cluster, "workspace") or "")
        scope = {
            "subscription": _config_segment(cluster, "subscription"),
            "resource_group": _config_segment(cluster, "resource_group"),
            "region": _config_segment(cluster, "region"),
            "workspace": workspace_name if workspace_name and not any(char in workspace_name for char in "/?#") else None,
            "name": name if name and not any(char in name for char in "/?#") else None,
        }
        if any(value is None for value in scope.values()):
            return None
        region = str(scope["region"])
        rid = (
            f"/subscriptions/{scope['subscription']}/resourceGroups/{scope['resource_group']}/"
            f"regions/{region}/workspaces/{scope['workspace']}/{collection}/{scope['name']}"
        )
    return f"{CONSOLE_ORIGIN}/{quote(region, safe='')}/ssp/model/{detail_path}?{urlencode({'rid': rid})}"


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
    summary = node.get("summary_data") or []
    if not isinstance(summary, list):
        raise RuntimeError("node summary_data must be a list")
    for item in summary:
        if not isinstance(item, dict):
            raise RuntimeError("node summary entry must be an object")
        kind = str(item.get("resource_type") or "").upper()
        unit = str(item.get("unit") or "").strip()
        try:
            if kind == "MEMORY" and unit and unit.lower() not in {"gib", "gi"}:
                allocated = resource_number(f"{item.get('allocated')}{unit}", memory=True)
                total = resource_number(f"{item.get('total')}{unit}", memory=True)
            else:
                allocated = float(item.get("allocated") or 0)
                total = float(item.get("total") or 0)
                if kind == "CPU" and unit.lower() in {"m", "mcpu", "millicpu"}:
                    allocated /= 1000
                    total /= 1000
        except (TypeError, ValueError) as error:
            raise RuntimeError("node resource values must be numeric") from error
        if not math.isfinite(allocated) or not math.isfinite(total) or allocated < 0 or total < 0:
            raise RuntimeError("node resource values must be finite and non-negative")
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


def _list_queue_nodes(cluster: Any, cluster_name: str, queue: str) -> list[dict[str, Any]]:
    """Return a complete, internally consistent bound-node inventory."""
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()
    page_token: str | None = None
    expected_total: int | None = None

    for _ in range(MAX_NODE_PAGES):
        arguments: dict[str, Any] = {
            "cluster": cluster_name,
            "queue": queue,
            "page_size": NODE_PAGE_SIZE,
            "is_bound": True,
        }
        if page_token is not None:
            arguments["page_token"] = page_token
        response = cluster.client.list_queue_nodes(**arguments)
        if not isinstance(response, dict):
            raise RuntimeError("queue node response must be an object")

        page = response.get("nodes")
        if page is None:
            page = []
        if not isinstance(page, list):
            raise RuntimeError("queue node response nodes must be a list")

        page_total = _response_total(response, "queue node")
        if page_total is not None:
            if expected_total is None:
                expected_total = page_total
            elif page_total != expected_total:
                raise RuntimeError("queue node total_size changed during pagination")

        for raw in page:
            if not isinstance(raw, dict):
                raise RuntimeError("queue node entry must be an object")
            node_id = str(raw.get("id") or "")
            node_name = str(raw.get("name") or "")
            if not node_id and not node_name:
                raise RuntimeError("queue node entry has no identity")
            if node_id and node_id in seen_ids:
                raise RuntimeError("queue node pagination returned a duplicate node id")
            if node_name and node_name in seen_names:
                raise RuntimeError("queue node pagination returned a duplicate node name")
            if node_id:
                seen_ids.add(node_id)
            if node_name:
                seen_names.add(node_name)
            nodes.append(raw)

        if len(nodes) > MAX_PAGE_ITEMS:
            raise RuntimeError("queue node pagination exceeded the record limit")

        if expected_total is not None and len(nodes) > expected_total:
            raise RuntimeError("queue node pagination exceeded total_size")

        next_token = response.get("next_page_token")
        if next_token is None or next_token == "":
            if expected_total is not None and len(nodes) != expected_total:
                raise RuntimeError("queue node response is truncated")
            return nodes
        if not isinstance(next_token, str) or not next_token.strip():
            raise RuntimeError("queue node next_page_token must be a non-empty string")
        if not page:
            raise RuntimeError("queue node pagination returned an empty non-final page")
        if expected_total is not None and len(nodes) >= expected_total:
            raise RuntimeError("queue node pagination continued after total_size")
        if next_token in seen_tokens:
            raise RuntimeError("queue node pagination cursor repeated")
        seen_tokens.add(next_token)
        page_token = next_token

    raise RuntimeError("queue node pagination exceeded the page limit")


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
    """List pods through the management endpoint's established offset API."""
    client = cluster.client
    path = (
        f"/subscriptions/{client.subscription}/resourceGroups/{client.resource_group}/"
        f"regions/{client.region}/clusters/{cluster_name}/pods"
    )
    result: list[dict[str, Any]] = []
    skip = 0
    seen_offsets: set[int] = set()
    seen_pods: set[str] = set()
    expected_total: int | None = None
    for _ in range(MAX_NODE_PAGES):
        params: dict[str, Any] = {"node_id": node_id, "queue_name": queue, "page_size": 100}
        params["skip"] = skip
        response = client._make_management_request("GET", path, params=params)
        if not isinstance(response, dict):
            raise RuntimeError("pod response must be an object")
        page = response.get("pods") or []
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise RuntimeError("pod response pods must be a list of objects")
        for item in page:
            identity = str(item.get("uid") or item.get("id") or item.get("name") or "")
            if identity and identity in seen_pods:
                raise RuntimeError("pod pagination returned a duplicate pod")
            if identity:
                seen_pods.add(identity)
        result.extend(page)
        if len(result) > MAX_PAGE_ITEMS:
            raise RuntimeError("pod pagination exceeded the record limit")
        page_total = _response_total(response, "pod", default=expected_total)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise RuntimeError("pod total_size changed during pagination")
            expected_total = page_total
        total = expected_total if expected_total is not None else len(result)
        next_token = response.get("next_page_token")
        if not page:
            if next_token not in (None, ""):
                raise RuntimeError("pod pagination continued after final page")
            return result
        if len(result) >= total:
            if next_token not in (None, ""):
                raise RuntimeError("pod pagination continued after total_size")
            return result
        next_skip = skip + len(page)
        if next_skip <= skip or next_skip in seen_offsets:
            raise RuntimeError("pod pagination made no progress")
        seen_offsets.add(next_skip)
        skip = next_skip
    raise RuntimeError("pod pagination exceeded the page limit")


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


def _iso_time(value: Any) -> str | None:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed else None


def _priority(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    return {"1": "NORMAL", "2": "HIGH", "3": "HIGHEST"}.get(normalized, normalized)


def _resource_priority(resource: dict[str, Any]) -> str | None:
    spec = resource.get("spec") or {}
    return _priority(spec.get("priority") if isinstance(spec, dict) else None)


def _running_training_lifecycle(
    cluster: Any, queue: str,
) -> tuple[dict[str, dict[str, Any]], bool]:
    queue_id = cluster._get_queue_id(queue)
    rows: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_ids: set[str] = set()
    expected_total: int | None = None
    for _ in range(MAX_NODE_PAGES):
        response = _list_training_jobs_page(cluster, queue_id, "RUNNING", page_token)
        if not isinstance(response, dict):
            raise RuntimeError("training lifecycle response must be an object")
        page = _training_job_page(response, "training lifecycle")
        for item in page:
            identity = str(item.get("uid") or item.get("id") or item.get("name") or "")
            if identity and identity in seen_ids:
                raise RuntimeError("training lifecycle pagination returned a duplicate")
            if identity:
                seen_ids.add(identity)
        rows.extend(page)
        if len(rows) > MAX_PAGE_ITEMS:
            raise RuntimeError("training lifecycle pagination exceeded the record limit")
        page_total = _response_total(response, "training lifecycle", default=expected_total)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise RuntimeError("training lifecycle total_size changed during pagination")
            expected_total = page_total
        next_token = response.get("next_page_token")
        if not page:
            if next_token not in (None, ""):
                raise RuntimeError("training lifecycle pagination continued after final page")
            break
        if expected_total is not None and len(rows) >= expected_total:
            if next_token not in (None, ""):
                raise RuntimeError("training lifecycle pagination continued after total_size")
            break
        if next_token in (None, ""):
            # The API may omit a token when the SDK caps the first page.  Keep
            # the rows but report ``complete=False`` from the total_size check.
            break
        if not isinstance(next_token, str) or not next_token.strip():
            raise RuntimeError("training lifecycle response has invalid next_page_token")
        if next_token in seen_tokens:
            raise RuntimeError("training lifecycle pagination cursor repeated")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        raise RuntimeError("training lifecycle pagination exceeded the page limit")
    result = {}
    for row in rows:
        uid = str(row.get("uid") or "")
        if not uid:
            continue
        status = row.get("status") or {}
        result[uid] = {
            "_resource_id": str(row.get("id") or "") or None,
            "_resource_name": str(row.get("name") or "") or None,
            "resource_create_time": _iso_time(status.get("create_time")),
            "start_time": _iso_time(status.get("start_time")),
            "priority": _resource_priority(row),
            "runtime_source": "training_status_start",
            "runtime_quality": "exact",
        }
    return result, expected_total is None or expected_total <= len(rows)


def _workspace_resource_lifecycle(
    cluster: Any, *, api_prefix: str, resource_name: str,
) -> tuple[list[dict[str, Any]], bool]:
    client = cluster.client
    base_path = client._get_base_path().rstrip("/")
    rows: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_ids: set[str] = set()
    expected_total: int | None = None
    for _ in range(MAX_NODE_PAGES):
        response = client._make_signed_base_request(
            client.compute_base_endpoint,
            "GET",
            f"{api_prefix}{base_path}/{resource_name}",
            params={
                "page_size": 1000,
                **({"page_token": page_token} if page_token is not None else {}),
                "filter": 'state="RUNNING"',
            },
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"{resource_name} lifecycle response must be an object")
        page = response.get(resource_name) or []
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise RuntimeError(f"{resource_name} lifecycle rows must be a list of objects")
        for item in page:
            identity = str(item.get("uid") or item.get("id") or item.get("name") or "")
            if identity and identity in seen_ids:
                raise RuntimeError(f"{resource_name} lifecycle pagination returned a duplicate")
            if identity:
                seen_ids.add(identity)
        rows.extend(page)
        if len(rows) > MAX_PAGE_ITEMS:
            raise RuntimeError(f"{resource_name} lifecycle pagination exceeded the record limit")
        page_total = _response_total(response, f"{resource_name} lifecycle", default=expected_total)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise RuntimeError(f"{resource_name} lifecycle total_size changed during pagination")
            expected_total = page_total
        next_token = response.get("next_page_token")
        if not page:
            if next_token not in (None, ""):
                raise RuntimeError(f"{resource_name} lifecycle pagination continued after final page")
            break
        if expected_total is not None and len(rows) >= expected_total:
            if next_token not in (None, ""):
                raise RuntimeError(f"{resource_name} lifecycle pagination continued after total_size")
            break
        if next_token in (None, ""):
            break
        if not isinstance(next_token, str) or not next_token.strip():
            raise RuntimeError(f"{resource_name} lifecycle response has invalid next_page_token")
        if next_token in seen_tokens:
            raise RuntimeError(f"{resource_name} lifecycle pagination cursor repeated")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        raise RuntimeError(f"{resource_name} lifecycle pagination exceeded the page limit")
    return rows, expected_total is None or expected_total <= len(rows)


def _running_aid_lifecycle(cluster: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    rows, complete = _workspace_resource_lifecycle(
        cluster, api_prefix="/aid/v1", resource_name="aids",
    )
    result = {}
    for row in rows:
        uid = str(row.get("uid") or "")
        if uid:
            result[uid] = {
                "_resource_id": str(row.get("id") or "") or None,
                "_resource_name": str(row.get("name") or "") or None,
                "resource_create_time": _iso_time(row.get("create_time")),
                "priority": _resource_priority(row),
            }
    return result, complete


def _available_transition(status: dict[str, Any]) -> str | None:
    transitions = [
        _parse_time(item.get("last_transition_time"))
        for item in status.get("conditions") or []
        if str(item.get("type") or "") == "Available"
        and str(item.get("status") or "").lower() == "true"
    ]
    valid = [item for item in transitions if item is not None]
    return max(valid).isoformat() if valid else None


def _running_air_lifecycle(cluster: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    rows, complete = _workspace_resource_lifecycle(
        cluster, api_prefix="/air/data/v1", resource_name="airs",
    )
    result = {}
    for row in rows:
        uid = str(row.get("uid") or "")
        if not uid:
            continue
        status = row.get("status") or {}
        result[uid] = {
            "_resource_id": str(row.get("id") or "") or None,
            "_resource_name": str(row.get("name") or "") or None,
            "resource_create_time": _iso_time(status.get("create_time")),
            "start_time": _available_transition(status),
            "priority": _resource_priority(row),
            "runtime_source": "air_available_condition",
            "runtime_quality": "observed",
        }
    return result, complete


def _aid_pod_start_times(
    cluster: Any, resource_id: str, pod_uids: set[str],
) -> dict[str, str]:
    if not resource_id or not pod_uids:
        return {}
    client = cluster.client
    path = f"/aid/v1/{resource_id.lstrip('/')}".rstrip("/") + "/events"
    events: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_events: set[str] = set()
    expected_total: int | None = None
    for _ in range(MAX_NODE_PAGES):
        params: dict[str, Any] = {"page_size": 100, "order_by": "created_at asc"}
        if page_token is not None:
            params["page_token"] = page_token
        response = client._make_signed_base_request(
            client.compute_base_endpoint,
            "GET",
            path,
            params=params,
        )
        if not isinstance(response, dict):
            raise RuntimeError("AID event response must be an object")
        page = response.get("events") or []
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise RuntimeError("AID event response events must be a list of objects")
        for event in page:
            identity = json.dumps(event, sort_keys=True, default=str)
            if identity in seen_events:
                raise RuntimeError("AID event pagination returned a duplicate")
            seen_events.add(identity)
        events.extend(page)
        if len(events) > MAX_PAGE_ITEMS:
            raise RuntimeError("AID event pagination exceeded the record limit")
        page_total = _response_total(response, "AID event", default=expected_total)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise RuntimeError("AID event total_size changed during pagination")
            expected_total = page_total
        next_token = response.get("next_page_token")
        if not page:
            if next_token not in (None, ""):
                raise RuntimeError("AID event pagination continued after final page")
            break
        if expected_total is not None and len(events) >= expected_total:
            if next_token not in (None, ""):
                raise RuntimeError("AID event pagination continued after total_size")
            break
        if next_token in (None, ""):
            # Without a total_size, a page without a token is the only safe
            # indication that the endpoint reached its end.
            break
        if not isinstance(next_token, str) or not next_token.strip():
            raise RuntimeError("AID event response has invalid next_page_token")
        if next_token in seen_tokens:
            raise RuntimeError("AID event pagination cursor repeated")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        raise RuntimeError("AID event pagination exceeded the page limit")

    starts: dict[str, list[datetime]] = {uid: [] for uid in pod_uids}
    for event in events:
        involved = event.get("involvedObject") or {}
        uid = str(involved.get("uid") or "")
        if (
            uid not in starts
            or str(event.get("type") or "") != "Normal"
            or str(event.get("reason") or "") != "Started"
        ):
            continue
        timestamp = _parse_time(event.get("firstTimestamp"))
        if timestamp:
            starts[uid].append(timestamp)
    return {
        uid: max(values).isoformat()
        for uid, values in starts.items()
        if values
    }


def _pending_workloads(cluster: Any, queue: str) -> tuple[list[dict[str, Any]], bool]:
    queue_id = cluster._get_queue_id(queue)
    jobs: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_job_ids: set[str] = set()
    expected_total: int | None = None
    for _ in range(MAX_NODE_PAGES):
        response = _list_training_jobs_page(cluster, queue_id, "PENDING", page_token)
        if not isinstance(response, dict):
            raise RuntimeError("pending workload response must be an object")
        page = _training_job_page(response, "pending workload")
        duplicate = False
        for item in page:
            identity = str(item.get("uid") or item.get("id") or item.get("name") or "")
            if identity and identity in seen_job_ids:
                duplicate = True
            if identity:
                seen_job_ids.add(identity)
        if duplicate:
            raise RuntimeError("pending workload pagination returned a duplicate")
        jobs.extend(page)
        if len(jobs) > MAX_PAGE_ITEMS:
            raise RuntimeError("pending workload pagination exceeded the record limit")
        page_total = _response_total(response, "pending workload", default=expected_total)
        if page_total is not None:
            if expected_total is not None and page_total != expected_total:
                raise RuntimeError("pending workload total_size changed during pagination")
            expected_total = page_total
        next_token = response.get("next_page_token")
        if not page:
            if next_token not in (None, ""):
                raise RuntimeError("pending workload pagination continued after final page")
            break
        if expected_total is not None and len(jobs) >= expected_total:
            if next_token not in (None, ""):
                raise RuntimeError("pending workload pagination continued after total_size")
            break
        if next_token in (None, ""):
            break
        if not isinstance(next_token, str) or not next_token.strip():
            raise RuntimeError("pending workload response has invalid next_page_token")
        if next_token in seen_tokens:
            raise RuntimeError("pending workload pagination cursor repeated")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        raise RuntimeError("pending workload pagination exceeded the page limit")
    complete = expected_total is None or expected_total <= len(jobs)
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
                "cpu_per_replica": _optional_resource_number(resource.get("cpu_count")),
                "memory_gib_per_replica": _optional_resource_number(resource.get("memory_gib"), memory=True),
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
        item = {
            "workload_id": str(job.get("name") or ""),
            "workload_name": str(job.get("display_name") or job.get("name") or ""),
            "resource_name": str(job.get("name") or ""),
            "user": str((job.get("ownership") or {}).get("creator_name") or "unknown").lower(),
            "status": "PENDING",
            "create_time": created.isoformat() if created else None,
            "resource_create_time": created.isoformat() if created else None,
            "queue_age_seconds": max(0, (now - created).total_seconds()) if created else None,
            "priority": _resource_priority(job),
            "num_nodes": sum(item["replicas"] for item in task_resources),
            "gpus_per_node": per_replica.get("gpu_per_replica"),
            "cpus_per_node": per_replica.get("cpu_per_replica"),
            "memory_per_node_gib": per_replica.get("memory_gib_per_replica"),
            "total_gpu": requested_total("gpu_per_replica") or 0,
            "total_cpu": requested_total("cpu_per_replica"),
            "total_memory_gib": requested_total("memory_gib_per_replica"),
            "resource_basis": "requested",
            "task_resources": task_resources,
        }
        console_url = _workload_console_url(
            cluster, "trainingJob", resource_id=job.get("id"),
            resource_name=job.get("name"),
            workspace=(job.get("workspace") or {}).get("name")
            if isinstance(job.get("workspace"), dict) else None,
        )
        if console_url:
            item["console_url"] = console_url
        result.append(item)
    return result, complete


class ClusterCollector:
    def __init__(self, cluster: Any, queue: str, cluster_name: str, *, gateway: Any = None) -> None:
        self.cluster = cluster
        self.queue = queue
        self.cluster_name = cluster_name
        if gateway is None:
            from .gateway import ClusterxGateway
            gateway = ClusterxGateway(cluster)
        self.gateway = gateway
        self._history_cache: dict[str, dict[str, float]] = {}
        self._history_fetched_at: datetime | None = None
        self._history_last_attempt_at: datetime | None = None
        self._history_window_hours: int | None = None
        self._history_query_available = False
        self._lifecycle_cache: dict[str, dict[str, Any]] = {}
        self._aid_pod_start_cache: dict[str, str] = {}

    def get_realtime_log(self, resource_name: str, worker: str, lines: int) -> str:
        """Fetch one explicitly selected Worker log outside snapshot collection."""
        return self.gateway.realtime_log(resource_name, worker, lines)

    def _enrich_lifecycle(
        self, workloads: dict[str, dict[str, Any]], warnings: list[str],
    ) -> None:
        current_ids = set(workloads)
        self._lifecycle_cache = {
            key: value for key, value in self._lifecycle_cache.items() if key in current_ids
        }

        fetchers = {
            "trainingJob": lambda: self.gateway.running_training_lifecycle(self.queue),
            "aid": lambda: self.gateway.running_aid_lifecycle(),
            "air": lambda: self.gateway.running_air_lifecycle(),
        }
        for kind, fetch in fetchers.items():
            if not any(item.get("type") == kind for item in workloads.values()):
                continue
            try:
                rows, complete = fetch()
                for workload_id, values in rows.items():
                    cached = self._lifecycle_cache.setdefault(workload_id, {})
                    cached.update({key: value for key, value in values.items() if value is not None})
                if not complete:
                    warnings.append(f"{kind} lifecycle inventory is incomplete")
            except Exception:
                warnings.append(f"{kind} lifecycle inventory is unavailable")

        aid_workloads = [item for item in workloads.values() if item.get("type") == "aid"]
        current_aid_pods = {
            str(placement.get("_pod_uid") or "")
            for item in aid_workloads
            for placement in item.get("placements") or []
            if placement.get("_pod_uid")
        }
        self._aid_pod_start_cache = {
            key: value for key, value in self._aid_pod_start_cache.items()
            if key in current_aid_pods
        }
        unresolved = []
        for workload in aid_workloads:
            pod_uids = {
                str(item.get("_pod_uid") or "")
                for item in workload.get("placements") or []
                if item.get("_pod_uid")
            }
            if pod_uids and not pod_uids.issubset(self._aid_pod_start_cache):
                unresolved.append((workload, pod_uids))
        failures = 0
        if unresolved:
            pool = ThreadPoolExecutor(max_workers=min(8, len(unresolved)), thread_name_prefix="monitor-aid")
            try:
                futures = {
                    pool.submit(
                        _aid_pod_start_times,
                        self.cluster,
                        str(workload.get("_resource_id") or ""),
                        pod_uids,
                    ): pod_uids
                    for workload, pod_uids in unresolved
                }
                done, pending_futures = wait(futures, timeout=20)
                failures += len(pending_futures)
                for future in done:
                    try:
                        self._aid_pod_start_cache.update(future.result())
                    except Exception:
                        failures += 1
                for future in pending_futures:
                    future.cancel()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        if failures:
            warnings.append(f"AID start events are unavailable for {failures} workloads")

        for workload_id, workload in workloads.items():
            cached = self._lifecycle_cache.get(workload_id) or {}
            resource_name = workload.get("resource_name") or cached.get("_resource_name")
            workload["resource_name"] = str(resource_name or "")
            workload["console_url"] = _workload_console_url(
                self.cluster, str(workload.get("type") or ""),
                resource_id=workload.get("_resource_id") or cached.get("_resource_id"),
                resource_name=resource_name,
                workspace=workload.get("workspace"),
            )
            workload["resource_create_time"] = cached.get("resource_create_time")
            workload["priority"] = cached.get("priority") or workload.get("priority")
            start_time = cached.get("start_time")
            source = cached.get("runtime_source")
            quality = cached.get("runtime_quality")
            if workload.get("type") == "aid":
                pod_uids = {
                    str(item.get("_pod_uid") or "")
                    for item in workload.get("placements") or []
                    if item.get("_pod_uid")
                }
                if pod_uids and pod_uids.issubset(self._aid_pod_start_cache):
                    start_time = max(self._aid_pod_start_cache[uid] for uid in pod_uids)
                    source = "aid_pod_started_event"
                    quality = "observed"
            workload["start_time"] = start_time
            if start_time:
                anchor = start_time
            elif workload.get("create_time"):
                anchor = workload.get("create_time")
                source = "pod_create_time"
                quality = "estimated"
            elif workload.get("resource_create_time"):
                anchor = workload.get("resource_create_time")
                source = "resource_create_time"
                quality = "estimated"
            else:
                anchor = None
                source = None
                quality = "unavailable"
            workload["runtime_anchor_time"] = anchor
            workload["runtime_source"] = source
            workload["runtime_quality"] = quality
            workload["runtime_estimated"] = quality == "estimated"
            workload.pop("_resource_id", None)
            if workload.get("console_url") is None:
                workload.pop("console_url", None)
            for placement in workload.get("placements") or []:
                placement.pop("_pod_uid", None)

    def collect(
        self, *, telemetry_minutes: int = 5,
        historical_window_hours: int = 24,
        historical_refresh_minutes: int = 5,
        deadline_seconds: float = 90.0,
    ) -> dict[str, Any]:
        started = time.monotonic()
        raw_nodes = self.gateway.list_queue_nodes(self.cluster_name, self.queue)
        nodes = normalize_nodes(raw_nodes)
        occupied = [
            item for item in nodes
            if item["allocated_gpu"] or item["allocated_cpu"] or item["allocated_memory_gib"]
        ]
        pods_by_node: dict[str, list[dict[str, Any]]] = {}
        failures: list[str] = []
        pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="monitor-pod")
        try:
            futures = {
                pool.submit(self.gateway.list_node_pods, self.cluster_name, self.queue, item["id"]): item["node"]
                for item in occupied
            }
            remaining = max(0.01, deadline_seconds - (time.monotonic() - started))
            done, pending_futures = wait(futures, timeout=remaining)
            for future in pending_futures:
                failures.append(futures[future])
                future.cancel()
            for future in done:
                name = futures[future]
                try:
                    pods_by_node[name] = future.result()
                except Exception:
                    failures.append(name)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
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
                    "_pod_uid": str(pod.get("uid") or ""),
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
                    "resource_name": str(raw_workload.get("name") or ""),
                    "create_time": None, "start_time": None,
                    "priority": _resource_priority(raw_workload),
                    "_resource_id": str(raw_workload.get("id") or ""),
                    "placements": [], "gpus": [], "total_gpu": 0,
                    "total_cpu": None, "total_memory_gib": None,
                    "resource_basis": "attributed", "task_resources": [],
                })
                if raw_workload.get("id"):
                    workload["_resource_id"] = str(raw_workload["id"])
                if raw_workload.get("name"):
                    workload["resource_name"] = str(raw_workload["name"])
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
        self._enrich_lifecycle(workloads, warnings)

        try:
            telemetry = self.gateway.query_gpu_telemetry(
                self.queue, self.cluster_name, telemetry_minutes,
            )
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
                self._history_cache = self.gateway.query_workload_history(
                    self.queue, self.cluster_name, historical_window_hours,
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
            pending, pending_complete = self.gateway.pending_workloads(self.queue)
        except Exception:
            pending, pending_complete = [], False
            warnings.append("pending workload inventory is unavailable")

        after = self.gateway.list_queue_nodes(self.cluster_name, self.queue)
        if _node_signature(raw_nodes) != _node_signature(after):
            raise RuntimeError("queue node allocation changed during collection")
        if time.monotonic() - started > deadline_seconds:
            raise TimeoutError("collection deadline exceeded")
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
