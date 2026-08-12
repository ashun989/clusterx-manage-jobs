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

The command joins running jobs, users, requested resources, runtime Workers,
and queue nodes. It reports occupied nodes plus independently ranked candidates
for minimum coordinated GPUs, jobs, and users. It never calls a stop API. Pass
`--cpus-per-node` and `--memory-per-node-gib` to include those requested
resources; otherwise the conclusion is GPU-only. Default output is a terminal
report; use `--json` for JSON stdout or `--out <path>` to save schema version 1.
The default terminal report uses Rich colored tables when `requirements.txt`
is installed and falls back to plain text otherwise.
The Rich report is grouped into queue overview, search diagnostics, fragmented
node occupancy, and per-strategy plan cards. Each plan keeps separate job and
placement tables; node, job, CPU, memory, and GPU details are folded to the
terminal width rather than truncated. Plain-text fallback carries the same
search, job, freed-node, and placement information.
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
| `--strategy` | `all`, `min-gpu`, `min-jobs`, or `min-users` | `all` |
| `--candidate-scope` | `fragmented`, `full`, or `all` occupied nodes | `fragmented` |
| `--alternatives` | Maximum ranked plans per strategy, including rank 1 | `3`, integer from `1` through `10` |
| `--search-seconds` | Local solver time budget | `10`, positive number |
| `--json` | Write schema-versioned JSON to stdout instead of terminal UI | Disabled |
| `--out` | Also save the complete JSON report | Unset |

`--candidate-scope` controls which occupied nodes may contribute jobs to a
plan. It defaults to `fragmented`, preserving fragment-only analysis. Use
`full` for GPU-saturated nodes or `all` to compare both populations in one
optimization. A full node may be occupied by one job or shared by multiple
jobs. Jobs remain indivisible: selecting any placement charges the job's total
GPU allocation and evaluates every node released by that job.

`--alternatives` defaults to `3` and accepts `1` through `10`. It returns that
many ranked, distinct job sets per selected strategy; `--strategy all` groups
results independently under `min-gpu`, `min-jobs`, and `min-users` without
cross-strategy deduplication, so the default can display up to nine plan cards.
The same job set may appear in more than one strategy group with a separate
rank. Exact ranks use `Minimum` / `Alternative`;
heuristic ranks use `Lowest found` / `Alternative found`.

`--search-seconds` defaults to `10` and bounds local solving only. Exact search
measures the first 1,000 states, estimates whether enumeration fits within 80%
of the budget, and reserves 20% for heuristic fallback. JSON analysis reports
the budget, elapsed time, estimated and examined states, and switch reason.

Only jobs placed on nodes selected by `--candidate-scope` are candidates.
Exact search is controlled by the measured time budget rather than a fixed
state threshold. Larger searches use deterministic, multi-start job/node
heuristics followed by redundant-job pruning. Exact plans
are labeled `Minimum`; heuristic plans are labeled `Lowest found` and do not
claim global optimality. Suggestions expose `strategy`, `rank`, `primary_cost`,
and `delta_from_best`.

Schema version 1 has this top-level shape (values abbreviated):

```json
{
  "schema_version": 1,
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
    "running_jobs": 48
  },
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
    "requested_strategy": "all"
  },
  "fragmented_nodes": [{
    "node": "<node>",
    "allocated_gpu": 7,
    "total_gpu": 8,
    "free_gpu": 1,
    "jobs": [{
      "job_id": "<job-id>",
      "job_name": "<job-name>",
      "user": "<user>",
      "gpu": 1,
      "cpu": 8,
      "memory_gib": 32.0
    }],
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
    "jobs": ["<job-id>"],
    "gpus": 32,
    "job_count": 1,
    "users": 1,
    "job_details": [{
      "job_id": "<job-id>",
      "job_name": "<job-name>",
      "user": "<user>",
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
Missing Worker mappings, truncated node inventory, allocation mismatches, or a
changing job snapshot cause a safe failure rather than a partial suggestion.
If a node's allocated GPU count is greater than the GPU resources attributable
to visible running Workers, that node is not claimed as fully releasable. This
conservative rule may leave a fragmented node out of every suggestion.

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
