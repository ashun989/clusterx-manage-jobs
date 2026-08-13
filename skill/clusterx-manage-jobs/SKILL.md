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
- For queue packing, per-user/per-workload node allocation, GPU fragmentation, or
  full-node scheduling analysis, run `python3 scripts/queue_plan.py`. This is a
  read-only report and must never stop jobs. Install `requirements.txt` for
  HTTP connection reuse and colored Rich tables; retain the built-in plain-text
  fallback when Rich is unavailable.
- Run Clusterx through `python3 scripts/clusterx_exec.py --cwd <project> --`
  so project configuration overrides the persistent global configuration and
  Clusterx stdout/stderr are redacted before they are returned.
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
   and report material differences. Version 2026.8.11 is tested; treat other
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
3. Validate mounts. Accept the simple `TYPE:ID:PATH[:SUBDIR]` form or a JSON
   object. Keep JSON quoted as one shell argument.
   When a smoke manifest declares `target_source: clusterx_config`, resolve
   the selected protected config, derive generic runtime targets from every
   `PV_AFS` and `PV_AOSS` `mount_path`, and never ask the user to supply those
   paths. Do not display the generated target arguments. Require at least one
   configured mount of each type requested by the manifest.
4. Invoke a runner by absolute path, such as
   `bash /absolute/path/to/runner.sh`, and pass runtime settings through
   repeated `-e KEY=VALUE` options. Never use `bash -c`, `bash -lc`, or the
   corresponding command-string mode of another shell: Clusterx 2026.8.11
   joins command arguments without preserving shell quoting, and the wrapper
   rejects these unsafe forms before submission.
5. Build the `clusterx run` command without executing it.
6. Pipe previews built outside the wrapper through `scripts/redact.py`.
7. Show the redacted command plus a concise resource and risk summary.
8. If the user explicitly requested submission and the command matches that
   request, execute it without another confirmation. If the user requested
   only a preview, stop after the preview.
9. Do not add `--enable-privileged` unless the user explicitly requested it.
   Ask before adding it later because that expands the authorized risk.
10. Capture the job ID and report the initial status without exposing
   credentials.
11. Suggest the relevant `get-job`, `log`, or `stats` follow-up.

## Query and stop jobs

- Execute `list`, `get-job`, `get-node`, `log`, and `stats` as read-only
  operations without confirmation unless another action is implied.
- For requests such as "why can 2 x 8 GPU not schedule" or "which jobs could
  be coordinated to release full nodes", run `queue_plan.py --nodes M`;
  `--gpus-per-node` defaults to 8. Add `--cpus-per-node` and
  `--memory-per-node-gib` only when the target workload specifies them. Treat
  every suggestion as a coordination candidate, not authorization to stop
  anything. Use `--strategy all|min-gpu|min-workloads|min-users`; default to
  `min-gpu`; `min-jobs` remains a deprecated compatibility alias for
  `min-workloads`. Use `--candidate-scope fragmented|full|all`; default to `fragmented`
  for backward-compatible fragment cleanup. A `full` node is any occupied node
  whose allocated GPUs equal or exceed its GPU capacity, including nodes shared
  by multiple workloads. Queue packing reads the same node Pod workload
  inventory as the SSP console, so training jobs, development instances
  (`aid`), inference workloads, and unknown workload types are attributed and
  may be coordination candidates. Node allocation remains the capacity source
  of truth; unattributed resources are displayed but never claimed as releasable.
  Use `--alternatives N` (default `1`, range `1` to `10`) for ranked plans per
  strategy. Use `--search-seconds S` (default `10`) to bound only local solving;
  exact search calibrates itself from measured state throughput and reserves
  time for a heuristic fallback.
  The default workload summaries include the last `--minutes` window's per-GPU
  compute and memory utilization average plus the per-card range. Add
  `--show-gpu-details` to expand a deduplicated per-card terminal table; JSON
  always includes per-card telemetry. The opening overview groups attributed
  workload counts and allocated GPU, CPU, and memory by user; unattributed node
  resources remain separate. Utilization is observational and must never affect
  capacity attribution, candidate ranking, or stopping decisions.
  Add `--refresh-seconds S` for a fixed-rate read-only monitor. Refreshes are
  serialized; scheduled ticks that occur while a complete query is still
  running are skipped rather than overlapped. Interactive Rich terminals use
  an alternate-screen live dashboard. Arrow keys, Page Up/Down, Home/End, and
  the mouse wheel scroll the report; `q` or Ctrl-C exits. Other terminal input
  is consumed without echo and terminal modes are restored before printing only
  the last complete report. Non-TTY stdin/stdout or no-Rich output appends
  labeled plain-text snapshots. In refresh JSON mode, stdout is NDJSON and
  `--out` keeps the latest complete pretty-printed snapshot.
- For SSP Worker discovery, use `get-job <job-id> --workers`; use the live-help
  pagination, filter, and ordering options when the result set is large. Treat
  Worker fields as runtime observations and do not infer missing nodes.
- For SSP Prometheus statistics, use `--scope queue` for configured queue
  summaries and `--scope job --job <exact-job-name>` for a workload. Read live
  help for the supported metrics and keep queue/job identifiers out of reports.
- On SSP with Clusterx 2026.8.11, treat a pods-endpoint HTTP 404 from `log` as
  a log-discovery failure, not proof that the job failed. Check `get-job`
  separately. The same 404 can occur while a job is `Running` and after it is
  `Succeeded`, even when the workload flushes output.
- SSP job details may return empty `nodes` and `nodes_ip`, and `get-node` may
  not provide a fallback. Never invent node or log data. For jobs requiring
  observable progress, write sanitized status/results to approved shared
  storage and state that this is a fallback rather than stdout/stderr.
- Before `stop`, resolve and display the exact job ID/name. Stop it directly
  when the user explicitly requested that exact target. If a partial name has
  no unique exact resolution, show the candidates and ask the user to choose;
  never infer a destructive target.
- Clusterx 2026.8.11 supports batch `stop` filters. Use them only when the user
  explicitly requests that exact batch scope. Preview the resolved matching
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
