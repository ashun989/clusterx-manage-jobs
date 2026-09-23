import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib
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

    def test_skill_documents_ssp_runtime_behavior(self):
        skill_root = ROOT / "skills" / "clusterx-manage-jobs"
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        reference_text = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")

        for text in (skill_text, reference_text):
            self.assertIn("32 Unicode characters", text)
            self.assertIn("nodes_ip", text)
            self.assertIn("--worker", text)
            self.assertIn("--hours", text)
            self.assertIn("10,000", text)
            self.assertIn("--page-token", text)
            self.assertIn("--streaming", text)

    def test_skill_documents_2026_8_19_features(self):
        skill_root = ROOT / "skills" / "clusterx-manage-jobs"
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        reference_text = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")
        combined = f"{skill_text}\n{reference_text}"

        self.assertIn("2026.8.19", combined)
        self.assertIn("--sp-block", reference_text)
        self.assertIn("--workers", reference_text)
        self.assertIn("--page-size", reference_text)
        self.assertIn("next_page_token", combined)
        self.assertIn("historical", combined.lower())
        self.assertIn("batch `stop`", skill_text)
        self.assertIn("--scope queue", combined)
        self.assertIn("--scope job", combined)
        self.assertIn("--job <exact-job-name>", combined)

    def test_skill_uses_risk_based_confirmation(self):
        skill_root = ROOT / "skills" / "clusterx-manage-jobs"
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

    def test_monitor_documentation_is_complete_and_version_matches(self):
        skill_root = ROOT / "skills" / "clusterx-manage-jobs"
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        server_readme = (ROOT / "server/README.md").read_text(encoding="utf-8")
        client_readme = (ROOT / "client/README.md").read_text(encoding="utf-8")
        web_readme = (ROOT / "web/README.md").read_text(encoding="utf-8")
        deploy_readme = (ROOT / "deploy/README.md").read_text(encoding="utf-8")
        release_doc = (ROOT / "docs/release.md").read_text(encoding="utf-8")
        version = (ROOT / "server/VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "2.1.2")
        self.assertIn(
            f'version = "{version}"',
            (ROOT / "server/pyproject.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            version,
            json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))["version"],
        )
        self.assertEqual(
            version,
            (ROOT / "server/VERSION").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            version,
            tomllib.loads(
                (ROOT / "client/pyproject.toml").read_text(encoding="utf-8")
            )["project"]["version"],
        )
        self.assertEqual(
            version,
            (skill_root / "VERSION").read_text(encoding="utf-8").strip(),
        )
        cli_reference = (
            skill_root / "references" / "clusterx-cli.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn(version, readme)
        self.assertNotIn(version, server_readme)
        self.assertNotIn(version, client_readme)
        self.assertNotIn(version, web_readme)
        self.assertNotIn(version, deploy_readme)
        self.assertNotIn(version, release_doc)
        for marker in (
            "server/README.md",
            "client/README.md",
            "web/README.md",
            "deploy/README.md",
            "SKILL.md",
            "docs/release.md",
        ):
            self.assertIn(marker, readme)
        for marker in (
            "clusterx-monitor serve",
            "clusterx-monitor admin init",
            "setup-required",
            "Argon2id",
            "--static-dir",
        ):
            self.assertIn(marker, server_readme)
        for marker in (
            "clusterx-monitor-cli",
            "clusterx-exec",
            "--cluster-user",
            "CLUSTERX_MONITOR_URL",
            "--user",
        ):
            self.assertIn(marker, client_readme)
        for marker in ("config.js", "--allowed-origin", "npm run build"):
            self.assertIn(marker, web_readme)
        self.assertIn("release-manifest.json", release_doc)
        for marker in (
            "Exit status `0`",
            "service unavailable",
            "cached snapshot",
            "min-workloads",
            "power telemetry",
            "never claimed as releasable",
            "utilization.low_gpu_activity",
            "--finding-category",
            "warming-up",
            "CLUSTERX_MONITOR_URL",
        ):
            self.assertIn(marker, cli_reference)
        self.assertFalse((skill_root / "scripts").exists())
        self.assertTrue((ROOT / "client/src/clusterx_monitor_cli/monitor_cli.py").exists())
        self.assertTrue((ROOT / "client" / "pyproject.toml").exists())


if __name__ == "__main__":
    unittest.main()
