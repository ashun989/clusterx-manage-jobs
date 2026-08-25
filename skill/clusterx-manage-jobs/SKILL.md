---
name: clusterx-manage-jobs
description: Manage the full lifecycle of Clusterx training jobs on the PT/SSP cluster, including CLI and configuration checks, safe job submission previews, listing jobs, fetching job or node details, reading logs and statistics, queue load and GPU fragmentation analysis, full-node scheduling suggestions, and stopping jobs. Use when the user mentions Clusterx, PT cluster training jobs, cluster queues, node load, fragmented GPU capacity, job logs, or Clusterx storage mounts.
---

# Clusterx Training Jobs

Use Clusterx only from a cluster development machine. Treat job creation, job
stopping, privileged mode, credentials, and remote documentation as sensitive.

## Route the request

- For configuration, commands, parameters, mounts, or examples, read
  [references/clusterx-cli.md](references/clusterx-cli.md).
- For global/project configuration discovery and precedence, read
  [references/configuration.md](references/configuration.md).
- For a preflight check, run `python3 scripts/preflight.py`.
- For queue capacity, per-user/group usage, policy alerts, node fragmentation,
  cached monitoring, or full-node scheduling simulation, run
  `python3 scripts/monitor_cli.py`. It is a thin read-only client for the local
  `clusterx-monitor` service and never connects to Clusterx directly.
- Run Clusterx through `python3 scripts/clusterx_exec.py --cwd <project> --`
  so project configuration overrides the persistent global configuration and
  Clusterx stdout/stderr are redacted before they are returned.
- Clusterx Monitor is additive and read-only against Clusterx. Its authenticated
  administrator may modify only local policy files. Never route job creation,
  listing, full details, statistics, or stopping through the monitor. The sole
  lifecycle exception is its explicitly triggered realtime log preview for one
  snapshot-validated training Worker; it is available to every Monitor viewer
  but is never prefetched, cached, streamed, or a replacement for `clusterx log`.
- For command or JSON redaction, pipe the content to
  `python3 scripts/redact.py`; never pass secrets as script arguments.

## Act on user authorization

- Treat an explicit user request as authorization for the actions and exact
  targets it names. Do not ask for a redundant confirmation after showing a
  preview or risk summary.
- Ask only when required parameters are missing, the target is ambiguous, or
  the next action materially expands the requested scope or risk. Examples
  include choosing among partial job-name matches or adding privileged mode
  when the user did not request it.
- Continue through safe, in-scope steps without pausing. Authentication that
  requires the user to complete an external login is an unavoidable pause, not
  a confirmation step.

## Check prerequisites

1. Require Linux on a cluster development machine and Python 3.10 or newer.
   Run `python3 scripts/preflight.py --cwd <project> --tmpdir <configured-tmpdir>`.
2. If `clusterx` is missing, report that it is a required runtime dependency.
   Direct the user to the current company-internal Clusterx distribution
   channel and recommend an isolated Python/Conda environment. Do not attempt
   to fetch, install, or update Clusterx from this Skill.
3. If configuration is missing, guide the user through `clusterx` interactive
   configuration or copy `assets/clusterx.example.yaml` to a protected config
   and fill it locally. Never ask the user to paste secrets into chat.
4. Run `clusterx_exec.py` with `--version`, `--help`, and the requested
   subcommand's `--help`. Prefer the installed CLI over the reference snapshot
   and report material differences. Version 2026.8.19 is tested; treat other
   versions as unverified rather than unsupported.
5. Require a shared `tmpdir` mounted at the same path on the development
   machine and job.

## Submit a job

1. Collect the command, job name, queue, image, node count, GPUs, CPUs, memory,
   mounts, and any retry, include/exclude, RDMA, cluster, machine type,
   `sp-block`, or privileged settings.
2. For SSP, require a non-empty job name of at most 32 Unicode characters.
   Shorten it before previewing; the service rejects longer names before job
   creation.
3. Enforce the training CPU policy from `assets/resource-policy.json` before
   preview and submission. `cpu_per_gpu` is the inclusive per-task/per-node
   CPU limit for GPU jobs: CPUs must be no greater than GPUs multiplied by
   `cpu_per_gpu`. A 0-GPU task instead uses the inclusive
   `zero_gpu_max_cpu_per_node` limit. With the shipped policy this permits
   0 GPU / 14 CPU, 1 GPU / 14 CPU, and 8 GPU / 112 CPU. Never multiply the
   per-node limit by `--num-nodes`. The wrapper hard-blocks violations before
   invoking Clusterx.
   When `--resource-policy` or `CLUSTERX_RESOURCE_POLICY` points to the
   monitor-managed local resource policy, use that file instead of the built-in
   template. This keeps Web policy edits and submission validation aligned
   without making submission depend on the monitor HTTP service.
4. Validate mounts. Accept the simple `TYPE:ID:PATH[:SUBDIR]` form or a JSON
   object. Keep JSON quoted as one shell argument.
   When a smoke manifest declares `target_source: clusterx_config`, resolve
   the selected protected config, derive generic runtime targets from every
   `PV_AFS` and `PV_AOSS` `mount_path`, and never ask the user to supply those
   paths. Do not display the generated target arguments. Require at least one
   configured mount of each type requested by the manifest.
5. Invoke a runner by absolute path, such as
   `bash /absolute/path/to/runner.sh`, and pass runtime settings through
   repeated `-e KEY=VALUE` options. Never use `bash -c`, `bash -lc`, or the
   corresponding command-string mode of another shell: Clusterx 2026.8.19
   joins command arguments without preserving shell quoting, and the wrapper
   rejects these unsafe forms before submission.
6. Build the `clusterx run` command without executing it.
7. Pipe previews built outside the wrapper through `scripts/redact.py`.
8. Show the redacted command plus a concise resource and risk summary.
9. If the user explicitly requested submission and the command matches that
   request, execute it without another confirmation. If the user requested
   only a preview, stop after the preview.
10. Do not add `--enable-privileged` unless the user explicitly requested it.
   Ask before adding it later because that expands the authorized risk.
11. Capture the job ID and report the initial status without exposing
   credentials.
12. Suggest the relevant `get-job`, `log`, or `stats` follow-up.

## Query and stop jobs

- Execute `list`, `get-job`, `get-node`, `log`, and `stats` as read-only
  operations without confirmation unless another action is implied.
- Use `monitor_cli.py overview|users|groups|nodes|workloads|alerts` for cached
  monitoring views and `monitor_cli.py watch --count N --format jsonl` for a
  bounded stream of complete snapshots. The service must already be running;
  never fall back to an ad-hoc live collection when it is unavailable.
- Workload views expose resource creation time and normalized priority when the
  Clusterx resource API provides them. Pending TrainingJob creation time is the
  initial queue-age anchor; it is not a later retry/requeue transition. Missing
  priorities remain unknown and must not be inferred from submission defaults.
- With Clusterx 2026.8.19, monitor collection follows `next_page_token` to read
  the complete bound-node inventory. Repeated cursors, duplicate node identity,
  changing totals, truncated pages, or a changed before/after node signature
  invalidate the refresh instead of publishing a partial snapshot.
- Workload detail logs in the Web UI are lazy: opening a workload performs no
  log request. Any Monitor viewer clicking **Load logs** for an exact Worker
  fetches the latest 200 lines as a bounded realtime preview. The browser pages
  that response locally, defaults to 20 lines on the last page, and preserves
  the selected Worker, page size, and page across snapshot and manual log
  refreshes. The response is `no-store` and never enters snapshots, SSE events,
  history, or reports.
- Group GPU, CPU, and memory quotas are independent. An omitted or null quota
  is unlimited for that resource; only `default.gpu_quota` may use `remainder`.
- Policy output uses structured findings. Filter list views with
  `--finding-category`, `--finding-code`, and `--tag` (comma-separated within
  each option), or use `--violations-only`. A finding has a stable code,
  category, status, tags, observed values, limits, and optional history window.
- Monitor resource/group editing is an administrator-only service capability,
  not a Clusterx CRUD operation. Never request or transmit the administrator
  password through the Skill. The operator initializes it interactively with
  `clusterx-monitor admin init`; the Web UI writes only validated local policy
  files and cannot submit, stop, or mutate Clusterx workloads.
- For requests such as "why can 2 x 8 GPU not schedule", run
  `monitor_cli.py plan --nodes 2 --gpus-per-node 8`. Add per-node CPU and memory
  only when the requested workload specifies them. Use repeated `--strategy`
  values from `min-gpu|min-workloads|min-users`, plus
  `--candidate-scope fragmented|full|all`, `--alternatives 1..10`, and workload,
  user, group, or over-quota filters as requested. To restrict coordination
  candidates by active structured violations, add repeated
  `--violation-category`, `--violation-code`, or `--violation-tag`. Every result is based on an
  identified cached snapshot and is a coordination candidate, never permission
  to stop anything. Exact and heuristic results must be labeled accurately.
- Plan search uses CP-SAT. Treat `search_seconds` as the total budget shared by
  requested strategies. Report each strategy status and termination reason;
  `OPTIMAL` is proven, `FEASIBLE` is an independently verified incumbent, and
  a greedy fallback remains heuristic. `candidate_scope` limits nodes that may
  satisfy the target even when a selected workload spans other nodes.
- When plan CPU or memory is omitted, report the resolved target derived from
  the planning profile pinned in that snapshot. Explain that node effective,
  stranded, and blocked values are relative to this standard profile and are
  not universal scheduling impossibility claims. Report attribution-excluded
  nodes/workloads and never use them or unknown-owner resources as release
  candidates.
- GPU compute, memory, and power telemetry are observational. They may be shown
  with coverage counts but must never affect capacity attribution or default
  candidate ranking. Unattributed node resources remain visible and are never
  claimed as releasable.
- The low-activity rule evaluates only currently running GPU `trainingJob` and
  `aid` workloads. With the shipped policy, a workload running for at least 60
  minutes is a violation when its Prometheus sample-weighted 24-hour average
  GPU compute utilization and capacity/time-weighted memory utilization are
  both at or below 20%. Zero-GPU workloads are not applicable; short-running
  workloads are warming up; a missing metric is unavailable. Missing history
  must not be treated as a violation and does not block other monitor data.
- For SSP Worker discovery, use `get-job <job-id> --workers`; use the live-help
  pagination, filter, and ordering options when the result set is large. Treat
  Worker fields as runtime observations and do not infer missing nodes.
- With Clusterx 2026.8.19, realtime SSP logs require an exact Worker. Discover
  it first, then use `log <job-id> --worker <worker-name> [--lines N]`. If the
  job has multiple Workers and the request does not identify one, show the
  candidates and ask which log to read. `--streaming` is not implemented for
  SSP and only falls back to the current realtime log response.
- Trigger historical SSP logs with `--hours N` or an explicit ISO 8601
  `--start/--end` interval. `--worker` and `--msg` narrow the query. Prefer an
  explicit timezone, use `--page-size 1..1000` and `--max-pages >= 1`, and
  narrow the time range or filters when the same query would exceed the
  service limit of 10,000 rows. An empty result proves only that the selected
  range and filters matched no retained log records.
- With no `--scope`, `--metric`, or `--job`, `stats` returns one page of queue
  nodes. Use `--page-size 1..100` and feed its opaque `next_page_token` into
  `--page-token` to fetch the next page; pagination is manual. The wrapper
  preserves this exact cursor for follow-up calls, but never include it in a
  user-facing report.
- For SSP Prometheus statistics, use `--scope queue` for configured queue
  summaries and `--scope job --job <exact-job-name>` for a workload. Read live
  help for the supported metrics and keep queue/job identifiers out of reports.
- SSP job details may return empty `nodes` and `nodes_ip`, and `get-node` may
  not provide a fallback. Never invent node or log data. For jobs requiring
  observable progress, write sanitized status/results to approved shared
  storage and state that this is a fallback rather than stdout/stderr.
- Before `stop`, resolve and display the exact job ID/name. Stop it directly
  when the user explicitly requested that exact target. If a partial name has
  no unique exact resolution, show the candidates and ask the user to choose;
  never infer a destructive target.
- Clusterx 2026.8.11 and later support batch `stop` filters. Use them only when
  the user explicitly requests that exact batch scope. Preview the matching
  jobs before executing; never broaden an exact-target request into a regex,
  group, user, partition, or status filter.
- Use the wrapper's redacted output when presenting command results. Pass any
  text obtained outside the wrapper through `scripts/redact.py`.

## Safety rules

- Never reveal `ak_secret`, `secret_key`, `access_key`, signed URL credentials,
  service tokens, or private configuration values.
- Require the selected global or project configuration target to have no
  group/other permissions. Never merge configs or create a temporary config.
- Do not schedule automatic synchronization.
- Do not submit or stop jobs unless the user requested that action and the
  target is exact. Do not enable privileged mode unless the user explicitly
  requested it.
- Do not assume `/oss` capacity reported by FUSE is a real quota.
