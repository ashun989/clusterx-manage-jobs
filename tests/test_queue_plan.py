import importlib.util
import io
from pathlib import Path
import sys
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
        by_strategy = {plan["strategy"]: plan for plan in plans}
        self.assertEqual(by_strategy["min-gpu"]["gpus"], 3)
        self.assertEqual(by_strategy["min-tasks"]["tasks"], 2)
        self.assertEqual(by_strategy["min-users"]["users"], 1)

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

    def test_multinode_job_cost_counts_whole_job(self):
        m = self.module
        jobs = {"multi": job("multi", "u", 4)}
        nodes = {"n1": node("n1", 2, {"multi": {"gpu": 2}}),
                 "n2": node("n2", 2, {"multi": {"gpu": 2}})}
        plans, _ = m.solve_candidates(nodes, jobs, m.Target(2, 8))
        self.assertEqual((plans[0]["gpus"], plans[0]["tasks"]), (4, 1))

    def test_small_search_limit_marks_heuristic(self):
        m = self.module
        jobs = {str(i): job(str(i), "u", 1) for i in range(6)}
        nodes = {f"n{i}": node(f"n{i}", 1, {str(i): {"gpu": 1}})
                 for i in range(6)}
        plans, optimality = m.solve_candidates(
            nodes, jobs, m.Target(3, 8), max_states=1)
        self.assertEqual(optimality, "heuristic")
        self.assertTrue(plans)

    def test_script_contains_no_stop_operation(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("stop_training_job", source)
        self.assertNotIn("clusterx stop", source)

    def test_gpus_per_node_defaults_to_eight(self):
        with mock.patch.object(sys, "argv", ["queue_plan.py", "--nodes", "2"]):
            args = self.module.parse_args()
        self.assertEqual(args.gpus_per_node, 8)
        self.assertEqual(args.strategy, "all")

    def test_duplicate_plan_records_all_optimal_strategies(self):
        m = self.module
        jobs = {"a": job("a", "u", 1), "b": job("b", "u", 1)}
        nodes = {"n1": node("n1", 1, {"a": {"gpu": 1}}),
                 "n2": node("n2", 1, {"b": {"gpu": 1}})}
        plans, _ = m.solve_candidates(nodes, jobs, m.Target(2, 8))
        self.assertEqual(len(plans), 1)
        self.assertEqual(
            plans[0]["also_optimal_for"], ["min-tasks", "min-users"])
        jobs["a"]["placements"] = [{"node": "n1", "gpu": 1}]
        jobs["b"]["placements"] = [{"node": "n2", "gpu": 1}]
        report = m.build_report(
            {"nodes": nodes, "jobs": jobs}, m.Target(2, 8), "queue", "cluster")
        text = m.render_text(report)
        self.assertIn(
            "Also optimal for: minimum tasks, minimum users", text)

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
        self.assertIn("Plan 1", output)
        self.assertIn("Also optimal for:", output)
        self.assertIn("Minimum tasks, Minimum users", output)
        self.assertIn("\x1b[", output)


if __name__ == "__main__":
    unittest.main()
