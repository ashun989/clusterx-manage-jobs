# Clusterx development-page extension

This Manifest V3 extension safely fills a Clusterx development-page form from
a user-selected local YAML file. It does not store the file, credentials,
absolute paths or page data, and it never clicks the page's final submission
button.

## Build and test

```bash
npm install
npm test
npm run build
```

Load `dist/` through Chrome's developer mode. Select a local `.yaml`/`.yml`
file from the extension popup, then explicitly choose “fill current page”.

The extension validates the page scope before writing values, fills queue,
RDMA and storage mounts, and keeps image, instance size, shared memory,
WebIDE, SSH and priority choices for manual review. Storage credentials are
used only for the current fill operation.
