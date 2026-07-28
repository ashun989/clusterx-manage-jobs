import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/maintenance/check_updates.py"


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
            self.assertIn("fingerprint", report["note"])
            self.assertTrue(report["reference_differs"])

    def test_approved_source_fingerprint_reports_no_change(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            content = "same source\n"
            import hashlib
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            reference = temp / "reference.md"
            reference.write_text("curated reference\n", encoding="utf-8")
            manifest = temp / "sources.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sources": [{
                            "id": "clusterx-main",
                            "url": "https://example.test/doc",
                            "reference_path": "reference.md",
                            "approved_source_sha256": digest,
                        }]
                    }
                ),
                encoding="utf-8",
            )
            cli = temp / "lark-cli"
            payload = json.dumps(
                {"data": {"document": {"content": content, "revision_id": 3}}}
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
            run = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--source", "clusterx-main",
                    "--staging-dir", str(temp / "staging"),
                    "--manifest", str(manifest),
                ],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertFalse(report["changed"])
            self.assertTrue(report["reference_differs"])

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
