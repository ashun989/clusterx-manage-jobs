import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "client/src"
sys.path.insert(0, str(SCRIPTS))
MODULE_ROOT = SCRIPTS / "clusterx_monitor_cli"
SPEC = importlib.util.spec_from_file_location(
    "clusterx_config_resolver", MODULE_ROOT / "config_resolver.py"
)
resolver = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)

from clusterx_monitor_cli import clusterx_exec
from clusterx_monitor_cli import monitor_cli


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
            self.assertEqual(selection.path, local.resolve())

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
                    str(MODULE_ROOT / "clusterx_exec.py"),
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
                [str(config.resolve()), "log", "job-1"],
            )

    def test_node_policy_uses_explicit_or_protected_identity_and_never_user_env(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            identity = temp / "identity.yaml"
            identity.write_text(
                "schema_version: 1\ncluster_users:\n  https://monitor.test: alice\n",
                encoding="utf-8",
            )
            identity.chmod(0o600)
            self.assertEqual(
                clusterx_exec._configured_cluster_user(
                    "https://monitor.test/", None, identity,
                ),
                "alice",
            )
            self.assertEqual(
                clusterx_exec._configured_cluster_user(
                    "https://monitor.test/", " Bob ", identity,
                ),
                "Bob",
            )
            with mock.patch.dict(os.environ, {"USER": "wrong-user"}, clear=False):
                self.assertIsNone(
                    clusterx_exec._configured_cluster_user(
                        "https://other.test", None, identity,
                    )
                )

    def test_node_policy_warns_on_include_without_rewriting_arguments(self):
        responses = [
            mock.Mock(status_code=200, json=lambda: {"node_allocation": {"enabled": True}}),
            mock.Mock(status_code=200, json=lambda: {
                "identity": {"group": "team"}, "nodes": [{"node": "team-1"}],
            }),
        ]
        with mock.patch.dict(os.environ, {"CLUSTERX_MONITOR_URL": "https://monitor.test"}, clear=False), \
             mock.patch.object(clusterx_exec.requests, "get", side_effect=responses), \
             mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as stderr:
            error = clusterx_exec._check_node_policy(
                ["run", "--include", "other-1", "runner.sh"],
                explicit_user="alice", identity_path=Path("/missing/identity.yaml"),
            )
        self.assertIsNone(error)
        self.assertIn("does not contain any node", stderr.getvalue())

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
                    str(MODULE_ROOT / "clusterx_exec.py"),
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
                            str(MODULE_ROOT / "clusterx_exec.py"),
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
                    str(MODULE_ROOT / "clusterx_exec.py"),
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

    def test_wrapper_enforces_training_cpu_boundaries_before_clusterx(self):
        allowed = (
            [],
            ["--gpus-per-task", "0", "--cpus-per-task", "14"],
            ["--gpus-per-task", "1", "--cpus-per-task", "14"],
            ["--gpus-per-task=8", "--cpus-per-task=112"],
            ["-N", "4", "--gpus-per-task", "8", "--cpus-per-task", "112"],
        )
        rejected = (
            ["--gpus-per-task", "0", "--cpus-per-task", "15"],
            ["--gpus-per-task", "1", "--cpus-per-task", "15"],
            ["--gpus-per-task=8", "--cpus-per-task=113"],
            ["--gpus-per-task", "1.5", "--cpus-per-task", "4"],
            ["--gpus-per-task", "1", "--cpus-per-task", "nan"],
        )
        cases = [(item, 0) for item in allowed] + [(item, 2) for item in rejected]
        for resource_args, expected in cases:
            with self.subTest(resource_args=resource_args), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                project = temp / "project"
                self._config(project / ".clusterx/clusterx.yaml")
                invoked = temp / "invoked"
                binary = temp / "clusterx"
                binary.write_text("#!/bin/sh\ntouch \"$WRAPPER_INVOKED\"\n", encoding="utf-8")
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
                env = os.environ.copy()
                env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
                env["WRAPPER_INVOKED"] = str(invoked)
                run = subprocess.run(
                    [
                        sys.executable, str(MODULE_ROOT / "clusterx_exec.py"),
                        "--cwd", str(project), "--", "run", *resource_args, "true",
                    ],
                    text=True, capture_output=True, env=env,
                )
                self.assertEqual(run.returncode, expected, run.stderr)
                self.assertEqual(invoked.exists(), expected == 0)

    def test_resource_policy_precedence_is_explicit_then_environment_then_builtin(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            self._config(project / ".clusterx/clusterx.yaml")
            binary = temp / "clusterx"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            restrictive = temp / "restrictive.json"
            permissive = temp / "permissive.json"
            base = {"training": {"cpu_per_gpu": 1, "zero_gpu_max_cpu_per_node": 1}}
            restrictive.write_text(json.dumps(base), encoding="utf-8")
            base["training"] = {"cpu_per_gpu": 2, "zero_gpu_max_cpu_per_node": 2}
            permissive.write_text(json.dumps(base), encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            env["CLUSTERX_RESOURCE_POLICY"] = str(restrictive)
            command = [
                sys.executable, str(MODULE_ROOT / "clusterx_exec.py"),
                "--cwd", str(project), "--resource-policy", str(permissive), "--",
                "run", "--gpus-per-task", "1", "--cpus-per-task", "2", "true",
            ]
            explicit = subprocess.run(command, text=True, capture_output=True, env=env)
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            environment = subprocess.run(
                [part for part in command if part not in {"--resource-policy", str(permissive)}],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(environment.returncode, 2)

    def test_cluster_user_precedence_uses_environment_without_falling_back_to_user(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "identity.yaml"
            identity.write_text(
                "schema_version: 1\ncluster_users:\n  http://monitor: mapped-user\n",
                encoding="utf-8",
            )
            identity.chmod(0o600)
            with mock.patch.dict(os.environ, {"CLUSTERX_USER": "env-user"}, clear=False):
                self.assertEqual(
                    clusterx_exec._configured_cluster_user("http://monitor", None, identity),
                    "env-user",
                )
                self.assertEqual(
                    clusterx_exec._configured_cluster_user("http://monitor", "explicit-user", identity),
                    "explicit-user",
                )
                self.assertEqual(monitor_cli._configured_user(None), "env-user")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLUSTERX_USER", None)
                self.assertEqual(
                    clusterx_exec._configured_cluster_user("http://monitor", None, identity),
                    "mapped-user",
                )
                self.assertIsNone(monitor_cli._configured_user(None))


if __name__ == "__main__":
    unittest.main()
