# clusterx-monitor-server

The Server is the read-only Monitor service for the single queue selected by
the configured Clusterx profile. It collects queue data, evaluates resource
and group policy, stores aggregate history, exposes the HTTP API, and can
optionally serve a built Web directory.

## Features and boundaries

- queue, node, workload, user and group snapshots;
- quota, utilization, capacity and group-node placement findings;
- public node ownership and explicit user-to-group access lookup;
- optional mutually exclusive group node allocation with advisory placement
  findings;
- scheduling simulation, history, SSE events, metrics and lazy running-worker
  logs;
- authenticated local editing of resource policy and group allocation files.
- authenticated cold-start node allocation previews with workload-, GPU-, and
  user-impact priorities.

The Server never creates, stops or mutates Clusterx jobs. Pending workloads do
not receive node placement findings. Every running Workload has an authoritative
`placement_context`; every placement records its `owner_group` and ownership
relation. `placement.outside_owned_pool` means the Workload had enough remaining
GPU quota to cover every GPU placed on other groups' nodes.
`placement.quota_borrowed` means that remaining quota was smaller than the
foreign GPU total. Every running foreign placement produces one of these two
findings. Both are warnings by default and become violations when the owner
group has active group-local pending pressure. Unknown owner-group pressure
remains a warning with explicit evidence. When allocation is disabled,
placements are marked `unmanaged` rather than treated as owned.

Every node snapshot keeps the ECP node name in `node` as its stable internal
identity and exposes the authoritative `host_ip` plus a nullable Clusterx
`hostname`. For a valid IPv4 address the hostname is derived as lowercase
`host-<dashed-ip>` (for example `10.140.62.215` becomes
`host-10-140-62-215`). Group configuration and planner payloads continue to
use the ECP node name; `clusterx run --include/--exclude` uses `hostname`.

The planning API keeps resource candidate scope, Workload group filters, node
owner group filters, and Workload placement relation independent. Non-default
ownership filters are rejected when allocation is disabled, and group-relative
node scopes require `node_owner_groups`. No invalid request is silently
broadened to all nodes or all Workloads.
The evaluated snapshot/API schema is version `2`; Server, Web, CLI and Skill
version `3.x` must be deployed together and are not compatible with 2.x clients.

Pending pressure includes queue-scoped pending TrainingJobs, development AIDs,
and deployment AIRs. AID/AIR rows must expose queue identity so workspace-level
resources from another queue are not counted. The pending inventory is marked
incomplete if a source cannot be read completely or a pending row cannot be
scoped to the queue; queue and group pressure then remains unknown rather than
being inferred from the sources that happened to respond. Pending resources are
not assigned node placements.

The administrator endpoint `POST /api/v1/admin/node-allocation/plans` accepts a
retained `snapshot_id` and the current group-file revision, then returns three
preview alternatives. Each alternative assigns every current queue node to
exactly one group, requires every finite numeric GPU quota to match assigned
node capacity exactly, and reports the running workloads, GPUs, and users affected by
changing ownership. It also weighs known aggregate pending GPU, CPU, and memory
demand for each group against the capacity assigned to that group. Pending
resource gaps are aggregate indicators and do not prove per-replica placement
feasibility. Missing pending sources or resource shapes are reported, and only
known demand influences the objective. Unknown or unattributed running workloads
are reported as context but do not influence the objective. CPU-only and
memory-only running workloads contribute to workload impact. The result is never written automatically;
the Web administrator must select a feasible proposal, review the draft, and
save it through the normal revision-checked endpoint. A changed snapshot or
group revision invalidates application of the preview.

Numeric GPU quotas must be multiples of 8. When `default.gpu_quota` is
`remainder`, the service resolves it from the current queue's total GPU
capacity after subtracting explicit group quotas; if that result is not a
multiple of 8, every cold-start proposal is returned as infeasible. The
`default` group's members are the currently known users not explicitly
assigned to another group and must remain empty in persisted configuration.

## Configuration

`serve` requires protected Clusterx, resource-policy, group-policy and admin
configuration paths:

```bash
clusterx-monitor serve \
  --clusterx-config ~/.config/clusterx.yaml \
  --policy-config config/resource-policy.local.json \
  --group-config config/groups.local.yaml \
  --auth-config config/admin.local.yaml \
  --history-db ~/.clusterx-monitor/history.sqlite3 \
  --host 127.0.0.1 --port 8765
```

The resource and group files are hot-reloaded as a complete last-known-good
pair. Initial missing or invalid files enter authenticated `setup-required`
mode. The Web administrator uses revision checks, atomic replacement, backups,
CSRF protection and audit records. Administrator passwords are stored as
Argon2id hashes.

Initialize or rotate the administrator credentials interactively:

```bash
clusterx-monitor admin init \
  --auth-config config/admin.local.yaml \
  --username clusterx-admin
```

Keep credentials and private group membership files mode `600`. The service
supports `--allowed-host` for non-loopback access and `--allowed-origin` when
the Web is deployed separately. Put HTTPS and authentication at a trusted
reverse proxy before exposing the service beyond its controlled network.

## API-only and bundled Web modes

The installed Server is API-only by default. To serve a Web build from the same
process, add:

```bash
--static-dir /path/to/web-dist
```

For separate deployment, publish the Web archive with Nginx or another static
server and set its `config.js` API endpoint. See [deploy/README.md](../deploy/README.md)
for Docker and systemd templates.

## Runtime endpoints

- `/healthz`: process health;
- `/readyz`: snapshot readiness;
- `/metrics`: Prometheus text metrics;
- `/api/v1/...`: snapshots, policy, history, access and administration APIs;
- `/api/v1/admin/node-allocation/plans`: authenticated cold-start allocation
  previews (administrators only);
- `/api/v1/events`: snapshot update stream.

The service is intentionally scoped to one queue. It does not merge or expose
other queue inventories.

## Build and test

```bash
python3 -m build server
pytest -q
```

The repository test suite uses simulated Clusterx gateways and does not submit
jobs to the real cluster. See [docs/development.md](../docs/development.md) and
[docs/release.md](../docs/release.md) for repository-wide checks and release
packaging.

## Email subscriptions

The dashboard is anonymous. A verified PJLab mailbox can follow any visible
workload and manage that mailbox's own subscriptions. The browser gets an
independent 30-day mailbox session. All routes below are under
`/api/v1/subscriptions` and documented in the service OpenAPI schema.

| Method and route | Behavior |
| --- | --- |
| `GET /capabilities` | Whether following is exposed, the applicable stable event codes and allowed queue thresholds. |
| `POST /challenge` | Queues an email code and returns `challenge_id`. It expires after 10 minutes and permits five verification attempts. |
| `POST /verify` | Accepts `challenge_id`, `code` and optional `target: {kind, uid}`. Sets the email session and follows the optional target with defaults. |
| `GET /session`, `POST /logout` | Read the masked address and the CSRF token, or close this browser session. Existing subscriptions continue. |
| `GET /` (base path) | List the current mailbox's subscriptions and preferences. |
| `GET /{kind}/{uid}`, `PUT /{kind}/{uid}`, `DELETE /{kind}/{uid}` | Read its summary, follow or update, or cancel. An empty PUT body uses the preset for a new follow and preserves existing preferences. An explicit body replaces preferences. |
| `POST /unsubscribe` | Verifies a link token and idempotently cancels its subscription. The browser displays a confirmation page before making this request. |

Write requests require the configured same origin. Session writes also require
the `X-CSRF-Token` from the email session. Responses are private and use
`Cache-Control: no-store`. The scoped cookie uses HttpOnly and SameSite Strict,
with Secure when the configured origin uses HTTPS. On the current isolated PT
HTTP endpoint, browser codes and sessions travel over plaintext HTTP. SMTP
always uses certificate-verified TLS.

Use `workload_uid` with `kind` for subscriptions. The optional additive field
represents the verified Clusterx identity and is consistent across the Pending
and running views. Preserve existing `workload_id` links. Replace settings
with a JSON body such as `{"events":["job.failed","queue.wait"],"queue_minutes":30}`.
An explicit empty events array keeps the task followed.

Training jobs default to platform-confirmed `job.failed`. Running, completed
and stopped are separate events. Pending alerts accept 30, 60 or 120 minutes
and require a complete Pending inventory. AID and AIR expose supported policy
and device signals without a default email event. GPU alerts require enough
samples and full current GPU attribution for the selected metric; they use PT's
effective low-activity policy and do not interpret missing data as idle.
Explicit platform end times and observed rule transitions are delivered only
after the corresponding preference was enabled. Duplicate events are collapsed,
and recovered conditions can trigger again.

Run the simulated HTTP, mail, lifecycle and privacy example with
`python -m pytest -q tests/test_notifications.py`. It uses temporary private
SQLite data, fake mailbox addresses and simulated SMTP. See the deployment guide
for running the background worker in simulation mode.
