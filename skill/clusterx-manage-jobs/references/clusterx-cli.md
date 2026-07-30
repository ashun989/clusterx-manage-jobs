# Clusterx CLI reference

Source snapshot: Feishu document revision 43, Clusterx 2026.7.28.

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
| `--no-env` | Do not inherit current environment | `False` |
| `--environment`, `-e` | Environment setting | unset |
| `--queue`, `--partition`, `-q`, `-p` | Queue | config/default |
| `--cluster-name`, `-C` | Override configured cluster | config |
| `--machine-type` | Machine specification | unset |
| `--enable-privileged` | Host-root privileged mode | `False` |
| `--sp-block` | A3 logical supernode chip count | unset |
| `--image` | Container image URL | config/default |
| `--mount` | Volume mount specification | config/default |
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

Clusterx 2026.7.28 converts the positional command tokens to a string with
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

Clusterx 2026.7.28 adds queue- and job-level SSP metrics:

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

## SSP log limitation

With Clusterx 2026.7.28, `clusterx log <job-id>` fetches
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
