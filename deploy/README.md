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
