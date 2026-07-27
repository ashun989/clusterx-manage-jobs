# Clusterx CLI reference

Source snapshot: `使用 Clusterx CLI 工具提交训练任务至PT集群.pdf`,
Clusterx 2026.7.1.

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
| `queue` | Default target queue |
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
| `--image` | Container image URL | config/default |
| `--mount` | Volume mount specification | config/default |

Example shape:

```bash
clusterx run \
  -J <job-name> \
  -q <queue> \
  --image <registry/image:tag> \
  --mount '<mount-spec>' \
  bash -c '<training-command>'
```

Do not copy example credentials, signed URLs, queue names, images, resource IDs,
or endpoints from documentation into a real job.

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
- The job command is correctly quoted.
- The preview is redacted and the user has confirmed submission.

Expected result includes a job schema/status such as `Queuing`. Record the job
ID for later `get-job`, `log`, `stats`, or `stop` operations.

## Snapshot change history

| Version | Change |
| --- | --- |
| 2026.6.4 | Added privileged-mode switch |
| 2026.6.9 | Improved cluster-ID configuration and arguments |
| 2026.6.11 | Distinguished D/PT clusters and endpoints |
| 2026.6.12 | Added SSP GPU utilization, memory, power, and temperature stats |
| 2026.7.1 | Added JSON volume mounts for PV_AOSS and other complex volumes |
