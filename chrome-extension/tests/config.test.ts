import { describe, expect, it } from "vitest";

import { ConfigError, parseClusterxConfig } from "../src/config";
import { scrubProfileSecrets } from "../src/types";

const validConfig = `
default: ssp
ssp:
  cluster_type: PT
  subscription: sub-example
  resource_group: default
  region: cn-test-01
  workspace: ws-example
  cluster: cluster-example
  ak_id: ROOT_ACCESS_KEY
  ak_secret: ROOT_SECRET_VALUE
  queue: queue-example
  rdma_name: rdma-example
  image: registry.example/model:latest
  tmpdir: /shared/tmp
  mount:
    - type: PV_AFS
      id: afs-example
      subdir: models/current
      mount_path: /data/models
    - type: PV_AOSS
      name: bucket-example
      endpoint: https://objects.example
      subdir: datasets
      mount_path: /data/objects
      metadata:
        items:
          - key: access_key
            value: OBJECT_ACCESS_KEY
          - key: secret_key
            value: OBJECT_SECRET_VALUE
`;

describe("parseClusterxConfig", () => {
  it("resolves only the default profile fields needed by the form", () => {
    const profile = parseClusterxConfig(validConfig);
    expect(profile.profileName).toBe("ssp");
    expect(profile.scope).toEqual({
      subscription: "sub-example",
      resourceGroup: "default",
      region: "cn-test-01",
      workspace: "ws-example",
    });
    expect(profile.mounts).toHaveLength(2);
    expect(JSON.stringify(profile)).not.toContain("ROOT_ACCESS_KEY");
    expect(JSON.stringify(profile)).not.toContain("ROOT_SECRET_VALUE");
    expect(JSON.stringify(profile)).not.toContain("tmpdir");
  });

  it("rejects malformed YAML without echoing source content", () => {
    const secret = "DO_NOT_ECHO_THIS_SECRET";
    expect(() => parseClusterxConfig(`default: [${secret}`)).toThrowError(ConfigError);
    try {
      parseClusterxConfig(`default: [${secret}`);
    } catch (error) {
      expect(String(error)).not.toContain(secret);
    }
  });

  it("rejects missing required fields", () => {
    expect(() => parseClusterxConfig("default: ssp\nssp: {}\n")).toThrow(/subscription/);
  });

  it("rejects duplicate and system mount paths", () => {
    const duplicate = validConfig.replace("/data/objects", "/data/models");
    expect(() => parseClusterxConfig(duplicate)).toThrow(/重复/);
    const system = validConfig.replace("/data/models", "/etc/models");
    expect(() => parseClusterxConfig(system)).toThrow(/系统目录/);
  });

  it("limits each mount type to ten", () => {
    const mount = `\n    - type: PV_AFS\n      id: afs-extra\n      mount_path: /data/extra`;
    const eleven = validConfig.replace(/\n    - type: PV_AOSS[\s\S]*/, mount.repeat(10));
    expect(() => parseClusterxConfig(eleven)).toThrow(/最多允许 10/);
  });

  it("scrubs object-storage credentials after use", () => {
    const profile = parseClusterxConfig(validConfig);
    scrubProfileSecrets(profile);
    const objectMount = profile.mounts.find((mount) => mount.type === "PV_AOSS");
    expect(objectMount).toMatchObject({ accessKey: "", secretKey: "" });
  });
});
