# Monitor deployment templates

The Monitor is released as four independently versioned components. Their
current versions are recorded in the component `VERSION` files.

- Server: Python wheel/sdist from `server/`. `Dockerfile.api` runs
  the API only; `Dockerfile` additionally serves Web assets from `--static-dir`.
- Web: a static tarball built from `web`, deployable by Nginx or another static
  server. Set `config.js`'s `apiBaseUrl` when the API is on another origin.
- Client: Python wheel/sdist from `client`, providing
  `clusterx-monitor-cli`, `clusterx-exec`, `clusterx-preflight`, and
  `clusterx-redact`.
- Skill: the versioned documentation/resource archive, installed separately
  after the Client. It does not contain executable CLI source files.

The examples run a single monitor instance with process-local snapshots,
planner coordination and sessions. Keep the listener on a trusted internal
network (or put an authenticated reverse proxy in front of it) and restrict
the firewall to approved operators. The realtime log endpoint remains
anonymous by design for this release.

Build all release artifacts and checksums with:

```bash
python3 scripts/build_release.py --output-dir dist/release
```

For an offline build with dependencies already installed, add
`--no-isolation`. The output also contains `release-manifest.json`, while each
archive has a matching `.sha256` file. Components may be versioned and
published independently after this release.

For a separate Web deployment, build `web/dist`, copy it to the static server,
and edit the generated `config.js` if necessary:

```bash
cd web
npm ci
CLUSTERX_WEB_OUT_DIR=dist npm run build
```

If the Web and API have different origins, configure the API with one or more
trusted origins:

```bash
clusterx-monitor serve ... --allowed-origin https://monitor.example
```

The systemd unit is a template: create the service account and directories,
copy it to `/etc/systemd/system/`, then run `systemctl daemon-reload` and
`systemctl enable --now clusterx-monitor`. The full Dockerfile performs the
static asset build as part of the image build; use `Dockerfile.api` for an
API-only image.

## PT development-host notifications

Keep the web server and notification worker as separate supervised processes
on the development host. For the PT tmux deployment, run
`deploy/pt-monitor-supervise.sh serve` and
`deploy/pt-monitor-supervise.sh notifications` in separate sessions.
Set `PT_MONITOR_PROJECT`, `PT_MONITOR_RELEASE`, `PT_MONITOR_PYTHON`,
`PT_MONITOR_ALLOWED_HOST`, `CLUSTERX_CONFIG`, and
`CLUSTERX_MONITOR_NOTIFICATIONS_DIR` in the deployment environment. Install the
wheel into the release's `python/` directory and place the Web build at `web/`. Use the existing trusted-host and history options
for the web server. Install the Server wheel and Web build as one versioned
release, then set `CLUSTERX_MONITOR_NOTIFICATIONS_DIR` for the server
(or pass `--notifications-dir`).

Create a private directory (mode 0700) with these mode 0600 files:

- `origin` contains the browser-facing origin, with the port and no trailing slash.
- `notification-key` contains a randomly generated key of at least 32 bytes.
- `smtp.json` contains the shared PJLab `username` (full address) and client
  application `password`. SMTP uses `smtp.pjlab.org.cn:465` with verified
  implicit TLS. Keep this file out of source and artifact bundles.

Use the same installed Python environment as the server:

```bash
python -m clusterx_monitor.notifications.worker run \
  --notifications-dir "$PT_MONITOR_NOTIFICATIONS" --clusterx-config "$CLUSTERX_CONFIG"

python -m clusterx_monitor.notifications.worker status \
  --notifications-dir "$PT_MONITOR_NOTIFICATIONS"
```

The worker reopens its private credentials on each send cycle. After the
server readiness and worker startup checks, create `enabled` in the directory
to expose subscriptions on the configured origin. Create `paused` to stop
outbound mail and new verification challenges without interrupting the
dashboard or preventing signed-in users from cancelling. Remove that file to
resume. Removing `enabled` hides the feature. Back up the SQLite database with
SQLite's backup interface for a live service. Keep the database and key when
rolling back to an earlier release. If SMTP rejects an attempt temporarily,
retry after 1, 5 or 15 minutes within the delivery lifetime. An indeterminate
send is recorded for inspection. The status output omits email addresses and
message contents.

For an isolated test instance, set a loopback origin, use a separate private
directory and start the worker with `run --simulate --notifications-dir PATH`.
The worker records queue items as simulated without contacting SMTP. A
simulated process can omit both the SMTP configuration and the Clusterx profile.
