import argparse
import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/clusterx-manage-jobs/scripts/queue_plan.py"


def load_module():
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("clusterx_queue_plan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def node(name, allocated, jobs, *, cpu=0, memory=0):
    return {"node": name, "state": "RUNNING", "allocated_gpu": allocated,
            "total_gpu": 8, "allocated_cpu": cpu, "total_cpu": 112,
            "allocated_memory_gib": memory, "total_memory_gib": 1920,
            "jobs": jobs}


def job(job_id, user, gpu):
    return {"job_id": job_id, "job_name": job_id, "user": user,
            "total_gpu": gpu, "placements": []}


def raw_node(name, gpu=0, cpu=0, memory=0):
    return {
        "id": f"/clusters/c/nodes/{name}", "name": name,
        "host_ip": "10.0.0.1", "state": "RUNNING",
        "summary_data": [
            {"resource_type": "DEVICE", "allocated": gpu, "total": 8, "unit": "device"},
            {"resource_type": "CPU", "allocated": cpu, "total": 112, "unit": "core"},
            {"resource_type": "MEMORY", "allocated": memory, "total": 1920, "unit": "GiB"},
        ],
    }


def gpu_metric(workload_id, hostname, pod, gpu_uuid, device, metric, value):
    return {
        "metric": {
            "label_resource_compute_sensecore_cn_workload_uid": workload_id,
            "Hostname": hostname,
            "exported_pod": pod,
            "UUID": gpu_uuid,
            "gpu": str(device),
            "queue_plan_metric": metric,
        },
        "value": [0, str(value)],
    }


def fake_prometheus_stats(cache_path, *query_tokens):
    module = types.ModuleType("clusterx.launcher.ssp.prometheus_stats")
    module.build_cache_path = mock.Mock(return_value=cache_path)
    module.get_query_token = mock.Mock(
        side_effect=[{"query_token": token} for token in query_tokens]
    )
    return module


class StepClock:
    def __init__(self, step=0.01):
        self.value = 0.0
        self.step = step

    def __call__(self):
        value = self.value
        self.value += self.step
        return value


class QueuePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_two_by_eight_returns_three_strategies(self):
        m = self.module
        jobs = {"a": job("a", "u1", 1), "b": job("b", "u2", 1),
                "c": job("c", "u2", 1), "d": job("d", "u3", 4),
                **{x: job(x, "u1", 1) for x in "efgh"}}
        nodes = {
            "n1": node("n1", 1, {"a": {"gpu": 1}}),
            "n2": node("n2", 2, {"b": {"gpu": 1}, "c": {"gpu": 1}}),
            "n3": node("n3", 4, {"d": {"gpu": 4}}),
            "n4": node("n4", 4, {x: {"gpu": 1} for x in "efgh"}),
        }
        plans, optimality = m.solve_candidates(nodes, jobs, m.Target(2, 8))
        self.assertEqual(optimality, "exact")
        by_strategy = {plan["strategy"]: plan for plan in plans if plan["rank"] == 1}
        self.assertEqual(by_strategy["min-gpu"]["gpus"], 3)
        self.assertEqual(by_strategy["min-workloads"]["workload_count"], 2)
        self.assertEqual(by_strategy["min-users"]["users"], 1)

    def test_pooled_http_session_installs_and_restores_requests(self):
        import requests

        m = self.module
        original_request = requests.request
        original_get = requests.get
        session = mock.Mock()
        session.request = mock.Mock()
        session.get = mock.Mock()
        with mock.patch("requests.Session", return_value=session), mock.patch(
            "requests.adapters.HTTPAdapter"
        ) as adapter:
            with m._pooled_http_session(pool_size=7):
                self.assertIs(requests.request, session.request)
                self.assertIs(requests.get, session.get)
            adapter.assert_called_once_with(
                pool_connections=7, pool_maxsize=7, max_retries=0,
                pool_block=True,
            )
        self.assertIs(requests.request, original_request)
        self.assertIs(requests.get, original_get)
        self.assertEqual(session.mount.call_count, 2)
        session.close.assert_called_once_with()

    def test_existing_free_nodes_need_no_pause(self):
        m = self.module
        nodes = {"n1": node("n1", 0, {}), "n2": node("n2", 0, {}),
                 "n3": node("n3", 4, {"a": {"gpu": 4}})}
        plans, optimality = m.solve_candidates(
            nodes, {"a": job("a", "u", 4)}, m.Target(2, 8))
        self.assertEqual((plans, optimality), ([], "not-needed"))

    def test_cpu_only_task_is_included_when_cpu_blocks_target(self):
        m = self.module
        jobs = {"cpu": job("cpu", "u", 0), "gpu": job("gpu", "u", 1)}
        nodes = {"n1": node("n1", 1,
                 {"cpu": {"gpu": 0, "cpu": 80},
                  "gpu": {"gpu": 1, "cpu": 16}}, cpu=96)}
        plans, _ = m.solve_candidates(nodes, jobs, m.Target(1, 8, cpus=64))
        self.assertEqual(set(plans[0]["jobs"]), {"cpu", "gpu"})

    def test_cpu_only_node_can_be_repacked_for_cpu_target(self):
        m = self.module
        jobs = {"cpu": job("cpu", "u", 0)}
        jobs["cpu"]["placements"] = [{"node": "n1", "gpu": 0, "cpu": 80}]
        nodes = {"n1": node("n1", 0, {"cpu": {"gpu": 0, "cpu": 80}}, cpu=80)}
        plans, _ = m.solve_candidates(nodes, jobs, m.Target(1, 8, cpus=64))
        self.assertTrue(plans)
        self.assertEqual(plans[0]["jobs"], ["cpu"])
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(1, 8, cpus=64), "q", "c"
        )
        self.assertEqual(report["fragmented_nodes"][0]["node"], "n1")

    def test_multinode_job_cost_counts_whole_job(self):
        m = self.module
        jobs = {"multi": job("multi", "u", 4)}
        nodes = {"n1": node("n1", 2, {"multi": {"gpu": 2}}),
                 "n2": node("n2", 2, {"multi": {"gpu": 2}})}
        plans, _ = m.solve_candidates(nodes, jobs, m.Target(2, 8))
        self.assertEqual((plans[0]["gpus"], plans[0]["job_count"]), (4, 1))

    def test_small_search_limit_marks_heuristic(self):
        m = self.module
        jobs = {str(i): job(str(i), "u", 1) for i in range(20)}
        nodes = {f"n{i}": node(f"n{i}", 1, {str(i): {"gpu": 1}})
                 for i in range(20)}
        plans, optimality = m.solve_candidates(
            nodes, jobs, m.Target(3, 8), search_seconds=0.05,
            clock=StepClock(0.00002))
        self.assertEqual(optimality, "heuristic")
        self.assertTrue(plans)

    def test_heuristic_prunes_small_jobs_when_large_job_alone_is_feasible(self):
        m = self.module
        jobs = {
            "small-a": job("small-a", "u", 1),
            "small-b": job("small-b", "u", 1),
            "large": job("large", "u", 192),
        }
        jobs["small-a"]["placements"] = [{"node": "f1", "gpu": 1}]
        jobs["small-b"]["placements"] = [{"node": "f2", "gpu": 1}]
        jobs["large"]["placements"] = [
            {"node": f"n{i}", "gpu": 8} for i in range(24)
        ]
        nodes = {
            "f1": node("f1", 1, {"small-a": {"gpu": 1}}),
            "f2": node("f2", 1, {"small-b": {"gpu": 1}}),
            **{
                f"n{i}": node(f"n{i}", 8, {"large": {"gpu": 8}})
                for i in range(24)
            },
        }
        plans, optimality = m.solve_candidates(
            nodes, jobs, m.Target(4, 8), candidate_scope="all",
            search_seconds=0.2, clock=StepClock()
        )
        self.assertEqual(optimality, "heuristic")
        by_strategy = {plan["strategy"]: plan for plan in plans if plan["rank"] == 1}
        self.assertEqual(by_strategy["min-gpu"]["jobs"], ["large"])
        self.assertEqual(by_strategy["min-gpu"]["gpus"], 192)
        self.assertEqual(by_strategy["min-gpu"]["job_count"], 1)

    def test_heuristic_plans_are_single_job_irreducible(self):
        m = self.module
        jobs = {name: job(name, "u", 1) for name in "abcde"}
        nodes = {
            f"n{i}": node(f"n{i}", 1, {name: {"gpu": 1}})
            for i, name in enumerate(jobs)
        }
        plans, _ = m.solve_candidates(
            nodes, jobs, m.Target(3, 8), search_seconds=0.2,
            clock=StepClock()
        )
        for plan in plans:
            for job_id in plan["jobs"]:
                reduced = m._candidate_from_jobs(
                    set(plan["jobs"]) - {job_id}, nodes, jobs, m.Target(3, 8)
                )
                self.assertLess(len(reduced["freed_nodes"]), 3)

    def test_script_contains_no_stop_operation(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("stop_training_job", source)
        self.assertNotIn("clusterx stop", source)

    def test_resource_parser_supports_cpu_and_memory_units(self):
        m = self.module
        self.assertEqual(m._resource_number("500m"), 0.5)
        self.assertEqual(m._resource_number("240.0GiB", memory=True), 240)
        self.assertEqual(m._resource_number("1024MiB", memory=True), 1)

    def test_development_workloads_are_collected_and_releasable(self):
        m = self.module
        raw = raw_node("fragmented-node", gpu=2, cpu=56, memory=960)
        pods = []
        for index, gpu in enumerate((0, 1, 0, 1)):
            resource = {"cpu": "14.0", "memory": "240.0GiB"}
            if gpu:
                resource["accelerate_device_count"] = gpu
            pods.append({
                "name": f"dev-{index}-0", "resource": resource,
                "workload": {"uid": f"aid-{index}", "name": f"dev-{index}", "type": "aid"},
                "workspace": {"name": "workspace"},
                "ownership": {"creator_name": f"user-{index}"},
            })

        class Client:
            subscription = "s"
            resource_group = "r"
            region = "z"

            def list_queue_nodes(self, **kwargs):
                return {"nodes": [raw], "total_size": 1}

            def _make_management_request(self, *args, **kwargs):
                return {"pods": pods, "total_size": len(pods)}

        class Cluster:
            client = Client()

            def stats_prometheus(self, **kwargs):
                raise RuntimeError("not available")

        snapshot, warnings = m.collect_snapshot(Cluster(), "queue", "cluster")
        self.assertEqual(len(snapshot["jobs"]), 4)
        self.assertEqual(sum(w["total_gpu"] for w in snapshot["jobs"].values()), 2)
        report = m.build_report(snapshot, m.Target(1, 8), "queue", "cluster")
        fragment = report["fragmented_nodes"][0]
        self.assertEqual(fragment["node"], "fragmented-node")
        self.assertEqual(len(fragment["workloads"]), 4)
        self.assertEqual({w["type"] for w in fragment["workloads"]}, {"aid"})
        self.assertTrue(report["suggestions"])
        self.assertEqual(report["summary"]["workload_counts"], {"aid": 4})
        self.assertTrue(warnings)

    def test_multinode_workload_uses_earliest_pod_create_time(self):
        m = self.module
        raw_nodes = [raw_node("node-a", gpu=1), raw_node("node-b", gpu=1)]
        pods_by_node = {
            raw_nodes[0]["id"]: [{
                "name": "worker-a", "create_time": "2026-08-12T10:05:00Z",
                "resource": {"accelerate_device_count": 1},
                "workload": {"uid": "shared", "name": "shared", "type": "trainingJob"},
                "ownership": {"creator_name": "user"},
            }],
            raw_nodes[1]["id"]: [{
                "name": "worker-b", "create_time": "2026-08-12T10:00:00Z",
                "resource": {"accelerate_device_count": 1},
                "workload": {"uid": "shared", "name": "shared", "type": "trainingJob"},
                "ownership": {"creator_name": "user"},
            }],
        }

        class Client:
            subscription = "s"
            resource_group = "r"
            region = "z"

            def list_queue_nodes(self, **kwargs):
                return {"nodes": raw_nodes, "total_size": len(raw_nodes)}

            def _make_management_request(self, method, path, *, params):
                pods = pods_by_node[params["node_id"]]
                return {"pods": pods, "total_size": len(pods)}

        class Cluster:
            client = Client()

            def stats_prometheus(self, **kwargs):
                return []

        snapshot, _ = m.collect_snapshot(Cluster(), "queue", "cluster")
        self.assertEqual(snapshot["jobs"]["shared"]["create_time"], "2026-08-12T10:00:00Z")

    def test_consistency_snapshot_overlaps_prometheus_collection(self):
        m = self.module
        raw_nodes = [raw_node("n1", gpu=1)]
        second_snapshot_started = threading.Event()
        release_second_snapshot = threading.Event()
        overlap_observed = []

        class Client:
            subscription = "s"
            resource_group = "r"
            region = "z"
            calls = 0

            def list_queue_nodes(self, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    second_snapshot_started.set()
                    release_second_snapshot.wait(timeout=2)
                return {"nodes": raw_nodes, "total_size": 1}

            def _make_management_request(self, method, path, *, params):
                return {
                    "pods": [{
                        "name": "pod-0",
                        "resource": {"accelerate_device_count": 1},
                        "workload": {
                            "uid": "job", "name": "job",
                            "type": "trainingJob",
                        },
                        "ownership": {"creator_name": "user"},
                    }],
                    "total_size": 1,
                }

        class Cluster:
            client = Client()

            def stats_prometheus(self, **kwargs):
                overlap_observed.append(second_snapshot_started.is_set())
                release_second_snapshot.set()
                return []

        def query_utilization(*args, **kwargs):
            overlap_observed.append(second_snapshot_started.wait(timeout=1))
            return []

        with mock.patch.object(m, "_query_gpu_utilization", query_utilization):
            snapshot, _ = m.collect_snapshot(Cluster(), "queue", "cluster")
        self.assertEqual(snapshot["jobs"]["job"]["total_gpu"], 1)
        self.assertEqual(overlap_observed, [True, True])

    def test_gpu_utilization_query_combines_compute_and_memory_once(self):
        m = self.module

        class Client:
            def __init__(self):
                self.queries = []

            def query_prometheus(self, token, query):
                self.queries.append((token, query))
                return {"status": "success", "data": {"result": [{"metric": {}}]}}

        class Cluster:
            cfg = {
                "workspace": 'workspace"escaped', "subscription": "subscription",
                "resource_group": "group", "region": "region",
            }
            client = Client()

        prometheus_stats = fake_prometheus_stats(Path("/tmp/token.json"), "token")
        with mock.patch.dict(sys.modules, {
            "clusterx.launcher.ssp.prometheus_stats": prometheus_stats,
        }):
            rows = m._query_gpu_utilization(
                Cluster(), "queue", "cluster", 17
            )
        self.assertEqual(rows, [{"metric": {}}])
        self.assertEqual(len(Cluster.client.queries), 1)
        token, query = Cluster.client.queries[0]
        self.assertEqual(token, "token")
        self.assertIn("gpu_util", query)
        self.assertIn("gpu_memory_used", query)
        self.assertIn("gpu_memory_total", query)
        self.assertEqual(query.count("[17m]"), 3)
        self.assertIn('workspace\\"escaped', query)
        self.assertIn(m.GPU_COMPUTE_UTIL, query)
        self.assertIn(m.GPU_MEMORY_UTIL, query)

    def test_gpu_utilization_query_refreshes_rejected_token_once(self):
        m = self.module

        class AuthError(Exception):
            response = type("Response", (), {"status_code": 403})()

        class Client:
            def __init__(self):
                self.tokens = []

            def query_prometheus(self, token, query):
                self.tokens.append(token)
                if len(self.tokens) == 1:
                    raise AuthError("expired")
                return {"status": "success", "data": {"result": []}}

        class Cluster:
            cfg = {
                "workspace": "workspace", "subscription": "subscription",
                "resource_group": "group", "region": "region",
            }
            client = Client()

        prometheus_stats = fake_prometheus_stats(
            Path("/tmp/nonexistent-queue-plan-token.json"), "old", "new"
        )
        with mock.patch.dict(sys.modules, {
            "clusterx.launcher.ssp.prometheus_stats": prometheus_stats,
        }):
            self.assertEqual(
                m._query_gpu_utilization(Cluster(), "queue", "cluster", 5), []
            )
        self.assertEqual(Cluster.client.tokens, ["old", "new"])
        self.assertEqual(prometheus_stats.get_query_token.call_count, 2)

    def test_gpu_utilization_maps_aid_and_training_jobs_per_card(self):
        m = self.module
        jobs = {
            "train": job("train", "trainer", 2),
            "aid": job("aid", "developer", 1),
        }
        jobs["train"].update({
            "type": "trainingJob",
            "placements": [{"node": "n1", "pod": "train-0", "gpu": 2}],
        })
        jobs["aid"].update({
            "type": "aid",
            "placements": [{"node": "n2", "pod": "aid-0", "gpu": 1}],
        })
        series = []
        for workload_id, hostname, pod, uuid, device, compute, memory in (
            ("train", "host-a", "train-0", "gpu-a", 0, 10, 40),
            ("train", "host-a", "train-0", "gpu-b", 1, 30, 60),
            ("aid", "host-b", "aid-0", "gpu-c", 3, 80, 25),
        ):
            series.extend([
                gpu_metric(workload_id, hostname, pod, uuid, device,
                           m.GPU_COMPUTE_UTIL, compute),
                gpu_metric(workload_id, hostname, pod, uuid, device,
                           m.GPU_MEMORY_UTIL, memory),
            ])
        warnings = m._attach_gpu_utilization(
            jobs, series, {"host-a": "n1", "host-b": "n2"}
        )
        self.assertEqual(warnings, [])
        self.assertEqual(jobs["train"]["gpu_utilization"], {
            "allocated_gpu_count": 2,
            "reported_gpu_count": 2,
            "gpu_compute_util_avg_pct": 20.0,
            "gpu_compute_util_min_pct": 10.0,
            "gpu_compute_util_max_pct": 30.0,
            "gpu_memory_util_avg_pct": 50.0,
            "gpu_memory_util_min_pct": 40.0,
            "gpu_memory_util_max_pct": 60.0,
        })
        self.assertEqual(jobs["aid"]["gpu_utilization"]["reported_gpu_count"], 1)
        self.assertEqual(jobs["aid"]["gpus"][0]["device_index"], "3")

        nodes = {
            "n1": node("n1", 2, {"train": {"gpu": 2}}),
            "n2": node("n2", 1, {"aid": {"gpu": 1}}),
        }
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs, "gpu_utilization_window_minutes": 17},
            m.Target(1, 8), "queue", "cluster",
        )
        self.assertEqual(report["gpu_utilization"]["window_minutes"], 17)
        self.assertEqual(report["gpu_utilization"]["allocated_gpu_count"], 3)
        self.assertEqual(report["gpu_utilization"]["reported_gpu_count"], 3)
        self.assertEqual(len(report["gpu_utilization"]["workloads"]), 2)
        fragment = next(
            item for item in report["fragmented_nodes"] if item["node"] == "n1"
        )
        self.assertEqual(
            fragment["workloads"][0]["gpu_utilization"]["gpu_compute_util_avg_pct"],
            20.0,
        )
        plain = m.render_text(report)
        self.assertIn("GPU telemetry: 3/3", plain)
        self.assertIn("GPU util 20.0% [10.0–30.0]", plain)
        self.assertNotIn("Per-GPU utilization (17m):", plain)
        detailed = m.render_text(report, show_gpu_details=True)
        self.assertIn("Per-GPU utilization (17m):", detailed)
        self.assertIn("n1 GPU 0", detailed)

        from rich.console import Console
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False, width=180)
        self.assertTrue(m.render_rich(
            report, console=console, show_gpu_details=True
        ))
        output = stream.getvalue()
        self.assertIn("Per-GPU utilization · 17m", output)
        self.assertIn("Node util", output)
        self.assertIn("GPU mem", output)

    def test_user_summaries_aggregate_workloads_resources_and_utilization(self):
        m = self.module
        jobs = {
            "bob-dev": job("bob-dev", "bob", 3),
            "bob-train": job("bob-train", "bob", 1),
            "alice-job": job("alice-job", "alice", 2),
            "unknown-task": job("unknown-task", "", 0),
        }
        jobs["bob-dev"].update({
            "type": "aid",
            "placements": [
                {"node": "n1", "gpu": 2, "cpu": 8, "memory_gib": 32},
                {"node": "n2", "gpu": 1, "cpu": 4, "memory_gib": 16},
            ],
            "gpus": [
                {"gpu_compute_util_pct": 10, "gpu_memory_util_pct": 40},
                {"gpu_compute_util_pct": 30, "gpu_memory_util_pct": 60},
            ],
        })
        jobs["bob-train"].update({
            "type": "trainingJob",
            "placements": [
                {"node": "n1", "gpu": 1, "cpu": 2, "memory_gib": 8},
            ],
            "gpus": [
                {"gpu_compute_util_pct": 50, "gpu_memory_util_pct": 80},
            ],
        })
        jobs["alice-job"].update({
            "type": "inference",
            "placements": [
                {"node": "n2", "gpu": 2, "cpu": 100, "memory_gib": 200},
            ],
        })
        jobs["unknown-task"].update({
            "placements": [
                {"node": "n3", "gpu": 0, "cpu": 120, "memory_gib": 10},
            ],
        })
        nodes = {
            "n1": node(
                "n1", 3, {"bob-dev": {"gpu": 2}, "bob-train": {"gpu": 1}},
                cpu=10, memory=40,
            ),
            "n2": node(
                "n2", 3, {"bob-dev": {"gpu": 1}, "alice-job": {"gpu": 2}},
                cpu=104, memory=216,
            ),
            "n3": node(
                "n3", 0, {"unknown-task": {"gpu": 0, "cpu": 120}},
                cpu=120, memory=10,
            ),
        }
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(1, 8), "queue", "cluster"
        )

        self.assertEqual(
            [item["user"] for item in report["user_summaries"]],
            ["bob", "alice", "unknown"],
        )
        bob = report["user_summaries"][0]
        self.assertEqual(bob["workload_count"], 2)
        self.assertEqual(
            bob["workload_counts"], {"aid": 1, "trainingJob": 1}
        )
        self.assertEqual(
            (bob["allocated_gpu"], bob["allocated_cpu"],
             bob["allocated_memory_gib"]),
            (4, 14, 56),
        )
        self.assertEqual(bob["gpu_utilization"], {
            "allocated_gpu_count": 4,
            "reported_gpu_count": 3,
            "gpu_compute_util_avg_pct": 30.0,
            "gpu_compute_util_min_pct": 10.0,
            "gpu_compute_util_max_pct": 50.0,
            "gpu_memory_util_avg_pct": 60.0,
            "gpu_memory_util_min_pct": 40.0,
            "gpu_memory_util_max_pct": 80.0,
        })
        unknown = report["user_summaries"][2]
        self.assertEqual(unknown["workload_counts"], {"unknown": 1})
        self.assertEqual(unknown["allocated_cpu"], 120)

        plain = m.render_text(report)
        self.assertIn("Attributed resources by user:", plain)
        self.assertIn("bob: 2 · aid 1, trainingJob 1", plain)
        self.assertIn("4 GPU, 14 CPU, 56 GiB memory", plain)
        self.assertIn("GPU util 30.0% [10.0–50.0] (3/4)", plain)

        from rich.console import Console
        for width in (80, 120, 160):
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, width=width)
            self.assertTrue(m.render_rich(report, console=console))
            output = stream.getvalue()
            self.assertIn("Attributed resources by user · 5m", output)
            for user in ("bob", "alice", "unknown"):
                self.assertIn(user, output)
            overview_output = output.split("Search diagnostics", 1)[0]
            self.assertIn("trainingJob", overview_output)
            self.assertNotIn("…", overview_output)

    def test_gpu_utilization_ignores_stale_and_reports_partial_coverage(self):
        m = self.module
        jobs = {"current": job("current", "user", 2)}
        jobs["current"]["placements"] = [
            {"node": "n1", "pod": "current-0", "gpu": 2}
        ]
        series = [
            gpu_metric("current", "host-a", "current-0", "gpu-a", 0,
                       m.GPU_COMPUTE_UTIL, 15),
            gpu_metric("current", "host-a", "current-0", "gpu-a", 0,
                       m.GPU_MEMORY_UTIL, 35),
            gpu_metric("old", "host-a", "old-0", "gpu-b", 1,
                       m.GPU_COMPUTE_UTIL, 99),
            gpu_metric("old", "host-a", "old-0", "gpu-b", 1,
                       m.GPU_MEMORY_UTIL, 99),
            gpu_metric("current", "host-a", "current-0", "gpu-c", 2,
                       m.GPU_COMPUTE_UTIL, "NaN"),
        ]
        warnings = m._attach_gpu_utilization(jobs, series, {"host-a": "n1"})
        self.assertEqual(len(jobs["current"]["gpus"]), 1)
        self.assertEqual(jobs["current"]["gpu_utilization"]["reported_gpu_count"], 1)
        self.assertTrue(any("1/2" in warning for warning in warnings))

    def test_gpu_utilization_discards_ambiguous_pod_series(self):
        m = self.module
        jobs = {"current": job("current", "user", 1)}
        jobs["current"]["placements"] = [
            {"node": "n1", "pod": "current-0", "gpu": 1}
        ]
        series = []
        for uuid, device in (("gpu-a", 0), ("gpu-b", 1)):
            series.extend([
                gpu_metric("current", "host-a", "current-0", uuid, device,
                           m.GPU_COMPUTE_UTIL, 10),
                gpu_metric("current", "host-a", "current-0", uuid, device,
                           m.GPU_MEMORY_UTIL, 20),
            ])
        warnings = m._attach_gpu_utilization(jobs, series, {"host-a": "n1"})
        self.assertEqual(jobs["current"]["gpus"], [])
        self.assertTrue(any("ambiguous" in warning for warning in warnings))
        self.assertTrue(any("0/1" in warning for warning in warnings))

    def test_gpu_sort_key_orders_numeric_device_indexes(self):
        m = self.module
        rows = [
            {"node": "n1", "device_index": "10", "gpu_uuid": "b"},
            {"node": "n1", "device_index": "2", "gpu_uuid": "a"},
            {"node": "n0", "device_index": "7", "gpu_uuid": "c"},
        ]
        self.assertEqual(
            [(row["node"], row["device_index"]) for row in sorted(rows, key=m._gpu_sort_key)],
            [("n0", "7"), ("n1", "2"), ("n1", "10")],
        )

    def test_runtime_is_consistent_across_json_text_and_rich_summaries(self):
        from rich.console import Console

        m = self.module
        jobs = {"a": job("a", "u", 1)}
        jobs["a"].update({
            "create_time": "2026-08-10T09:53:00Z",
            "type": "aid",
            "placements": [{"node": "n1", "gpu": 1, "cpu": 8, "memory_gib": 32}],
        })
        nodes = {"n1": node("n1", 1, {"a": {"gpu": 1}})}
        fixed_now = datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc)
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(1, 8), "queue", "cluster",
            now=lambda: fixed_now,
        )
        expected_seconds = 2 * 86400 + 3 * 3600 + 7 * 60
        fragment = report["fragmented_nodes"][0]["workloads"][0]
        suggestion = report["suggestions"][0]["workload_details"][0]
        for workload in (fragment, suggestion):
            self.assertEqual(workload["create_time"], "2026-08-10T09:53:00Z")
            self.assertEqual(workload["runtime_seconds"], expected_seconds)
        self.assertEqual(report["generated_at"], "2026-08-12T13:00:00+00:00")
        self.assertIn("running 2d 03h", m.render_text(report))

        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False, width=160)
        self.assertTrue(m.render_rich(report, console=console))
        output = stream.getvalue()
        self.assertGreaterEqual(output.count("Running"), 2)
        self.assertGreaterEqual(output.count("2d 03h"), 2)

    def test_invalid_missing_and_future_times_are_unavailable(self):
        m = self.module
        fixed_now = datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc)
        for create_time in (None, "not-a-time", "2026-08-12T13:00:01Z"):
            with self.subTest(create_time=create_time):
                jobs = {"a": job("a", "u", 1)}
                jobs["a"]["create_time"] = create_time
                jobs["a"]["placements"] = [{"node": "n1", "gpu": 1}]
                nodes = {"n1": node("n1", 1, {"a": {"gpu": 1}})}
                report = m.build_report(
                    {"nodes": nodes, "jobs": jobs}, m.Target(1, 8), "q", "c",
                    now=lambda: fixed_now,
                )
                workload = report["fragmented_nodes"][0]["workloads"][0]
                self.assertIsNone(workload["create_time"])
                self.assertIsNone(workload["runtime_seconds"])
                self.assertIn("running -", m.render_text(report))

    def test_runtime_display_boundaries(self):
        m = self.module
        self.assertEqual(m._format_runtime(0), "<1m")
        self.assertEqual(m._format_runtime(25 * 60), "25m")
        self.assertEqual(m._format_runtime((3 * 60 + 7) * 60), "3h 07m")
        self.assertEqual(m._format_runtime((4 * 24 + 3) * 3600), "4d 03h")
        self.assertEqual(m._format_runtime(None), "-")

    def test_unattributed_load_is_visible_but_not_releasable(self):
        m = self.module
        nodes = {"n1": node("n1", 2, {})}
        nodes["n1"]["unattributed"] = {"gpu": 2, "cpu": 0, "memory_gib": 0}
        report = m.build_report({"nodes": nodes, "jobs": {}}, m.Target(1, 8), "q", "c")
        self.assertEqual(report["fragmented_nodes"][0]["unattributed"]["gpu"], 2)
        self.assertEqual(report["suggestions"], [])

    def test_gpus_per_node_defaults_to_eight(self):
        with mock.patch.object(sys, "argv", ["queue_plan.py", "--nodes", "2"]):
            args = self.module.parse_args()
        self.assertEqual(args.gpus_per_node, 8)
        self.assertEqual(args.strategy, "min-gpu")
        self.assertEqual(args.candidate_scope, "fragmented")
        self.assertEqual(args.alternatives, 1)
        self.assertEqual(args.search_seconds, 10.0)
        self.assertFalse(args.show_gpu_details)
        self.assertIsNone(args.refresh_seconds)

    def test_refresh_deadline_skips_ticks_during_full_queries(self):
        m = self.module
        self.assertEqual(m._next_refresh_deadline(0, 5, 3), (5, 0))
        self.assertEqual(m._next_refresh_deadline(0, 5, 5), (5, 0))
        self.assertEqual(m._next_refresh_deadline(0, 5, 11), (15, 2))

        args = argparse.Namespace(refresh_seconds=5, as_json=True)
        clock_values = iter((0, 11, 26))
        sleeps = []
        collections = []

        def collect(*unused):
            collections.append(len(collections))
            if len(collections) == 3:
                raise KeyboardInterrupt
            return {}

        with mock.patch.object(m, "_collect_report", side_effect=collect), \
                mock.patch.object(m, "_emit_report") as emit:
            with self.assertRaises(KeyboardInterrupt):
                m._run_reports(
                    object(), args, "queue", "cluster",
                    clock=lambda: next(clock_values), sleep=sleeps.append,
                )
        self.assertEqual(sleeps, [4, 4])
        self.assertEqual(emit.call_count, 2)

    def test_refresh_json_is_compact_ndjson(self):
        m = self.module
        args = argparse.Namespace(
            out=None, as_json=True, refresh_seconds=5,
            show_gpu_details=False,
        )
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            m._emit_report({"schema_version": 2}, args)
        self.assertEqual(stream.getvalue(), '{"schema_version":2}\n')

    def test_full_scope_includes_shared_full_node(self):
        m = self.module
        jobs = {"a": job("a", "u1", 4), "b": job("b", "u2", 4)}
        nodes = {"n1": node(
            "n1", 8, {"a": {"gpu": 4}, "b": {"gpu": 4}}
        )}
        plans, optimality = m.solve_candidates(
            nodes, jobs, m.Target(1, 8), candidate_scope="full"
        )
        self.assertEqual(optimality, "exact")
        self.assertEqual(set(plans[0]["jobs"]), {"a", "b"})
        self.assertEqual(plans[0]["freed_nodes"], ["n1"])

    def test_all_scope_compares_fragmented_and_full_job_plans(self):
        m = self.module
        jobs = {
            **{name: job(name, name, 1) for name in "abcd"},
            "multi": job("multi", "shared", 16),
        }
        nodes = {
            "f1": node("f1", 2, {"a": {"gpu": 1}, "b": {"gpu": 1}}),
            "f2": node("f2", 2, {"c": {"gpu": 1}, "d": {"gpu": 1}}),
            "n1": node("n1", 8, {"multi": {"gpu": 8}}),
            "n2": node("n2", 8, {"multi": {"gpu": 8}}),
        }
        plans, _ = m.solve_candidates(
            nodes, jobs, m.Target(2, 8), candidate_scope="all"
        )
        by_strategy = {plan["strategy"]: plan for plan in plans if plan["rank"] == 1}
        self.assertEqual(set(by_strategy["min-gpu"]["jobs"]), set("abcd"))
        self.assertEqual(by_strategy["min-gpu"]["gpus"], 4)
        self.assertEqual(by_strategy["min-workloads"]["jobs"], ["multi"])
        self.assertEqual(by_strategy["min-users"]["jobs"], ["multi"])

    def test_report_exposes_candidate_scope_and_full_node_count(self):
        m = self.module
        jobs = {"a": job("a", "u", 8)}
        jobs["a"]["placements"] = [{"node": "n1", "gpu": 8}]
        nodes = {"n1": node("n1", 8, {"a": {"gpu": 8}})}
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(1, 8),
            "queue", "cluster", "full",
        )
        self.assertEqual(report["analysis"]["candidate_scope"], "full")
        self.assertEqual(report["summary"]["full_nodes"], 1)

    def test_same_plan_is_grouped_independently_by_strategy(self):
        m = self.module
        jobs = {"a": job("a", "u", 1), "b": job("b", "u", 1)}
        nodes = {"n1": node("n1", 1, {"a": {"gpu": 1}}),
                 "n2": node("n2", 1, {"b": {"gpu": 1}})}
        plans, _ = m.solve_candidates(nodes, jobs, m.Target(2, 8))
        self.assertEqual(len(plans), 3)
        self.assertEqual(
            [plan["strategy"] for plan in plans],
            ["min-gpu", "min-workloads", "min-users"],
        )
        self.assertTrue(all(plan["rank"] == 1 for plan in plans))
        self.assertTrue(all("also_strategies" not in plan for plan in plans))
        jobs["a"]["placements"] = [{"node": "n1", "gpu": 1}]
        jobs["b"]["placements"] = [{"node": "n2", "gpu": 1}]
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(2, 8), "queue", "cluster")
        text = m.render_text(report)
        self.assertIn("min-workloads rank 1", text)

    def test_rich_report_contains_tables_and_color(self):
        from rich.console import Console

        m = self.module
        jobs = {"a": job("a", "u", 1)}
        jobs["a"]["placements"] = [
            {"node": "n1", "gpu": 1, "cpu": 8, "memory_gib": 32}
        ]
        nodes = {"n1": node("n1", 1, {"a": {"gpu": 1}})}
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(1, 8), "queue", "cluster")
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True, color_system="standard", width=120)
        self.assertTrue(m.render_rich(report, console=console))
        output = stream.getvalue()
        self.assertIn("ClusterX Queue Packing", output)
        self.assertIn("Fragmented nodes", output)
        self.assertIn("Search diagnostics", output)
        self.assertIn("Plan 1", output)
        self.assertIn("min-workloads · Workloads", output)
        self.assertIn("Memory GiB", output)
        self.assertIn("\x1b[", output)

    def test_rich_report_keeps_all_placements_at_common_widths(self):
        from rich.console import Console

        m = self.module
        job_name = "distributed-job-with-complete-placement-details"
        jobs = {"multi": job("multi", "long-user-name", 192)}
        jobs["multi"]["job_name"] = job_name
        jobs["multi"]["placements"] = [
            {"node": f"node-{i:02d}", "gpu": 8, "cpu": 16, "memory_gib": 64}
            for i in range(24)
        ]
        nodes = {
            f"node-{i:02d}": node(f"node-{i:02d}", 8, {"multi": {"gpu": 8}})
            for i in range(24)
        }
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(4, 8),
            "queue", "cluster", "full", alternatives=1,
        )
        for width in (80, 120, 160):
            stream = io.StringIO()
            console = Console(file=stream, force_terminal=False, width=width)
            self.assertTrue(m.render_rich(report, console=console))
            output = stream.getvalue()
            for i in range(24):
                self.assertIn(f"node-{i:02d}", output)
            self.assertNotIn("…", output)
        plain = m.render_text(report)
        self.assertIn(job_name, plain)
        for i in range(24):
            self.assertIn(f"node-{i:02d}: 8 GPU, 16 CPU, 64 GiB memory", plain)

    def test_min_jobs_strategy_is_accepted(self):
        with mock.patch.object(
            sys, "argv", ["queue_plan.py", "--nodes", "2", "--strategy", "min-jobs"]
        ):
            args = self.module.parse_args()
        self.assertEqual(args.strategy, "min-workloads")

    def test_alternatives_returns_ranked_plans_per_strategy(self):
        m = self.module
        jobs = {name: job(name, f"u{name}", gpu) for name, gpu in zip("abcd", (1, 2, 3, 4))}
        nodes = {
            f"n{i}": node(f"n{i}", jobs[name]["total_gpu"], {name: {"gpu": jobs[name]["total_gpu"]}})
            for i, name in enumerate(jobs)
        }
        plans, optimality = m.solve_candidates(
            nodes, jobs, m.Target(1, 8), alternatives=3
        )
        self.assertEqual(optimality, "exact")
        for strategy in ("min-gpu", "min-workloads", "min-users"):
            group = [plan for plan in plans if plan["strategy"] == strategy]
            self.assertEqual([plan["rank"] for plan in group], [1, 2, 3])
            self.assertEqual(group[0]["delta_from_best"], 0)
            self.assertNotIn("also_strategies", group[0])
        gpu_group = [plan for plan in plans if plan["strategy"] == "min-gpu"]
        self.assertEqual([plan["primary_cost"] for plan in gpu_group], [1, 2, 3])
        self.assertEqual([plan["delta_from_best"] for plan in gpu_group], [0, 1, 2])

    def test_alternative_and_search_arguments_are_validated(self):
        for value in ("0", "11"):
            with mock.patch.object(
                sys, "argv", ["queue_plan.py", "--nodes", "2", "--alternatives", value]
            ), self.assertRaises(SystemExit):
                self.module.parse_args()
        with mock.patch.object(
            sys, "argv", ["queue_plan.py", "--nodes", "2", "--search-seconds", "0"]
        ), self.assertRaises(SystemExit):
            self.module.parse_args()
        with mock.patch.object(
            sys, "argv", ["queue_plan.py", "--nodes", "2", "--refresh-seconds", "0"]
        ), self.assertRaises(SystemExit):
            self.module.parse_args()

    def test_report_includes_search_measurements(self):
        m = self.module
        jobs = {"a": job("a", "u", 1)}
        jobs["a"]["placements"] = [{"node": "n1", "gpu": 1}]
        report = m.build_report(
            {"nodes": {"n1": node("n1", 1, {"a": {"gpu": 1}})}, "jobs": jobs},
            m.Target(1, 8), "queue", "cluster", search_seconds=2.5,
        )
        analysis = report["analysis"]
        self.assertEqual(analysis["search_budget_seconds"], 2.5)
        self.assertEqual(analysis["estimated_states"], 1)
        self.assertGreaterEqual(analysis["states_examined"], 1)
        self.assertEqual(analysis["switch_reason"], "completed")


if __name__ == "__main__":
    unittest.main()
