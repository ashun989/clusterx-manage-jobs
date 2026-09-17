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
not receive node placement findings. Borrowing another group's node does not
waive quota. Placement findings are warnings by default; when the owner group
has active group-local pending pressure, both outside-owned-pool and borrowed
placements become violations. Unknown owner-group pressure keeps the finding
as a warning and records the unknown state. When node allocation is disabled,
the access API reports that state and exposes all nodes in the configured queue.
The planning API keeps resource candidate scope separate from node ownership
and workload placement scope; ownership filters are evaluated against the
pinned snapshot's effective public assignments.

The administrator endpoint `POST /api/v1/admin/node-allocation/plans` accepts a
retained `snapshot_id` and the current group-file revision, then returns three
preview alternatives. Each alternative assigns every current queue node to
exactly one group, requires every finite numeric GPU quota to match assigned
node capacity exactly, and reports the running workloads, GPUs, and users affected by
changing ownership. Unknown or unattributed workloads are reported as context
but do not influence the objective. The result is never written automatically;
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
