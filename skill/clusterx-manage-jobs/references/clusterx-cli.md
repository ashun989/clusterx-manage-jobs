# Clusterx CLI reference

Source snapshot: Feishu document revision 53, Clusterx 2026.8.11. Options noted
below were cross-checked against the installed CLI's dynamic help.

This is a sanitized operational reference. Prefer the installed CLI's dynamic
help when it differs from this snapshot.

## Installation and configuration

Clusterx currently runs from a cluster development machine. The source
document distributes a wheel using a signed URL; do not reuse or persist that
URL. Obtain a current wheel or internal package source, preferably install it
in an isolated environment, and verify:

```bash
clusterx --version
clusterx --help
```

The first invocation starts configuration. Show configuration with:

```bash
clusterx config --show
```

The default configuration path is:

```text
~/.config/clusterx.yaml
```

Required configuration fields in the snapshot:

| Field | Purpose |
| --- | --- |
| `cluster_type` | Platform cluster type; the snapshot uses SSP |
| `subscription` | Subscription ID |
| `resource_group` | Resource group |
| `region` | Region |
| `workspace` | Workspace |
| `cluster` | Cluster |
| `ak_id` | Account access-key ID |
| `ak_secret` | Account secret |

Conditional or optional fields:

| Field | Purpose |
| --- | --- |
| `rdma_name` | RDMA network; confirm against the selected cluster |
| `queue` | Preferred default target queue |
| `partition` | Deprecated compatibility alias for `queue` |
| `storage_ak_id` | Separate storage account access-key ID |
| `storage_ak_secret` | Separate storage account secret |
| `image` | Default container image |
| `tmpdir` | Shared directory used for generated job command scripts |
| `mount` | Default volume mount |

The `tmpdir` must be mounted at the same path in both the development machine
and the submitted job. Clusterx writes a temporary launch script there and the
job starts by reading that script.

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

Always inspect live help before constructing a command:

```bash
clusterx <command> --help
```

## `clusterx run`

The positional argument is the command to run. Snapshot options include:

| Option | Meaning | Snapshot default |
| --- | --- | --- |
| `--job-name`, `-J` | Job name | `clusterx-root` |
| `--num-nodes`, `-N` | Node count | `1` |
| `--gpus-per-task` | GPUs per task | `0` |
| `--cpus-per-task` | CPUs per task | `4` |
| `--memory-per-task` | Memory in GiB | `10` |
| `--include` | Comma-separated hostnames to include | unset |
| `--exclude` | Comma-separated hostnames to exclude | unset |
| `--priority` | `1=NORMAL`, `2=HIGH`, `3=HIGHEST` | `1` |
| `--retry` | Maximum retries | no retry |
| `--no-env` | Do not export the current shell environment | `False` |
| `--environment`, `-e` | SSP job-spec `KEY=VALUE`; repeat for multiple values | unset |
| `--queue`, `--partition`, `-q`, `-p` | Queue | config/default |
| `--cluster-name`, `-C` | Override configured cluster | config |
| `--machine-type` | Machine specification | unset |
| `--enable-privileged` | Host-root privileged mode | `False` |
| `--sp-block` | A3 logical supernode chip count | unset |
| `--image` | Container image URL | config/default |
| `--mount`, `--empty-mount` | Repeatable volume mount specification | config/default |
| `--shm-size-gib` | Shared memory in GiB | `64` |
| `--storage-ak-id` | Storage access-key ID | config/default |
| `--storage-ak-secret` | Storage access-key secret | config/default |

Example shape:

```bash
clusterx run \
  -J <job-name> \
  -q <queue> \
  --image <registry/image:tag> \
  --mount '<mount-spec>' \
  -e MAX_STEPS=3 \
  bash /absolute/path/to/runner.sh
```

Do not copy example credentials, signed URLs, queue names, images, resource IDs,
or endpoints from documentation into a real job.

Clusterx 2026.8.11 converts the positional command tokens to a string with
`" ".join(cmd)` before writing its shared launch script. It does not shell-quote
individual arguments, so command-string forms such as `bash -c`, `bash -lc`,
`sh -c`, and their absolute-path equivalents lose the command boundary and can
fail before the runner starts. The Skill wrapper rejects these forms. Put
compound setup in a reviewable runner script, invoke that script by absolute
path, and pass environment settings with repeated `-e KEY=VALUE` options.

### A3 logical supernodes

`--sp-block S` requests an A3 Ascend logical supernode. Use it only when the
selected cluster and queue provide A3 resources. Let `N` be chips per node and
`M` be the node count:

- `S`, `N`, and `M` must be positive integers.
- For one node, `S` must equal `N`.
- For multiple nodes, `S` must be a multiple of `N` and divide `N × M`.

Do not add `--sp-block` to ordinary GPU jobs or infer A3 availability from the
option merely appearing in dynamic help.

## SSP Prometheus statistics

Clusterx 2026.8.11 provides queue- and job-level SSP metrics:

```bash
clusterx stats --scope queue --metric all
clusterx stats --scope job --job <exact-job-name> --metric all
```

The supported scopes are `workspace`, `cluster`, `queue`, and `job`.
`--minutes` selects the positive lookback window and defaults to `5`;
`--step` defaults to `30` seconds and is reserved for range queries. Job scope
requires `--job`. The metric set depends on scope; inspect `stats --help`
before querying. Current metrics include CPU and memory utilization, GPU count,
utilization, memory utilization, total/per-device power, memory bandwidth
utilization, temperature, and `all`.

## Queue packing analysis

Use the Skill's read-only queue analyzer when aggregate free GPUs exist but a
full-node job cannot schedule:

```bash
python3 scripts/queue_plan.py --cwd <project> --nodes 2
```

The command joins the queue node inventory with the same per-node Pod workload
API used by the SSP console. It attributes training jobs, development instances
(`aid`), inference workloads, users, workspaces, and requested resources. It
reports occupied nodes plus independently ranked candidates for minimum
coordinated GPUs, workloads, and users. It never calls a stop API. Pass
`--cpus-per-node` and `--memory-per-node-gib` to include those requested
resources; otherwise the conclusion is GPU-only. Default output is a terminal
report; use `--json` for JSON stdout or `--out <path>` to save schema version 2.
The default terminal report uses Rich colored tables when `requirements.txt`
is installed and falls back to plain text otherwise.
The Rich report is grouped into queue overview, search diagnostics, fragmented
node occupancy, and per-strategy plan cards. Each plan keeps separate workload
and placement tables; node, workload, type, CPU, memory, and GPU details are folded to the
terminal width rather than truncated. Plain-text fallback carries the same
search, workload, freed-node, and placement information.
The opening overview also groups Pod-attributed workloads and allocated GPU,
CPU, and memory by user. Per-user GPU compute and memory utilization is averaged
over the user's reported cards and includes the observed range and telemetry
coverage. Unattributed node resources remain separate and are never assigned to
a user.
Complete options:

| Option | Meaning | Default / validation |
| --- | --- | --- |
| `--nodes` | Required schedulable node count | Required, positive integer |
| `--gpus-per-node` | GPU requirement per target node | `8`, positive integer |
| `--cpus-per-node` | Optional CPU requirement per target node | Unset, positive integer when supplied |
| `--memory-per-node-gib` | Optional memory requirement per target node | Unset, positive integer when supplied |
| `--queue`, `-q` | Queue override | Selected Clusterx config |
| `--cluster-name` | Cluster override | Selected Clusterx config |
| `--config` | Explicit protected Clusterx YAML | Config discovery when omitted |
| `--cwd` | Config discovery starting directory | Current directory |
| `--minutes` | Prometheus lookback window only | `5`, positive integer |
| `--strategy` | `all`, `min-gpu`, `min-workloads`, `min-users`; `min-jobs` is a compatibility alias | `min-gpu` |
| `--candidate-scope` | `fragmented`, `full`, or `all` occupied nodes | `fragmented` |
| `--alternatives` | Maximum ranked plans per strategy, including rank 1 | `1`, integer from `1` through `10` |
| `--search-seconds` | Local solver time budget | `10`, positive number |
| `--show-gpu-details` | Expand a deduplicated per-GPU terminal table | Disabled; JSON always includes per-GPU data |
| `--refresh-seconds` | Fixed interval for serialized full-query refreshes | Unset; positive number when supplied |
| `--json` | Write schema-versioned JSON to stdout instead of terminal UI | Disabled |
| `--out` | Also save the complete JSON report | Unset |

`--candidate-scope` controls which occupied nodes may contribute workloads to a
plan. It defaults to `fragmented`, preserving fragment-only analysis. Use
`full` for GPU-saturated nodes or `all` to compare both populations in one
optimization. A full node may be occupied by one workload or shared by multiple
workloads. Workloads remain indivisible: selecting any placement charges the
workload's total GPU allocation and evaluates every node released by it.

`--strategy` defaults to `min-gpu`, and `--alternatives` defaults to `1` while
accepting `1` through `10`. It returns that many ranked, distinct workload sets
per selected strategy. Explicit `--strategy all` groups results independently
under `min-gpu`, `min-workloads`, and `min-users` without cross-strategy deduplication,
so `--strategy all --alternatives 3` can display up to nine plan cards.
The same workload set may appear in more than one strategy group with a separate
rank. Exact ranks use `Minimum` / `Alternative`;
heuristic ranks use `Lowest found` / `Alternative found`.

`--search-seconds` defaults to `10` and bounds local solving only. Exact search
measures the first 1,000 states, estimates whether enumeration fits within 80%
of the budget, and reserves 20% for heuristic fallback. JSON analysis reports
the budget, elapsed time, estimated and examined states, and switch reason.

`--refresh-seconds` enables a fixed-rate monitor while preserving complete
snapshot collection and consistency checks. Queries never overlap: if a full
collection and render crosses one or more scheduled ticks, those ticks are
skipped and the next future deadline is used. Interactive Rich terminals
automatically use an alternate-screen live dashboard: the previous complete
snapshot remains visible while the next query runs, new results replace it in
place, and the absolute scroll position is preserved. Use Up/Down or the mouse
wheel for line scrolling, Page Up/Down for page scrolling, Home/End for bounds,
and `q` or Ctrl-C to exit. A fixed footer shows collection state and visible
line range. Unsupported keys, controls, and pasted text are consumed without
echo or shell input leakage. Mouse tracking may require Shift for terminal text
selection. On exit, input and mouse modes are restored and only the last
complete report is printed in full. Full-screen mode requires Rich plus TTY
stdin and stdout; other output appends labeled plain-text snapshots. With
`--json`, each refresh is one compact NDJSON record. `--out` is overwritten with
the latest complete, pretty-printed report after every successful refresh.

Only workloads placed on nodes selected by `--candidate-scope` are candidates.
Exact search is controlled by the measured time budget rather than a fixed
state threshold. Larger searches use deterministic, multi-start job/node
heuristics followed by redundant-job pruning. Exact plans
are labeled `Minimum`; heuristic plans are labeled `Lowest found` and do not
claim global optimality. Suggestions expose `strategy`, `rank`, `primary_cost`,
and `delta_from_best`.

Workload rows show the `--minutes` window's per-GPU compute and memory
utilization as the card average plus the minimum-to-maximum range. The
fragmented-node table aggregates only the workload's cards on that node; plan
tables aggregate the complete workload. `--show-gpu-details` adds one
deduplicated terminal table for all currently attributed GPUs, ordered by
workload, node, and device index. Utilization is observational only and never
changes capacity attribution, candidate ranking, or stop authorization.

Schema version 2 has this top-level shape (values abbreviated):

```json
{
  "schema_version": 2,
  "generated_at": "<UTC ISO-8601>",
  "queue": "<queue>",
  "cluster": "<cluster>",
  "target": {
    "nodes": 4,
    "gpus_per_node": 8,
    "cpus_per_node": null,
    "memory_per_node_gib": null
  },
  "summary": {
    "total_nodes": 56,
    "total_gpu": 448,
    "allocated_gpu": 420,
    "free_gpu": 28,
    "currently_schedulable_nodes": 0,
    "fragmented_nodes": 5,
    "full_nodes": 50,
    "running_workloads": 48,
    "workload_counts": {"trainingJob": 40, "aid": 8}
  },
  "user_summaries": [{
    "user": "<user>",
    "workload_count": 4,
    "workload_counts": {"aid": 3, "trainingJob": 1},
    "allocated_gpu": 5,
    "allocated_cpu": 64,
    "allocated_memory_gib": 256.0,
    "gpu_utilization": {
      "allocated_gpu_count": 5,
      "reported_gpu_count": 4,
      "gpu_compute_util_avg_pct": 53.4,
      "gpu_compute_util_min_pct": 41.2,
      "gpu_compute_util_max_pct": 65.6,
      "gpu_memory_util_avg_pct": 72.3,
      "gpu_memory_util_min_pct": 70.1,
      "gpu_memory_util_max_pct": 74.5
    }
  }],
  "analysis": {
    "needs_repacking": true,
    "resource_scope": "gpu",
    "optimality": "heuristic",
    "candidate_scope": "all",
    "search_budget_seconds": 10.0,
    "search_elapsed_seconds": 0.84,
    "estimated_states": 368830,
    "states_examined": 10270,
    "switch_reason": "estimated-time",
    "requested_strategy": "min-gpu"
  },
  "gpu_utilization": {
    "window_minutes": 5,
    "allocated_gpu_count": 420,
    "reported_gpu_count": 420,
    "workloads": [{
      "workload_id": "<workload-id>",
      "workload_name": "<workload-name>",
      "type": "aid",
      "user": "<user>",
      "workspace": "<workspace>",
      "create_time": "<UTC ISO-8601 or null>",
      "runtime_seconds": 184020,
      "total_gpu": 2,
      "allocated_gpu_count": 2,
      "reported_gpu_count": 2,
      "gpu_compute_util_avg_pct": 53.4,
      "gpu_compute_util_min_pct": 41.2,
      "gpu_compute_util_max_pct": 65.6,
      "gpu_memory_util_avg_pct": 72.3,
      "gpu_memory_util_min_pct": 70.1,
      "gpu_memory_util_max_pct": 74.5,
      "gpus": [{
        "node": "<node>",
        "pod": "<pod>",
        "device_index": "0",
        "gpu_uuid": "<GPU UUID>",
        "gpu_compute_util_pct": 41.2,
        "gpu_memory_util_pct": 70.1
      }]
    }]
  },
  "fragmented_nodes": [{
    "node": "<node>",
    "allocated_gpu": 7,
    "total_gpu": 8,
    "free_gpu": 1,
    "workloads": [{
      "workload_id": "<workload-id>",
      "workload_name": "<workload-name>",
      "type": "aid",
      "actionable": true,
      "user": "<user>",
      "workspace": "<workspace>",
      "create_time": "<UTC ISO-8601 or null>",
      "runtime_seconds": 184020,
      "gpu_utilization": {
        "allocated_gpu_count": 1,
        "reported_gpu_count": 1,
        "gpu_compute_util_avg_pct": 41.2,
        "gpu_compute_util_min_pct": 41.2,
        "gpu_compute_util_max_pct": 41.2,
        "gpu_memory_util_avg_pct": 70.1,
        "gpu_memory_util_min_pct": 70.1,
        "gpu_memory_util_max_pct": 70.1
      },
      "gpu": 1,
      "cpu": 8,
      "memory_gib": 32.0
    }],
    "unattributed": {"gpu": 0, "cpu": 0, "memory_gib": 0},
    "attribution_excess": {"gpu": 0, "cpu": 0, "memory_gib": 0},
    "metrics": {"gpu-util": 53.4}
  }],
  "suggestions": [{
    "strategy": "min-gpu",
    "rank": 1,
    "primary_cost": 32,
    "delta_from_best": 0,
    "optimality": "heuristic",
    "target_nodes": ["<node>"],
    "freed_nodes": ["<node>"],
    "workloads": ["<workload-id>"],
    "gpus": 32,
    "workload_count": 1,
    "users": 1,
    "workload_details": [{
      "workload_id": "<workload-id>",
      "workload_name": "<workload-name>",
      "type": "trainingJob",
      "actionable": true,
      "user": "<user>",
      "workspace": "<workspace>",
      "create_time": "<UTC ISO-8601 or null>",
      "runtime_seconds": 184020,
      "gpu_utilization": {
        "allocated_gpu_count": 32,
        "reported_gpu_count": 32,
        "gpu_compute_util_avg_pct": 53.4,
        "gpu_compute_util_min_pct": 41.2,
        "gpu_compute_util_max_pct": 65.6,
        "gpu_memory_util_avg_pct": 72.3,
        "gpu_memory_util_min_pct": 70.1,
        "gpu_memory_util_max_pct": 74.5
      },
      "total_gpu": 32,
      "placements": [{
        "node": "<node>",
        "gpu": 8,
        "cpu": 64,
        "memory_gib": 800.0
      }]
    }]
  }],
  "warnings": []
}
```

`switch_reason` is `completed` after full exact enumeration,
`estimated-time` when measured throughput predicts that exact enumeration will
exceed 80% of the budget, `exact-deadline` when exact enumeration reaches that
deadline, `not-needed` when enough nodes are already schedulable, or
`no-eligible-candidates` when the selected scope contains no candidate nodes.
Missing Pod mappings, truncated node inventory, or a changing node-allocation
snapshot cause a safe failure rather than a partial suggestion. Node allocation
is the capacity source of truth. Positive allocation-to-Pod differences are
reported as unattributed and never claimed as releasable. If Pod attribution
exceeds node allocation, the affected node is excluded for that resource and a
warning is emitted.

The terminal workload summaries show `Running` as a compact reference duration
(`25m`, `3h 07m`, or `4d 03h`). It is calculated once per report from the
earliest valid Pod `create_time` for the workload to top-level `generated_at`;
the JSON form exposes both `create_time` and integer `runtime_seconds`. Missing,
invalid, or future timestamps produce JSON `null` values and `-` in terminal
output. This is observational metadata only and does not affect packing results.

Per-GPU utilization is fetched in one additional Prometheus request containing
both compute and memory metrics. Series are accepted only when workload UID,
Pod, and Hostname match the current allocation snapshot. Missing or non-finite
values produce JSON `null` and terminal `-`; stale series are ignored, and an
over-complete ambiguous Pod mapping is omitted with a warning. The top-level
coverage counts refer to Pod-attributed GPUs, so they may be lower than node
allocation when the report already identifies unattributed resources.

Configuration discovery starts from the current directory when `--cwd` is
omitted. Exit status `0` means the report completed (including "no suggestion"
or "no pause needed"), `1` means live analysis failed or the snapshot was not
trustworthy, and `2` means arguments or protected configuration are invalid.

## Storage mounts

Simple file-storage form:

```text
TYPE:ID:MOUNT_PATH[:SUBDIR]
```

Example shape:

```text
PV_AFS:<volume-id>:/data
```

Multiple mounts are comma-separated in one `--mount` value.

Complex object-storage mounts can be passed as one-line JSON. Common fields:

```json
{
  "type": "PV_AOSS",
  "name": "<volume-name>",
  "mount_path": "/oss/data",
  "endpoint": "<internal-endpoint>",
  "subdir": "/",
  "metadata": {
    "items": [
      {"key": "access_key", "value": "<secret>"},
      {"key": "secret_key", "value": "<secret>"}
    ]
  }
}
```

Validate JSON before submitting. Keep the JSON inside single quotes so the
shell passes it as one argument. Never print its secret values in a preview.
When possible, prefer protected configuration fields over inline credentials.

## Submission checks

Before `run`, verify:

- The job and development machine mount the same storage needed for code,
  datasets, outputs, and `tmpdir`.
- The target queue, cluster, machine type, RDMA name, and image are current.
- CPU, memory, GPU, node count, retry, priority, and privileged mode match the
  user's request.
- The job invokes an absolute runner script and does not use a shell
  command-string mode such as `bash -c` or `bash -lc`.
- The preview is redacted and the user explicitly requested submission. Do not
  ask for a redundant confirmation when the command matches that request.

Expected result includes a job schema/status such as `Queuing`. Record the job
ID for later `get-job`, `log`, `stats`, or `stop` operations.

## Job details and Workers

For SSP runtime Worker discovery:

```bash
clusterx get-job <job-id> --workers
```

Live help provides `--page-size` (1-100), `--page-token`, `--skip`,
`--request-id`, `--filter`, and `--order-by`. Worker filters support `name`,
`phase`, `pod_ip`, and `host_ip`; ordering supports `name` or `phase` with an
optional `asc`/`desc`. Keep pagination tokens and infrastructure addresses out
of reports unless the user needs them.

## Stopping jobs

Clusterx 2026.8.11 can stop one job by ID or select multiple jobs using regex,
group, user, partition, and status filters. Batch stopping is destructive: use
filters only when the user explicitly requests that exact batch scope, list and
show the resolved matches first, and do not broaden an exact-job request into a
filter. Use the CLI's confirmation option as required by live help.

## SSP log limitation

With Clusterx 2026.8.11, `clusterx log <job-id>` fetches
`trainingJobs/<job-id>/pods` before reading pod logs. This endpoint may return
HTTP 404 both while an SSP job is `Running` and after `get-job` reports
`Succeeded`, even when the workload flushes stdout. Keeping the task alive does
not reliably avoid the failure.

Report job status and log retrieval as separate results. Empty `nodes` and
`nodes_ip` fields and incomplete SSP support in `get-node` may prevent a
node-based fallback. Never invent missing logs or node details. For workloads
that require observable progress, write a sanitized status or result file to
approved shared storage and clearly identify it as a fallback rather than
stdout/stderr.

## SSP job-name limit

SSP accepts job names from 1 through 32 Unicode characters. Validate this
before showing the submission preview. A longer name is rejected before job
creation with `invalid TrainingJob.Name`; shorten the name and rebuild the
preview.

## Snapshot change history

| Version | Change |
| --- | --- |
| 2026.6.4 | Added privileged-mode switch |
| 2026.6.9 | Improved cluster-ID configuration and arguments |
| 2026.6.11 | Distinguished D/PT clusters and endpoints |
| 2026.6.12 | Added SSP GPU utilization, memory, power, and temperature stats |
| 2026.7.1 | Added JSON volume mounts for PV_AOSS and other complex volumes |
| 2026.7.28 | Added queue/job SSP metrics and A3 `--sp-block` scheduling |
| 2026.8.11 | Added SSP Worker listing/filtering, repeatable mounts, explicit environment semantics, and batch stop filters |
