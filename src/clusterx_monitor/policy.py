from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import stat
from threading import RLock
from typing import Any, Iterable

import yaml
from pydantic import ValidationError

from .auth import _atomic_write
from .models import GroupConfig, PolicyConfig


STATUS_DEFINITIONS = {
    "compliant": {"description": "All applicable policy checks are within their inclusive limits.", "propagation": "Does not raise the status of a parent object."},
    "burst": {"description": "A group is above quota while no qualifying pending pressure is active.", "propagation": "Propagates from a group to its current users, but is not a violation."},
    "violation": {"description": "A policy rule is violated, or a group is above quota while pending pressure is active.", "propagation": "Workload findings propagate to the user; quota findings propagate from group to user."},
    "unknown": {"description": "The available inventory or telemetry is insufficient to reach a policy conclusion.", "propagation": "A group quota unknown state propagates to its users; missing history alone does not."},
    "pending": {"description": "The workload is queued and is not evaluated as a running workload.", "propagation": "Pending jobs contribute to pressure only after the configured wait and count thresholds."},
}

RULE_CATALOG = [
    {"code": "resource.development.gpu_limit", "category": "resource-shape", "applies_to": "aid", "title": "Development GPU limit", "description": "Development instances may use zero or one GPU; more GPUs are a violation."},
    {"code": "resource.development.cpu_limit", "category": "resource-shape", "applies_to": "aid", "title": "Development CPU per-node limit", "description": "CPU is checked per development instance against the separate zero-GPU and one-GPU limits."},
    {"code": "resource.development.memory_limit", "category": "resource-shape", "applies_to": "aid", "title": "Development memory per-node limit", "description": "Memory is checked per development instance against the separate zero-GPU and one-GPU limits."},
    {"code": "runtime.development.one_gpu_limit", "category": "runtime", "applies_to": "aid", "title": "One-GPU development runtime limit", "description": "Only one-GPU development instances have a runtime limit; equality is compliant."},
    {"code": "resource.training.cpu_ratio", "category": "resource-shape", "applies_to": "trainingJob", "title": "Training CPU-to-GPU ratio", "description": "Each task/node may request at most GPU count times cpu_per_gpu; zero-GPU tasks use a separate limit."},
    {"code": "resource.training.memory_ratio", "category": "resource-shape", "applies_to": "trainingJob", "title": "Training memory-to-GPU ratio", "description": "Each task/node may request at most GPU count times memory_gib_per_gpu; zero-GPU tasks use a separate limit."},
    {"code": "quota.gpu", "category": "quota", "applies_to": "group", "title": "Group GPU quota", "description": "Usage above quota is burst without pending pressure and violation with active pressure; equality is compliant."},
    {"code": "quota.cpu", "category": "quota", "applies_to": "group", "title": "Derived group CPU quota", "description": "Group CPU quota is GPU quota times cpu_per_gpu."},
    {"code": "quota.memory", "category": "quota", "applies_to": "group", "title": "Derived group memory quota", "description": "Group memory quota is GPU quota times memory_gib_per_gpu."},
    {"code": "utilization.low_gpu_activity", "category": "utilization", "applies_to": "GPU workload", "title": "Historical low GPU activity", "description": "Both historical compute and capacity/time-weighted memory utilization are at or below their inclusive thresholds."},
    {"code": "node.idle", "category": "node-classification", "applies_to": "node", "title": "Idle node", "description": "The schedulable node has no allocated GPU, CPU, or memory."},
    {"code": "node.gpu_full", "category": "node-classification", "applies_to": "node", "title": "GPU-full node", "description": "All GPUs are allocated."},
    {"code": "node.fragmented", "category": "node-classification", "applies_to": "node", "title": "Fragmented node", "description": "The node is partially allocated and its free GPU capacity remains usable for the configured standard planning profile."},
    {"code": "node.cpu_memory_blocked", "category": "node-classification", "applies_to": "node", "title": "CPU/memory-blocked node", "description": "Raw free GPUs exceed the number usable for the configured standard planning profile; smaller explicit requests may still fit."},
    {"code": "attribution.resource_excess", "category": "attribution", "applies_to": "node", "title": "Inconsistent resource attribution", "description": "Pod-attributed resources exceed node allocated resources; the node and touching workloads are excluded from planning."},
    {"code": "quota.pending_pressure", "category": "quota", "applies_to": "queue", "title": "Pending pressure", "description": "Pressure becomes active when the configured number of pending training jobs individually reach the wait threshold; 0-GPU jobs count."},
]


def _finding(
    code: str, category: str, status: str, message: str, *,
    tags: Iterable[str] = (), observed: dict[str, Any] | None = None,
    limit: dict[str, Any] | None = None, window_hours: int | None = None,
    source_type: str | None = None, source_id: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code, "category": category, "status": status,
        "message": message, "tags": sorted(set(tags)),
        "observed": observed or {}, "limit": limit or {},
    }
    if window_hours is not None:
        item["window_hours"] = window_hours
    if source_type is not None:
        item["source_type"] = source_type
    if source_id is not None:
        item["source_id"] = source_id
    return item


def _set_finding_facets(row: dict[str, Any]) -> None:
    findings = row.get("policy_findings") or []
    row["finding_categories"] = sorted({str(item["category"]) for item in findings})
    row["finding_codes"] = sorted({str(item["code"]) for item in findings})
    row["finding_tags"] = sorted({str(tag) for item in findings for tag in item.get("tags", [])})


def _alert(
    severity: str, kind: str, subject: Any, message: str, *,
    code: str, category: str, subject_type: str, tags: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "severity": severity, "kind": kind, "subject": subject,
        "message": message, "code": code, "category": category,
        "subject_type": subject_type, "tags": sorted(set(tags)),
        "finding_categories": [category], "finding_codes": [code],
        "finding_tags": sorted(set(tags)),
    }


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} root must be a mapping")
    return payload


def load_policy(path: str | Path, group_path: str | Path) -> PolicyConfig:
    policy_path = Path(path)
    groups_path = Path(group_path)
    try:
        mode = stat.S_IMODE(groups_path.resolve(strict=True).stat().st_mode)
    except OSError as error:
        raise ValueError(f"private group configuration is unavailable: {error}") from error
    if mode & 0o077:
        raise ValueError("private group configuration permissions must be 600")

    policy_payload = _load_mapping(policy_path, "resource policy")
    group_payload = _load_mapping(groups_path, "group policy")
    if "groups" in policy_payload:
        raise ValueError("resource policy must not contain private groups")
    unexpected = set(group_payload) - {"schema_version", "groups"}
    if unexpected:
        raise ValueError("group policy contains unsupported fields: " + ", ".join(sorted(unexpected)))
    if group_payload.get("schema_version") != policy_payload.get("schema_version"):
        raise ValueError("resource and group policy schema versions do not match")
    merged = {**policy_payload, "groups": group_payload.get("groups")}
    return PolicyConfig.model_validate(merged)


class ConfigConflictError(ValueError):
    pass


def _default_resource_mapping() -> dict[str, Any]:
    payload = PolicyConfig(groups={
        "default": GroupConfig(gpu_quota="remainder", members=()),
    }).model_dump(mode="json")
    payload.pop("groups")
    return payload


def _default_group_mapping() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "groups": {"default": {"gpu_quota": "remainder", "members": []}},
    }


MISSING_CONFIG = b"clusterx-monitor:missing-config:v1"


def _bytes_revision(payload: bytes | None) -> str:
    return hashlib.sha256(payload if payload is not None else MISSING_CONFIG).hexdigest()


def _parse_config_text(kind: str, text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text) if kind == "resource" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as error:
        raise ValueError(str(error)) from error
    if not isinstance(payload, dict):
        raise ValueError(f"{kind} configuration root must be a mapping")
    return payload


class PolicyManager:
    """Hot-reload a policy atomically, retaining the last valid version."""

    def __init__(
        self, path: str | Path, group_path: str | Path, *,
        allow_unconfigured: bool = False, audit_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.group_path = Path(group_path)
        self._lock = RLock()
        self._policy: PolicyConfig | None = None
        self._mtimes_ns: tuple[int, int] | None = None
        self.error: str | None = None
        self.audit_error: str | None = None
        self.audit_path = Path(audit_path).expanduser().resolve() if audit_path else self.path.parent / "admin-audit.local.jsonl"
        if not self.reload(force=True) and not allow_unconfigured:
            raise ValueError(f"initial policy configuration is invalid: {self.error}")

    @property
    def configured(self) -> bool:
        with self._lock:
            return self._policy is not None

    @property
    def policy(self) -> PolicyConfig:
        with self._lock:
            if self._policy is None:
                raise RuntimeError(f"no valid policy loaded: {self.error}")
            return self._policy

    @property
    def policy_revision(self) -> str | None:
        with self._lock:
            if self._policy is None:
                return None
            payload = json.dumps(
                self._policy.model_dump(mode="json"), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()

    def reload(self, *, force: bool = False) -> bool:
        try:
            mtimes_ns = (self.path.stat().st_mtime_ns, self.group_path.stat().st_mtime_ns)
            if not force and mtimes_ns == self._mtimes_ns:
                return False
            policy = load_policy(self.path, self.group_path)
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as error:
            with self._lock:
                self.error = str(error)
            return False
        with self._lock:
            self._policy = policy
            self._mtimes_ns = mtimes_ns
            self.error = None
        return True

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            policy = self._policy.model_dump(mode="json") if self._policy else None
            if policy is not None:
                policy["groups"] = {
                    name: {
                        "gpu_quota": group.gpu_quota,
                        "member_count": len(group.members),
                    }
                    for name, group in self._policy.groups.items()
                }
            return {
                "valid": self._policy is not None and self.error is None,
                "using_last_known_good": self._policy is not None and self.error is not None,
                "error": self.error,
                "audit_error": self.audit_error,
                "policy": policy,
                "status_definitions": STATUS_DEFINITIONS,
                "rule_catalog": RULE_CATALOG,
                "evaluation_behavior": {
                    "configuration_error": "Missing or invalid initial files enter authenticated setup-required mode. A later hot-reload error keeps the complete last-known-good resource and group policy.",
                    "historical_telemetry_unavailable": "The snapshot is still published, historical low-utilization evaluation is skipped, and a telemetry warning is emitted.",
                    "historical_scope": "Only currently running GPU trainingJob and aid workloads are evaluated; no completed-workload history is stored.",
                    "default_group": "The default GPU quota is max(0, current bound GPU capacity minus all explicit group GPU quotas).",
                    "planning_profile": "Node effective/blocked capacity and omitted plan CPU/memory use the configurable standard planning profile; training ratios remain submission limits.",
                    "cluster_access": "Monitoring and planning are read-only against Clusterx; authenticated administrators may write only the configured local policy files.",
                },
            }

    @staticmethod
    def _raw_file(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    def _effective_resource_mapping(self) -> dict[str, Any]:
        if self._policy is None:
            return _default_resource_mapping()
        payload = self._policy.model_dump(mode="json")
        payload.pop("groups")
        return payload

    def _effective_group_mapping(self) -> dict[str, Any]:
        if self._policy is None:
            return _default_group_mapping()
        return {
            "schema_version": self._policy.schema_version,
            "groups": {
                name: group.model_dump(mode="json")
                for name, group in self._policy.groups.items()
            },
        }

    def _admin_file(self, kind: str, path: Path) -> dict[str, Any]:
        raw = self._raw_file(path)
        if raw is None:
            fallback = (
                self._effective_resource_mapping() if kind == "resource"
                else self._effective_group_mapping()
            )
            text = (
                json.dumps(fallback, ensure_ascii=False, indent=2) + "\n"
                if kind == "resource" else
                yaml.safe_dump(fallback, sort_keys=False, allow_unicode=True)
            )
            return {
                "format": "json" if kind == "resource" else "yaml",
                "text": text,
                "revision": _bytes_revision(None),
                "parse_error": "configuration file is missing",
            }
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            text = raw.decode("utf-8", errors="replace")
            parse_error: str | None = f"configuration is not valid UTF-8: {error}"
        else:
            try:
                _parse_config_text(kind, text)
                parse_error = None
            except ValueError as error:
                parse_error = str(error)
        return {
            "format": "json" if kind == "resource" else "yaml",
            "text": text,
            "revision": _bytes_revision(raw),
            "parse_error": parse_error,
        }

    def admin_config(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": self._policy is not None,
                "effective_config_valid": self._policy is not None and self.error is None,
                "resource": self._admin_file("resource", self.path),
                "groups": self._admin_file("groups", self.group_path),
                "validation_error": self.error,
                "audit_error": self.audit_error,
            }

    def _audit(self, *, actor: str, kind: str, before: str, after: str) -> None:
        path = self.audit_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("refusing to write audit log through a symlink")
        record = json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "kind": kind,
            "before_revision": before,
            "after_revision": after,
        }, ensure_ascii=False, sort_keys=True) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, record.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _valid_disk_component(self, kind: str) -> dict[str, Any] | None:
        path = self.path if kind == "resource" else self.group_path
        raw = self._raw_file(path)
        if raw is None:
            return None
        try:
            value = _parse_config_text(kind, raw.decode("utf-8"))
            if kind == "resource":
                if "groups" in value:
                    raise ValueError("resource policy must not contain private groups")
                PolicyConfig.model_validate({**value, "groups": _default_group_mapping()["groups"]})
            else:
                unexpected = set(value) - {"schema_version", "groups"}
                if unexpected:
                    raise ValueError("group policy contains unsupported fields")
                PolicyConfig.model_validate({**_default_resource_mapping(), "groups": value.get("groups")})
            return value
        except (UnicodeDecodeError, ValueError, ValidationError):
            return None

    def update_config(
        self, kind: str, text: str, expected_revision: str, *, actor: str,
    ) -> dict[str, Any]:
        if kind not in {"resource", "groups"}:
            raise ValueError("unsupported configuration kind")
        if not isinstance(text, str):
            raise ValueError("configuration text must be a string")
        with self._lock:
            destination = self.path if kind == "resource" else self.group_path
            current_bytes = self._raw_file(destination)
            before = _bytes_revision(current_bytes)
            if not hmac.compare_digest(str(expected_revision), before):
                raise ConfigConflictError("configuration changed after it was loaded")
            payload = _parse_config_text(kind, text)
            if kind == "resource":
                if "groups" in payload:
                    raise ValueError("resource policy must not contain private groups")
                candidate_resource = payload
                candidate_groups = (
                    self._valid_disk_component("groups")
                    or self._effective_group_mapping()
                )
            else:
                unexpected = set(payload) - {"schema_version", "groups"}
                if unexpected:
                    raise ValueError("group policy contains unsupported fields: " + ", ".join(sorted(unexpected)))
                candidate_resource = (
                    self._valid_disk_component("resource")
                    or self._effective_resource_mapping()
                )
                candidate_groups = payload
            merged = {**candidate_resource, "groups": candidate_groups.get("groups")}
            validated = PolicyConfig.model_validate(merged)
            if kind == "resource":
                normalized = validated.model_dump(mode="json")
                normalized.pop("groups")
                content = json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            else:
                normalized = {
                    "schema_version": validated.schema_version,
                    "groups": {
                        name: group.model_dump(mode="json")
                        for name, group in validated.groups.items()
                    },
                }
                content = yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True).encode("utf-8")
            if destination.is_file():
                _atomic_write(destination.with_name(destination.name + ".bak"), destination.read_bytes())
            _atomic_write(destination, content)
            after = _bytes_revision(content)
            self.reload(force=True)
            if self._policy is None:
                self.error = "setup requires both valid resource and group configurations"
            try:
                self._audit(actor=actor, kind=kind, before=before, after=after)
                self.audit_error = None
            except (OSError, ValueError) as error:
                self.audit_error = str(error)
            return self.admin_config()


def _sum(values: Iterable[Any]) -> float:
    return sum(float(value or 0) for value in values)


def _clean(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 3)


def _telemetry_summary(workloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cards = [card for workload in workloads for card in workload.get("gpus", [])]
    allocated = int(_sum(workload.get("total_gpu") for workload in workloads))
    reported = [
        card for card in cards
        if any(card.get(field) is not None for field in (
            "gpu_compute_util_pct", "gpu_memory_util_pct", "gpu_power_w"
        ))
    ]
    compute = [float(card["gpu_compute_util_pct"]) for card in cards if card.get("gpu_compute_util_pct") is not None]
    memory = [float(card["gpu_memory_util_pct"]) for card in cards if card.get("gpu_memory_util_pct") is not None]
    powers = [float(card["gpu_power_w"]) for card in cards if card.get("gpu_power_w") is not None]

    def average(items: list[float]) -> float | None:
        return round(sum(items) / len(items), 2) if items else None

    return {
        "allocated_gpu_count": allocated,
        "reported_gpu_count": len(reported),
        "compute_reported_gpu_count": len(compute),
        "memory_reported_gpu_count": len(memory),
        "power_reported_gpu_count": len(powers),
        "gpu_compute_util_avg_pct": average(compute),
        "gpu_memory_util_avg_pct": average(memory),
        "gpu_power_total_w": round(sum(powers), 2) if powers else None,
        "gpu_power_avg_w": average(powers),
    }


def _runtime_hours(workload: dict[str, Any], now: datetime) -> float | None:
    timestamp = workload.get("start_time") or workload.get("create_time")
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 3600)


def _workload_limits(
    workload: dict[str, Any], policy: PolicyConfig, now: datetime
) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    kind = str(workload.get("type") or "unknown")
    placements = workload.get("placements") or []
    runtime = _runtime_hours(workload, now)
    workload["runtime_hours"] = round(runtime, 2) if runtime is not None else None
    workload["runtime_estimated"] = not bool(workload.get("start_time"))
    if kind == "aid":
        cfg = policy.development
        total_gpu = float(workload.get("total_gpu") or 0)
        if total_gpu > cfg.max_gpu:
            findings.append(_finding(
                "resource.development.gpu_limit", "resource-shape", "violation",
                f"development GPU {total_gpu:g} exceeds {cfg.max_gpu}",
                tags=("development", "gpu"), observed={"gpu": total_gpu},
                limit={"max_gpu": cfg.max_gpu},
            ))
        max_cpu = (
            cfg.zero_gpu_max_cpu_per_node
            if total_gpu == 0 else cfg.one_gpu_max_cpu_per_node
        )
        max_memory = (
            cfg.zero_gpu_max_memory_gib_per_node
            if total_gpu == 0 else cfg.one_gpu_max_memory_gib_per_node
        )
        for placement in placements:
            if float(placement.get("cpu") or 0) > max_cpu:
                findings.append(_finding(
                    "resource.development.cpu_limit", "resource-shape", "violation",
                    "development CPU per node exceeds limit",
                    tags=("development", "cpu"),
                    observed={"cpu_per_node": float(placement.get("cpu") or 0)},
                    limit={"max_cpu_per_node": max_cpu},
                ))
            if float(placement.get("memory_gib") or 0) > max_memory:
                findings.append(_finding(
                    "resource.development.memory_limit", "resource-shape", "violation",
                    "development memory per node exceeds limit",
                    tags=("development", "memory"),
                    observed={"memory_gib_per_node": float(placement.get("memory_gib") or 0)},
                    limit={"max_memory_gib_per_node": max_memory},
                ))
        if total_gpu == 1 and runtime is not None and runtime > cfg.one_gpu_max_runtime_hours:
            findings.append(_finding(
                "runtime.development.one_gpu_limit", "runtime", "violation",
                "one-GPU development runtime exceeds limit",
                tags=("development", "gpu", "runtime"),
                observed={"runtime_hours": round(runtime, 2)},
                limit={"max_runtime_hours": cfg.one_gpu_max_runtime_hours},
            ))
    elif kind == "trainingJob":
        cfg = policy.training
        for placement in placements:
            gpus = float(placement.get("gpu") or 0)
            max_cpu = cfg.zero_gpu_max_cpu_per_node if gpus == 0 else gpus * cfg.cpu_per_gpu
            max_memory = (
                cfg.zero_gpu_max_memory_gib_per_node
                if gpus == 0 else gpus * cfg.memory_gib_per_gpu
            )
            if float(placement.get("cpu") or 0) > max_cpu:
                findings.append(_finding(
                    "resource.training.cpu_ratio", "resource-shape", "violation",
                    "training CPU per node exceeds resource ratio",
                    tags=("training", "cpu", "gpu-ratio"),
                    observed={"cpu_per_node": float(placement.get("cpu") or 0), "gpu_per_node": gpus},
                    limit={"max_cpu_per_node": max_cpu},
                ))
            if float(placement.get("memory_gib") or 0) > max_memory:
                findings.append(_finding(
                    "resource.training.memory_ratio", "resource-shape", "violation",
                    "training memory per node exceeds resource ratio",
                    tags=("training", "memory", "gpu-ratio"),
                    observed={"memory_gib_per_node": float(placement.get("memory_gib") or 0), "gpu_per_node": gpus},
                    limit={"max_memory_gib_per_node": max_memory},
                ))

    history = workload.setdefault("historical_telemetry", {
        "window_hours": policy.low_utilization.window_hours,
        "fetched_at": None, "collection_status": "unavailable",
        "gpu_compute_util_avg_pct": None, "gpu_memory_util_avg_pct": None,
        "compute_sample_count": 0, "memory_sample_count": 0,
    })
    total_gpu = float(workload.get("total_gpu") or 0)
    compute = history.get("gpu_compute_util_avg_pct")
    memory = history.get("gpu_memory_util_avg_pct")
    if kind not in {"trainingJob", "aid"} or total_gpu <= 0:
        history["evaluation_status"] = "not-applicable"
    elif runtime is None:
        history["evaluation_status"] = "unavailable"
    elif runtime * 60 < policy.low_utilization.min_observation_minutes:
        history["evaluation_status"] = "warming-up"
    elif compute is None or memory is None:
        history["evaluation_status"] = "unavailable"
    else:
        history["evaluation_status"] = "evaluated"
        if (
            float(compute) <= policy.low_utilization.gpu_compute_threshold_pct
            and float(memory) <= policy.low_utilization.gpu_memory_threshold_pct
        ):
            findings.append(_finding(
                "utilization.low_gpu_activity", "utilization", "violation",
                "historical GPU compute and memory utilization are both at or below limits",
                tags=("gpu", "historical", "low-utilization"),
                observed={
                    "gpu_compute_util_pct": float(compute),
                    "gpu_memory_util_pct": float(memory),
                },
                limit={
                    "gpu_compute_util_pct": policy.low_utilization.gpu_compute_threshold_pct,
                    "gpu_memory_util_pct": policy.low_utilization.gpu_memory_threshold_pct,
                    "min_observation_minutes": policy.low_utilization.min_observation_minutes,
                },
                window_hours=policy.low_utilization.window_hours,
            ))

    unique = {item["code"] + repr(item.get("observed")): item for item in findings}
    findings = sorted(unique.values(), key=lambda item: (item["category"], item["code"], item["message"]))
    return ("violation" if findings else "compliant", findings)


def apply_policy(raw_snapshot: dict[str, Any], policy: PolicyConfig) -> dict[str, Any]:
    snapshot = deepcopy(raw_snapshot)
    now = datetime.now(timezone.utc)
    nodes = snapshot.get("nodes") or []
    workloads = snapshot.get("workloads") or []
    planning_excluded_nodes = {
        str(node.get("node")) for node in nodes
        if not bool(node.get("planning_eligible", True))
    }
    bound_gpu = int(_sum(node.get("total_gpu") for node in nodes))
    explicit_quota = sum(
        int(group.gpu_quota)
        for name, group in policy.groups.items()
        if name != "default" and isinstance(group.gpu_quota, int)
    )
    default_quota = max(0, bound_gpu - explicit_quota)
    user_groups = {
        member: group_name
        for group_name, group in policy.groups.items()
        for member in group.members
    }

    for pending_item in snapshot.get("pending_workloads") or []:
        pending_user = str(pending_item.get("user") or "unknown").strip().lower()
        pending_item["user"] = pending_user
        pending_item["group"] = "unattributed" if pending_user == "unknown" else user_groups.get(pending_user, "default")
        pending_item["type"] = "trainingJob"
        pending_item["policy_status"] = "pending"
        pending_item["policy_findings"] = []
        pending_item["policy_reasons"] = []
        _set_finding_facets(pending_item)
        pending_item["total_gpu"] = int(pending_item.get("num_nodes") or 0) * int(pending_item.get("gpus_per_node") or 0)
        pending_item["placements"] = []
        pending_item["gpus"] = []
        pending_item["telemetry"] = _telemetry_summary([])

    eligible_pending = [
        item for item in snapshot.get("pending_workloads") or []
        if float(item.get("queue_age_seconds") or 0)
        >= policy.pending_pressure.min_wait_minutes * 60
    ]
    pending_complete = bool(snapshot.get("pending_complete", True))
    pressure_state = (
        "unknown" if not pending_complete else
        "active" if len(eligible_pending) >= policy.pending_pressure.min_jobs else
        "inactive"
    )

    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for workload in workloads:
        user = str(workload.get("user") or "unknown").strip().lower()
        workload["user"] = user
        if user == "unknown":
            group = "unattributed"
        else:
            group = user_groups.get(user, "default")
        workload["group"] = group
        excluded_placements = sorted({
            str(placement.get("node")) for placement in workload.get("placements", [])
            if str(placement.get("node")) in planning_excluded_nodes
        })
        workload["planning_eligible"] = not excluded_placements
        workload["planning_exclusion_reasons"] = (
            ["attribution.resource_excess"] if excluded_placements else []
        )
        workload["planning_excluded_nodes"] = excluded_placements
        status, findings = _workload_limits(workload, policy, now)
        workload["policy_status"] = status
        workload["policy_findings"] = findings
        workload["policy_reasons"] = [item["message"] for item in findings]
        _set_finding_facets(workload)
        workload["telemetry"] = _telemetry_summary([workload])
        by_user[user].append(workload)
        by_group[group].append(workload)

    group_summaries: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    for group_name in [*policy.groups, *(name for name in by_group if name not in policy.groups)]:
        group_workloads = by_group.get(group_name, [])
        training = [w for w in group_workloads if w.get("type") == "trainingJob"]
        gpu = _sum(w.get("total_gpu") for w in training)
        cpu = _sum(p.get("cpu") for w in training for p in w.get("placements", []))
        memory = _sum(p.get("memory_gib") for w in training for p in w.get("placements", []))
        if group_name == "default":
            quota = default_quota
        elif group_name in policy.groups and isinstance(policy.groups[group_name].gpu_quota, int):
            quota = int(policy.groups[group_name].gpu_quota)
        else:
            quota = None
        over = []
        if quota is not None:
            if gpu > quota:
                over.append("gpu")
            if cpu > quota * policy.training.cpu_per_gpu:
                over.append("cpu")
            if memory > quota * policy.training.memory_gib_per_gpu:
                over.append("memory")
        status = (
            "unknown" if group_name == "unattributed" else
            "unknown" if pressure_state == "unknown" and over else
            "violation" if pressure_state == "active" and over else
            "burst" if over else
            "compliant"
        )
        quota_findings = []
        resource_values = {
            "gpu": (gpu, quota),
            "cpu": (cpu, quota * policy.training.cpu_per_gpu if quota is not None else None),
            "memory": (memory, quota * policy.training.memory_gib_per_gpu if quota is not None else None),
        }
        for resource in over:
            observed, limit = resource_values[resource]
            quota_findings.append(_finding(
                f"quota.{resource}", "quota", status,
                f"group {resource} usage exceeds quota",
                tags=("quota", resource, "pending-pressure" if pressure_state == "active" else "burst"),
                observed={resource: _clean(observed)},
                limit={f"{resource}_quota": _clean(limit)},
            ))
        summary = {
            "group": group_name,
            "gpu_quota": quota,
            "cpu_quota": quota * policy.training.cpu_per_gpu if quota is not None else None,
            "memory_quota_gib": quota * policy.training.memory_gib_per_gpu if quota is not None else None,
            "allocated_gpu": _clean(gpu),
            "allocated_cpu": _clean(cpu),
            "allocated_memory_gib": _clean(memory),
            "members": sorted({str(w["user"]) for w in group_workloads}),
            "status": status,
            "over_resources": over,
            "policy_findings": quota_findings,
            "policy_reasons": [item["message"] for item in quota_findings],
            "telemetry": _telemetry_summary(group_workloads),
        }
        _set_finding_facets(summary)
        group_summaries.append(summary)
        if group_name == "unattributed" and group_workloads:
            alerts.append(_alert(
                "warning", "attribution", "unattributed",
                "workloads with unknown ownership are excluded from quota attribution",
                code="attribution.unknown_owner", category="attribution",
                subject_type="group", tags=("ownership", "unattributed"),
            ))
        elif status in {"violation", "unknown"}:
            for finding in quota_findings:
                alerts.append(_alert(
                    "error" if status == "violation" else "warning",
                    "group-quota", group_name, finding["message"],
                    code=finding["code"], category=finding["category"],
                    subject_type="group", tags=finding["tags"],
                ))

    group_states = {item["group"]: item["status"] for item in group_summaries}
    group_summary_map = {item["group"]: item for item in group_summaries}
    user_summaries: list[dict[str, Any]] = []
    for user, items in sorted(by_user.items()):
        group_name = items[0]["group"]
        user_findings = [
            {**finding, "source_type": "workload", "source_id": str(workload.get("workload_id"))}
            for workload in items for finding in workload.get("policy_findings", [])
        ]
        user_findings.extend(
            {**finding, "source_type": "group", "source_id": group_name}
            for finding in group_summary_map.get(group_name, {}).get("policy_findings", [])
        )
        unique_findings = {
            (item["code"], item.get("source_type"), item.get("source_id"), repr(item.get("observed"))): item
            for item in user_findings
        }
        user_findings = sorted(
            unique_findings.values(),
            key=lambda item: (item["category"], item["code"], item.get("source_id", "")),
        )
        summary = {
            "user": user,
            "group": group_name,
            "workload_count": len(items),
            "allocated_gpu": _clean(_sum(w.get("total_gpu") for w in items)),
            "allocated_cpu": _clean(_sum(p.get("cpu") for w in items for p in w.get("placements", []))),
            "allocated_memory_gib": _clean(_sum(p.get("memory_gib") for w in items for p in w.get("placements", []))),
            "status": (
                "violation" if any(w["policy_status"] == "violation" for w in items)
                or group_states.get(group_name) == "violation"
                else "unknown" if group_states.get(group_name) == "unknown"
                else "burst" if group_states.get(group_name) == "burst"
                else "compliant"
            ),
            "policy_findings": user_findings,
            "policy_reasons": [item["message"] for item in user_findings],
            "telemetry": _telemetry_summary(items),
        }
        _set_finding_facets(summary)
        user_summaries.append(summary)
    for workload in workloads:
        for finding in workload.get("policy_findings", []):
            if finding.get("status") != "violation":
                continue
            alerts.append(_alert(
                "error", "workload-policy", workload.get("workload_id"),
                finding["message"], code=finding["code"],
                category=finding["category"], subject_type="workload",
                tags=finding.get("tags", []),
            ))
    if bound_gpu < explicit_quota:
        alerts.append(_alert(
            "error", "pool-capacity", "queue",
            f"bound GPU capacity {bound_gpu} is below explicit quota {explicit_quota}",
            code="quota.pool_capacity", category="quota", subject_type="queue",
            tags=("quota", "capacity"),
        ))
    if pressure_state == "unknown":
        alerts.append(_alert(
            "warning", "pending", "queue",
            "pending workload list is incomplete; quota pressure is unknown",
            code="quota.pending_pressure_unknown", category="quota",
            subject_type="queue", tags=("pending", "incomplete"),
        ))
    if snapshot.get("historical_telemetry_status") == "unavailable":
        alerts.append(_alert(
            "warning", "telemetry", "queue",
            "historical GPU telemetry is unavailable; low-utilization checks were skipped",
            code="telemetry.history_unavailable", category="telemetry",
            subject_type="queue", tags=("historical", "prometheus", "unavailable"),
        ))

    schedulable_states = {"RUNNING", "IDLE", "MIXED", "running", "idle", "mixed"}
    for node in nodes:
        free_gpu = max(0, float(node.get("total_gpu") or 0) - float(node.get("allocated_gpu") or 0))
        free_cpu = max(0, float(node.get("total_cpu") or 0) - float(node.get("allocated_cpu") or 0))
        free_memory = max(0, float(node.get("total_memory_gib") or 0) - float(node.get("allocated_memory_gib") or 0))
        cpu_fit = math.floor(free_cpu / policy.planning.default_cpu_per_gpu)
        memory_fit = math.floor(free_memory / policy.planning.default_memory_gib_per_gpu)
        effective = max(0, min(int(free_gpu), cpu_fit, memory_fit))
        allocated_gpu = float(node.get("allocated_gpu") or 0)
        if node.get("state") not in schedulable_states:
            classification = "unavailable"
        elif not allocated_gpu and not float(node.get("allocated_cpu") or 0) and not float(node.get("allocated_memory_gib") or 0):
            classification = "idle"
        elif allocated_gpu >= float(node.get("total_gpu") or 0) > 0:
            classification = "gpu-full"
        elif free_gpu > effective:
            classification = "cpu-memory-blocked"
        else:
            classification = "fragmented"
        node["free_gpu"] = _clean(free_gpu)
        node["effective_free_gpu"] = effective
        node["stranded_gpu"] = _clean(free_gpu - effective)
        node["classification"] = classification
        if any(float(value or 0) > 0 for value in (node.get("unattributed") or {}).values()):
            alerts.append(_alert(
                "warning", "node-attribution", node.get("node"),
                "allocated node resources could not be attributed to a workload",
                code="attribution.node_resource", category="attribution",
                subject_type="node", tags=("node", "unattributed"),
            ))
        if not bool(node.get("planning_eligible", True)):
            alerts.append(_alert(
                "warning", "node-attribution", node.get("node"),
                "Pod-attributed resources exceed node allocation; node is excluded from planning",
                code="attribution.resource_excess", category="attribution",
                subject_type="node", tags=("node", "attribution-excess", "planning-excluded"),
            ))
        node_workloads = [
            workload for workload in workloads
            if any(str(p.get("node")) == str(node.get("node")) for p in workload.get("placements", []))
        ]
        node_cards = []
        for workload in node_workloads:
            scoped = deepcopy(workload)
            scoped["gpus"] = [card for card in workload.get("gpus", []) if str(card.get("node")) == str(node.get("node"))]
            scoped["total_gpu"] = sum(float(p.get("gpu") or 0) for p in workload.get("placements", []) if str(p.get("node")) == str(node.get("node")))
            node_cards.append(scoped)
        node["telemetry"] = _telemetry_summary(node_cards)
    snapshot["capacity"] = {
        "bound_gpu": bound_gpu,
        "schedulable_gpu": int(_sum(
            n.get("total_gpu") for n in nodes
            if n.get("state") in schedulable_states and bool(n.get("planning_eligible", True))
        )),
        "schedulable_free_gpu": int(_sum(
            max(0, float(n.get("total_gpu") or 0) - float(n.get("allocated_gpu") or 0))
            for n in nodes
            if n.get("state") in schedulable_states and bool(n.get("planning_eligible", True))
        )),
        "effective_free_gpu": int(_sum(
            n.get("effective_free_gpu") for n in nodes
            if n.get("state") in schedulable_states and bool(n.get("planning_eligible", True))
        )),
        "allocated_gpu": int(_sum(n.get("allocated_gpu") for n in nodes)),
        "free_gpu": int(_sum(float(n.get("total_gpu") or 0) - float(n.get("allocated_gpu") or 0) for n in nodes)),
        "explicit_gpu_quota": explicit_quota,
        "default_gpu_quota": default_quota,
        "planning_eligible_gpu": int(_sum(
            n.get("total_gpu") for n in nodes
            if n.get("state") in schedulable_states and bool(n.get("planning_eligible", True))
        )),
    }
    snapshot["planning_profile"] = policy.planning.model_dump(mode="json")
    snapshot["pending_pressure"] = {
        "state": pressure_state,
        "eligible_jobs": len(eligible_pending),
        "min_jobs": policy.pending_pressure.min_jobs,
        "min_wait_minutes": policy.pending_pressure.min_wait_minutes,
    }
    snapshot["workloads"] = workloads
    snapshot["users"] = user_summaries
    snapshot["groups"] = group_summaries
    snapshot["alerts"] = alerts
    snapshot["telemetry"] = _telemetry_summary(workloads)
    if not snapshot.get("telemetry_available", True):
        snapshot["telemetry_status"] = "unavailable"
    elif snapshot["telemetry"]["reported_gpu_count"] < snapshot["telemetry"]["allocated_gpu_count"]:
        snapshot["telemetry_status"] = "partial"
    else:
        snapshot["telemetry_status"] = "available"
    return snapshot
