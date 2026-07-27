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
                "#!/bin/sh\nprintf '%s\\n' 'clusterx 1.0 access_token=leaked'\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            config = temp / "clusterx.yaml"
            config.write_text(
                "\n".join(
                    f"{key}: placeholder"
                    for key in (
                        "cluster_type", "subscription", "resource_group", "region",
                        "workspace", "cluster", "ak_id", "ak_secret",
                    )
                ),
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
            self.assertTrue(json.loads(run.stdout)["ok"])

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


if __name__ == "__main__":
    unittest.main()
