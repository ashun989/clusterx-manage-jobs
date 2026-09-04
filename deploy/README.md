# Monitor deployment templates

The examples run a single monitor instance with process-local snapshots,
planner coordination and sessions. Keep the listener on a trusted internal
network (or put an authenticated reverse proxy in front of it) and restrict
the firewall to approved operators. The realtime log endpoint remains
anonymous by design for this release.

Build frontend assets before packaging:

```bash
cd web && npm ci && npm run build
```

The systemd unit is a template: create the service account and directories,
copy it to `/etc/systemd/system/`, then run `systemctl daemon-reload` and
`systemctl enable --now clusterx-monitor`. The Dockerfile performs the static
asset build as part of the image build.
