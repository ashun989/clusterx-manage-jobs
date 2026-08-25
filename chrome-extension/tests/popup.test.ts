import { beforeEach, describe, expect, it, vi } from "vitest";

const yaml = `
default: ssp
ssp:
  subscription: sub-example
  resource_group: default
  region: cn-test-01
  workspace: ws-example
  queue: queue-example
  rdma_name: rdma-example
  image: registry.example/model:latest
  ak_id: ROOT_ACCESS_KEY
  ak_secret: ROOT_SECRET_VALUE
  mount:
    - type: PV_AOSS
      name: bucket-example
      endpoint: https://objects.example
      mount_path: /data/objects
      metadata:
        items:
          - key: access_key
            value: OBJECT_ACCESS_KEY
          - key: secret_key
            value: OBJECT_SECRET_VALUE
`;

describe("popup message lifecycle", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    document.body.innerHTML = `
      <input id="config-file" type="file">
      <button id="choose-file"></button>
      <button id="fill"></button>
      <p id="state"></p>
      <ul id="results"></ul>
      <span id="extension-version"></span>
    `;
  });

  it("reads one local file, sends a whitelisted request, and scrubs credentials", async () => {
    let requestReference: { profile: { mounts: Array<{ accessKey?: string; secretKey?: string }> } } | undefined;
    let transmittedCopy: unknown;
    const sendMessage = vi.fn(async (_tabId: number, request: typeof requestReference) => {
      requestReference = request;
      transmittedCopy = structuredClone(request);
      return {
        ok: true,
        fatal: false,
        items: [{ key: "queue", label: "队列", status: "filled", message: "已填充" }],
      };
    });
    vi.stubGlobal("chrome", {
      runtime: { getManifest: () => ({ version: "0.1.10" }) },
      tabs: {
        query: vi.fn(async () => [{ id: 7 }]),
        sendMessage,
      },
    });

    await import("../src/popup");
    const input = document.querySelector<HTMLInputElement>("#config-file")!;
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [new File([yaml], "clusterx.yaml", { type: "application/yaml" })],
    });
    input.dispatchEvent(new Event("change"));
    document.querySelector<HTMLButtonElement>("#fill")!.click();

    await vi.waitFor(() => expect(sendMessage).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(requestReference?.profile.mounts[0]).toMatchObject({
      accessKey: "",
      secretKey: "",
    }));
    expect(JSON.stringify(transmittedCopy)).not.toContain("ROOT_ACCESS_KEY");
    expect(JSON.stringify(transmittedCopy)).not.toContain("ROOT_SECRET_VALUE");
    expect(JSON.stringify(transmittedCopy)).toContain("OBJECT_ACCESS_KEY");
    expect(document.querySelector("#results")?.textContent).toContain("队列：已填充");
    expect((globalThis as { chrome?: { storage?: unknown } }).chrome?.storage).toBeUndefined();
    expect(document.querySelector("#extension-version")?.textContent).toBe("0.1.10");
  });

  it("reads the requested AFS UUID mapping in a temporary inactive tab", async () => {
    const afsYaml = yaml.replace(/    - type: PV_AOSS[\s\S]*/, `    - type: PV_AFS
      id: 11111111-1111-4111-8111-111111111111
      mount_path: /data/models
`);
    let fillRequest: { type?: string; afsCatalog?: Record<string, string> } | undefined;
    const sendMessage = vi.fn(async (tabId: number, request: { type?: string }) => {
      if (tabId === 8 && request.type === "clusterx.read-afs-catalog") {
        return {
          ok: true,
          catalog: {
            "11111111-1111-4111-8111-111111111111": "afs-team-models",
          },
        };
      }
      fillRequest = structuredClone(request);
      return {
        ok: true,
        fatal: false,
        items: [{ key: "mount.afs.0", label: "文件存储挂载 1", status: "filled", message: "已精确匹配" }],
      };
    });
    const remove = vi.fn(async () => undefined);
    const create = vi.fn(async () => ({ id: 8 }));
    vi.stubGlobal("chrome", {
      runtime: { getManifest: () => ({ version: "0.1.10" }) },
      tabs: {
        query: vi.fn(async () => [{ id: 7 }]),
        create,
        remove,
        sendMessage,
      },
    });

    await import("../src/popup");
    const input = document.querySelector<HTMLInputElement>("#config-file")!;
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [new File([afsYaml], "clusterx.yaml", { type: "application/yaml" })],
    });
    input.dispatchEvent(new Event("change"));
    document.querySelector<HTMLButtonElement>("#fill")!.click();

    await vi.waitFor(() => expect(fillRequest?.type).toBe("clusterx.fill-development"));
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      active: false,
      url: "https://console.d.pjlab.org.cn/cn-test-01/afs/list",
    }));
    expect(remove).toHaveBeenCalledWith(8);
    expect(fillRequest?.afsCatalog).toEqual({
      "11111111-1111-4111-8111-111111111111": "afs-team-models",
    });
  });

  it("asks Chrome to remember the last directory without storing a file handle", async () => {
    const pickedFile = new File([yaml], "clusterx.yaml", { type: "application/yaml" });
    const showOpenFilePicker = vi.fn(async () => [{ getFile: async () => pickedFile }]);
    vi.stubGlobal("showOpenFilePicker", showOpenFilePicker);
    vi.stubGlobal("chrome", {
      runtime: { getManifest: () => ({ version: "0.1.10" }) },
      tabs: {},
    });

    await import("../src/popup");
    document.querySelector<HTMLButtonElement>("#choose-file")!.click();

    await vi.waitFor(() => expect(document.querySelector("#state")?.textContent).toContain("clusterx.yaml"));
    expect(showOpenFilePicker).toHaveBeenCalledWith(expect.objectContaining({
      id: "clusterx-config-yaml",
      multiple: false,
    }));
    expect((globalThis as { chrome?: { storage?: unknown } }).chrome?.storage).toBeUndefined();
  });
});
