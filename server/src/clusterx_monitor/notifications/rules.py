"""Translate trustworthy PT observations into independent notification signals."""

from dataclasses import dataclass
import re

from ..collector import _parse_time

TRAINING = "trainingJob"
KINDS = (TRAINING, "aid", "air")
UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
QUEUE_MINUTES = (30, 60, 120)
TERMINAL = {"FAILED": "job.failed", "COMPLETED": "job.completed", "TERMINATED": "job.stopped"}
LIFECYCLE = ("job.started", *TERMINAL.values())


@dataclass(frozen=True)
class Rule:
    label: str
    kinds: tuple[str, ...] = KINDS


RULES = {
    "job.failed": Rule("任务失败", (TRAINING,)),
    "job.started": Rule("开始运行", (TRAINING,)),
    "job.completed": Rule("正常完成", (TRAINING,)),
    "job.stopped": Rule("已停止", (TRAINING,)),
    "queue.wait": Rule("排队超时", (TRAINING,)),
    "gpu.compute": Rule("GPU 算力持续低活跃"),
    "gpu.memory": Rule("GPU 显存持续低活跃"),
    "gpu.power": Rule("GPU 功率持续低活跃"),
    "policy.resources": Rule("资源配置超限", (TRAINING, "aid")),
    "placement.outside_owned_pool": Rule("跨组节点占用"),
    "placement.quota_borrowed": Rule("借用组配额"),
    "runtime.near_limit": Rule("接近单卡开发机运行时限", ("aid",)),
}


def epoch(value):
    parsed = _parse_time(value)
    return parsed.timestamp() if parsed else None


def identity(workload):
    uid = workload.get("workload_uid")
    kind = workload.get("type")
    if kind in KINDS and isinstance(uid, str) and UID.fullmatch(uid):
        return kind, uid
    return None


def default_events(kind):
    return ["job.failed"] if kind == TRAINING else []


def key_for(event, queue_minutes):
    return f"queue.wait.{queue_minutes}" if event == "queue.wait" else event


def signal(level, at=None, **details):
    return {"level": level, "at": at, "details": details}


def snapshot_signals(task, snapshot, policy, observed, pending):
    result = {}
    kind = task["type"]
    if kind == TRAINING:
        created = epoch(task.get("resource_create_time"))
        if not pending or snapshot.get("pending_complete") and created and created <= observed:
            for minutes in QUEUE_MINUTES:
                at = created + minutes * 60 if pending else None
                result[key_for("queue.wait", minutes)] = signal(
                    int(at is not None and at <= observed), at, minutes=minutes,
                )
    if pending or not snapshot.get("policy_config", {}).get("valid", True):
        return result

    findings = task.get("policy_findings", [])
    resource_findings = [row for row in findings if row.get("category") == "resource-shape"]
    if kind in RULES["policy.resources"].kinds:
        violations = sorted({row["code"] for row in resource_findings if row.get("status") == "violation"})
        if violations or not any(row.get("status") == "unknown" for row in resource_findings):
            result["policy.resources"] = signal(int(bool(violations)), codes=violations)
    context = task.get("placement_context") or {}
    if snapshot.get("node_allocation", {}).get("enabled") and not any(
        row.get("code") == "placement.owner_unknown"
        or "pending-pressure-unknown" in (row.get("tags") or []) for row in findings
    ) and context:
        for code in ("placement.outside_owned_pool", "placement.quota_borrowed"):
            statuses = {row.get("status") for row in findings if row.get("code") == code}
            level = 2 if "violation" in statuses else int("warning" in statuses)
            result[code] = signal(level, severity=("违规" if level == 2 else "警告") if level else None)
    if kind == "aid" and task.get("total_gpu") == 1 and task.get("runtime_quality") in ("exact", "observed"):
        started = epoch(task.get("runtime_anchor_time"))
        limit = policy.development.one_gpu_max_runtime_hours
        if started and limit and started <= observed:
            at = started + max(0, limit - 6) * 3600
            result["runtime.near_limit"] = signal(int(at <= observed), at, limit_hours=limit, lead_hours=min(limit, 6))

    history = task.get("historical_telemetry") or {}
    coverage = task.get("telemetry") or {}
    cfg = policy.low_utilization
    fetched = epoch(history.get("fetched_at"))
    expected = task.get("total_gpu") or 0
    if not (snapshot.get("telemetry_available") and expected and fetched
            and 0 <= observed - fetched <= (cfg.refresh_minutes + 2) * 60
            and history.get("evaluation_status") in ("evaluated", "partial")):
        return result
    low = next((row for row in findings if row.get("code") == "utilization.low_gpu_activity"), {})
    triggered = low.get("observed", {}).get("triggered_metrics", [])
    for metric, value_field, threshold in (
        ("compute", "gpu_compute_util_avg_pct", cfg.gpu_compute_threshold_pct),
        ("memory", "gpu_memory_util_avg_pct", cfg.gpu_memory_threshold_pct),
        ("power", "gpu_power_util_avg_pct", cfg.gpu_power_threshold_pct),
    ):
        count = coverage.get(f"{metric}_reported_gpu_count", 0)
        samples = history.get(f"{metric}_sample_count", 0)
        value = history.get(value_field)
        if threshold is not None and value is not None and samples >= expected and count == expected:
            result[f"gpu.{metric}"] = signal(
                int(metric in triggered), value_pct=value, threshold_pct=threshold,
                hours=history.get("window_hours", cfg.window_hours), covered=count,
                expected=expected, samples=samples,
            )
    return result
