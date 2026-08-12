import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/clusterx-manage-jobs/scripts/preflight.py"


class PreflightTests(unittest.TestCase):
    def test_success_and_version_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            binary = temp / "clusterx"
            binary.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'clusterx 2026.8.11 access_token=leaked'\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            config = temp / "clusterx.yaml"
            config.write_text(
                "default: ssp\n"
                "ssp:\n"
                + "\n".join(
                    f"  {key}: placeholder"
                    for key in (
                        "subscription", "resource_group", "region",
                        "workspace", "cluster", "ak_id", "ak_secret",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            shared = temp / "shared"
            shared.mkdir()
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            run = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--config", str(config),
                    "--tmpdir", str(shared), "--json",
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("leaked", run.stdout)
            payload = json.loads(run.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["checks"]["python"]["supported"])
            self.assertEqual(
                payload["checks"]["clusterx"]["compatibility"],
                "tested",
            )

    def test_missing_binary_and_unsafe_config_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            config = temp / "clusterx.yaml"
            config.write_text("ak_secret: value\n", encoding="utf-8")
            config.chmod(0o644)
            env = os.environ.copy()
            env["PATH"] = str(temp)
            run = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(config), "--json"],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(run.returncode, 1)
            payload = json.loads(run.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["checks"]["config"]["permissions_safe"])

    def test_project_config_overrides_global(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            nested = project / "src"
            nested.mkdir(parents=True)
            local_dir = project / ".clusterx"
            local_dir.mkdir()
            local = local_dir / "clusterx.yaml"
            local.write_text(
                "default: ssp\nssp:\n"
                "  cluster_type: PT\n  subscription: x\n"
                "  resource_group: x\n  region: x\n  workspace: x\n"
                "  cluster: x\n  ak_id: x\n  ak_secret: x\n",
                encoding="utf-8",
            )
            local.chmod(0o600)
            binary = temp / "clusterx"
            binary.write_text("#!/bin/sh\nprintf 'clusterx 1.0\\n'\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env.pop("CLUSTERX_CFG_PATH", None)
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            env["DEV_ENV"] = str(temp / "global")
            run = subprocess.run(
                [sys.executable, str(SCRIPT), "--cwd", str(nested), "--json"],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            self.assertEqual(payload["checks"]["config"]["source"], "project")
            self.assertEqual(payload["checks"]["config"]["path"], str(local))


if __name__ == "__main__":
    unittest.main()
