---
name: clusterx-manage-jobs
description: Manage the full lifecycle of Clusterx training jobs on the PT/SSP cluster, including CLI and configuration checks, safe job submission previews, listing jobs, fetching job or node details, reading logs and statistics, and stopping jobs. Use when the user mentions Clusterx, PT cluster training jobs, cluster queues, job logs, or Clusterx storage mounts.
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
- Run Clusterx through `python3 scripts/clusterx_exec.py --cwd <project> --`
  so project configuration overrides the persistent global configuration.
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
   and report material differences. Version 2026.7.1 is tested; treat other
   versions as unverified rather than unsupported.
5. Require a shared `tmpdir` mounted at the same path on the development
   machine and job.

## Submit a job

1. Collect the command, job name, queue, image, node count, GPUs, CPUs, memory,
   mounts, and any retry, include/exclude, RDMA, cluster, machine type, or
   privileged settings.
2. For SSP, require a non-empty job name of at most 32 Unicode characters.
   Shorten it before previewing; the service rejects longer names before job
   creation.
3. Validate mounts. Accept the simple `TYPE:ID:PATH[:SUBDIR]` form or a JSON
   object. Keep JSON quoted as one shell argument.
4. Build the `clusterx run` command without executing it.
5. Pipe the preview through `scripts/redact.py`.
6. Show the redacted command plus a concise resource and risk summary.
7. If the user explicitly requested submission and the command matches that
   request, execute it without another confirmation. If the user requested
   only a preview, stop after the preview.
8. Do not add `--enable-privileged` unless the user explicitly requested it.
   Ask before adding it later because that expands the authorized risk.
9. Capture the job ID and report the initial status without exposing
   credentials.
10. Suggest the relevant `get-job`, `log`, or `stats` follow-up.

## Query and stop jobs

- Execute `list`, `get-job`, `get-node`, `log`, and `stats` as read-only
  operations without confirmation unless another action is implied.
- On SSP with Clusterx 2026.7.1, treat a pods-endpoint HTTP 404 from `log` as
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
- Redact command output before presenting it if it may contain configuration,
  mount JSON, signed URLs, or environment variables.

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
