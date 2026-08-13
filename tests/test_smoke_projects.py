import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "smoke-projects"


class SmokeProjectTests(unittest.TestCase):
    def test_project_manifests(self):
        manifests = sorted(SMOKE.glob("*/project.json"))
        self.assertEqual(len(manifests), 3)
        for path in manifests:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue((path.parent / data["entrypoint"]).is_file())
            self.assertEqual(data["resources"]["nodes"], 1)
        storage = json.loads(
            (SMOKE / "storage-access/project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(storage["target_source"], "clusterx_config")
        self.assertEqual(storage["target_scope"], "all_mounts")
        self.assertEqual(
            set(storage["required_target_types"]),
            {"file", "object"},
        )

    def test_combined_storage_project_write_verify_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_target = root / "file"
            object_target = root / "object"
            file_target.mkdir()
            object_target.mkdir()
            run = subprocess.run(
                [
                    sys.executable,
                    str(SMOKE / "storage-access" / "main.py"),
                    "--run-id",
                    "local-test",
                    "--target",
                    f"file:file-primary:{file_target}",
                    "--target",
                    f"object:object-primary:{object_target}",
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(
                [target["storage_type"] for target in result["targets"]],
                ["file", "object"],
            )
            self.assertTrue(
                all(target["cleanup"] for target in result["targets"])
            )
            self.assertNotIn(str(root), run.stdout)
            self.assertFalse((file_target / ".clusterx-smoke").exists())
            self.assertFalse((object_target / ".clusterx-smoke").exists())

    def test_smoke_projects_contain_no_private_storage_identifiers(self):
        private_markers = (
            "zengquansheng",
            "omnilab",
            "xyz2",
            "/data/",
            "/oss/",
        )
        for path in SMOKE.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for marker in private_markers:
                self.assertNotIn(marker, text, path)

    def test_gpu_module_imports_without_torch(self):
        path = SMOKE / "gpu-matmul/main.py"
        spec = importlib.util.spec_from_file_location("gpu_smoke", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.run))

    def test_live_log_project_flushes_and_persists_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result_file = Path(directory) / "result.json"
            run = subprocess.run(
                [
                    sys.executable,
                    str(SMOKE / "ssp-live-log/main.py"),
                    "--run-id",
                    "local-live-log",
                    "--steps",
                    "2",
                    "--interval-seconds",
                    "0",
                    "--result-file",
                    str(result_file),
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            events = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(
                [event["event"] for event in events],
                ["started", "progress", "progress", "completed"],
            )
            self.assertNotIn(str(result_file), run.stdout)
            self.assertTrue(events[-1]["result_persisted"])
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["steps"], 2)

    def test_skill_documents_ssp_runtime_limitations(self):
        skill_root = ROOT / "skill" / "clusterx-manage-jobs"
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        reference_text = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")

        for text in (skill_text, reference_text):
            self.assertIn("32 Unicode characters", text)
            self.assertIn("nodes_ip", text)
            self.assertIn("Running", text)
            self.assertIn("Succeeded", text)
            self.assertIn("HTTP 404", text)

    def test_skill_documents_2026_8_11_features(self):
        skill_root = ROOT / "skill" / "clusterx-manage-jobs"
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        reference_text = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")
        combined = f"{skill_text}\n{reference_text}"

        self.assertIn("2026.8.11", combined)
        self.assertIn("--sp-block", reference_text)
        self.assertIn("--workers", reference_text)
        self.assertIn("--page-size", reference_text)
        self.assertIn("batch `stop`", skill_text)
        self.assertIn("--scope queue", combined)
        self.assertIn("--scope job", combined)
        self.assertIn("--job <exact-job-name>", combined)

    def test_skill_uses_risk_based_confirmation(self):
        skill_root = ROOT / "skill" / "clusterx-manage-jobs"
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        reference_text = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")
        normalized_skill = " ".join(skill_text.split())
        normalized_reference = " ".join(reference_text.split())

        self.assertIn(
            "Do not ask for a redundant confirmation",
            normalized_skill,
        )
        self.assertIn(
            "execute it without another confirmation",
            normalized_skill,
        )
        self.assertIn(
            "Stop it directly",
            normalized_skill,
        )
        self.assertIn(
            "Do not ask for a redundant confirmation",
            normalized_reference,
        )
        self.assertNotIn("Ask for explicit confirmation", skill_text)
        self.assertNotIn("user approval is required", skill_text)

    def test_queue_plan_documentation_is_complete_and_version_matches(self):
        skill_root = ROOT / "skill" / "clusterx-manage-jobs"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        configuration = (
            skill_root / "references" / "configuration.md"
        ).read_text(encoding="utf-8")
        cli_reference = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")
        self.assertIn(f"`{version}`", readme)
        for marker in (
            "requirements.txt",
            "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs",
            "--strategy all",
            "--candidate-scope all",
            "--alternatives 3",
            "--search-seconds 10",
            "使用 $clusterx-manage-jobs",
        ):
            self.assertIn(marker, readme)
        self.assertIn("--cwd` 可省略", configuration)
        for marker in (
            "Exit status `0`",
            "`1` means live analysis failed",
            "`2` means arguments",
            "capacity source of truth",
            "never claimed as releasable",
            "Complete options:",
            "`--strategy` defaults to `min-gpu`",
            "`--refresh-seconds` enables a fixed-rate monitor",
            "NDJSON",
            '"schema_version": 2',
            "`estimated-time`",
            "`exact-deadline`",
            "cross-strategy deduplication",
            "min-workloads",
            "unattributed",
            "--show-gpu-details",
            '"gpu_utilization"',
            "reported_gpu_count",
        ):
            self.assertIn(marker, cli_reference)
        self.assertNotIn("plus deduplicated candidates", cli_reference)
        self.assertNotIn("the report deduplicates", cli_reference)
        self.assertNotIn("最少任务", readme)


if __name__ == "__main__":
    unittest.main()
