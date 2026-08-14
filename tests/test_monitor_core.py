from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import yaml


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RESOURCE_POLICY = ROOT / "skill/clusterx-manage-jobs/assets/resource-policy.json"
GROUP_POLICY = ROOT / "config/groups.example.yaml"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clusterx_monitor.collector import (
    ClusterCollector,
    _attach_telemetry,
    _node_signature,
    _pending_workloads,
    _query_workload_history,
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
        "total_gpu": gpu_count, "placements": placements, "gpus": cards,
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
        self.assertEqual(by_id["aid-zero-ok"]["policy_status"], "compliant")
        self.assertIn("development CPU per node exceeds limit", by_id["aid-zero-over"]["policy_reasons"])
        self.assertIn("development memory per node exceeds limit", by_id["aid-zero-over"]["policy_reasons"])
        self.assertIn("training CPU per node exceeds resource ratio", by_id["cpu"]["policy_reasons"])

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

    def test_collector_rejects_a_truncated_bound_node_inventory(self):
        client = mock.Mock()
        client.list_queue_nodes.return_value = {"nodes": [{}], "total_size": 2}
        cluster = mock.Mock(client=client)
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            ClusterCollector(cluster, "queue", "cluster").collect()

    def test_pending_inventory_tracks_completeness_and_zero_gpu_jobs(self):
        cluster = mock.Mock()
        cluster._get_queue_id.return_value = "queue-id"
        cluster.client.list_training_jobs.return_value = {
            "total_size": 2,
            "training_jobs": [{
                "name": "cpu-only", "ownership": {"creator_name": "UserA"},
                "metadata": {"created_at": datetime.now(timezone.utc).isoformat()},
                "spec": {"vc_job": {"tasks": [{
                    "replicas": 1,
                    "resource_spec": {"accelerate_device_count": 0, "cpu_count": 14, "memory_gib": 240},
                }]}},
            }],
        }
        jobs, complete = _pending_workloads(cluster, "queue")
        self.assertFalse(complete)
        self.assertEqual(jobs[0]["gpus_per_node"], 0)
        self.assertEqual(jobs[0]["user"], "usera")


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
        ])
        matching = {
            "workload_id": "low", "finding_categories": ["utilization"],
            "finding_codes": ["utilization.low_gpu_activity"],
            "finding_tags": ["historical", "gpu"],
            "policy_findings": [{"status": "violation"}],
        }
        rows = self.module._filter_rows([matching, {
            "workload_id": "other", "finding_categories": ["quota"],
            "finding_codes": ["quota.gpu"], "finding_tags": ["quota"],
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
