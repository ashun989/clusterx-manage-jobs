import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/clusterx-manage-jobs/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "clusterx_config_resolver", SCRIPTS / "config_resolver.py"
)
resolver = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)


class ConfigResolverTests(unittest.TestCase):
    def _config(self, path: Path, mode: int = 0o600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("default: ssp\nssp: {}\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def test_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            nested = project / "src"
            nested.mkdir(parents=True)
            local = self._config(project / ".clusterx/clusterx.yaml")
            global_config = self._config(temp / "dev/clusterx/clusterx.yaml")
            env_config = self._config(temp / "env.yaml")
            explicit = self._config(temp / "explicit.yaml")

            selection = resolver.resolve_config(
                explicit=explicit,
                cwd=nested,
                environ={
                    "CLUSTERX_CFG_PATH": str(env_config),
                    "DEV_ENV": str(temp / "dev"),
                },
            )
            self.assertEqual(selection.source, "explicit")
            self.assertEqual(selection.path, explicit)

            selection = resolver.resolve_config(
                cwd=nested,
                environ={
                    "CLUSTERX_CFG_PATH": str(env_config),
                    "DEV_ENV": str(temp / "dev"),
                },
            )
            self.assertEqual(selection.source, "environment")

            selection = resolver.resolve_config(
                cwd=nested, environ={"DEV_ENV": str(temp / "dev")}
            )
            self.assertEqual(selection.source, "project")
            self.assertEqual(selection.path, local)

            local.unlink()
            selection = resolver.resolve_config(
                cwd=nested, environ={"DEV_ENV": str(temp / "dev")}
            )
            self.assertEqual(selection.source, "global")
            self.assertEqual(selection.path, global_config)

    def test_native_fallback_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            selection = resolver.resolve_config(
                cwd=temp,
                environ={},
                home=temp / "home",
            )
            self.assertEqual(selection.source, "native")
            self.assertEqual(
                selection.path, temp / "home/.config/clusterx.yaml"
            )
            unsafe = self._config(temp / "unsafe.yaml", 0o640)
            inspection = resolver.inspect_config(
                resolver.ConfigSelection(unsafe, "explicit")
            )
            self.assertFalse(inspection["permissions_safe"])

    def test_dev_env_is_used_only_when_explicitly_set(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            global_config = self._config(
                temp / "dev/clusterx/clusterx.yaml"
            )
            native_home = temp / "home"
            selection = resolver.resolve_config(
                cwd=temp,
                environ={},
                home=native_home,
            )
            self.assertEqual(selection.source, "native")
            self.assertEqual(
                selection.path,
                native_home / ".config/clusterx.yaml",
            )
            selection = resolver.resolve_config(
                cwd=temp,
                environ={"DEV_ENV": str(temp / "dev")},
                home=native_home,
            )
            self.assertEqual(selection.source, "global")
            self.assertEqual(selection.path, global_config)

    def test_wrapper_sets_config_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            config = self._config(project / ".clusterx/clusterx.yaml")
            output = temp / "output"
            binary = temp / "clusterx"
            binary.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$CLUSTERX_CFG_PATH\" > \"$WRAPPER_OUTPUT\"\n"
                "printf '%s\\n' \"$@\" >> \"$WRAPPER_OUTPUT\"\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            env["WRAPPER_OUTPUT"] = str(output)
            env.pop("CLUSTERX_CFG_PATH", None)
            run = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "clusterx_exec.py"),
                    "--cwd",
                    str(project),
                    "--",
                    "log",
                    "job-1",
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines(),
                [str(config), "log", "job-1"],
            )

    def test_wrapper_redacts_clusterx_output(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            self._config(project / ".clusterx/clusterx.yaml")
            binary = temp / "clusterx"
            binary.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'{\"key\":\"access_key\",\"value\":\"ACCESS\"}'\n"
                "printf '%s\\n' 'ak_secret=SECRET' >&2\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            run = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "clusterx_exec.py"),
                    "--cwd",
                    str(project),
                    "--",
                    "run",
                    "true",
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn("ACCESS", run.stdout)
            self.assertNotIn("SECRET", run.stderr)
            self.assertIn("<redacted>", run.stdout)
            self.assertIn("<redacted>", run.stderr)

    def test_wrapper_rejects_shell_command_strings_before_clusterx(self):
        unsafe_commands = [
            ["bash", "-lc", "cd /workspace && python train.py"],
            ["/bin/bash", "-c", "python train.py"],
            ["sh", "-e", "-c", "python train.py"],
            ["dash", "-ec", "python train.py"],
            ["zsh", "-lc", "python train.py"],
            ["ksh", "-c", "python train.py"],
        ]
        for command in unsafe_commands:
            with self.subTest(command=command):
                with tempfile.TemporaryDirectory() as directory:
                    temp = Path(directory)
                    project = temp / "project"
                    self._config(project / ".clusterx/clusterx.yaml")
                    invoked = temp / "invoked"
                    binary = temp / "clusterx"
                    binary.write_text(
                        "#!/bin/sh\n"
                        "touch \"$WRAPPER_INVOKED\"\n",
                        encoding="utf-8",
                    )
                    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
                    env = os.environ.copy()
                    env["PATH"] = (
                        f"{temp}{os.pathsep}{env.get('PATH', '')}"
                    )
                    env["WRAPPER_INVOKED"] = str(invoked)
                    run = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPTS / "clusterx_exec.py"),
                            "--cwd",
                            str(project),
                            "--",
                            "run",
                            "-J",
                            "test-job",
                            *command,
                        ],
                        text=True,
                        capture_output=True,
                        env=env,
                    )
                    self.assertEqual(run.returncode, 2)
                    self.assertFalse(invoked.exists())
                    self.assertIn("absolute runner script", run.stderr)
                    self.assertIn("-e KEY=VALUE", run.stderr)

    def test_wrapper_allows_direct_runner_script(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            self._config(project / ".clusterx/clusterx.yaml")
            output = temp / "output"
            binary = temp / "clusterx"
            binary.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$WRAPPER_OUTPUT\"\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            env["WRAPPER_OUTPUT"] = str(output)
            run = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "clusterx_exec.py"),
                    "--cwd",
                    str(project),
                    "--",
                    "run",
                    "-e",
                    "MAX_STEPS=3",
                    "bash",
                    "/workspace/run.sh",
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines(),
                [
                    "run",
                    "-e",
                    "MAX_STEPS=3",
                    "bash",
                    "/workspace/run.sh",
                ],
            )


if __name__ == "__main__":
    unittest.main()
