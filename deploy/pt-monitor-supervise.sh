#!/usr/bin/env bash
# Run a versioned release in a tmux session on a PT development host.
set -euo pipefail
umask 077
mode=${1:?use serve or notifications}
project=${PT_MONITOR_PROJECT:?repository directory containing config and history}
release=${PT_MONITOR_RELEASE:?directory containing python and web release artifacts}
python=${PT_MONITOR_PYTHON:?Python with Monitor runtime dependencies}
private=${CLUSTERX_MONITOR_NOTIFICATIONS_DIR:?private notification directory}
cluster_config=${CLUSTERX_CONFIG:?private Clusterx profile}
state="${PT_MONITOR_STATE:-$project/.clusterx-monitor}"
export PYTHONPATH="$release/python"
mkdir -p "$state"

case "$mode" in
  serve)
    cmd=("$python" -m clusterx_monitor.cli serve
      --clusterx-config "$cluster_config"
      --policy-config "$project/config/resource-policy.local.json"
      --group-config "$project/config/groups.local.yaml"
      --auth-config "$project/config/admin.local.yaml"
      --audit-log "$project/config/admin-audit.local.jsonl"
      --history-db "$state/history.sqlite3"
      --host "${PT_MONITOR_HOST:-0.0.0.0}" --port "${PT_MONITOR_PORT:-8765}"
      --allowed-host "${PT_MONITOR_ALLOWED_HOST:?browser-facing hostname or IP}"
      --static-dir "$release/web" --notifications-dir "$private")
    ;;
  notifications)
    cmd=("$python" -m clusterx_monitor.notifications.worker run
      --clusterx-config "$cluster_config" --notifications-dir "$private")
    ;;
  *) exit 2 ;;
esac

exec >>"$state/$mode.log" 2>&1
child=
stop() {
  if [[ -n "$child" ]]; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" || true
  fi
  exit
}
trap stop HUP INT TERM
while true; do
  "${cmd[@]}" &
  child=$!
  wait "$child" || true
  child=
  sleep 4
done
