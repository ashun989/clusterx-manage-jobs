# Clusterx CLI Reference

Source snapshot: Feishu document revision 77, Clusterx `2026.8.19`. This is a
sanitized operational reference cross-checked against installed dynamic help.
Always prefer the installed CLI when it differs from this snapshot.

```bash
clusterx --version
clusterx <command> --help
```

## Installation and configuration

Clusterx runs from a Linux cluster development machine. The authoritative
Feishu document may contain a temporary signed wheel URL; never copy, persist,
or publish that URL. Maintainers use the repository-only installation helper,
while installed Skill users obtain Clusterx through the current internal
distribution channel.

The first invocation initializes configuration. Inspect it with
`clusterx config --show`. The native configuration path is
`~/.config/clusterx.yaml`; required SSP fields include subscription, resource
group, region, workspace, cluster, access-key ID and secret. Queue, image,
RDMA, storage credentials, `tmpdir`, and mounts are conditional or optional.
The selected file and symlink target must have mode `600`.

The `tmpdir` must be mounted at the same absolute path on the development
machine and submitted task because Clusterx writes its generated launch script
there.

## Command map

```text
clusterx config
clusterx get-job
clusterx get-node
clusterx list
clusterx log
clusterx run
clusterx stats
clusterx stop
```

Run Clusterx through `clusterx-exec` so configuration precedence is
preserved and stdout/stderr are redacted.

## `clusterx run`

The positional argument is the command to run. The verified option surface is:

| Option | Meaning | Snapshot default |
| --- | --- | --- |
| `--job-name`, `-J` | SSP job name | `clusterx-root` |
| `--num-nodes`, `-N` | Node count | `1` |
| `--gpus-per-task` | GPUs per task/node | `0` |
| `--cpus-per-task` | CPUs per task/node | `4` |
| `--memory-per-task` | Memory per task in GB | `10` |
| `--include`, `--exclude` | Hostname placement filters | unset |
| `--priority` | `1=NORMAL`, `2=HIGH`, `3=HIGHEST` | `1` |
| `--retry` | Maximum retries | no retry |
| `--no-env` | Do not inherit the current environment | `False` |
| `--environment`, `-e` | Repeatable SSP `KEY=VALUE` setting | unset |
| `--queue`, `--partition`, `-q`, `-p` | Queue | config/default |
| `--cluster-name`, `-C` | Cluster override | config |
| `--machine-type` | Machine specification | unset |
| `--enable-privileged` | Host-root privileged mode | `False` |
| `--sp-block` | A3 logical supernode chip count | unset |
| `--image` | Container image | config/default |
| `--mount`, `--empty-mount` | Repeatable volume mount | config/default |
| `--shm-size-gib` | Shared memory | `64` |

When `CLUSTERX_MONITOR_URL` is configured, the Skill wrapper performs an
advisory group-node check before `run`. Pass `--cluster-user <name>` before the
wrapper's `--`, set `CLUSTERX_USER`, or configure the protected local identity
mapping. Explicit `--cluster-user` wins over the environment and mapping. The
name is the caller-supplied Clusterx identity and is never inferred from
`$USER`.
If the Monitor reports node allocation disabled, the wrapper skips node
recommendations and treats the queue as fully accessible. The wrapper never
rewrites `--include` or `--exclude`.
| `--storage-ak-id`, `--storage-ak-secret` | Storage credentials | config/default |

Clusterx `2026.8.19` joins positional command tokens without shell quoting.
Never submit `bash -c`, `bash -lc`, `sh -c`, or equivalent command-string forms.
Invoke an absolute runner script and pass settings through repeated
`-e KEY=VALUE` arguments.

SSP job names must contain 1–32 Unicode characters. Do not add privileged mode
unless the user explicitly requested it. Validate and redact mount JSON before
showing a preview.

When the user explicitly requested submission and the preview matches that
request, execute it directly. Do not ask for a redundant confirmation.

### Training CPU policy

`clusterx-exec` hard-validates the public policy before invoking
`clusterx run`. For a GPU task, the inclusive per-task limit is:

```text
cpus_per_task <= gpus_per_task * cpu_per_gpu
```

For a 0-GPU task it is:

```text
cpus_per_task <= zero_gpu_max_cpu_per_node
```

The shipped policy sets both factors to 14, so 0 GPU / 14 CPU, 1 GPU / 14 CPU,
and 8 GPU / 112 CPU are allowed. Node count never multiplies the per-task
limit. The wrapper option `--resource-policy` overrides
`CLUSTERX_RESOURCE_POLICY`, which overrides `assets/resource-policy.json`.

### A3 logical supernodes

Use `--sp-block S` only when the selected cluster and queue provide A3
resources. Let `N` be chips per node and `M` be node count: all values must be
positive; for one node `S=N`; for multiple nodes `N` divides `S` and `S`
divides `N*M`.

## SSP node statistics and pagination

With no `--scope`, `--metric`, or `--job`, `stats` queries one page of nodes
bound to the configured or explicitly selected queue:

```bash
clusterx stats --page-size 100
clusterx stats --page-size 100 --page-token <next-page-token>
```

`--page-size` accepts 1–100 and defaults to 100. Pagination is manual: the
first response prints `total_size` and, when another page exists, an opaque
`next_page_token`. Pass that exact cursor through `--page-token`; do not expose
it in reports. Clusterx does not automatically retrieve subsequent pages.

## SSP Prometheus statistics

```bash
clusterx stats --scope queue --metric all
clusterx stats --scope job --job <exact-job-name> --metric all
```

Supported scopes are workspace, cluster, queue, and job. `--minutes` selects a
positive lookback and defaults to 5. Depending on scope, metrics include CPU,
memory, GPU count/utilization/memory, total and per-device power, bandwidth,
and temperature. Inspect live `stats --help` before constructing a query.
Node-list pagination options do not change Prometheus queries.

## Monitoring service client

Queue monitoring and scheduling simulation are additive capabilities that are
read-only against Clusterx. Authenticated administration writes only local
policy files. The client never replaces Clusterx job lifecycle commands:

```bash
clusterx-monitor-cli status --format json
clusterx-monitor-cli overview --format json
clusterx-monitor-cli users --violations-only --format json
clusterx-monitor-cli groups --format json
clusterx-monitor-cli nodes --classification fragmented --format json
clusterx-monitor-cli workloads --status pending --priority high --format json
clusterx-monitor-cli alerts \
  --finding-category utilization \
  --finding-code utilization.low_gpu_activity \
  --tag low-utilization --format json

Node ownership is public to all anonymous Monitor viewers. To resolve a
caller's effective group pool, provide `--user <cluster-user>` or configure
`CLUSTERX_USER`:

```bash
clusterx-monitor-cli nodes --mine --format json
```

The CLI does not use the local `$USER`. With allocation disabled, the access
response states that all queue
nodes are available. Group borrowing remains quota-counted and only adds the
corresponding advisory placement finding. Pending workloads cannot be assigned
to nodes and never receive placement findings.
```

Scheduling simulation uses an identified cached snapshot:

```bash
clusterx-monitor-cli plan \
  --nodes 2 --gpus-per-node 8 \
  --strategy min-gpu --strategy min-workloads --strategy min-users \
  --candidate-scope all --alternatives 3 --search-seconds 10 \
  --finding-category utilization \
  --finding-code utilization.low_gpu_activity \
  --finding-tag low-utilization \
  --format json
```

Candidate filters include repeated `--type`, `--group`, `--user`, `--workload`,
`--exclude-workload`, `--exclude-user`, `--finding-category`,
`--finding-code`, `--finding-tag`, and `--over-quota-only`. Plan node
candidate scope is controlled independently with
`--candidate-node-scope all|selected_group_nodes|other_group_nodes`; the two
group-relative values require selected repeated `--group` filters. Workload
placement scope is controlled with
`--placement-relation any|owned_only|foreign_only|mixed|includes_foreign`.
List views use
comma-separated `--finding-category`, `--finding-code`, and `--tag` values.
`--violations-only` and `--fail-on violation` read structured findings rather
than parsing display messages. Suggestions are
coordination candidates only. The monitor and CLI contain no stop operation.

Workload snapshots and table output include `resource_create_time` and
`priority`. For Pending TrainingJobs, `status.create_time` is both the resource
creation time and the initial queue-age anchor used by the monitor; it does not
represent a later retry/requeue transition. Priorities are normalized to
`NORMAL`, `HIGH`, or `HIGHEST` when Clusterx returns `spec.priority`.
`workloads --priority` filters these values case-insensitively. Resources whose
API response omits priority remain null instead of inheriting an assumed
default.

The simulator uses an integer OR-Tools CP-SAT model instead of workload-subset
enumeration. `--search-seconds` is the total budget shared fairly by the
requested strategies. Each strategy reports `OPTIMAL`, `FEASIBLE`,
`INFEASIBLE`, or `UNKNOWN`, its termination reason, objective/bound data, and
whether the requested Top-K alternatives are complete. A feasible solution is
independently recomputed against the pinned snapshot before it is returned;
the deterministic greedy path is only a verified timeout fallback and is
always labeled heuristic. `candidate-scope` limits the nodes that may satisfy
the requested target, including for workloads spanning multiple nodes.
`candidate-node-scope` further intersects that range with effective public node
ownership. `placement-scope` filters the release workload domain by its
placement relationship to its group. If node allocation is disabled, both
effective ownership restrictions are unrestricted.

The Web workload drawer lets any Monitor viewer fetch a bounded realtime
training log preview after explicitly selecting a Worker and clicking the load
action. Merely loading the dashboard or opening workload details does not call
Clusterx log APIs. Log content is returned with `Cache-Control: no-store`, is
not added to snapshots or SSE, and remains separate from the full `clusterx
log` workflow. Realtime Clusterx logs have no page cursor, so the Web UI fetches
the latest 200 lines once and paginates them locally, defaulting to 20 lines on
the last page while preserving the selected page across refreshes.

The monitor consumes Clusterx 2026.8.19 node cursors until the complete bound
inventory is present. It rejects cursor cycles, duplicate nodes, page-total
drift, premature termination, and any allocation change between the complete
before/after inventories, so a partial or mixed-time node list is never
published as a snapshot.

When CPU or memory is omitted, the service resolves it from the planning
profile stored in that exact snapshot (by default 14 CPU and 240 GiB per GPU).
Explicit CPU/memory overrides the profile. The response exposes both requested
and resolved targets plus the defaults applied. This planning profile is not a
minimum submission rule: training CPU/memory ratios remain inclusive maxima.
Node `effective_free_gpu`, `stranded_gpu`, and `cpu-memory-blocked` are likewise
relative to the standard profile, so a smaller explicit request may still fit.

Nodes whose Pod-attributed resources exceed their reported allocation remain
visible for monitoring but are excluded from planning, together with every
workload placed on them. Plan results report exclusion counts and reasons.
Unknown-owner/unattributed resources are never release candidates.

For bounded monitoring use:

```bash
clusterx-monitor-cli watch --view alerts --count 10 --format jsonl
```

Install `clusterx-monitor-cli` before using these commands. The client resolves its endpoint from `--endpoint`, then
`CLUSTERX_MONITOR_URL`, then the backwards-compatible default
`http://127.0.0.1:8765`. The service may be shared from another development
machine. It never falls back to a live Clusterx query. Exit status `0` means
success, `2` invalid input, `3` service unavailable, `4` a `--fail-on` condition,
and `130` interruption.
HTTP `422` plan validation failures map to exit `2`. `--fail-on stale` evaluates
the original snapshot freshness even for filtered list views; `--fail-on` is
only offered on commands where stale or violation has a defined meaning.

GPU compute, memory, and power telemetry are observational and include coverage
counts. They never change capacity attribution or default plan ranking.
Unattributed resources remain visible but are never claimed as releasable.

The historical low-activity rule is independent of pending pressure. Prometheus
is queried every 5 minutes for a 24-hour window, grouped by workload UID across
Pod restarts and node movement. Running GPU `trainingJob`, `aid`, and `air`
workloads qualify after 60 minutes. Compute <=20% OR memory <=20% triggers the
rule when both metrics exist. Independently, enabled historical average power
at or below `gpu_power_threshold_pct` percent of `gpu_power_limit_w` triggers it.
Power is sum of valid power samples divided by sample count; missing data is
never zero. The shipped reference is 400 W and threshold 25% (100 W/card).
Both are administrator-editable; this reference does not change hardware power
limits. Older configurations omit the threshold and keep power checking disabled;
explicit null also disables it. Missing power does not block compute/memory,
and missing compute/memory does not block power. Partial evaluation is `partial`;
zero-GPU workloads are `not-applicable`, newer workloads are `warming-up`, and
no evaluable conditions means `unavailable`. Findings identify triggered metrics.
Snapshot historical telemetry includes `gpu_power_avg_w`, `power_sample_count`,
`gpu_power_util_avg_pct` and `gpu_power_limit_w`; the percentage is recalculated
from cached watts when configuration changes. Sample count is not GPU coverage.
Query failures leave ordinary snapshots available with a warning. Completed
workloads are not evaluated; SQLite retains cluster-level trends only.


### Monitor administrator configuration

Initialize the single administrator without placing a password in argv or a
shell environment variable:

```bash
clusterx-monitor admin init \
  --auth-config config/admin.local.yaml \
  --username clusterx-admin
```

The command prompts twice with hidden input and stores only an Argon2id hash in
a mode-`600`, Git-ignored file. `--force` explicitly rotates an existing
credential and invalidates active sessions when the service observes the new
hash. Serve with local writable policy paths and the protected auth file:

```bash
clusterx-monitor serve \
  --clusterx-config <protected-clusterx-config> \
  --policy-config config/resource-policy.local.json \
  --group-config config/groups.local.yaml \
  --auth-config config/admin.local.yaml \
  --history-db ~/.clusterx-monitor/history.sqlite3 \
  --history-retention-days 30 \
  --host 127.0.0.1 --port 8765
```

The Web UI encodes the active view, table query/filter/sort, trend range, and
detail stack in the URL. Problem details can prefill an editable scheduling
simulation; an exact node uses the backend `target_nodes` constraint. Aggregate
trend points persist in SQLite with WAL and bounded time/count/size retention.
They contain capacity, pending, telemetry, alert-count, and node-classification
metrics only—never workload, user/member, or log content. Configure the bounds
with `--history-retention-days`, `--history-max-points`, and
`--history-max-db-mib`.

The administrator UI saves each resource/group file independently after schema
validation and revision matching. Writes use a same-directory temporary file,
`fsync`, atomic replacement, mode `600`, a protected `.bak`, and a redacted
audit record. Missing policy files put the service in `setup-required`; saving
both valid configurations starts collection without restarting.
If a file is malformed, an authenticated administrator receives its raw text,
parse error, and disk-byte revision so it can be repaired without restoring a
backup. A hot-reload error keeps the complete last-known-good pair. Audit-log
failure is reported as a persistent degradation after the config commit; it
does not falsely report the already-committed configuration as unsaved.

Authentication and authorization are enforced by the FastAPI service, never by
React. Sessions are random, server-side, memory-only, and carried in an
HttpOnly/SameSite=Strict cookie. Mutations additionally require exact-origin
JSON requests and a per-session CSRF token. Public policy responses never expose
group members. Loopback remains the default. A non-loopback bind is accepted
only with one or more explicit `--allowed-host` values; wildcard hosts are
rejected. This enables an existing controlled NAT, but the built-in server does
not terminate TLS and emits a cleartext-credential warning. Prefer a reviewed
HTTPS reverse proxy for persistent shared access.

To share the Web-managed training CPU rules with job submission without making
CRUD depend on monitor availability, set:

```bash
export CLUSTERX_RESOURCE_POLICY="$PWD/config/resource-policy.local.json"
```

## Query and stop

Use `list`, `get-job`, `get-node`, `log`, and `stats` as read-only operations.
For Worker discovery use `get-job <job-id> --workers` and inspect live `--page-size`
and pagination help. Use `--scope queue` for queue metrics and
`--scope job --job <exact-job-name>` for workload metrics. Queue and job stats
include CPU, memory, GPU utilization, GPU memory,
power, bandwidth, and temperature metrics depending on scope.

Before `stop`, resolve and display the exact job ID/name. Batch filters are only
allowed when the user explicitly requested that exact batch scope. SSP may
return empty `nodes` and `nodes_ip`; do not invent placement information when
these fields are absent.

### Job details and Workers

Use `clusterx get-job <job-id> --workers` for SSP Worker discovery. Live help
provides page size/token, skip, request ID, filter, and ordering controls.
Worker filters cover name, phase, Pod IP, and host IP; do not expose pagination
tokens or infrastructure addresses unless required by the request.

### Stopping jobs

Clusterx can stop an exact job or select jobs with regex, group, user,
partition, and status filters. Batch stopping is destructive: use filters only
for the exact batch scope requested by the user and list resolved matches
first. Never broaden an exact-job request into a filter.

### SSP realtime and historical logs

Clusterx 2026.8.19 no longer discovers every Pod implicitly. Realtime logs
require one exact Worker returned by `get-job --workers`:

```bash
clusterx get-job <job-id> --workers
clusterx log <job-id> --worker <worker-name> --lines 200
```

If multiple Workers exist, select the one requested instead of guessing.
`--streaming` remains unimplemented for SSP and falls back to the current
realtime response.

Passing any of `--msg`, `--start`, `--end`, or `--hours` selects historical
log search. `--worker` is then an optional filter:

```bash
clusterx log <job-id> --hours 6
clusterx log <job-id> \
  --start 2026-08-18T00:00:00Z --end 2026-08-18T12:00:00Z
clusterx log <job-id> --worker <worker-name> --msg error --hours 24
```

Use explicit ISO 8601 offsets or `Z` to avoid timezone ambiguity. Historical
`--page-size` accepts 1–1000 and defaults to 1000; `--max-pages` defaults to 10
and must be at least 1. The monitor data plane returns at most 10,000 rows for
the same query, so narrow the time range or add Worker/message filters when a
result is truncated. An empty historical result does not prove the task did not
run; check `get-job` independently.

### SSP job-name limit

SSP accepts 1–32 Unicode characters. Validate and shorten the name before
preview; longer names fail before creation with `invalid TrainingJob.Name`.

## Storage and safety

Simple file storage uses `TYPE:ID:MOUNT_PATH[:SUBDIR]`; multiple mounts may be
comma-separated. Complex PV_AOSS mounts use one quoted JSON object containing
type, name, endpoint, mount path, subdirectory, and metadata items. Validate
JSON before submission and keep it in one shell argument.

Never display access keys, secret keys, signed URL credentials, tokens, private
keys, or mount secrets. Prefer protected configuration over inline credentials.
Do not infer real quota from `/oss` FUSE capacity.

Before `run`, verify the queue, cluster, machine type, RDMA, image, node/GPU/CPU/
memory resources, retry, priority, mounts, shared `tmpdir`, runner path, and
privileged mode. Redact the preview and capture the returned job ID for later
`get-job`, `log`, `stats`, or `stop` operations.

## Snapshot change history

| Version | Change |
| --- | --- |
| 2026.6.4 | Added privileged-mode switch |
| 2026.6.9 | Improved cluster-ID configuration and arguments |
| 2026.6.11 | Distinguished D/PT clusters and endpoints |
| 2026.6.12 | Added SSP GPU utilization, memory, power, and temperature stats |
| 2026.7.1 | Added JSON mounts for PV_AOSS and complex volumes |
| 2026.7.28 | Added queue/job SSP metrics and A3 `--sp-block` scheduling |
| 2026.8.11 | Added Worker filters, repeatable mounts, explicit environments, and batch stop filters |
| 2026.8.19 | Fixed realtime logs, added historical log search, and added paginated node statistics |
