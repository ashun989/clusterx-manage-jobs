# Clusterx smoke projects

These projects validate the configured PT/SSP GPU and storage mounts. Each
program prints one JSON result and exits non-zero when validation or cleanup
fails. Storage payloads are deterministic and removed before a successful
exit.

Run them through the installed Clusterx skill wrapper. For example, ask Codex
to use `$clusterx-manage-jobs` and submit the resources and entrypoint declared
by `gpu-matmul/project.json` or `storage-access/project.json`. Do not add
credentials or signed URLs to these projects. Keep real mount paths, account
names, workspace names, and storage identifiers out of Git.

`storage-access` validates all file-storage and object-storage mounts in one
job. Pass each mounted path at runtime with a generic alias:

```bash
python3 main.py \
  --run-id <run-id> \
  --target file:file-primary:/mounted/file/path \
  --target file:file-shared:/mounted/shared/path \
  --target object:object-primary:/mounted/object/path \
  --target object:object-shared:/mounted/shared-object/path
```

Smoke results never print target paths or raw exception messages, which may
contain private mount details. The storage result contains only the storage
type and alias.

`ssp-live-log` remains active for about 60 seconds, flushes progress every five
seconds, and persists a sanitized final result to an explicitly supplied shared
path. It is used to distinguish running-pod log access from post-completion
HTTP 404 behavior.
