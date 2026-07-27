# Clusterx smoke projects

These projects validate the configured PT/SSP GPU and storage mounts. Each
program prints one JSON result and exits non-zero when validation or cleanup
fails. Storage payloads are deterministic and removed before a successful
exit.

Run them through the installed Clusterx skill wrapper. Do not add credentials
or signed URLs to these projects.
