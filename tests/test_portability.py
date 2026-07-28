import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill/clusterx-manage-jobs"
PACKAGER = ROOT / "scripts/package_skill.py"
INSTALLER = ROOT / "scripts/maintenance/install_clusterx.py"


class PortabilityTests(unittest.TestCase):
    def test_runtime_skill_has_no_developer_only_files(self):
        relative_files = {
            path.relative_to(SKILL).as_posix()
            for path in SKILL.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertNotIn("references/sources.json", relative_files)
        self.assertNotIn("scripts/check_updates.py", relative_files)
        self.assertNotIn("scripts/install_clusterx.py", relative_files)

    def test_config_template_is_secret_free_and_complete(self):
        template = (
            SKILL / "assets/clusterx.example.yaml"
        ).read_text(encoding="utf-8")
        required = (
            "default: ssp",
            "subscription:",
            "resource_group:",
            "region:",
            "workspace:",
            "cluster:",
            "ak_id:",
            "ak_secret:",
            "tmpdir:",
            "mount:",
            "type: PV_AFS",
            "type: PV_AOSS",
            "metadata:",
            "key: access_key",
            "key: secret_key",
        )
        for field in required:
            self.assertIn(field, template)
        self.assertNotIn("/data/", template)
        self.assertNotIn("/oss/", template)
        self.assertNotIn("zengquansheng", template)
        self.assertEqual(template.count("type: PV_AFS"), 2)
        self.assertEqual(template.count("type: PV_AOSS"), 2)
        self.assertEqual(template.count("<"), template.count(">"))

    def test_package_contains_only_installable_skill(self):
        spec = importlib.util.spec_from_file_location(
            "clusterx_package_skill",
            PACKAGER,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = module.build_archive(output)
            first_bytes = Path(first["archive"]).read_bytes()
            second = module.build_archive(output)
            second_bytes = Path(second["archive"]).read_bytes()
            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(
                first["sha256"],
                hashlib.sha256(first_bytes).hexdigest(),
            )
            with tarfile.open(first["archive"], "r:gz") as bundle:
                names = bundle.getnames()
                archived_text = "\n".join(
                    bundle.extractfile(name).read().decode("utf-8")
                    for name in names
                    if bundle.getmember(name).isfile()
                ).lower()
            self.assertIn("clusterx-manage-jobs/SKILL.md", names)
            self.assertIn(
                "clusterx-manage-jobs/assets/clusterx.example.yaml",
                names,
            )
            self.assertFalse(
                any(
                    name.startswith(("tests/", "smoke-projects/"))
                    or "__pycache__" in name
                    for name in names
                )
            )
            for marker in module.FORBIDDEN_RUNTIME_TEXT:
                self.assertNotIn(marker, archived_text)

    def test_installer_extracts_url_and_validates_package_name(self):
        spec = importlib.util.spec_from_file_location(
            "clusterx_install_helper",
            INSTALLER,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)

        secret = "temporary-signature"
        payload = {
            "data": {
                "document": {
                    "content": (
                        "Install "
                        f"https://packages.example/clusterx-1-py3-none-any.whl?sig={secret}"
                    )
                }
            }
        }
        url = module.extract_wheel_url(payload)
        self.assertIn(secret, url)
        self.assertEqual(
            module.wheel_filename(url),
            "clusterx-1-py3-none-any.whl",
        )

        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "clusterx.whl"
            metadata = (
                "Metadata-Version: 2.1\n"
                "Name: clusterx\n"
                "Version: 1.0\n"
                "Requires-Python: >=3.10\n"
            )
            with zipfile.ZipFile(wheel, "w") as bundle:
                bundle.writestr("clusterx-1.dist-info/METADATA", metadata)
            result = module.validate_clusterx_wheel(wheel)
            self.assertEqual(result["name"], "clusterx")
            self.assertEqual(result["requires_python"], ">=3.10")

    def test_installer_allows_only_approved_plain_http_endpoint(self):
        spec = importlib.util.spec_from_file_location(
            "clusterx_install_http_helper",
            INSTALLER,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)

        approved = (
            "http://xceph-outside.pjlab.org.cn:8060/"
            "clusterx-2026.7.28-py3-none-any.whl?temporary=signature"
        )
        payload = {
            "data": {"document": {"content": f"Install {approved}"}}
        }
        self.assertEqual(module.extract_wheel_url(payload), approved)
        self.assertEqual(
            module.wheel_filename(approved),
            "clusterx-2026.7.28-py3-none-any.whl",
        )

        with self.assertRaisesRegex(ValueError, "not trusted"):
            module.wheel_filename(
                "http://packages.example/clusterx-1-py3-none-any.whl"
            )

    def test_installer_fetches_source_without_proxy(self):
        spec = importlib.util.spec_from_file_location(
            "clusterx_install_fetch_helper",
            INSTALLER,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            cli = temp / "lark-cli"
            payload = {
                "data": {
                    "document": {
                        "content": (
                            "https://packages.example/"
                            "clusterx-1-py3-none-any.whl?sig=temporary"
                        )
                    }
                }
            }
            cli.write_text(
                "#!/bin/sh\n"
                "if [ -n \"$HTTPS_PROXY\" ]; then exit 9; fi\n"
                "if [ \"$1\" = auth ]; then exit 0; fi\n"
                f"printf '%s' '{json.dumps(payload)}'\n",
                encoding="utf-8",
            )
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
            old_path = os.environ.get("PATH")
            old_proxy = os.environ.get("HTTPS_PROXY")
            try:
                os.environ["PATH"] = f"{temp}{os.pathsep}{old_path or ''}"
                os.environ["HTTPS_PROXY"] = "http://proxy.invalid"
                fetched = module.fetch_document("https://example.test/doc")
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path
                if old_proxy is None:
                    os.environ.pop("HTTPS_PROXY", None)
                else:
                    os.environ["HTTPS_PROXY"] = old_proxy
            self.assertEqual(fetched, payload)


if __name__ == "__main__":
    unittest.main()
