import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skill/clusterx-manage-jobs/scripts/check_updates.py"


class CheckUpdatesTests(unittest.TestCase):
    def test_fetch_is_sanitized_and_reports_change(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            reference = temp / "reference.md"
            reference.write_text("old\n", encoding="utf-8")
            manifest = temp / "sources.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sources": [{
                            "id": "clusterx-main",
                            "title": "test",
                            "url": "https://example.test/doc",
                            "reference_path": "reference.md",
                        }]
                    }
                ),
                encoding="utf-8",
            )
            cli = temp / "lark-cli"
            payload = json.dumps(
                {
                    "data": {
                        "document": {
                            "content": "new\naccess_token=remote-secret\n",
                            "revision_id": "2",
                        }
                    }
                }
            )
            cli.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = auth ]; then exit 0; fi\n"
                f"printf '%s' '{payload}'\n",
                encoding="utf-8",
            )
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            staging = temp / "staging"
            run = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--source", "clusterx-main",
                    "--staging-dir", str(staging), "--manifest", str(manifest),
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(run.returncode, 10, run.stderr)
            report = json.loads(run.stdout)
            candidate = Path(report["candidate"]).read_text(encoding="utf-8")
            self.assertNotIn("remote-secret", candidate)
            self.assertIn("<redacted>", candidate)
            self.assertEqual(reference.read_text(encoding="utf-8"), "old\n")
            self.assertNotIn("user approval is required", report["note"])
            self.assertIn("staged", report["note"])

    def test_rejects_invalid_source_id(self):
        run = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--source", "../bad",
                "--staging-dir", "/tmp/unused",
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(run.returncode, 2)
        self.assertIn("source id", run.stdout)


if __name__ == "__main__":
    unittest.main()
