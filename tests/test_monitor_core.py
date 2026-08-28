from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import yaml


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RESOURCE_POLICY = ROOT / "skill/clusterx-manage-jobs/assets/resource-policy.json"
GROUP_POLICY = ROOT / "config/groups.example.yaml"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clusterx_monitor.collector import (
    ClusterCollector,
    _aid_pod_start_times,
    _attach_telemetry,
    _available_transition,
    _list_queue_nodes,
    _node_signature,
    _pending_workloads,
    _priority,
    _query_workload_history,
    _running_air_lifecycle,
    _running_training_lifecycle,
    _workload_console_url,
    resource_number,
)
from clusterx_monitor.auth import AdminAuth, initialize_auth_config
from clusterx_monitor.models import PlanRequest, PolicyConfig
from clusterx_monitor.planner import solve_plan
from clusterx_monitor.policy import PolicyManager, apply_policy, load_policy
from clusterx_monitor.store import PlanCache, SnapshotStore


class AdminAuthTests(unittest.TestCase):
    def test_non_loopback_binding_requires_explicit_non_wildcard_trusted_hosts(self):
        from clusterx_monitor.cli import _trusted_hosts

        self.assertIn("127.0.0.1", _trusted_hosts("127.0.0.1", []))
        with self.assertRaisesRegex(ValueError, "requires at least one"):
            _trusted_hosts("0.0.0.0", [])
        with self.assertRaisesRegex(ValueError, "explicit hostname or IP"):
            _trusted_hosts("0.0.0.0", ["*"])
        trusted = _trusted_hosts("0.0.0.0", ["10.140.80.10", "monitor.internal"])
        self.assertIn("10.140.80.10", trusted)
        self.assertIn("monitor.internal", trusted)

    def test_auth_config_is_hashed_protected_and_sessions_are_server_side(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.yaml"
            initialize_auth_config(path, "admin", "a-strong-test-password")
            text = path.read_text(encoding="utf-8")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("$argon2id$", text)
            self.assertNotIn("a-strong-test-password", text)
            auth = AdminAuth(path)
            token, session = auth.login("admin", "a-strong-test-password", "127.0.0.1")
            self.assertEqual(auth.session(token), session)
            initialize_auth_config(
                path, "admin", "a-different-strong-password", overwrite=True,
            )
            self.assertIsNone(auth.session(token))
            rotated_token, _ = auth.login("admin", "a-different-strong-password", "127.0.0.1")
            auth.logout(rotated_token)
            self.assertIsNone(auth.session(rotated_token))

    def test_auth_rejects_weak_password_permissions_and_rate_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.yaml"
            with self.assertRaisesRegex(ValueError, "at least 12"):
                initialize_auth_config(path, "admin", "short")
            initialize_auth_config(path, "admin", "a-strong-test-password")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions must be 600"):
                AdminAuth(path)
            path.chmod(0o600)
            auth = AdminAuth(path)
            for _ in range(5):
                with self.assertRaisesRegex(ValueError, "invalid administrator credentials"):
                    auth.login("admin", "wrong", "127.0.0.1")
            with self.assertRaisesRegex(PermissionError, "too many login attempts"):
                auth.login("admin", "a-strong-test-password", "127.0.0.1")

    def test_admin_init_cli_uses_hidden_confirmed_input(self):
        from clusterx_monitor import cli

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "admin.yaml"
            with mock.patch.object(
                cli.getpass, "getpass", side_effect=["a-strong-test-password", "a-strong-test-password"],
            ), mock.patch.object(
                sys, "argv", [
                    "clusterx-monitor", "admin", "init", "--auth-config", str(path),
                    "--username", "admin",
                ],
            ), mock.patch("sys.stdout"):
                self.assertEqual(cli.main(), 0)
            self.assertNotIn("a-strong-test-password", path.read_text(encoding="utf-8"))


def example_policy():
    with tempfile.TemporaryDirectory() as directory:
        groups = Path(directory) / "groups.yaml"
        groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
        groups.chmod(0o600)
        return load_policy(RESOURCE_POLICY, groups)


def placement(node: str, gpu: int, cpu: int = 4, memory: int = 10, pod: str = "pod"):
    return {"node": node, "pod": pod, "gpu": gpu, "cpu": cpu, "memory_gib": memory}


def workload(
    name: str, user: str, kind: str, placements: list[dict], *,
    created: datetime | None = None, power: float | None = None,
):
    gpu_count = sum(item["gpu"] for item in placements)
    cards = []
    for item in placements:
        for index in range(item["gpu"]):
            cards.append({
                "node": item["node"], "pod": item["pod"], "device_index": str(index),
                "gpu_uuid": f"{name}-{index}", "gpu_compute_util_pct": 50,
                "gpu_memory_util_pct": 60, "gpu_power_w": power,
            })
    return {
        "workload_id": name, "workload_name": name, "user": user, "type": kind,
        "total_gpu": gpu_count,
        "total_cpu": sum(item["cpu"] for item in placements),
        "total_memory_gib": sum(item["memory_gib"] for item in placements),
        "resource_basis": "attributed", "task_resources": [],
        "placements": placements, "gpus": cards,
        "create_time": created.isoformat() if created else None, "start_time": None,
    }


def node(name: str, gpu: int, cpu: int = 0, memory: int = 0):
    return {
        "node": name, "state": "RUNNING", "allocated_gpu": gpu, "total_gpu": 8,
        "allocated_cpu": cpu, "total_cpu": 112,
        "allocated_memory_gib": memory, "total_memory_gib": 1920,
        "workloads": {}, "unattributed": {"gpu": 0, "cpu": 0, "memory_gib": 0},
        "attribution_excess": {"gpu": 0, "cpu": 0, "memory_gib": 0},
    }


def snapshot(nodes: list[dict], workloads: list[dict], pending=None, pending_complete=True):
    return {
        "schema_version": 1, "snapshot_id": "s1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster": "c", "queue": "q", "nodes": nodes, "workloads": workloads,
        "pending_workloads": pending or [], "pending_complete": pending_complete,
        "warnings": [], "telemetry_window_minutes": 5,
    }


class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = example_policy()

    def test_policy_config_has_expected_quota_and_unique_members(self):
        self.assertEqual(
            sum(group.gpu_quota for name, group in self.policy.groups.items()
                if name != "default" and isinstance(group.gpu_quota, int)),
            8,
        )
        self.assertEqual(self.policy.groups["example-team"].members, ("alice",))
        self.assertIsNone(self.policy.groups["example-team"].cpu_quota)
        self.assertIsNone(self.policy.groups["example-team"].memory_quota_gib)
        self.assertEqual(self.policy.groups["example-zero-quota"].cpu_quota, 14)
        self.assertEqual(self.policy.groups["example-zero-quota"].memory_quota_gib, 240)
        payload = self.policy.model_dump(mode="json")
        payload["groups"]["default"]["members"] = ["alice"]
        with self.assertRaisesRegex(ValueError, "appears in both"):
            PolicyConfig.model_validate(payload)

    def test_policy_business_bounds_and_planning_upper_limits(self):
        payload = self.policy.model_dump(mode="json")
        payload["refresh_seconds"] = 9
        with self.assertRaises(ValueError):
            PolicyConfig.model_validate(payload)
        payload = self.policy.model_dump(mode="json")
        payload["telemetry_lookback_minutes"] = 61
        with self.assertRaises(ValueError):
            PolicyConfig.model_validate(payload)
        payload = self.policy.model_dump(mode="json")
        payload["pending_pressure"]["min_wait_minutes"] = 1441
        with self.assertRaises(ValueError):
            PolicyConfig.model_validate(payload)
        payload = self.policy.model_dump(mode="json")
        payload["planning"]["default_cpu_per_gpu"] = payload["training"]["cpu_per_gpu"] + 1
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            PolicyConfig.model_validate(payload)
        payload = self.policy.model_dump(mode="json")
        payload["development"]["max_instances_per_user"] = 0
        with self.assertRaises(ValueError):
            PolicyConfig.model_validate(payload)

        payload = self.policy.model_dump(mode="json")
        payload["development"].pop("max_instances_per_user")
        self.assertEqual(
            PolicyConfig.model_validate(payload).development.max_instances_per_user,
            1,
        )

    def test_group_resource_quotas_are_optional_and_independent(self):
        payload = self.policy.model_dump(mode="json")
        payload["groups"] = {
            "unlimited": {"members": ["alice"]},
            "cpu-limited": {
                "cpu_quota": 10, "memory_quota_gib": 100, "members": ["bob"],
            },
            "default": {},
        }
        policy = PolicyConfig.model_validate(payload)
        items = [
            workload("unlimited", "alice", "trainingJob", [placement("n1", 8, 1000, 5000)]),
            workload("limited", "bob", "trainingJob", [placement("n2", 8, 11, 100)]),
        ]
        result = apply_policy(snapshot([node("n1", 8), node("n2", 8)], items), policy)
        groups = {item["group"]: item for item in result["groups"]}
        self.assertEqual(groups["unlimited"]["status"], "compliant")
        self.assertIsNone(groups["unlimited"]["gpu_quota"])
        self.assertIsNone(groups["unlimited"]["cpu_quota"])
        self.assertIsNone(groups["unlimited"]["memory_quota_gib"])
        self.assertEqual(groups["cpu-limited"]["status"], "burst")
        self.assertEqual(groups["cpu-limited"]["over_resources"], ["cpu"])
        self.assertEqual(groups["cpu-limited"]["policy_findings"][0]["code"], "quota.cpu")
        self.assertEqual(groups["cpu-limited"]["policy_findings"][0]["limit"], {"cpu_quota": 10})

        pressured = apply_policy(
            snapshot([node("n1", 8), node("n2", 8)], items, [{"queue_age_seconds": 601}]),
            policy,
        )
        pressured_group = next(
            item for item in pressured["groups"] if item["group"] == "cpu-limited"
        )
        self.assertEqual(pressured_group["status"], "violation")

    def test_default_gpu_quota_supports_remainder_numeric_and_unlimited(self):
        payload = self.policy.model_dump(mode="json")
        payload["groups"] = {
            "explicit": {"gpu_quota": 3, "members": []},
            "default": {"gpu_quota": "remainder", "members": []},
        }
        remainder = apply_policy(snapshot([node("n", 0)], []), PolicyConfig.model_validate(payload))
        self.assertEqual(remainder["capacity"]["default_gpu_quota"], 5)

        payload["groups"]["default"]["gpu_quota"] = 2
        numeric = apply_policy(snapshot([node("n", 0)], []), PolicyConfig.model_validate(payload))
        self.assertEqual(numeric["capacity"]["default_gpu_quota"], 2)
        self.assertEqual(numeric["capacity"]["explicit_gpu_quota"], 5)

        payload["groups"]["default"].pop("gpu_quota")
        unlimited = apply_policy(snapshot([node("n", 0)], []), PolicyConfig.model_validate(payload))
        self.assertIsNone(unlimited["capacity"]["default_gpu_quota"])

    def test_group_quota_validation_rejects_invalid_values(self):
        payload = self.policy.model_dump(mode="json")
        payload["groups"]["example-team"]["gpu_quota"] = None
        self.assertIsNone(PolicyConfig.model_validate(payload).groups["example-team"].gpu_quota)
        payload["groups"]["example-team"]["gpu_quota"] = "remainder"
        with self.assertRaisesRegex(ValueError, "only the default group"):
            PolicyConfig.model_validate(payload)
        payload = self.policy.model_dump(mode="json")
        payload["groups"]["example-team"]["cpu_quota"] = -1
        with self.assertRaisesRegex(ValueError, "greater than or equal to 0|non-negative"):
            PolicyConfig.model_validate(payload)
        payload = self.policy.model_dump(mode="json")
        payload["groups"]["example-team"]["memory_quota_gib"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            PolicyConfig.model_validate(payload)

    def test_default_remainder_power_and_node_blocking(self):
        nodes = [node(f"n{i}", 0) for i in range(64)]
        item = workload("w", "alice", "trainingJob", [placement("n0", 1)], power=300)
        nodes[0]["allocated_gpu"] = 1
        nodes[0]["allocated_cpu"] = 108
        nodes[0]["allocated_memory_gib"] = 1700
        result = apply_policy(snapshot(nodes, [item]), self.policy)
        self.assertEqual(result["capacity"]["bound_gpu"], 512)
        self.assertEqual(result["capacity"]["default_gpu_quota"], 504)
        self.assertEqual(result["capacity"]["schedulable_free_gpu"], 511)
        self.assertLess(result["capacity"]["effective_free_gpu"], 511)
        self.assertEqual(result["telemetry"]["gpu_power_total_w"], 300)
        self.assertEqual(result["nodes"][0]["classification"], "cpu-memory-blocked")
        self.assertGreater(result["nodes"][0]["stranded_gpu"], 0)

    def test_workload_totals_are_normalized_once_for_summaries(self):
        item = workload("w", "alice", "trainingJob", [
            placement("n1", 1, 4, 10), placement("n2", 2, 8, 20),
        ])
        item.update({"total_gpu": 99, "total_cpu": 99, "total_memory_gib": 99})
        result = apply_policy(
            snapshot([node("n1", 1, 4, 10), node("n2", 2, 8, 20)], [item]),
            self.policy,
        )
        normalized = result["workloads"][0]
        self.assertEqual(
            (normalized["total_gpu"], normalized["total_cpu"], normalized["total_memory_gib"]),
            (3, 12, 30),
        )
        group = next(row for row in result["groups"] if row["group"] == "example-team")
        user = result["users"][0]
        self.assertEqual(
            (group["allocated_gpu"], group["allocated_cpu"], group["allocated_memory_gib"]),
            (3, 12, 30),
        )
        self.assertEqual(
            (user["allocated_gpu"], user["allocated_cpu"], user["allocated_memory_gib"]),
            (3, 12, 30),
        )

    def test_group_summary_includes_air_aid_and_training_workloads(self):
        items = [
            workload("air", "alice", "air", [placement("n1", 2, 20, 400)]),
            workload("aid", "alice", "aid", [placement("n2", 1, 6, 120)]),
            workload("training", "alice", "trainingJob", [placement("n3", 1, 4, 240)]),
        ]
        result = apply_policy(
            snapshot([
                node("n1", 2, 20, 400), node("n2", 1, 6, 120), node("n3", 1, 4, 240),
            ], items),
            self.policy,
        )
        group = next(row for row in result["groups"] if row["group"] == "example-team")
        user = result["users"][0]
        expected = (4, 30, 760)
        self.assertEqual(
            (group["allocated_gpu"], group["allocated_cpu"], group["allocated_memory_gib"]),
            expected,
        )
        self.assertEqual(
            (user["allocated_gpu"], user["allocated_cpu"], user["allocated_memory_gib"]),
            expected,
        )

    def test_missing_running_cpu_and_memory_stay_unknown(self):
        item = workload("w", "alice", "trainingJob", [placement("n", 1)])
        item["placements"][0]["cpu"] = None
        item["placements"][0]["memory_gib"] = None
        result = apply_policy(snapshot([node("n", 1)], [item]), self.policy)
        self.assertIsNone(result["workloads"][0]["total_cpu"])
        self.assertIsNone(result["workloads"][0]["total_memory_gib"])
        self.assertIsNone(result["users"][0]["allocated_cpu"])
        self.assertIsNone(result["users"][0]["allocated_memory_gib"])

    def test_zero_quota_bursts_then_violates_under_pressure(self):
        item = workload("w", "bob", "trainingJob", [placement("n", 1)])
        no_pressure = apply_policy(snapshot([node("n", 1)], [item]), self.policy)
        group = next(row for row in no_pressure["groups"] if row["group"] == "example-zero-quota")
        self.assertEqual(group["status"], "burst")
        pending = [{"queue_age_seconds": 601, "workload_id": "pending"}]
        pressured = apply_policy(snapshot([node("n", 1)], [item], pending), self.policy)
        group = next(row for row in pressured["groups"] if row["group"] == "example-zero-quota")
        self.assertEqual(group["status"], "violation")

    def test_pending_incomplete_is_unknown_and_does_not_violate_quota(self):
        item = workload("w", "bob", "trainingJob", [placement("n", 1)])
        result = apply_policy(snapshot([node("n", 1)], [item], pending_complete=False), self.policy)
        group = next(row for row in result["groups"] if row["group"] == "example-zero-quota")
        self.assertEqual(group["status"], "unknown")
        self.assertEqual(result["pending_pressure"]["state"], "unknown")

    def test_pending_missing_age_is_unknown_instead_of_inactive(self):
        pending = [{"queue_age_seconds": None, "workload_id": "pending"}]
        result = apply_policy(snapshot([node("n", 1)], [], pending), self.policy)
        self.assertEqual(result["pending_pressure"]["state"], "unknown")
        self.assertEqual(result["pending_pressure"]["eligible_jobs"], 0)
        self.assertEqual(result["pending_pressure"]["unknown_age_jobs"], 1)
        alert = next(item for item in result["alerts"] if item["code"] == "quota.pending_pressure_unknown")
        self.assertIn("timestamp-unavailable", alert["tags"])

    def test_development_and_zero_gpu_training_limits(self):
        old = datetime.now(timezone.utc) - timedelta(hours=73)
        aid = workload("aid", "user", "aid", [placement("n", 1, 15, 240)], created=old)
        zero_gpu_ok = workload("aid-zero-ok", "user", "aid", [placement("n", 0, 8, 140)])
        zero_gpu_over = workload("aid-zero-over", "user", "aid", [placement("n", 0, 9, 141)])
        cpu = workload("cpu", "user", "trainingJob", [placement("n", 0, 15, 10)])
        result = apply_policy(
            snapshot([node("n", 1, 44, 531)], [aid, zero_gpu_ok, zero_gpu_over, cpu]),
            self.policy,
        )
        by_id = {item["workload_id"]: item for item in result["workloads"]}
        self.assertEqual(by_id["aid"]["policy_status"], "violation")
        self.assertIn("development CPU per node exceeds limit", by_id["aid"]["policy_reasons"])
        self.assertIn("one-GPU development runtime exceeds limit", by_id["aid"]["policy_reasons"])
        runtime_finding = next(
            item for item in by_id["aid"]["policy_findings"]
            if item["code"] == "runtime.development.one_gpu_limit"
        )
        self.assertEqual(runtime_finding["observed"]["runtime_quality"], "estimated")
        self.assertEqual(runtime_finding["observed"]["runtime_source"], "pod_create_time")
        self.assertTrue(by_id["aid"]["runtime_estimated"])
        self.assertEqual(by_id["aid-zero-ok"]["policy_status"], "compliant")
        self.assertIn("development CPU per node exceeds limit", by_id["aid-zero-over"]["policy_reasons"])
        self.assertIn("development memory per node exceeds limit", by_id["aid-zero-over"]["policy_reasons"])
        self.assertIn("training CPU per node exceeds resource ratio", by_id["cpu"]["policy_reasons"])

    def test_development_instance_limit_is_user_scoped(self):
        items = [
            workload("alice-a", "alice", "aid", [placement("n1", 1)]),
            workload("alice-b", "alice", "aid", [placement("n2", 0)]),
            workload("alice-training", "alice", "trainingJob", [placement("n3", 1)]),
            workload("alice-air", "alice", "air", [placement("n4", 0)]),
            workload("charlie-a", "charlie", "aid", [placement("n5", 1)]),
            workload("unknown-a", "unknown", "aid", [placement("n6", 0)]),
            workload("unknown-b", "unknown", "aid", [placement("n7", 0)]),
        ]
        result = apply_policy(
            snapshot([node(f"n{index}", 1) for index in range(1, 8)], items),
            self.policy,
        )
        users = {item["user"]: item for item in result["users"]}
        alice = users["alice"]
        self.assertEqual(alice["development_instance_count"], 2)
        self.assertEqual(alice["status"], "violation")
        finding = next(
            item for item in alice["policy_findings"]
            if item["code"] == "quota.development.instances_per_user"
        )
        self.assertEqual(finding["observed"], {"development_instances": 2})
        self.assertEqual(finding["limit"], {"max_instances_per_user": 1})
        self.assertEqual(users["charlie"]["development_instance_count"], 1)
        self.assertEqual(users["charlie"]["status"], "compliant")
        self.assertEqual(users["unknown"]["development_instance_count"], 2)
        self.assertNotIn(
            "quota.development.instances_per_user",
            users["unknown"]["finding_codes"],
        )
        self.assertTrue(all(item["policy_status"] == "compliant" for item in result["workloads"]))
        alice_group = next(item for item in result["groups"] if item["group"] == "example-team")
        self.assertEqual(alice_group["status"], "compliant")
        alert = next(
            item for item in result["alerts"]
            if item["code"] == "quota.development.instances_per_user"
        )
        self.assertEqual(
            (alert["kind"], alert["subject"], alert["subject_type"]),
            ("user-policy", "alice", "user"),
        )
        relaxed_policy = self.policy.model_copy(update={
            "development": self.policy.development.model_copy(update={
                "max_instances_per_user": 2,
            }),
        })
        relaxed = apply_policy(
            snapshot([node(f"n{index}", 1) for index in range(1, 8)], items),
            relaxed_policy,
        )
        relaxed_alice = next(item for item in relaxed["users"] if item["user"] == "alice")
        self.assertEqual(relaxed_alice["status"], "compliant")

    def test_runtime_quality_variants_are_preserved(self):
        now = datetime.now(timezone.utc)
        exact = workload("exact", "alice", "trainingJob", [placement("n", 1)])
        exact["start_time"] = (now - timedelta(hours=2)).isoformat()
        exact["runtime_anchor_time"] = exact["start_time"]
        exact["runtime_source"] = "training_status_start"
        exact["runtime_quality"] = "exact"
        observed = workload("observed", "alice", "aid", [placement("n", 1)])
        observed["start_time"] = (now - timedelta(hours=73)).isoformat()
        observed["runtime_anchor_time"] = observed["start_time"]
        observed["runtime_source"] = "aid_pod_started_event"
        observed["runtime_quality"] = "observed"
        unavailable = workload("unavailable", "alice", "air", [placement("n", 1)])
        resource_fallback = workload("resource", "alice", "air", [placement("n", 0)])
        resource_fallback["resource_create_time"] = (now - timedelta(hours=3)).isoformat()

        result = apply_policy(
            snapshot([node("n", 3, 16, 40)], [exact, observed, unavailable, resource_fallback]),
            self.policy,
        )
        by_id = {item["workload_id"]: item for item in result["workloads"]}
        self.assertEqual(by_id["exact"]["runtime_quality"], "exact")
        self.assertFalse(by_id["exact"]["runtime_estimated"])
        observed_finding = next(
            item for item in by_id["observed"]["policy_findings"]
            if item["code"] == "runtime.development.one_gpu_limit"
        )
        self.assertEqual(observed_finding["observed"]["runtime_quality"], "observed")
        self.assertEqual(observed_finding["observed"]["runtime_source"], "aid_pod_started_event")
        self.assertIsNone(by_id["unavailable"]["runtime_hours"])
        self.assertEqual(by_id["unavailable"]["runtime_quality"], "unavailable")
        self.assertEqual(by_id["resource"]["runtime_source"], "resource_create_time")
        self.assertEqual(by_id["resource"]["runtime_quality"], "estimated")
        self.assertGreaterEqual(by_id["resource"]["runtime_hours"], 3)

    def test_partial_power_does_not_hide_other_telemetry_coverage(self):
        item = workload("w", "alice", "trainingJob", [placement("n", 1)])
        result = apply_policy(snapshot([node("n", 1)], [item]), self.policy)
        self.assertEqual(result["telemetry"]["reported_gpu_count"], 1)
        self.assertIsNone(result["telemetry"]["gpu_power_total_w"])
        self.assertEqual(result["telemetry_status"], "available")

    def test_low_utilization_boundary_and_status_propagation(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        low = workload("low", "alice", "trainingJob", [placement("n", 1)], created=old)
        low["historical_telemetry"] = {
            "window_hours": 24, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "collection_status": "available", "gpu_compute_util_avg_pct": 20,
            "gpu_memory_util_avg_pct": 20, "compute_sample_count": 100,
            "memory_sample_count": 100,
        }
        result = apply_policy(snapshot([node("n", 1)], [low]), self.policy)
        evaluated = result["workloads"][0]
        self.assertEqual(evaluated["historical_telemetry"]["evaluation_status"], "evaluated")
        self.assertIn("utilization.low_gpu_activity", evaluated["finding_codes"])
        low_finding = next(
            item for item in evaluated["policy_findings"]
            if item["code"] == "utilization.low_gpu_activity"
        )
        self.assertEqual(low_finding["observed"]["runtime_quality"], "estimated")
        self.assertEqual(low_finding["observed"]["runtime_source"], "pod_create_time")
        self.assertEqual(evaluated["policy_status"], "violation")
        self.assertEqual(result["users"][0]["status"], "violation")
        group = next(item for item in result["groups"] if item["group"] == "example-team")
        self.assertEqual(group["status"], "compliant")
        self.assertNotIn("utilization.low_gpu_activity", group["finding_codes"])

    def test_low_utilization_skips_zero_gpu_warming_and_missing_metric(self):
        now = datetime.now(timezone.utc)
        zero = workload("zero", "alice", "trainingJob", [placement("n", 0)], created=now - timedelta(hours=2))
        warming = workload("warming", "alice", "aid", [placement("n", 1)], created=now - timedelta(minutes=30))
        missing = workload("missing", "alice", "trainingJob", [placement("n", 1)], created=now - timedelta(hours=2))
        for item in (warming, missing):
            item["historical_telemetry"] = {
                "window_hours": 24, "fetched_at": now.isoformat(), "collection_status": "available",
                "gpu_compute_util_avg_pct": 10, "gpu_memory_util_avg_pct": 10,
                "compute_sample_count": 10, "memory_sample_count": 10,
            }
        missing["historical_telemetry"]["gpu_memory_util_avg_pct"] = None
        result = apply_policy(snapshot([node("n", 2)], [zero, warming, missing]), self.policy)
        by_id = {item["workload_id"]: item for item in result["workloads"]}
        self.assertEqual(by_id["zero"]["historical_telemetry"]["evaluation_status"], "not-applicable")
        self.assertEqual(by_id["warming"]["historical_telemetry"]["evaluation_status"], "warming-up")
        self.assertEqual(by_id["missing"]["historical_telemetry"]["evaluation_status"], "unavailable")
        self.assertFalse(any("utilization.low_gpu_activity" in item["finding_codes"] for item in by_id.values()))

    def test_unavailable_and_unattributed_nodes_are_explicit(self):
        item = node("offline", 1)
        item["state"] = "NOT_READY"
        item["unattributed"]["gpu"] = 1
        result = apply_policy(snapshot([item], []), self.policy)
        self.assertEqual(result["nodes"][0]["classification"], "unavailable")
        self.assertEqual(result["capacity"]["schedulable_gpu"], 0)
        self.assertTrue(any(alert["kind"] == "node-attribution" for alert in result["alerts"]))

    def test_historical_query_failure_emits_structured_warning(self):
        raw = snapshot([node("n", 0)], [])
        raw["historical_telemetry_status"] = "unavailable"
        result = apply_policy(raw, self.policy)
        alert = next(item for item in result["alerts"] if item["code"] == "telemetry.history_unavailable")
        self.assertEqual(alert["category"], "telemetry")
        self.assertEqual(alert["subject_type"], "queue")

    def test_policy_manager_keeps_last_good_on_invalid_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            groups = Path(directory) / "groups.yaml"
            path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
            groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
            groups.chmod(0o600)
            manager = PolicyManager(path, groups)
            original = manager.policy
            groups.write_text("groups: [broken]", encoding="utf-8")
            self.assertFalse(manager.reload(force=True))
            self.assertIs(manager.policy, original)
            self.assertIsNotNone(manager.error)
            groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
            path.write_text("{broken", encoding="utf-8")
            self.assertFalse(manager.reload(force=True))
            self.assertIs(manager.policy, original)
            path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
            self.assertTrue(manager.reload(force=True))
            self.assertIsNone(manager.error)

    def test_admin_update_rejects_invalid_group_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            groups = Path(directory) / "groups.yaml"
            path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
            groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
            groups.chmod(0o600)
            manager = PolicyManager(path, groups)
            current = manager.admin_config()
            before = groups.read_bytes()
            invalid = yaml.safe_load(current["groups"]["text"])
            invalid["groups"]["default"]["members"] = ["alice"]
            with self.assertRaisesRegex(ValueError, "appears in both"):
                manager.update_config(
                    "groups", yaml.safe_dump(invalid), current["groups"]["revision"], actor="admin",
                )
            self.assertEqual(groups.read_bytes(), before)
            self.assertFalse(manager.audit_path.exists())

    def test_admin_group_update_omits_unlimited_quota_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            groups = Path(directory) / "groups.yaml"
            path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
            groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
            groups.chmod(0o600)
            manager = PolicyManager(path, groups)
            current = manager.admin_config()
            payload = {
                "schema_version": 1,
                "groups": {
                    "unlimited": {"members": ["alice"]},
                    "default": {"gpu_quota": "remainder", "members": []},
                },
            }
            manager.update_config(
                "groups", yaml.safe_dump(payload), current["groups"]["revision"], actor="admin",
            )
            saved = yaml.safe_load(groups.read_text(encoding="utf-8"))
            self.assertEqual(saved["groups"]["unlimited"], {"members": ["alice"]})
            public = manager.public_status()["policy"]["groups"]["unlimited"]
            self.assertEqual(
                public,
                {
                    "gpu_quota": None, "cpu_quota": None,
                    "memory_quota_gib": None, "member_count": 1,
                },
            )

    def test_admin_can_read_and_repair_malformed_raw_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            groups = Path(directory) / "groups.yaml"
            original = RESOURCE_POLICY.read_text(encoding="utf-8")
            path.write_text(original, encoding="utf-8")
            groups.write_text(GROUP_POLICY.read_text(encoding="utf-8"), encoding="utf-8")
            groups.chmod(0o600)
            manager = PolicyManager(path, groups)
            path.write_text("{broken", encoding="utf-8")
            self.assertFalse(manager.reload(force=True))
            current = manager.admin_config()
            self.assertEqual(current["resource"]["text"], "{broken")
            self.assertIsNotNone(current["resource"]["parse_error"])
            self.assertTrue(current["configured"])
            self.assertFalse(current["effective_config_valid"])
            repaired = manager.update_config(
                "resource", original, current["resource"]["revision"], actor="admin",
            )
            self.assertTrue(repaired["effective_config_valid"])
            self.assertIsNone(repaired["resource"]["parse_error"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_audit_failure_does_not_report_committed_config_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            groups = Path(directory) / "groups.yaml"
            path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
            groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
            groups.chmod(0o600)
            manager = PolicyManager(path, groups)
            current = manager.admin_config()
            payload = json.loads(current["resource"]["text"])
            payload["refresh_seconds"] = 31
            with mock.patch.object(manager, "_audit", side_effect=OSError("disk full")):
                result = manager.update_config(
                    "resource", json.dumps(payload), current["resource"]["revision"], actor="admin",
                )
            self.assertEqual(json.loads(path.read_text())["refresh_seconds"], 31)
            self.assertIn("disk full", result["audit_error"])
            self.assertIn("disk full", manager.public_status()["audit_error"])

    def test_policy_manager_rejects_an_invalid_initial_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            groups = Path(directory) / "groups.yaml"
            path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
            groups.write_text("groups: [broken", encoding="utf-8")
            groups.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "initial policy configuration is invalid"):
                PolicyManager(path, groups)

    def test_policy_manager_rejects_a_missing_initial_group_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-groups.yaml"
            with self.assertRaisesRegex(ValueError, "initial policy configuration is invalid"):
                PolicyManager(RESOURCE_POLICY, missing)

    def test_private_group_policy_requires_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = Path(directory) / "groups.yaml"
            groups.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
            groups.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions must be 600"):
                load_policy(RESOURCE_POLICY, groups)


class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = example_policy()

    def test_three_strategies_use_cached_snapshot(self):
        nodes = [node("n1", 1), node("n2", 2), node("n3", 4)]
        jobs = [
            workload("a", "u1", "trainingJob", [placement("n1", 1)]),
            workload("b", "u2", "trainingJob", [placement("n2", 2)]),
            workload("c", "u3", "trainingJob", [placement("n3", 4)]),
        ]
        evaluated = apply_policy(snapshot(nodes, jobs), self.policy)
        request = PlanRequest.model_validate({
            "snapshot_id": "s1", "target": {"nodes": 2, "gpus_per_node": 8},
            "strategies": ["min-gpu", "min-workloads", "min-users"],
            "candidate_scope": "fragmented", "alternatives": 1,
        })
        result = solve_plan(evaluated, request.model_dump(mode="json"))
        self.assertEqual(result["snapshot_id"], "s1")
        self.assertEqual(result["optimality"], "exact")
        self.assertEqual({item["strategy"] for item in result["plans"]},
                         {"min-gpu", "min-workloads", "min-users"})

    def test_plan_filters_and_already_free(self):
        nodes = [node("n1", 0), node("n2", 1)]
        jobs = [workload("a", "u", "trainingJob", [placement("n2", 1)])]
        evaluated = apply_policy(snapshot(nodes, jobs), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "filters": {},
        })
        self.assertEqual(result["optimality"], "not-needed")

    def test_planner_never_counts_an_unavailable_node_as_schedulable(self):
        offline = node("offline", 0)
        offline["state"] = "NOT_READY"
        running = node("running", 1)
        job = workload("a", "u", "trainingJob", [placement("running", 1)])
        evaluated = apply_policy(snapshot([offline, running], [job]), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented", "filters": {},
        })
        self.assertEqual(result["currently_schedulable_nodes"], [])
        self.assertEqual(result["plans"][0]["workloads"], ["a"])
        self.assertEqual(result["plans"][0]["cpus"], 4)
        self.assertEqual(result["plans"][0]["memory_gib"], 10)

    def test_planner_filters_structured_violations(self):
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        low = workload("low", "u1", "trainingJob", [placement("n1", 1)], created=old)
        low["historical_telemetry"] = {
            "window_hours": 24, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "collection_status": "available", "gpu_compute_util_avg_pct": 10,
            "gpu_memory_util_avg_pct": 10, "compute_sample_count": 20, "memory_sample_count": 20,
        }
        normal = workload("normal", "u2", "trainingJob", [placement("n2", 1)], created=old)
        normal["historical_telemetry"] = {**low["historical_telemetry"], "gpu_compute_util_avg_pct": 50}
        evaluated = apply_policy(snapshot([node("n1", 1), node("n2", 1)], [low, normal]), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented",
            "filters": {"violation_categories": ["utilization"], "violation_tags": ["low-utilization"]},
        })
        self.assertEqual(result["plans"][0]["workloads"], ["low"])

    def test_planner_maps_development_user_violation_only_to_aid_workloads(self):
        jobs = [
            workload("aid-a", "u", "aid", [placement("n1", 1)]),
            workload("aid-b", "u", "aid", [placement("n2", 1)]),
            workload("training", "u", "trainingJob", [placement("n3", 1)]),
        ]
        evaluated = apply_policy(
            snapshot([node("n1", 1), node("n2", 1), node("n3", 1)], jobs),
            self.policy,
        )
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented",
            "filters": {"violation_codes": ["quota.development.instances_per_user"]},
        })
        self.assertTrue(result["plans"])
        self.assertTrue(all(
            item["type"] == "aid"
            for plan in result["plans"] for item in plan["workload_details"]
        ))
        self.assertNotIn(
            "training",
            {workload_id for plan in result["plans"] for workload_id in plan["workloads"]},
        )

    def test_planner_resolves_defaults_from_the_pinned_snapshot_profile(self):
        busy = node("n", 1, 4, 10)
        job = workload("a", "u", "trainingJob", [placement("n", 1, 4, 10)])
        evaluated = apply_policy(snapshot([busy], [job]), self.policy)
        request = {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented", "filters": {},
        }
        result = solve_plan(evaluated, request)
        self.assertEqual(result["resolved_target"]["cpus_per_node"], 112)
        self.assertEqual(result["resolved_target"]["memory_per_node_gib"], 1920)
        self.assertEqual(result["defaults_applied"], ["cpus_per_node", "memory_per_node_gib"])
        newer = self.policy.model_copy(update={
            "planning": self.policy.planning.model_copy(update={
                "default_cpu_per_gpu": 10, "default_memory_gib_per_gpu": 100,
            }),
        })
        self.assertEqual(newer.planning.default_cpu_per_gpu, 10)
        pinned = solve_plan(evaluated, request)
        self.assertEqual(pinned["resolved_target"], result["resolved_target"])
        explicit = solve_plan(evaluated, {
            **request,
            "target": {
                "nodes": 1, "gpus_per_node": 8,
                "cpus_per_node": 80, "memory_per_node_gib": 800,
            },
        })
        self.assertEqual(explicit["defaults_applied"], [])
        self.assertEqual(explicit["resolved_target"]["cpus_per_node"], 80)

    def test_attribution_excess_nodes_and_touching_workloads_are_not_planned(self):
        excluded = node("bad", 4, 40, 400)
        excluded["planning_eligible"] = False
        excluded["planning_exclusion_reasons"] = ["attribution.resource_excess"]
        item = workload("bad-workload", "u", "trainingJob", [placement("bad", 4, 40, 400)])
        evaluated = apply_policy(snapshot([excluded], [item]), self.policy)
        self.assertFalse(evaluated["workloads"][0]["planning_eligible"])
        self.assertEqual(evaluated["capacity"]["schedulable_gpu"], 0)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented", "filters": {},
        })
        self.assertEqual(result["plans"], [])
        self.assertEqual(result["no_plan_reason"], "attribution-excluded")
        self.assertEqual(result["strategy_results"][0]["status"], "INFEASIBLE")
        self.assertEqual(result["planning_exclusions"]["node_count"], 1)
        self.assertEqual(result["planning_exclusions"]["workload_count"], 1)

    def test_unattributed_workloads_are_never_release_candidates(self):
        busy = node("n", 1, 4, 10)
        item = workload("unknown-workload", "unknown", "trainingJob", [placement("n", 1, 4, 10)])
        evaluated = apply_policy(snapshot([busy], [item]), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented", "filters": {},
        })
        self.assertEqual(result["plans"], [])
        self.assertNotEqual(result["no_plan_reason"], "no-candidates-after-filters")

    def test_cp_sat_one_node_search_proves_top_k_without_candidate_explosion(self):
        nodes = [node(f"n-{index:02d}", 1, 14, 240) for index in range(20)]
        jobs = [
            workload(
                f"w-{index:02d}", f"u-{index:02d}", "trainingJob",
                [placement(f"n-{index:02d}", 1, 14, 240)],
            )
            for index in range(20)
        ]
        evaluated = apply_policy(snapshot(nodes, jobs), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1",
            "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu", "min-workloads", "min-users"],
            "candidate_scope": "fragmented",
            "alternatives": 3,
            "search_seconds": 10,
            "filters": {},
        })
        self.assertEqual(result["optimality"], "exact")
        self.assertEqual(result["solver"]["backend"], "cp-sat")
        self.assertEqual(result["solver"]["candidate_workload_count"], 20)
        self.assertLess(result["search_elapsed_seconds"], 2)
        self.assertEqual(len(result["plans"]), 9)
        for strategy in result["strategy_results"]:
            self.assertEqual(strategy["status"], "OPTIMAL")
            self.assertTrue(strategy["top_k_complete"])
            self.assertEqual(strategy["returned_alternatives"], 3)
            signatures = {tuple(item["workloads"]) for item in strategy["plans"]}
            self.assertEqual(len(signatures), 3)

    def test_cp_sat_preserves_each_lexicographic_strategy(self):
        jobs = [
            workload("big", "big-user", "trainingJob", [placement("big-node", 3, 42, 720)]),
            workload("small-a", "small-user", "trainingJob", [placement("small-node", 1, 14, 240)]),
            workload("small-b", "small-user", "trainingJob", [placement("small-node", 1, 14, 240)]),
        ]
        evaluated = apply_policy(snapshot([
            node("big-node", 3, 42, 720), node("small-node", 2, 28, 480),
        ], jobs), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu", "min-workloads", "min-users"],
            "candidate_scope": "fragmented", "alternatives": 1,
            "search_seconds": 3, "filters": {},
        })
        plans = {item["strategy"]: item for item in result["plans"]}
        self.assertEqual(plans["min-gpu"]["workloads"], ["small-a", "small-b"])
        self.assertEqual(plans["min-workloads"]["workloads"], ["big"])
        self.assertEqual(plans["min-users"]["workloads"], ["small-a", "small-b"])

    def test_candidate_scope_only_counts_nodes_inside_the_requested_scope(self):
        spanning = workload("spanning", "u", "trainingJob", [
            placement("z-fragmented", 1, 14, 240, "frag-pod"),
            placement("a-full", 8, 112, 1920, "full-pod"),
        ])
        evaluated = apply_policy(snapshot([
            node("z-fragmented", 1, 14, 240), node("a-full", 8, 112, 1920),
        ], [spanning]), self.policy)
        result = solve_plan(evaluated, {
            "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented",
            "alternatives": 1, "search_seconds": 2, "filters": {},
        })
        self.assertEqual(result["plans"][0]["target_nodes"], ["z-fragmented"])
        self.assertEqual(result["plans"][0]["freed_nodes"], ["z-fragmented"])

    def test_unknown_cp_sat_result_uses_verified_greedy_fallback(self):
        from clusterx_monitor.planning.domain import SolveAttempt

        busy = node("n", 1, 14, 240)
        item = workload("a", "u", "trainingJob", [placement("n", 1, 14, 240)])
        evaluated = apply_policy(snapshot([busy], [item]), self.policy)
        unknown = SolveAttempt(
            status="UNKNOWN", selected=(), objective_value=None,
            best_objective_bound=None, wall_time_seconds=1,
            deterministic_time_seconds=0.1, branches=1, conflicts=0,
        )
        with mock.patch(
            "clusterx_monitor.planning.solver.solve_once", return_value=unknown,
        ):
            result = solve_plan(evaluated, {
                "snapshot_id": "s1", "target": {"nodes": 1, "gpus_per_node": 8},
                "strategies": ["min-gpu"], "candidate_scope": "fragmented",
                "alternatives": 1, "search_seconds": 2, "filters": {},
            })
        self.assertEqual(result["optimality"], "heuristic")
        self.assertEqual(result["plans"][0]["rank_status"], "HEURISTIC")
        self.assertEqual(result["plans"][0]["workloads"], ["a"])


class ConsoleLinkTests(unittest.TestCase):
    def setUp(self):
        self.cluster = mock.Mock(cfg={
            "subscription": "subscription-id",
            "resource_group": "default",
            "region": "cn-pj-03",
            "workspace": "workspace-name",
        })

    def test_console_routes_cover_all_supported_workload_types(self):
        cases = {
            "trainingJob": ("training/detail/", "trainingJobs"),
            "aid": ("development/detail", "aids"),
            "air": ("air/detail/", "airs"),
        }
        for kind, (route, collection) in cases.items():
            rid = (
                "/subscriptions/subscription-id/resourceGroups/default/regions/cn-pj-03/"
                f"workspaces/workspace-name/{collection}/resource-name"
            )
            url = _workload_console_url(self.cluster, kind, resource_id=rid)
            self.assertIsNotNone(url)
            parsed = urlsplit(str(url))
            self.assertEqual(parsed.netloc, "console.d.pjlab.org.cn")
            self.assertEqual(parsed.path, f"/cn-pj-03/ssp/model/{route}")
            self.assertEqual(parse_qs(parsed.query)["rid"], [rid])

    def test_console_url_builds_resource_id_from_cluster_scope(self):
        url = _workload_console_url(
            self.cluster, "air", resource_name="infer-qwen38-0",
        )
        parsed = urlsplit(str(url))
        self.assertEqual(
            parse_qs(parsed.query)["rid"],
            [
                "/subscriptions/subscription-id/resourceGroups/default/regions/cn-pj-03/"
                "workspaces/workspace-name/airs/infer-qwen38-0"
            ],
        )

    def test_console_url_rejects_unknown_types_and_incomplete_scope(self):
        self.assertIsNone(_workload_console_url(self.cluster, "unknown", resource_name="name"))
        self.assertIsNone(_workload_console_url(
            mock.Mock(cfg={}), "aid", resource_name="name",
        ))


class LifecycleTests(unittest.TestCase):
    def test_priority_normalizes_clusterx_numeric_and_named_values(self):
        self.assertEqual(_priority(1), "NORMAL")
        self.assertEqual(_priority("high"), "HIGH")
        self.assertEqual(_priority("3"), "HIGHEST")
        self.assertIsNone(_priority(None))

    def test_training_lifecycle_uses_status_start_time_and_uid(self):
        cluster = mock.Mock()
        cluster._get_queue_id.return_value = "queue-id"
        cluster.client.list_training_jobs.return_value = {
            "total_size": 1,
            "training_jobs": [{
                "uid": "job-uid",
                "spec": {"priority": "HIGH"},
                "status": {
                    "create_time": "2026-08-13T06:28:12Z",
                    "start_time": "2026-08-13T08:02:26Z",
                },
            }],
        }
        rows, complete = _running_training_lifecycle(cluster, "queue")
        self.assertTrue(complete)
        self.assertEqual(rows["job-uid"]["start_time"], "2026-08-13T08:02:26+00:00")
        self.assertEqual(rows["job-uid"]["priority"], "HIGH")
        self.assertEqual(rows["job-uid"]["runtime_quality"], "exact")
        cluster.client.list_training_jobs.assert_called_once_with(
            filter_str='queue_id="queue-id" AND state="RUNNING"', page_size=1000,
        )

    def test_air_lifecycle_uses_latest_available_transition(self):
        client = mock.Mock(compute_base_endpoint="https://compute.example")
        client._get_base_path.return_value = "/subscriptions/s/workspaces/w/"
        client._make_signed_base_request.return_value = {
            "total_size": 1,
            "airs": [{
                "uid": "air-uid",
                "spec": {"priority": 3},
                "status": {
                    "create_time": "2026-08-14T08:00:00Z",
                    "conditions": [
                        {"type": "Available", "status": "False", "last_transition_time": "2026-08-14T08:01:00Z"},
                        {"type": "Available", "status": "True", "last_transition_time": "2026-08-14T08:02:00Z"},
                        {"type": "Available", "status": "True", "last_transition_time": "2026-08-14T08:03:00Z"},
                    ],
                },
            }],
        }
        rows, complete = _running_air_lifecycle(mock.Mock(client=client))
        self.assertTrue(complete)
        self.assertEqual(rows["air-uid"]["start_time"], "2026-08-14T08:03:00+00:00")
        self.assertEqual(rows["air-uid"]["priority"], "HIGHEST")
        self.assertEqual(rows["air-uid"]["runtime_quality"], "observed")
        self.assertEqual(
            _available_transition({"conditions": [{"type": "Available", "status": "False"}]}),
            None,
        )

    def test_aid_start_events_match_current_pod_and_paginate(self):
        client = mock.Mock(compute_base_endpoint="https://compute.example")
        client._make_signed_base_request.side_effect = [
            {"total_size": 4, "events": [
                {"type": "Normal", "reason": "Started", "firstTimestamp": "2026-08-14T08:00:05Z", "lastTimestamp": "2026-08-14T09:00:00Z", "count": 2, "involvedObject": {"uid": "current"}},
                {"type": "Normal", "reason": "Started", "firstTimestamp": "2026-08-14T08:00:10Z", "involvedObject": {"uid": "current"}},
            ]},
            {"total_size": 4, "events": [
                {"type": "Warning", "reason": "Started", "firstTimestamp": "2026-08-14T08:00:20Z", "involvedObject": {"uid": "current"}},
                {"type": "Normal", "reason": "Started", "firstTimestamp": "2026-08-13T08:00:00Z", "involvedObject": {"uid": "old"}},
            ]},
        ]
        starts = _aid_pod_start_times(
            mock.Mock(client=client), "/subscriptions/s/workspaces/w/aids/a", {"current"},
        )
        self.assertEqual(starts, {"current": "2026-08-14T08:00:10+00:00"})
        self.assertEqual(client._make_signed_base_request.call_count, 2)
        self.assertEqual(
            client._make_signed_base_request.call_args_list[1].kwargs["params"]["skip"], 2,
        )

    def test_aid_start_cache_resets_only_when_pod_uid_changes(self):
        collector = ClusterCollector(mock.Mock(), "queue", "cluster")

        def aid_workload(pod_uid: str) -> dict:
            return {
                "workload_id": "aid", "type": "aid", "create_time": "2026-08-14T07:59:00Z",
                "_resource_id": "/subscriptions/s/workspaces/w/aids/a",
                "placements": [{"_pod_uid": pod_uid}],
            }

        with mock.patch("clusterx_monitor.collector._running_aid_lifecycle", return_value=({}, True)), mock.patch(
            "clusterx_monitor.collector._aid_pod_start_times",
            side_effect=[{"pod-1": "2026-08-14T08:00:00+00:00"}, {"pod-2": "2026-08-15T08:00:00+00:00"}],
        ) as events:
            first = {"aid": aid_workload("pod-1")}
            collector._enrich_lifecycle(first, [])
            self.assertEqual(first["aid"]["start_time"], "2026-08-14T08:00:00+00:00")

            same = {"aid": aid_workload("pod-1")}
            collector._enrich_lifecycle(same, [])
            self.assertEqual(same["aid"]["start_time"], "2026-08-14T08:00:00+00:00")

            rebuilt = {"aid": aid_workload("pod-2")}
            collector._enrich_lifecycle(rebuilt, [])
            self.assertEqual(rebuilt["aid"]["start_time"], "2026-08-15T08:00:00+00:00")
            self.assertEqual(events.call_count, 2)

    def test_lifecycle_failure_falls_back_to_estimated_pod_time(self):
        collector = ClusterCollector(mock.Mock(), "queue", "cluster")
        item = {
            "workload_id": "job", "type": "trainingJob",
            "create_time": "2026-08-14T07:59:00Z", "placements": [],
        }
        warnings = []
        with mock.patch(
            "clusterx_monitor.collector._running_training_lifecycle",
            side_effect=RuntimeError("unavailable"),
        ):
            collector._enrich_lifecycle({"job": item}, warnings)
        self.assertEqual(item["runtime_anchor_time"], item["create_time"])
        self.assertEqual(item["runtime_source"], "pod_create_time")
        self.assertEqual(item["runtime_quality"], "estimated")
        self.assertTrue(item["runtime_estimated"])
        self.assertIn("trainingJob lifecycle inventory is unavailable", warnings)

    def test_lifecycle_failure_reuses_same_uid_trusted_start(self):
        collector = ClusterCollector(mock.Mock(), "queue", "cluster")

        def training_workload() -> dict:
            return {
                "workload_id": "job", "type": "trainingJob",
                "create_time": "2026-08-14T07:59:00Z", "placements": [],
            }

        exact = {
            "job": {
                "resource_create_time": "2026-08-14T06:00:00+00:00",
                "start_time": "2026-08-14T08:00:00+00:00",
                "priority": "HIGH",
                "runtime_source": "training_status_start",
                "runtime_quality": "exact",
            },
        }
        with mock.patch(
            "clusterx_monitor.collector._running_training_lifecycle",
            side_effect=[(exact, True), RuntimeError("temporary failure")],
        ):
            first = {"job": training_workload()}
            collector._enrich_lifecycle(first, [])
            warnings = []
            second = {"job": training_workload()}
            collector._enrich_lifecycle(second, warnings)

        self.assertEqual(second["job"]["start_time"], "2026-08-14T08:00:00+00:00")
        self.assertEqual(second["job"]["priority"], "HIGH")
        self.assertEqual(second["job"]["runtime_quality"], "exact")
        self.assertFalse(second["job"]["runtime_estimated"])
        self.assertIn("trainingJob lifecycle inventory is unavailable", warnings)

    def test_lifecycle_without_any_time_is_unavailable(self):
        collector = ClusterCollector(mock.Mock(), "queue", "cluster")
        item = {"workload_id": "job", "type": "trainingJob", "placements": []}
        with mock.patch(
            "clusterx_monitor.collector._running_training_lifecycle",
            return_value=({}, True),
        ):
            collector._enrich_lifecycle({"job": item}, [])
        self.assertIsNone(item["runtime_anchor_time"])
        self.assertIsNone(item["runtime_source"])
        self.assertEqual(item["runtime_quality"], "unavailable")
        self.assertFalse(item["runtime_estimated"])


class StoreAndTelemetryTests(unittest.TestCase):
    def test_snapshot_store_is_bounded_and_marks_stale(self):
        store = SnapshotStore(capacity=2)
        for index in range(3):
            store.publish({
                "snapshot_id": f"s{index}",
                "generated_at": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat(),
            })
        self.assertIsNone(store.get("s0"))
        self.assertEqual(store.latest()["snapshot_id"], "s2")
        self.assertTrue(store.status(10)["stale"])
        store.record_failure("failed")
        self.assertEqual(store.status(10)["last_error"], "failed")

    def test_snapshot_store_keeps_lightweight_history_beyond_full_snapshots(self):
        store = SnapshotStore(capacity=2, history_capacity=4)
        for index in range(4):
            store.publish({
                "snapshot_id": f"history-{index}",
                "generated_at": (datetime.now(timezone.utc) + timedelta(seconds=index)).isoformat(),
                "capacity": {"allocated_gpu": index, "bound_gpu": 8, "free_gpu": 8 - index},
                "pending_workloads": [{}] * index,
                "pending_pressure": {"eligible_jobs": index},
                "telemetry": {"gpu_compute_util_avg_pct": index * 10},
                "alerts": [], "nodes": [], "workloads": [],
            })
        self.assertIsNone(store.get("history-0"))
        self.assertEqual(len(store.history()["points"]), 4)
        self.assertEqual(store.history()["points"][0]["allocated_gpu"], 0)
        comparison = store.compare("history-2", "history-3")
        self.assertEqual(comparison["deltas"]["allocated_gpu"], 1)
        self.assertEqual(comparison["deltas"]["pending_workloads"], 1)

    def test_plan_cache_returns_copy(self):
        cache = PlanCache(capacity=1)
        cache.put("key", {"plans": []})
        first = cache.get("key")
        first["plans"].append("changed")
        self.assertEqual(cache.get("key")["plans"], [])

    def test_resource_parser_and_power_attachment(self):
        self.assertEqual(resource_number("14000m"), 14)
        self.assertEqual(resource_number("240GiB", memory=True), 240)
        jobs = {"w": workload("w", "u", "trainingJob", [placement("n", 1, pod="p")])}
        series = []
        for metric, value in (("gpu-compute-util", 50), ("gpu-memory-util", 60), ("gpu-power", 300)):
            series.append({
                "metric": {
                    "label_resource_compute_sensecore_cn_workload_uid": "w",
                    "Hostname": "host-a", "exported_pod": "p", "UUID": "gpu-1",
                    "gpu": "0", "monitor_metric": metric,
                }, "value": [0, str(value)],
            })
        warnings = _attach_telemetry(jobs, series, {"host-a": "n"})
        self.assertEqual(warnings, [])
        self.assertEqual(jobs["w"]["gpus"][0]["gpu_power_w"], 300)

    def test_historical_prometheus_series_merge_by_workload_uid(self):
        cluster = mock.Mock(cfg={"workspace": "ws"})
        rows = [
            {"metric": {"label_resource_compute_sensecore_cn_workload_uid": "w", "monitor_metric": "history-gpu-compute-util"}, "value": [0, "19.5"]},
            {"metric": {"label_resource_compute_sensecore_cn_workload_uid": "w", "monitor_metric": "history-gpu-memory-util"}, "value": [0, "20"]},
            {"metric": {"label_resource_compute_sensecore_cn_workload_uid": "w", "monitor_metric": "history-gpu-compute-samples"}, "value": [0, "720"]},
            {"metric": {"label_resource_compute_sensecore_cn_workload_uid": "w", "monitor_metric": "history-gpu-memory-samples"}, "value": [0, "710"]},
        ]
        with mock.patch("clusterx_monitor.collector._query_prometheus", return_value=rows) as query:
            result = _query_workload_history(cluster, "q", "c", 24)
        self.assertEqual(result["w"]["gpu_compute_util_avg_pct"], 19.5)
        self.assertEqual(result["w"]["gpu_memory_util_avg_pct"], 20)
        self.assertEqual(result["w"]["compute_sample_count"], 720)
        self.assertIn("sum_over_time", query.call_args.args[1])
        self.assertIn("sum by (label_resource_compute_sensecore_cn_workload_uid)", query.call_args.args[1])

    def test_historical_collection_cache_and_non_blocking_failure(self):
        client = mock.Mock()
        client.list_queue_nodes.return_value = {"nodes": [], "total_size": 0}
        cluster = mock.Mock(client=client, cfg={"workspace": "ws"})
        collector = ClusterCollector(cluster, "q", "c")
        with mock.patch("clusterx_monitor.collector._query_gpu_telemetry", return_value=[]), mock.patch(
            "clusterx_monitor.collector._pending_workloads", return_value=([], True)
        ), mock.patch("clusterx_monitor.collector._query_workload_history", return_value={}) as history:
            first = collector.collect(historical_refresh_minutes=5)
            second = collector.collect(historical_refresh_minutes=5)
        self.assertEqual(history.call_count, 1)
        self.assertEqual(first["historical_telemetry_status"], "available")
        self.assertEqual(second["historical_telemetry_status"], "available")

        failed = ClusterCollector(cluster, "q", "c")
        with mock.patch("clusterx_monitor.collector._query_gpu_telemetry", return_value=[]), mock.patch(
            "clusterx_monitor.collector._pending_workloads", return_value=([], True)
        ), mock.patch("clusterx_monitor.collector._query_workload_history", side_effect=RuntimeError("prom down")) as history:
            result = failed.collect(historical_refresh_minutes=5)
            second = failed.collect(historical_refresh_minutes=5)
        self.assertEqual(result["historical_telemetry_status"], "unavailable")
        self.assertEqual(second["historical_telemetry_status"], "unavailable")
        self.assertEqual(history.call_count, 1)
        self.assertIn("historical workload GPU telemetry is unavailable", result["warnings"])

    def test_collector_sums_running_resources_across_placements(self):
        def raw_node(name, node_id, gpu, cpu, memory):
            return {
                "name": name, "id": node_id, "host_ip": f"10.0.0.{node_id[-1]}", "state": "RUNNING",
                "summary_data": [
                    {"resource_type": "DEVICE", "allocated": gpu, "total": 8},
                    {"resource_type": "CPU", "allocated": cpu, "total": 112},
                    {"resource_type": "MEMORY", "allocated": memory, "total": 1920, "unit": "GiB"},
                ],
            }

        raw_nodes = [raw_node("n1", "id1", 1, 4, 10), raw_node("n2", "id2", 2, 8, 20)]
        client = mock.Mock()
        client.list_queue_nodes.return_value = {"nodes": raw_nodes, "total_size": 2}

        def pods(_method, _path, *, params):
            suffix = params["node_id"][-1]
            gpu, cpu, memory = (1, "4000m", "10GiB") if suffix == "1" else (2, "8000m", "20GiB")
            return {"total_size": 1, "pods": [{
                "name": f"pod-{suffix}", "create_time": "2026-08-14T00:00:00Z",
                "workload": {"uid": "w", "display_name": "train", "type": "trainingJob"},
                "ownership": {"creator_name": "alice"}, "workspace": {"name": "ws"},
                "resource": {"accelerate_device_count": gpu, "cpu": cpu, "memory": memory},
            }]}

        client._make_management_request.side_effect = pods
        cluster = mock.Mock(client=client, cfg={"workspace": "ws"})
        collector = ClusterCollector(cluster, "q", "c")
        with mock.patch("clusterx_monitor.collector._query_gpu_telemetry", return_value=[]), mock.patch(
            "clusterx_monitor.collector._pending_workloads", return_value=([], True)
        ), mock.patch("clusterx_monitor.collector._query_workload_history", return_value={}):
            result = collector.collect()
        item = result["workloads"][0]
        self.assertEqual(
            (item["total_gpu"], item["total_cpu"], item["total_memory_gib"]),
            (3, 12, 30),
        )
        self.assertEqual(item["resource_basis"], "attributed")

    def test_node_signature_detects_identity_state_totals_and_allocations(self):
        def raw(*, node_id="id", state="RUNNING", gpu_allocated=1, gpu_total=8):
            return [{
                "name": "n", "id": node_id, "state": state,
                "summary_data": [
                    {"resource_type": "DEVICE", "allocated": gpu_allocated, "total": gpu_total},
                    {"resource_type": "CPU", "allocated": 4, "total": 112},
                    {"resource_type": "MEMORY", "allocated": 10, "total": 1920, "unit": "GiB"},
                ],
            }]
        baseline = _node_signature(raw())
        for changed in (
            raw(node_id="other"), raw(state="IDLE"), raw(gpu_allocated=2), raw(gpu_total=16),
        ):
            self.assertNotEqual(baseline, _node_signature(changed))

    def test_queue_node_inventory_paginates(self):
        client = mock.Mock()

        def list_queue_nodes(**arguments):
            if "page_token" not in arguments:
                return {
                    "nodes": [{"id": "id-1", "name": "node-1"}],
                    "total_size": 2,
                    "next_page_token": "cursor-1",
                }
            self.assertEqual(arguments["page_token"], "cursor-1")
            return {
                "nodes": [{"id": "id-2", "name": "node-2"}],
                "total_size": 2,
            }

        client.list_queue_nodes.side_effect = list_queue_nodes
        nodes = _list_queue_nodes(mock.Mock(client=client), "cluster", "queue")
        self.assertEqual([node["name"] for node in nodes], ["node-1", "node-2"])
        self.assertEqual(client.list_queue_nodes.call_count, 2)
        self.assertNotIn("page_token", client.list_queue_nodes.call_args_list[0].kwargs)
        self.assertEqual(
            client.list_queue_nodes.call_args_list[1].kwargs["page_token"], "cursor-1",
        )

    def test_queue_node_pagination_rejects_inconsistent_responses(self):
        cases = {
            "nodes must be a list": [{"nodes": "", "total_size": 0}],
            "total_size must be an integer": [{"nodes": [], "total_size": True}],
            "truncated": [{
                "nodes": [{"id": "id-1", "name": "node-1"}], "total_size": 2,
            }],
            "duplicate node id": [
                {
                    "nodes": [{"id": "id-1", "name": "node-1"}],
                    "total_size": 2,
                    "next_page_token": "cursor-1",
                },
                {"nodes": [{"id": "id-1", "name": "node-2"}], "total_size": 2},
            ],
            "total_size changed": [
                {
                    "nodes": [{"id": "id-1", "name": "node-1"}],
                    "total_size": 2,
                    "next_page_token": "cursor-1",
                },
                {"nodes": [{"id": "id-2", "name": "node-2"}], "total_size": 3},
            ],
            "cursor repeated": [
                {
                    "nodes": [{"id": "id-1", "name": "node-1"}],
                    "total_size": 3,
                    "next_page_token": "cursor-1",
                },
                {
                    "nodes": [{"id": "id-2", "name": "node-2"}],
                    "total_size": 3,
                    "next_page_token": "cursor-1",
                },
            ],
        }
        for message, responses in cases.items():
            with self.subTest(message=message):
                client = mock.Mock()
                client.list_queue_nodes.side_effect = responses
                with self.assertRaisesRegex(RuntimeError, message):
                    _list_queue_nodes(mock.Mock(client=client), "cluster", "queue")

    def test_collector_uses_complete_paginated_inventory_before_and_after(self):
        client = mock.Mock()

        def list_queue_nodes(**arguments):
            if "page_token" not in arguments:
                return {
                    "nodes": [{
                        "id": "id-1", "name": "node-1", "state": "RUNNING",
                        "summary_data": [],
                    }],
                    "total_size": 2,
                    "next_page_token": "cursor-1",
                }
            return {
                "nodes": [{
                    "id": "id-2", "name": "node-2", "state": "RUNNING",
                    "summary_data": [],
                }],
                "total_size": 2,
            }

        client.list_queue_nodes.side_effect = list_queue_nodes
        cluster = mock.Mock(client=client, cfg={"workspace": "workspace"})
        collector = ClusterCollector(cluster, "queue", "cluster")
        with mock.patch(
            "clusterx_monitor.collector._query_gpu_telemetry", return_value=[],
        ), mock.patch(
            "clusterx_monitor.collector._pending_workloads", return_value=([], True),
        ), mock.patch(
            "clusterx_monitor.collector._query_workload_history", return_value={},
        ):
            result = collector.collect()
        self.assertEqual([node["node"] for node in result["nodes"]], ["node-1", "node-2"])
        self.assertEqual(client.list_queue_nodes.call_count, 4)

    def test_collector_rejects_a_truncated_bound_node_inventory(self):
        client = mock.Mock()
        client.list_queue_nodes.return_value = {
            "nodes": [{"id": "id-1", "name": "node-1"}], "total_size": 2,
        }
        cluster = mock.Mock(client=client)
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            ClusterCollector(cluster, "queue", "cluster").collect()

    def test_pending_inventory_tracks_completeness_and_zero_gpu_jobs(self):
        cluster = mock.Mock(cfg={
            "subscription": "subscription-id", "resource_group": "default",
            "region": "cn-pj-03", "workspace": "workspace-name",
        })
        cluster._get_queue_id.return_value = "queue-id"
        created = datetime.now(timezone.utc) - timedelta(minutes=15)
        cluster.client.list_training_jobs.return_value = {
            "total_size": 2,
            "training_jobs": [{
                "name": "cpu-only", "ownership": {"creator_name": "UserA"},
                "status": {"create_time": created.isoformat()},
                "spec": {"priority": "HIGH", "vc_job": {"tasks": [{
                    "replicas": 1,
                    "resource_spec": {"accelerate_device_count": 0, "cpu_count": 14, "memory_gib": 240},
                }]}},
            }],
        }
        jobs, complete = _pending_workloads(cluster, "queue")
        self.assertFalse(complete)
        self.assertEqual(jobs[0]["gpus_per_node"], 0)
        self.assertEqual(jobs[0]["user"], "usera")
        self.assertEqual(jobs[0]["create_time"], created.isoformat())
        self.assertEqual(jobs[0]["resource_create_time"], created.isoformat())
        self.assertEqual(jobs[0]["priority"], "HIGH")
        self.assertGreaterEqual(jobs[0]["queue_age_seconds"], 15 * 60)
        self.assertEqual(jobs[0]["total_cpu"], 14)
        self.assertEqual(jobs[0]["total_memory_gib"], 240)
        self.assertEqual(jobs[0]["resource_basis"], "requested")
        self.assertEqual(
            parse_qs(urlsplit(jobs[0]["console_url"]).query)["rid"],
            [
                "/subscriptions/subscription-id/resourceGroups/default/regions/cn-pj-03/"
                "workspaces/workspace-name/trainingJobs/cpu-only"
            ],
        )

    def test_pending_inventory_sums_heterogeneous_tasks_without_fake_per_node_shape(self):
        cluster = mock.Mock()
        cluster._get_queue_id.return_value = "queue-id"
        cluster.client.list_training_jobs.return_value = {
            "training_jobs": [{
                "name": "distributed", "ownership": {"creator_name": "Alice"},
                "spec": {"vc_job": {"tasks": [
                    {"name": "master", "role": "PYTORCH_MASTER", "replicas": 1,
                     "resource_spec": {"accelerate_device_count": 1, "cpu_count": 4, "memory_gib": 100}},
                    {"name": "worker", "role": "PYTORCH_WORKER", "replicas": 3,
                     "resource_spec": {"accelerate_device_count": 2, "cpu_count": 8, "memory_gib": 200}},
                ]}},
            }],
        }
        jobs, complete = _pending_workloads(cluster, "queue")
        self.assertTrue(complete)
        item = jobs[0]
        self.assertEqual(item["num_nodes"], 4)
        self.assertEqual((item["total_gpu"], item["total_cpu"], item["total_memory_gib"]), (7, 28, 700))
        self.assertIsNone(item["gpus_per_node"])
        self.assertIsNone(item["cpus_per_node"])
        self.assertIsNone(item["memory_per_node_gib"])
        self.assertEqual([row["name"] for row in item["task_resources"]], ["master", "worker"])

    def test_pending_inventory_keeps_homogeneous_shape_and_null_missing_resources(self):
        cluster = mock.Mock()
        cluster._get_queue_id.return_value = "queue-id"
        cluster.client.list_training_jobs.return_value = {
            "training_jobs": [
                {
                    "name": "homogeneous", "spec": {"vc_job": {"tasks": [
                        {"name": "master", "replicas": 1,
                         "resource_spec": {"accelerate_device_count": 1, "cpu_count": 4, "memory_gib": 100}},
                        {"name": "worker", "replicas": 2,
                         "resource_spec": {"accelerate_device_count": 1, "cpu_count": 4, "memory_gib": 100}},
                    ]}},
                },
                {
                    "name": "missing", "spec": {"vc_job": {"tasks": [
                        {"name": "worker", "replicas": 2,
                         "resource_spec": {"accelerate_device_count": 1, "memory_gib": "invalid"}},
                    ]}},
                },
            ],
        }
        jobs, _ = _pending_workloads(cluster, "queue")
        homogeneous, item = jobs
        self.assertEqual(
            (homogeneous["gpus_per_node"], homogeneous["cpus_per_node"], homogeneous["memory_per_node_gib"]),
            (1, 4, 100),
        )
        self.assertEqual(
            (homogeneous["total_gpu"], homogeneous["total_cpu"], homogeneous["total_memory_gib"]),
            (3, 12, 300),
        )
        self.assertEqual(item["gpus_per_node"], 1)
        self.assertIsNone(item["cpus_per_node"])
        self.assertIsNone(item["memory_per_node_gib"])
        self.assertEqual(item["total_gpu"], 2)
        self.assertIsNone(item["total_cpu"])
        self.assertIsNone(item["total_memory_gib"])

    def test_pending_inventory_ignores_removed_timestamp_fields(self):
        cluster = mock.Mock()
        cluster._get_queue_id.return_value = "queue-id"
        old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cluster.client.list_training_jobs.return_value = {
            "training_jobs": [{
                "name": "old-shape",
                "metadata": {"created_at": old_timestamp},
                "create_time": old_timestamp,
            }],
        }
        jobs, complete = _pending_workloads(cluster, "queue")
        self.assertTrue(complete)
        self.assertIsNone(jobs[0]["create_time"])
        self.assertIsNone(jobs[0]["resource_create_time"])
        self.assertIsNone(jobs[0]["queue_age_seconds"])
        self.assertIsNone(jobs[0]["priority"])


class SkillCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = ROOT / "skill/clusterx-manage-jobs/scripts/monitor_cli.py"
        spec = importlib.util.spec_from_file_location("monitor_skill_cli", script)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(cls.module)

    def test_cli_uses_http_and_never_imports_monitor_core(self):
        source = (ROOT / "skill/clusterx-manage-jobs/scripts/monitor_cli.py").read_text()
        self.assertNotIn("clusterx_monitor", source)
        self.assertNotIn("SSPCluster", source)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"service": "clusterx-monitor", "snapshot": {"stale": False}}
        with mock.patch.object(self.module.requests, "request", return_value=response), mock.patch.object(
            sys, "argv", ["monitor_cli.py", "status", "--format", "json"]
        ), mock.patch("sys.stdout"):
            self.assertEqual(self.module.main(), 0)

    def test_unavailable_returns_three(self):
        with mock.patch.object(
            self.module.requests, "request", side_effect=self.module.requests.RequestException("down")
        ), mock.patch.object(sys, "argv", ["monitor_cli.py", "status"]), mock.patch("sys.stderr"):
            self.assertEqual(self.module.main(), 3)

    def test_workload_table_includes_resource_totals_and_basis(self):
        with mock.patch.dict("os.environ", {"COLUMNS": "240"}):
            rendered = self.module._render_table([{
                "workload_name": "train", "total_gpu": 2, "total_cpu": 8,
                "total_memory_gib": 200, "resource_basis": "requested",
                "priority": "HIGH", "resource_create_time": "2026-08-14T07:55:00Z",
                "queue_age_seconds": 300,
                "start_time": "2026-08-14T08:00:00Z", "runtime_hours": 2.5,
                "runtime_quality": "exact", "runtime_source": "training_status_start",
            }], no_color=True)
        for field in (
            "total_gpu", "total_cpu", "total_memory_gib", "resource_basis",
            "priority", "resource_create_time", "queue_age_seconds",
            "start_time", "runtime_hours", "runtime_quality", "runtime_source",
        ):
            self.assertIn(field, rendered)

    def test_group_table_shows_independent_unlimited_quotas(self):
        with mock.patch.dict("os.environ", {"COLUMNS": "240"}):
            rendered = self.module._render_table([{
                "group": "team", "status": "compliant", "gpu_quota": 8,
                "cpu_quota": None, "memory_quota_gib": None, "allocated_gpu": 1,
            }], no_color=True)
        self.assertIn("cpu_quota", rendered)
        self.assertIn("memory_quota_gib", rendered)
        self.assertIn("不限", rendered)

    def test_api_validation_returns_two_and_filtered_stale_uses_snapshot(self):
        with mock.patch.object(
            self.module, "_snapshot", return_value={"snapshot_id": "s1"}
        ), mock.patch.object(
            self.module, "_request", side_effect=self.module.ApiError("invalid target", status=422)
        ), mock.patch.object(
            sys, "argv", ["monitor_cli.py", "plan", "--nodes", "1"]
        ), mock.patch("sys.stderr"):
            self.assertEqual(self.module.main(), 2)

        stale = {
            "snapshot_id": "s1", "freshness": {"stale": True},
            "nodes": [], "users": [], "groups": [], "workloads": [],
            "pending_workloads": [], "alerts": [],
        }
        with mock.patch.object(
            self.module, "_snapshot", return_value=stale,
        ), mock.patch.object(
            sys, "argv", ["monitor_cli.py", "nodes", "--fail-on", "stale", "--format", "json"],
        ), mock.patch("sys.stdout"):
            self.assertEqual(self.module.main(), 4)

    def test_nodes_does_not_expose_undefined_violation_failure_mode(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            self.module.build_parser().parse_args(["nodes", "--fail-on", "violation"])
        self.assertEqual(raised.exception.code, 2)

    def test_structured_finding_filters_and_fail_on(self):
        args = self.module.build_parser().parse_args([
            "workloads", "--finding-category", "utilization",
            "--finding-code", "utilization.low_gpu_activity", "--tag", "gpu",
            "--priority", "high",
        ])
        matching = {
            "workload_id": "low", "finding_categories": ["utilization"],
            "finding_codes": ["utilization.low_gpu_activity"],
            "finding_tags": ["historical", "gpu"],
            "priority": "HIGH",
            "policy_findings": [{"status": "violation"}],
        }
        rows = self.module._filter_rows([matching, {
            "workload_id": "other", "finding_categories": ["quota"],
            "finding_codes": ["quota.gpu"], "finding_tags": ["quota"], "priority": "NORMAL",
        }], args)
        self.assertEqual([item["workload_id"] for item in rows], ["low"])
        self.assertTrue(self.module._has_failure(rows, "violation"))

    def test_plan_sends_structured_violation_filters(self):
        with mock.patch.object(self.module, "_snapshot", return_value={"snapshot_id": "s1"}), mock.patch.object(
            self.module, "_request", return_value={"plans": []}
        ) as request, mock.patch.object(
            sys, "argv", [
                "monitor_cli.py", "plan", "--nodes", "1",
                "--violation-category", "utilization",
                "--violation-code", "utilization.low_gpu_activity",
                "--violation-tag", "low-utilization", "--format", "json",
            ]
        ), mock.patch("sys.stdout"):
            self.assertEqual(self.module.main(), 0)
        filters = request.call_args.kwargs["json"]["filters"]
        self.assertEqual(filters["violation_categories"], ["utilization"])
        self.assertEqual(filters["violation_codes"], ["utilization.low_gpu_activity"])
        self.assertEqual(filters["violation_tags"], ["low-utilization"])


if __name__ == "__main__":
    unittest.main()
