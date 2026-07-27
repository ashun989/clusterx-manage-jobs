import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skill/clusterx-manage-jobs/scripts/redact.py"
SPEC = importlib.util.spec_from_file_location("clusterx_redact", MODULE_PATH)
redact_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(redact_module)


class RedactTests(unittest.TestCase):
    def test_structured_and_cli_secrets(self):
        source = (
            'ak_secret: yaml-secret\n'
            '"access_token":"json-token"\n'
            "--client-secret cli-secret --password=inline-secret\n"
        )
        result = redact_module.redact(source)
        for secret in ("yaml-secret", "json-token", "cli-secret", "inline-secret"):
            self.assertNotIn(secret, result)
        self.assertGreaterEqual(result.count("<redacted>"), 4)

    def test_clusterx_mount_metadata(self):
        source = (
            '{"metadata":{"items":['
            '{"key":"access_key","value":"ACCESS"},'
            '{"key":"secret_key","value":"SECRET"}]}}'
        )
        result = redact_module.redact(source)
        self.assertNotIn("ACCESS", result)
        self.assertNotIn("SECRET", result)
        self.assertEqual(result.count("<redacted>"), 2)

    def test_url_headers_and_private_key(self):
        source = (
            "https://user:pass@example.test/a?X-Amz-Signature=signed&safe=yes\n"
            "Authorization: Bearer bearer-token\n"
            "Cookie: session=cookie-secret\n"
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY-----\n"
        )
        result = redact_module.redact(source)
        for secret in ("user:pass", "signed", "bearer-token", "cookie-secret", "private-material"):
            self.assertNotIn(secret, result)
        self.assertIn("safe=yes", result)

    def test_non_secret_content_is_unchanged(self):
        source = "queue=training image=registry.example/model:v1\n"
        self.assertEqual(redact_module.redact(source), source)


if __name__ == "__main__":
    unittest.main()
