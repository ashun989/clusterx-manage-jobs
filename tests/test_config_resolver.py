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
            with mock.patch.dict(os.environ, {"CLUSTERX_USER": ""}, clear=False):
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
            with mock.patch.dict(os.environ, {"USER": "wrong-user", "CLUSTERX_USER": ""}, clear=False):
                self.assertIsNone(
                    clusterx_exec._configured_cluster_user(
                        "https://other.test", None, identity,
                    )
                )

    def test_node_policy_warns_on_include_without_rewriting_arguments(self):
        responses = [
            mock.Mock(status_code=200, json=lambda: {"node_allocation": {"enabled": True}}),
            mock.Mock(status_code=200, json=lambda: {
                "identity": {"group": "team"},
                "nodes": [{"node": "team-1", "host_ip": "10.140.62.215"}],
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
        self.assertIn("does not contain any hostname", stderr.getvalue())
        self.assertIn("host-10-140-62-215", stderr.getvalue())

    def test_node_policy_uses_lowercase_hostname_with_old_monitor_fallback(self):
        self.assertEqual(
            clusterx_exec._monitor_node_hostname({"hostname": "host-10-140-62-215"}),
            "host-10-140-62-215",
        )
        responses = [
            mock.Mock(status_code=200, json=lambda: {"node_allocation": {"enabled": True}}),
            mock.Mock(status_code=200, json=lambda: {
                "identity": {"group": "team"},
                "nodes": [{"node": "team-1", "host_ip": "10.140.62.215"}],
            }),
        ]
        with mock.patch.dict(os.environ, {"CLUSTERX_MONITOR_URL": "https://monitor.test"}, clear=False), \
             mock.patch.object(clusterx_exec.requests, "get", side_effect=responses), \
             mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as stderr:
            error = clusterx_exec._check_node_policy(
                ["run", "--include", "host-10-140-62-215", "runner.sh"],
                explicit_user="alice", identity_path=Path("/missing/identity.yaml"),
            )
        self.assertIsNone(error)
        self.assertEqual(stderr.getvalue(), "")

    def test_node_policy_warns_when_hostname_is_not_lowercase(self):
        responses = [
            mock.Mock(status_code=200, json=lambda: {"node_allocation": {"enabled": True}}),
            mock.Mock(status_code=200, json=lambda: {
                "identity": {"group": "team"},
                "nodes": [{"node": "team-1", "hostname": "host-10-140-62-215", "host_ip": "10.140.62.215"}],
            }),
        ]
        with mock.patch.dict(os.environ, {"CLUSTERX_MONITOR_URL": "https://monitor.test"}, clear=False), \
             mock.patch.object(clusterx_exec.requests, "get", side_effect=responses), \
             mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as stderr:
            clusterx_exec._check_node_policy(
                ["run", "--include", "host-10-140-62-215".upper(), "runner.sh"],
                explicit_user="alice", identity_path=Path("/missing/identity.yaml"),
            )
        self.assertIn("must be lowercase", stderr.getvalue())
        self.assertIn("host-10-140-62-215", stderr.getvalue())

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
            ["--gpus-per-task", "1", "--memory-per-task", "120"],
            ["--gpus-per-task", "1", "--memory-per-task", "120GB"],
            ["--gpus-per-task", "1", "--memory-per-task", "120GiB"],
            ["--gpus-per-task", "1", "--memory-per-task", "128849018880B"],
            ["--gpus-per-task", "2", "--memory-per-task", "240"],
        )
        rejected = (
            ["--gpus-per-task", "0", "--cpus-per-task", "15"],
            ["--gpus-per-task", "1", "--cpus-per-task", "15"],
            ["--gpus-per-task=8", "--cpus-per-task=113"],
            ["--gpus-per-task", "1.5", "--cpus-per-task", "4"],
            ["--gpus-per-task", "1", "--cpus-per-task", "nan"],
            ["--gpus-per-task", "1", "--memory-per-task", "120.1"],
            ["--gpus-per-task", "0", "--memory-per-task", "240.1"],
            ["--gpus-per-task", "1", "--memory-per-task", "not-a-size"],
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
                env.pop("CLUSTERX_MONITOR_URL", None)
                env.pop("CLUSTERX_RESOURCE_POLICY", None)
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
            base = {"training": {
                "cpu_per_gpu": 1, "memory_gib_per_gpu": 120,
                "zero_gpu_max_cpu_per_node": 1,
                "zero_gpu_max_memory_gib_per_node": 240,
            }}
            restrictive.write_text(json.dumps(base), encoding="utf-8")
            base["training"]["cpu_per_gpu"] = 2
            base["training"]["zero_gpu_max_cpu_per_node"] = 2
            permissive.write_text(json.dumps(base), encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env.get('PATH', '')}"
            env["CLUSTERX_RESOURCE_POLICY"] = str(restrictive)
            env.pop("CLUSTERX_MONITOR_URL", None)
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

    def test_monitor_resource_policy_precedes_environment_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "local.json"
            local.write_text(json.dumps({"training": {
                "cpu_per_gpu": 1, "memory_gib_per_gpu": 1,
                "zero_gpu_max_cpu_per_node": 1,
                "zero_gpu_max_memory_gib_per_node": 1,
            }}), encoding="utf-8")
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"valid": True, "policy": {"training": {
                "cpu_per_gpu": 12, "memory_gib_per_gpu": 120,
                "zero_gpu_max_cpu_per_node": 8,
                "zero_gpu_max_memory_gib_per_node": 240,
            }}}
            with mock.patch.object(clusterx_exec.requests, "get", return_value=response) as get:
                limits, source = clusterx_exec._resolve_training_policy(
                    None,
                    environment={
                        "CLUSTERX_MONITOR_URL": "https://monitor.test",
                        "CLUSTERX_RESOURCE_POLICY": str(local),
                    },
                )
            self.assertEqual(source, "Monitor")
            self.assertEqual(limits, (12, 120, 8, 240))
            get.assert_called_once_with(
                "https://monitor.test/api/v1/policy", timeout=(3, 5),
                proxies={"http": "", "https": ""},
            )

    def test_monitor_last_known_good_is_effective_and_explicit_policy_stays_first(self):
        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / "explicit.json"
            explicit.write_text(json.dumps({"training": {
                "cpu_per_gpu": 5, "memory_gib_per_gpu": 50,
                "zero_gpu_max_cpu_per_node": 6,
                "zero_gpu_max_memory_gib_per_node": 60,
            }}), encoding="utf-8")
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"valid": False, "using_last_known_good": True, "policy": {"training": {
                "cpu_per_gpu": 12, "memory_gib_per_gpu": 120,
                "zero_gpu_max_cpu_per_node": 8,
                "zero_gpu_max_memory_gib_per_node": 240,
            }}}
            with mock.patch.object(clusterx_exec.requests, "get", return_value=response):
                remote, source = clusterx_exec._resolve_training_policy(
                    None, environment={"CLUSTERX_MONITOR_URL": "https://monitor.test"},
                )
            self.assertEqual(source, "Monitor")
            self.assertEqual(remote, (12, 120, 8, 240))

            with mock.patch.object(clusterx_exec.requests, "get") as get:
                local, source = clusterx_exec._resolve_training_policy(
                    str(explicit),
                    environment={"CLUSTERX_MONITOR_URL": "https://monitor.test"},
                )
            self.assertEqual(source, "explicit --resource-policy")
            self.assertEqual(local, (5, 50, 6, 60))
            get.assert_not_called()

    def test_failed_monitor_policy_uses_environment_then_bundled_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "local.json"
            local.write_text(json.dumps({"training": {
                "cpu_per_gpu": 3, "memory_gib_per_gpu": 30,
                "zero_gpu_max_cpu_per_node": 4,
                "zero_gpu_max_memory_gib_per_node": 40,
            }}), encoding="utf-8")
            env = {"CLUSTERX_MONITOR_URL": "https://monitor.test", "CLUSTERX_RESOURCE_POLICY": str(local)}
            with mock.patch.object(clusterx_exec.requests, "get", side_effect=clusterx_exec.requests.ConnectionError):
                limits, source = clusterx_exec._resolve_training_policy(None, environment=env)
            self.assertEqual(source, "CLUSTERX_RESOURCE_POLICY")
            self.assertEqual(limits, (3, 30, 4, 40))
            with mock.patch.object(clusterx_exec.requests, "get", side_effect=clusterx_exec.requests.ConnectionError):
                limits, source = clusterx_exec._resolve_training_policy(
                    None, environment={"CLUSTERX_MONITOR_URL": "https://monitor.test"},
                )
            self.assertEqual(source, "bundled default")
            self.assertEqual(limits, (14, 120, 14, 240))

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
