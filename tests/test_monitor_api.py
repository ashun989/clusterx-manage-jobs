from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RESOURCE_POLICY = ROOT / "skill/clusterx-manage-jobs/assets/resource-policy.json"
GROUP_POLICY = ROOT / "config/groups.example.yaml"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dependency installation is tested in CI
    TestClient = None

from clusterx_monitor.auth import AdminAuth, initialize_auth_config
from clusterx_monitor.policy import PolicyManager, apply_policy, load_policy
from clusterx_monitor.service import MonitorRuntime, create_app


def raw_snapshot():
    return {
        "schema_version": 1, "snapshot_id": "api-snapshot",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster": "c", "queue": "q", "telemetry_window_minutes": 5,
        "nodes": [{
            "node": "n1", "state": "RUNNING", "allocated_gpu": 1, "total_gpu": 8,
            "allocated_cpu": 4, "total_cpu": 112,
            "allocated_memory_gib": 10, "total_memory_gib": 1920,
            "workloads": {}, "unattributed": {"gpu": 0, "cpu": 0, "memory_gib": 0},
            "attribution_excess": {"gpu": 0, "cpu": 0, "memory_gib": 0},
        }],
        "workloads": [{
            "workload_id": "w", "workload_name": "w", "user": "alice",
            "type": "trainingJob", "total_gpu": 1,
            "placements": [{"node": "n1", "pod": "p", "gpu": 1, "cpu": 4, "memory_gib": 10}],
            "gpus": [{"node": "n1", "pod": "p", "device_index": "0", "gpu_uuid": "u",
                      "gpu_compute_util_pct": 50, "gpu_memory_util_pct": 60, "gpu_power_w": 300}],
            "create_time": None, "start_time": None,
        }],
        "pending_workloads": [], "pending_complete": True, "warnings": [],
    }


class DummyCollector:
    def collect(self, *, telemetry_minutes=5, historical_window_hours=24, historical_refresh_minutes=5):
        return raw_snapshot()


class RetryCollector:
    def __init__(self):
        self.calls = 0

    def collect(self, *, telemetry_minutes=5, historical_window_hours=24, historical_refresh_minutes=5):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("queue node allocation changed during collection")
        return raw_snapshot()


@unittest.skipIf(TestClient is None, "FastAPI is not installed")
class MonitorApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        policy_path = Path(self.temp.name) / "policy.json"
        group_path = Path(self.temp.name) / "groups.yaml"
        policy_path.write_text(RESOURCE_POLICY.read_text(), encoding="utf-8")
        group_path.write_text(GROUP_POLICY.read_text(), encoding="utf-8")
        group_path.chmod(0o600)
        auth_path = Path(self.temp.name) / "admin.yaml"
        initialize_auth_config(auth_path, "admin", "a-strong-test-password")
        self.auth = AdminAuth(auth_path)
        self.policy = PolicyManager(policy_path, group_path)
        self.runtime = MonitorRuntime(DummyCollector(), self.policy)
        self.runtime.snapshots.publish(apply_policy(raw_snapshot(), self.policy.policy))
        self.app = create_app(self.runtime, static_dir=Path(self.temp.name) / "missing", auth=self.auth)

    def tearDown(self):
        self.runtime.executor.shutdown(wait=False, cancel_futures=True)
        self.temp.cleanup()

    def test_status_snapshot_policy_and_read_only_routes(self):
        client = TestClient(self.app)
        status_response = client.get("/api/v1/status")
        self.assertTrue(status_response.json()["snapshot"]["ready"])
        self.assertIn("default-src 'self'", status_response.headers["content-security-policy"])
        self.assertEqual(status_response.headers["x-content-type-options"], "nosniff")
        snapshot = client.get("/api/v1/snapshots/latest").json()
        self.assertEqual(snapshot["snapshot_id"], "api-snapshot")
        self.assertTrue(client.get("/api/v1/policy").json()["valid"])
        policy_response = client.get("/api/v1/policy")
        public_policy = policy_response.json()["policy"]
        self.assertEqual(public_policy["groups"]["example-team"]["member_count"], 1)
        self.assertNotIn("members", public_policy["groups"]["example-team"])
        self.assertNotIn("alice", policy_response.text)
        self.assertIn("utilization.low_gpu_activity", {item["code"] for item in policy_response.json()["rule_catalog"]})
        self.assertIn("propagation", policy_response.json()["status_definitions"]["violation"])
        paths = self.app.openapi()["paths"]
        self.assertNotIn("delete", {method for spec in paths.values() for method in spec})
        self.assertEqual(
            {path for path, spec in paths.items() if "put" in spec},
            {"/api/v1/admin/config/resource", "/api/v1/admin/config/groups"},
        )

    def test_explicit_nat_host_is_accepted_without_wildcarding_other_hosts(self):
        app = create_app(
            self.runtime, static_dir=Path(self.temp.name) / "missing-nat",
            auth=self.auth, allowed_hosts=["127.0.0.1", "testserver", "10.140.80.10"],
        )
        client = TestClient(app)
        self.assertEqual(
            client.get("/api/v1/status", headers={"Host": "10.140.80.10:49430"}).status_code,
            200,
        )
        self.assertEqual(
            client.get("/api/v1/status", headers={"Host": "attacker.example"}).status_code,
            400,
        )

    def test_plan_is_pinned_to_snapshot_and_cached(self):
        client = TestClient(self.app)
        body = {
            "snapshot_id": "api-snapshot",
            "target": {"nodes": 1, "gpus_per_node": 8},
            "strategies": ["min-gpu"], "candidate_scope": "fragmented",
            "alternatives": 1, "search_seconds": 1, "filters": {},
        }
        first = client.post("/api/v1/plans", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["snapshot_id"], "api-snapshot")
        second = client.post("/api/v1/plans", json=body).json()
        self.assertTrue(second["cache_hit"])
        missing = dict(body, snapshot_id="missing")
        self.assertEqual(client.post("/api/v1/plans", json=missing).status_code, 404)

    def test_invalid_hot_reload_is_visible_in_snapshot(self):
        self.policy.group_path.write_text("groups: [broken]", encoding="utf-8")
        self.assertFalse(self.policy.reload(force=True))
        snapshot = self.runtime._apply_policy(raw_snapshot())
        self.assertFalse(snapshot["policy_config"]["valid"])
        self.assertTrue(snapshot["policy_config"]["using_last_known_good"])
        self.assertTrue(any(alert["kind"] == "policy-config" for alert in snapshot["alerts"]))

    def test_refresh_retries_one_inconsistent_resource_signature(self):
        collector = RetryCollector()
        runtime = MonitorRuntime(collector, self.policy)
        try:
            asyncio.run(runtime.refresh())
            latest = runtime.snapshots.latest()
            self.assertEqual(collector.calls, 2)
            self.assertIn("one retry was used", latest["warnings"][0])
        finally:
            runtime.executor.shutdown(wait=False, cancel_futures=True)

    def test_refresh_discards_a_snapshot_collected_across_policy_revisions(self):
        class ChangingCollector:
            def __init__(inner_self):
                inner_self.calls = 0

            def collect(inner_self, **_):
                inner_self.calls += 1
                if inner_self.calls == 1:
                    payload = json.loads(self.policy.path.read_text(encoding="utf-8"))
                    payload["refresh_seconds"] = 31
                    self.policy.path.write_text(json.dumps(payload), encoding="utf-8")
                    self.policy.reload(force=True)
                return raw_snapshot()

        collector = ChangingCollector()
        runtime = MonitorRuntime(collector, self.policy)
        try:
            asyncio.run(runtime.refresh())
            self.assertIsNone(runtime.snapshots.latest())
            self.assertTrue(runtime.config_changed.is_set())
            asyncio.run(runtime.refresh())
            self.assertIsNotNone(runtime.snapshots.latest())
            self.assertEqual(collector.calls, 2)
        finally:
            runtime.executor.shutdown(wait=False, cancel_futures=True)

    def test_built_frontend_routes_can_be_registered(self):
        static = Path(self.temp.name) / "static"
        static.mkdir()
        (static / "index.html").write_text("<html>monitor</html>", encoding="utf-8")
        app = create_app(self.runtime, static_dir=static)
        client = TestClient(app)
        self.assertIn("monitor", client.get("/").text)
        self.assertIn("monitor", client.get("/dashboard").text)
        self.assertEqual(client.get("/api/v1/missing").status_code, 404)

    def test_spa_and_assets_cannot_escape_the_static_root(self):
        root = Path(self.temp.name) / "web-root"
        static = root / "static"
        assets = static / "assets"
        assets.mkdir(parents=True)
        (static / "index.html").write_text("<html>monitor-shell</html>", encoding="utf-8")
        (assets / "app.js").write_text("safe asset", encoding="utf-8")
        secret = root / "private-secret.txt"
        secret.write_text("DO-NOT-EXPOSE", encoding="utf-8")
        (assets / "escape.txt").symlink_to(secret)
        app = create_app(self.runtime, static_dir=static)
        client = TestClient(app)
        self.assertEqual(client.get("/assets/app.js").text, "safe asset")
        for path in (
            "/%2e%2e/private-secret.txt",
            "/%252e%252e/private-secret.txt",
            "/assets/%2e%2e/private-secret.txt",
            "/%2f%2fetc/passwd",
            "/assets/escape.txt",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 404, (path, response.status_code, response.text[:100]))
            self.assertNotIn("DO-NOT-EXPOSE", response.text)
        fallback = client.get("/config/groups.local.yaml")
        self.assertEqual(fallback.status_code, 200)
        self.assertIn("monitor-shell", fallback.text)
        self.assertNotIn("DO-NOT-EXPOSE", fallback.text)

        symlink_static = root / "symlink-static"
        symlink_static.mkdir()
        (symlink_static / "index.html").symlink_to(secret)
        (symlink_static / "assets").symlink_to(root)
        symlink_client = TestClient(create_app(self.runtime, static_dir=symlink_static))
        self.assertEqual(symlink_client.get("/").status_code, 404)
        self.assertEqual(symlink_client.get("/assets/private-secret.txt").status_code, 404)
        self.assertNotIn("DO-NOT-EXPOSE", symlink_client.get("/assets/private-secret.txt").text)

    def test_request_body_limits_apply_to_declared_and_streamed_payloads(self):
        client = TestClient(self.app)
        too_large_plan = b'{"padding":"' + b"x" * 262_144 + b'"}'
        declared = client.post(
            "/api/v1/plans", content=too_large_plan,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(declared.status_code, 413)

        def chunks():
            for _ in range(5):
                yield b"x" * 60_000

        streamed = client.post(
            "/api/v1/plans", content=chunks(),
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
        self.assertEqual(streamed.status_code, 413)
        oversized_admin = client.post(
            "/api/v1/admin/login",
            content=b'{"username":"admin","password":"' + b"x" * 1_048_576 + b'"}',
            headers={"Content-Type": "application/json", "Origin": "http://testserver"},
        )
        self.assertEqual(oversized_admin.status_code, 413)

    def test_plan_coordinator_reuses_identical_work_and_rejects_other_work(self):
        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def compute():
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return {"plans": []}

            first = asyncio.create_task(self.runtime.coordinate_plan("same", compute))
            await started.wait()
            second = asyncio.create_task(self.runtime.coordinate_plan("same", compute))
            with self.assertRaisesRegex(RuntimeError, "already being computed"):
                await self.runtime.coordinate_plan("different", compute)
            release.set()
            first_result, second_result = await asyncio.gather(first, second)
            self.assertEqual(calls, 1)
            self.assertEqual(first_result, second_result)
            self.assertIsNot(first_result, second_result)

        asyncio.run(scenario())

    def test_admin_auth_csrf_private_read_and_atomic_resource_update(self):
        client = TestClient(self.app)
        unauthenticated = client.get("/api/v1/admin/config")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.headers["cache-control"], "no-store")
        self.assertEqual(client.get("/api/v1/status", headers={"Host": "attacker.example"}).status_code, 400)
        no_origin = client.post("/api/v1/admin/login", json={"username": "admin", "password": "a-strong-test-password"})
        self.assertEqual(no_origin.status_code, 403)
        invalid = client.post(
            "/api/v1/admin/login", json={"username": "admin", "password": "wrong"},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(invalid.status_code, 401)
        login = client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "a-strong-test-password"},
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertIn("SameSite=strict", login.headers["set-cookie"])
        csrf = login.json()["csrf_token"]
        private = client.get("/api/v1/admin/config").json()
        self.assertIn("alice", str(private["groups"]))
        resource = json.loads(private["resource"]["text"])
        resource["refresh_seconds"] = 31
        resource_text = json.dumps(resource)
        missing_csrf = client.put(
            "/api/v1/admin/config/resource",
            json={"revision": private["resource"]["revision"], "text": resource_text},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(missing_csrf.status_code, 403)
        updated = client.put(
            "/api/v1/admin/config/resource",
            json={"revision": private["resource"]["revision"], "text": resource_text},
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "X-CSRF-Token": csrf},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(json.loads(updated.json()["resource"]["text"])["refresh_seconds"], 31)
        self.assertEqual(self.policy.path.stat().st_mode & 0o777, 0o600)
        stale = client.put(
            "/api/v1/admin/config/resource",
            json={"revision": private["resource"]["revision"], "text": resource_text},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        )
        self.assertEqual(stale.status_code, 409)
        audit = self.policy.audit_path.read_text(encoding="utf-8")
        self.assertIn('"actor": "admin"', audit)
        self.assertNotIn("alice", audit)

    def test_authenticated_admin_can_repair_malformed_resource_text(self):
        client = TestClient(self.app)
        login = client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "a-strong-test-password"},
            headers={"Origin": "http://testserver"},
        ).json()
        headers = {"Origin": "http://testserver", "X-CSRF-Token": login["csrf_token"]}
        original = self.policy.path.read_text(encoding="utf-8")
        self.policy.path.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.policy.reload(force=True))
        damaged = client.get("/api/v1/admin/config").json()
        self.assertEqual(damaged["resource"]["text"], "{broken")
        self.assertIsNotNone(damaged["resource"]["parse_error"])
        repaired = client.put(
            "/api/v1/admin/config/resource",
            json={"revision": damaged["resource"]["revision"], "text": original},
            headers=headers,
        )
        self.assertEqual(repaired.status_code, 200, repaired.text)
        self.assertTrue(repaired.json()["effective_config_valid"])

    def test_setup_required_can_be_completed_without_restart(self):
        directory = Path(self.temp.name) / "setup"
        missing_resource = directory / "resource.json"
        missing_groups = directory / "groups.yaml"
        policy = PolicyManager(missing_resource, missing_groups, allow_unconfigured=True)
        runtime = MonitorRuntime(DummyCollector(), policy)
        app = create_app(runtime, static_dir=directory / "static", auth=self.auth)
        client = TestClient(app)
        try:
            self.assertTrue(client.get("/api/v1/status").json()["setup_required"])
            login = client.post(
                "/api/v1/admin/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            ).json()
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login["csrf_token"]}
            initial = client.get("/api/v1/admin/config").json()
            resource = client.put(
                "/api/v1/admin/config/resource",
                json={"revision": initial["resource"]["revision"], "text": initial["resource"]["text"]},
                headers=headers,
            ).json()
            self.assertFalse(resource["configured"])
            completed = client.put(
                "/api/v1/admin/config/groups",
                json={"revision": resource["groups"]["revision"], "text": resource["groups"]["text"]},
                headers=headers,
            )
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertTrue(completed.json()["configured"])
            self.assertTrue(policy.configured)
            self.assertEqual(missing_resource.stat().st_mode & 0o777, 0o600)
            self.assertEqual(missing_groups.stat().st_mode & 0o777, 0o600)
        finally:
            runtime.executor.shutdown(wait=False, cancel_futures=True)

    def test_auth_can_be_initialized_after_service_start_without_anonymous_access(self):
        path = Path(self.temp.name) / "late-admin.yaml"
        auth = AdminAuth(path, allow_missing=True)
        app = create_app(self.runtime, static_dir=Path(self.temp.name) / "missing-late", auth=auth)
        client = TestClient(app)
        self.assertFalse(client.get("/api/v1/status").json()["admin_configured"])
        unavailable = client.post(
            "/api/v1/admin/login",
            json={"username": "late", "password": "a-strong-test-password"},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(unavailable.status_code, 503)
        initialize_auth_config(path, "late", "a-strong-test-password")
        login = client.post(
            "/api/v1/admin/login",
            json={"username": "late", "password": "a-strong-test-password"},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(auth.configured)


if __name__ == "__main__":
    unittest.main()
