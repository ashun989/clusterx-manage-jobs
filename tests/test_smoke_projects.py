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
        self.assertEqual(len(manifests), 6)
        for path in manifests:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue((path.parent / data["entrypoint"]).is_file())
            self.assertEqual(data["resources"]["nodes"], 1)

    def test_storage_projects_write_verify_and_cleanup(self):
        projects = [
            "afs-data-zengquansheng",
            "afs-omnilab-shared",
            "aoss-zengquansheng",
            "aoss-xyz2-omni",
        ]
        for project in projects:
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                run = subprocess.run(
                    [
                        sys.executable,
                        str(SMOKE / project / "main.py"),
                        "--run-id",
                        "local-test",
                        "--target",
                        directory,
                    ],
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(run.stdout)
                self.assertTrue(result["ok"])
                self.assertTrue(result["cleanup"])
                self.assertFalse((Path(directory) / ".clusterx-smoke").exists())

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
            "apply the reviewed, sanitized changes without asking for another confirmation",
            normalized_skill,
        )
        self.assertIn(
            "Do not ask for a redundant confirmation",
            normalized_reference,
        )
        self.assertNotIn("Ask for explicit confirmation", skill_text)
        self.assertNotIn("user approval is required", skill_text)


if __name__ == "__main__":
    unittest.main()
