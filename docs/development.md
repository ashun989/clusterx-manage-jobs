# Development guide

This repository contains independently buildable Server, Client, Web and
Skill components. The Chrome extension and smoke projects are separate
development tools.

## Layout

```text
server/       Monitor API and collector
client/       Monitor CLI and Clusterx wrapper
web/          React/TypeScript dashboard
skills/       Codex Skill package and references
deploy/       Docker and systemd examples
scripts/      release and maintenance helpers
tests/        Python integration/unit tests
```

## Local checks

```bash
pytest -q
(cd web && npm test -- --run && npm run build)
(cd chrome-extension && npm test && npm run build)
python3 scripts/check_versions.py
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/clusterx-manage-jobs
```

Python tests import Server and Client sources from their component roots.
Tests use simulated gateways and do not require access to the real queue.

## Local Monitor

Local deployment helpers live in the ignored project directory `tmp/`. They
build and install the Server, Client and Web artifacts separately, then run the
installed Server in the caller's chosen process supervisor:

```bash
tmp/clusterx-monitor-build-server.local.sh
tmp/clusterx-monitor-build-client.local.sh
tmp/clusterx-monitor-build-web.local.sh
tmp/clusterx-monitor-build-skill.local.sh
tmp/clusterx-monitor-install.local.sh
tmux new-session -d -s monitor -c "$PWD" "$PWD/tmp/clusterx-monitor-serve.local.sh"
```

The Python packages are installed into `CLUSTERX_MONITOR_PYTHON` (the current
development Conda environment by default). Web artifacts and deployment files
are stored under `tmp/clusterx-monitor-local/`, so they can be reused after a
development-machine restart. The shared runtime data remains under
`.clusterx-monitor/`: `monitor.log` contains Server stdout/stderr and
`history.sqlite3` contains Monitor history. The service is API-only by default;
the Web build can be served separately or passed to the Server with
`--static-dir`.

## Reference maintenance

Repository-only Feishu maintenance helpers live under `scripts/maintenance/`.
Use `check_updates.py` to generate a sanitized candidate reference and
`install_clusterx.py` to retrieve an approved Clusterx package into a selected
environment. After manual review, update the reviewed revision and fingerprint
in `sources.json`. These helpers are not part of the Skill or Client release
artifacts.
