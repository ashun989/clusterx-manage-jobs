# clusterx-monitor-cli

The user-side Monitor and Clusterx command-line client. Install this package
on a cluster development machine; the Clusterx CLI itself remains an external
runtime dependency. It provides `clusterx-monitor-cli`, `clusterx-exec`,
`clusterx-preflight`, and `clusterx-redact` as independently installable
entrypoints. The Monitor identity for `nodes --mine` and placement checks is
provided by `--user`/`--cluster-user`, then `CLUSTERX_USER`, then the protected
identity mapping; it never falls back to `$USER`.

## Installation

```bash
python3 -m pip install clusterx-monitor-cli
```

The Clusterx CLI remains an external prerequisite and is not installed by this
package.

## Monitor CLI

Set the shared service URL with `--endpoint` or `CLUSTERX_MONITOR_URL`:

```bash
export CLUSTERX_MONITOR_URL=https://monitor.example
export CLUSTERX_USER=<cluster-user>
clusterx-monitor-cli status --format json
clusterx-monitor-cli overview
clusterx-monitor-cli nodes --format json
clusterx-monitor-cli nodes --mine --format json
clusterx-monitor-cli groups --violations-only
clusterx-monitor-cli plan --nodes 2 --gpus-per-node 8 \
  --candidate-scope all --candidate-node-scope other_group_nodes \
  --group research --placement-relation foreign_only --alternatives 3
```

Node ownership is public for the configured queue. `nodes --mine` uses `--user`
when present, otherwise `CLUSTERX_USER`, and must match the Clusterx identity
recorded by Monitor; the CLI never infers it from `$USER`. With allocation
disabled, the endpoint can return all nodes without either value.

If node allocation is disabled, the access response explicitly reports that
the policy is disabled and all queue nodes are available. If it is enabled,
the response returns the effective group pool. `placement.outside_owned_pool`
means the Workload's remaining GPU quota covers its total GPU placed on other
groups' nodes; `placement.quota_borrowed` means the remaining quota is smaller
than that foreign GPU total. Every running foreign placement produces one of
these findings. Placement findings are warnings unless the owner group has
active group-local pending pressure, when they become violations. Unknown
pressure remains a warning with unknown evidence. Pending Workloads do not
receive placement findings. Group rows also expose their `pending_pressure`
state and evidence.

Numeric group GPU quotas are validated as multiples of 8. The default group's
members are derived from the current Monitor user inventory and are not
explicitly assigned by the CLI or configuration.

The plan command keeps the resource candidate range (`fragmented`, `full`, or
`all`) separate from node ownership. `--candidate-node-scope` accepts `all`,
`selected_group_nodes`, or `other_group_nodes`; the latter two require repeated
`--group` filters. `--placement-relation` accepts `any`, `owned_only`,
`foreign_only`, `mixed`, or `includes_foreign`. Plan finding filters use
`--finding-category`, `--finding-code`, and `--finding-tag` and match warnings
as well as violations. These filters use the pinned snapshot's public node
ownership. If node allocation is disabled, non-default ownership filters are
rejected rather than silently broadened.

Monitor CLI only reads the Server API. Service-unavailable and stale-snapshot
conditions are reported through the CLI exit status and never trigger a direct
Clusterx fallback.

## Clusterx wrapper

Use the wrapper for configuration precedence, public resource-policy checks,
node-policy recommendations and secret-safe output:

```bash
clusterx-preflight --cwd <project> --tmpdir <shared-tmpdir>
clusterx-exec --cwd <project> -- list
clusterx-exec --cwd <project> -- get-job <job-id> --workers
clusterx-exec --cwd <project> -- log <job-id> --worker <worker-name>
clusterx-exec --cwd <project> --cluster-user <name> -- run <arguments>
```

For `run`, pass `--cluster-user` before the wrapper's `--`, set `CLUSTERX_USER`,
or configure a protected identity mapping. Explicit `--cluster-user` wins over
the environment and mapping. The wrapper never rewrites explicit
`--include/--exclude`; it only prints advisory warnings when the configured
group-node pool suggests a different placement. When the allocation policy is
disabled, it does not add node restrictions.

Clusterx placement values are lowercase hostnames derived from node IPv4
addresses, such as `host-10-140-62-215`; they are not ECP node names. The
wrapper compares explicit placement values with the `hostname` returned by
Monitor and derives the same value from `host_ip` when talking to an older
Monitor. Missing or invalid IPs are never guessed. `clusterx-monitor-cli nodes`
shows `node`, `hostname`, and `host_ip` together.

## Build and test

```bash
python3 -m build client
pytest -q
```

See [docs/release.md](../docs/release.md) for the combined artifact build.
