# Release guide

Server, Web, Client and Skill maintain independent version sources:

```text
server/VERSION
web/VERSION
client/VERSION
skills/clusterx-manage-jobs/VERSION
```

Do not copy a component version into documentation. Update the component's
`VERSION` and package metadata together, then run:

```bash
python3 scripts/check_versions.py
python3 scripts/build_release.py --output-dir /tmp/clusterx-release
```

The release directory contains Server and Client wheel/sdist files, the Web
static archive, the Skill archive, SHA-256 files and `release-manifest.json`.
Use `--no-isolation` only when the required build dependencies are already
installed.

Individual builds are also supported:

```bash
python3 -m build --outdir dist/server server
python3 -m build --outdir dist/client client
(cd web && CLUSTERX_WEB_OUT_DIR=dist npm run build)
python3 scripts/package_skill.py --output-dir dist/skill
```

The Skill archive contains documentation, references, agents metadata and
assets only. The Client package is the sole owner of the Monitor CLI and
Clusterx wrapper runtime code.
