# Monitor Web

The Web package is the read-only Monitor dashboard plus the authenticated
administrator editor for the local resource and group policies.

## Features

- queue overview, nodes, workloads, users, groups, alerts and history;
- public group/node ownership display for the configured queue;
- administrator group editor with a local draft, searchable member/node pickers,
  group rename/add/delete, and a final review/apply step;
- administrator-only cold-start node allocation preview with three impact
  priorities; selecting a proposal only updates the unsaved draft;
- node-allocation enable/disable state reflected in the access view;
- workload details, lazy running-worker logs and scheduling simulation;
- scheduling simulation filters for resource shape, selected-group node scope,
  and owned/borrowed/mixed placements;
- policy editing with revision checks, backups, CSRF protection and audit data.

The dashboard is a static application. It does not query Clusterx directly.
The Server remains the source of snapshots and API policy decisions.

## Configuration

The generated `dist/config.js` contains the API endpoint configuration. For a
separate static deployment, set `apiBaseUrl` to the Monitor Server URL:

```js
window.CLUSTERX_MONITOR_CONFIG = {
  apiBaseUrl: "https://monitor.example/api",
};
```

When the Web origin differs from the API origin, configure the Server with the
same origin using one or more `--allowed-origin` options.

## Build

```bash
npm ci
CLUSTERX_WEB_OUT_DIR=dist npm run build
```

Publish the contents of `dist/` to Nginx or another static file server. The
Server can host the same directory with `--static-dir` for a small internal
deployment.

The group editor keeps all changes in a browser-local draft until the
administrator confirms the review dialog. Temporary duplicate members/nodes
are shown as non-blocking conflicts and are rejected by the server at apply
time. Deleting a non-empty group moves its members and nodes into `default` in
the draft. A failed apply keeps the draft for correction; reloading explicitly
discards it.

The cold-start action is snapshot- and revision-pinned. It recomputes all
current node assignments, shows quota coverage and affected running
workloads/GPU/users, and fills the result back into the draft. Any later draft
edit invalidates that suggestion and requires a new preview. Pending workloads
are not counted because they have no node placement.

When node allocation is enabled, the simulator keeps resource candidate scope
(`fragmented`, `full`, `all`) separate from node ownership scope and placement
relationship. The selected workload group filter is reused for “selected group
nodes” and “outside selected groups”; when allocation is disabled, all nodes
remain available and the ownership filters are disabled.

## Development

```bash
npm test -- --run
npm run build
```

The Web component version is maintained in `VERSION` and `package.json`; use
the repository version checker to validate their consistency.
