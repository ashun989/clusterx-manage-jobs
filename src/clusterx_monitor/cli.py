from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import stat
import sys


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_TRUSTED_HOSTS = ["127.0.0.1", "localhost", "[::1]", "testserver"]


def _trusted_hosts(bind_host: str, configured: list[str]) -> list[str]:
    values = []
    for raw in configured:
        value = raw.strip()
        if not value or value == "*" or "/" in value or "://" in value:
            raise ValueError("--allowed-host must be an explicit hostname or IP without scheme or path")
        values.append(value)
    if bind_host not in LOOPBACK_HOSTS and not values:
        raise ValueError("non-loopback --host requires at least one explicit --allowed-host")
    return list(dict.fromkeys([*DEFAULT_TRUSTED_HOSTS, *values]))


def _protected_config(path: str | Path) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_file():
        raise ValueError(f"Clusterx config does not exist: {value}")
    if stat.S_IMODE(value.stat().st_mode) & 0o077:
        raise ValueError("Clusterx config permissions must be 600")
    return value


def build_runtime(
    clusterx_config: str | Path,
    policy_config: str | Path,
    group_config: str | Path,
    *, audit_log: str | Path | None = None, history_db: str | Path | None = None,
    history_retention_days: int = 30, history_max_points: int = 100_000,
    history_max_db_mib: int = 256,
):
    config = _protected_config(clusterx_config)
    os.environ["CLUSTERX_CFG_PATH"] = str(config)
    from clusterx.launcher.ssp.ssp import SSPCluster

    from .collector import ClusterCollector
    from .policy import PolicyManager
    from .service import MonitorRuntime

    cluster = SSPCluster()
    queue = cluster.cfg.get("queue") or cluster.cfg.get("partition")
    cluster_name = cluster.cfg.get("cluster")
    if not queue or not cluster_name:
        raise ValueError("queue and cluster are required in Clusterx config")
    policy = PolicyManager(
        policy_config, group_config, allow_unconfigured=True, audit_path=audit_log,
    )
    return MonitorRuntime(
        ClusterCollector(cluster, str(queue), str(cluster_name)), policy,
        history_db=history_db, history_retention_days=history_retention_days,
        history_max_points=history_max_points, history_max_db_mib=history_max_db_mib,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="clusterx-monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    admin = subparsers.add_parser("admin", help="Initialize administrator authentication")
    admin_subparsers = admin.add_subparsers(dest="admin_command", required=True)
    initialize = admin_subparsers.add_parser("init", help="Create or rotate the administrator password hash")
    initialize.add_argument("--auth-config", required=True)
    initialize.add_argument("--username", required=True)
    initialize.add_argument("--session-ttl-hours", type=int, default=8)
    initialize.add_argument("--force", action="store_true", help="replace an existing administrator configuration")
    serve = subparsers.add_parser("serve", help="Run the read-only monitoring service")
    serve.add_argument("--clusterx-config", required=True)
    serve.add_argument("--policy-config", required=True)
    serve.add_argument("--group-config", required=True)
    serve.add_argument("--auth-config", required=True)
    serve.add_argument("--audit-log")
    serve.add_argument("--history-db", default=os.environ.get("CLUSTERX_MONITOR_HISTORY_DB", "~/.clusterx-monitor/history.sqlite3"), help="SQLite trend database path")
    serve.add_argument("--history-retention-days", type=int, default=int(os.environ.get("CLUSTERX_MONITOR_HISTORY_RETENTION_DAYS", "30")))
    serve.add_argument("--history-max-points", type=int, default=int(os.environ.get("CLUSTERX_MONITOR_HISTORY_MAX_POINTS", "100000")))
    serve.add_argument("--history-max-db-mib", type=int, default=int(os.environ.get("CLUSTERX_MONITOR_HISTORY_MAX_DB_MIB", "256")))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument(
        "--allowed-host", action="append", default=[],
        help="trusted HTTP Host for non-loopback/NAT access; repeat as needed",
    )
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--static-dir")
    args = parser.parse_args()
    if args.command == "admin":
        try:
            from .auth import initialize_auth_config

            password = getpass.getpass("Administrator password: ")
            confirmation = getpass.getpass("Confirm administrator password: ")
            if password != confirmation:
                raise ValueError("administrator passwords do not match")
            destination = initialize_auth_config(
                args.auth_config, args.username, password,
                session_ttl_hours=args.session_ttl_hours, overwrite=args.force,
            )
            print(f"administrator configuration written to {destination}")
            return 0
        except (EOFError, ImportError, OSError, ValueError) as error:
            print(f"administrator initialization failed: {error}", file=sys.stderr)
            return 2
    try:
        trusted_hosts = _trusted_hosts(args.host, args.allowed_host)
    except ValueError as error:
        print(f"clusterx monitor configuration failed: {error}", file=sys.stderr)
        return 2
    if args.host not in LOOPBACK_HOSTS:
        print(
            "warning: clusterx-monitor is listening on a non-loopback interface over HTTP; "
            "administrator credentials and sessions are not protected by TLS",
            file=sys.stderr,
        )
    try:
        import uvicorn
        from .auth import AdminAuth
        from .service import create_app

        runtime = build_runtime(
            args.clusterx_config, args.policy_config, args.group_config,
            audit_log=args.audit_log, history_db=args.history_db,
            history_retention_days=args.history_retention_days,
            history_max_points=args.history_max_points,
            history_max_db_mib=args.history_max_db_mib,
        )
        auth = AdminAuth(args.auth_config, allow_missing=True)
        app = create_app(
            runtime, static_dir=args.static_dir, auth=auth,
            allowed_hosts=trusted_hosts,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"clusterx monitor failed: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
