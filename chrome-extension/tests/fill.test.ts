import { beforeEach, describe, expect, it, vi } from "vitest";

import { fillDevelopmentForm } from "../src/fill";
import type { ResolvedClusterxProfile } from "../src/types";

const profile: ResolvedClusterxProfile = {
  profileName: "ssp",
  scope: {
    subscription: "sub-example",
    resourceGroup: "default",
    region: "cn-test-01",
    workspace: "ws-example",
  },
  queue: "queue-example",
  rdmaName: "rdma-example",
  image: "registry.example/model:latest",
  mounts: [
    { type: "PV_AFS", id: "afs-example", subdir: "models", mountPath: "/data/models" },
    {
      type: "PV_AOSS",
      name: "bucket-example",
      endpoint: "https://objects.example",
      subdir: "datasets",
      mountPath: "/data/objects",
      accessKey: "OBJECT_ACCESS_KEY",
      secretKey: "OBJECT_SECRET_VALUE",
    },
  ],
};

const pageUrl = "https://console.d.pjlab.org.cn/cn-test-01/ssp/model/development/create?workspaceId=%2Fsubscriptions%2Fsub-example%2FresourceGroups%2Fdefault%2Fregions%2Fcn-test-01%2Fworkspaces%2Fws-example";

function transientOption(
  label: string,
  onChoose?: () => void,
  parent: HTMLElement = document.body,
  removeOnChoose = true,
): HTMLElement {
  const option = document.createElement("div");
  option.setAttribute("role", "option");
  option.textContent = label;
  option.setAttribute("name", label);
  option.setAttribute("title", label);
  option.addEventListener("click", () => {
    onChoose?.();
    if (removeOnChoose) option.remove();
  });
  parent.append(option);
  return option;
}

function setSelectedTab(tab: HTMLElement): void {
  for (const candidate of document.querySelectorAll('[role="tab"]')) {
    candidate.setAttribute("aria-selected", String(candidate === tab));
  }
}

interface FixtureOptions {
  customImage?: boolean;
  afsOption?: boolean;
  refreshRdma?: boolean;
  kmsCredentials?: string[][];
  kmsCredentialPages?: string[][][];
}

function installFixture(options: FixtureOptions = {}): HTMLButtonElement {
  document.body.innerHTML = `
    <table><tbody>
      <tr><td><input type="radio"></td><td><span>queue-other</span><span>-</span></td></tr>
      <tr><td><input id="queue-target" type="radio"></td><td><span>queue-example</span><span>-</span></td></tr>
    </tbody></table>
    <div id="rdma-wrap"><input id="rdma_name" role="combobox"><span>rdma-other</span></div>
    <div role="tab" id="official-tab" aria-selected="true">官方镜像</div>
    <div role="tab" id="custom-tab" aria-selected="false">自定义镜像</div>
    <div role="tab" id="public-tab" aria-selected="false">三方公开镜像</div>
    <div id="image-panel" role="tabpanel"></div>
    <article><a id="add-afs"><span aria-label="plus"></span> 添加文件存储卷挂载</a><span>(0/10)</span></article>
    <article><a class="sensed-typography-disabled"><span aria-label="plus"></span> 添加对象存储挂载</a><span>(1/1)</span></article>
    <article><a id="add-aoss"><span aria-label="plus"></span> 添加对象存储挂载</a><span>(0/10)</span></article>
    <button id="confirm">确 认</button>
  `;

  const wireRdma = (control: Element): void => {
    control.addEventListener("click", () => {
      transientOption("rdma-example", () => {
        const wrap = document.querySelector("#rdma-wrap span");
        if (wrap) wrap.textContent = "rdma-example";
      });
    });
  };
  const initialRdma = document.querySelector("#rdma_name")!;
  if (options.refreshRdma) {
    document.querySelector("#queue-target")?.addEventListener("click", () => {
      window.setTimeout(() => {
        const replacement = document.createElement("input");
        replacement.id = "rdma_name";
        replacement.setAttribute("role", "combobox");
        document.querySelector("#rdma_name")?.replaceWith(replacement);
        wireRdma(replacement);
      }, 10);
    });
  } else {
    wireRdma(initialRdma);
  }

  const imagePanel = document.querySelector<HTMLElement>("#image-panel")!;
  document.querySelector<HTMLElement>("#custom-tab")!.addEventListener("click", (event) => {
    setSelectedTab(event.currentTarget as HTMLElement);
    imagePanel.innerHTML = '<input id="custom-image" role="combobox">';
    imagePanel.querySelector("input")?.addEventListener("click", () => {
      if (options.customImage !== false) transientOption(profile.image);
    });
  });
  document.querySelector<HTMLElement>("#public-tab")!.addEventListener("click", (event) => {
    setSelectedTab(event.currentTarget as HTMLElement);
    imagePanel.innerHTML = '<input placeholder="请输入镜像地址">';
  });

  let afsIndex = 0;
  document.querySelector("#add-afs")?.addEventListener("click", () => {
    const index = afsIndex++;
    const row = document.createElement("section");
    row.innerHTML = `
      <div class="sensed-select"><div class="sensed-select-selector"><span><input id="dataSource_${index}_afs" role="combobox" aria-controls="dataSource_${index}_afs_list"></span></div></div>
      <div id="dataSource_${index}_afs_list"></div>
      <div class="sensed-select"><div class="sensed-select-selector access-selector"><span><input class="access-mode" role="combobox" aria-controls="access_${index}_list"></span></div></div>
      <div id="access_${index}_list"></div>
      <input placeholder="如 data、data/model或data/data/txt">
      <input placeholder="请输入如 /data 或 /data/data.txt">
    `;
    const source = row.querySelector<HTMLInputElement>('input[id$="_afs"]')!;
    const afsList = row.querySelector<HTMLElement>(`#dataSource_${index}_afs_list`)!;
    source.closest(".sensed-select-selector")?.addEventListener("click", () => {
      if (options.afsOption === false || afsList.children.length > 0) return;
      transientOption("afs-team-models", () => { source.value = "afs-team-models"; }, afsList, false);
      transientOption("afs-team-archive", () => { source.value = "afs-team-archive"; }, afsList, false);
    });
    const access = row.querySelector<HTMLInputElement>(".access-mode")!;
    const accessList = row.querySelector<HTMLElement>(`#access_${index}_list`)!;
    row.querySelector<HTMLElement>(".access-selector")?.addEventListener("click", () => {
      if (accessList.children.length === 0) {
        transientOption(
          "VOLUME_ACCESS_MODE_READ_WRITE",
          () => { access.value = "VOLUME_ACCESS_MODE_READ_WRITE"; },
          accessList,
          false,
        );
      }
    });
    document.body.insertBefore(row, document.querySelector("#add-afs")?.closest("article") ?? null);
  });

  let aossIndex = 0;
  document.querySelector("#add-aoss")?.addEventListener("click", () => {
    const index = aossIndex++;
    const credentials = options.kmsCredentials?.[index] ?? ["bucket-example"];
    const credentialPages = options.kmsCredentialPages?.[index];
    const row = document.createElement("section");
    row.innerHTML = `
      <div class="auth-fields">
        <label for="aksk_${index}">Access Key ID（AK） / Secret Access Key（SK）</label>
        <div class="sensed-select">
          <div class="sensed-select-selector">
            <span class="selected-credential"></span>
            <input id="aksk_${index}" role="combobox" aria-controls="aksk_${index}_list" placeholder="请选择密钥凭据">
          </div>
        </div>
        <a class="manual-switch">切换为手动填写AK/SK</a>
      </div>
      <div class="object-fields">
        <label for="dataSource_${index}_aoss">Bucket名称</label><input id="dataSource_${index}_aoss" placeholder="请输入">
        <input placeholder="请输入http(s)://yourdomain.xxx">
        <input placeholder="请输入存储目录，非必填">
        <input placeholder="请输入路径，如 /data">
      </div>
      <div id="aksk_${index}_list"></div>
    `;
    const authFields = row.querySelector<HTMLElement>(".auth-fields")!;
    const kmsList = row.querySelector<HTMLElement>(`#aksk_${index}_list`)!;
    const selected = row.querySelector<HTMLElement>(".selected-credential")!;
    const renderCredentials = (names: string[]): void => {
      kmsList.replaceChildren();
      for (const [optionIndex, credential] of names.entries()) {
        const option = transientOption(credential, () => {
          selected.innerHTML = `<span class="sensed-select-selection-item" title="${credential}">${credential}</span>`;
          kmsList.replaceChildren();
        }, kmsList, false);
        option.id = `aksk_${index}_option_${optionIndex}`;
      }
    };
    if (credentialPages) {
      let scrollTop = 0;
      Object.defineProperties(kmsList, {
        clientHeight: { configurable: true, value: 100 },
        scrollHeight: { configurable: true, value: 200 },
        scrollTop: {
          configurable: true,
          get: () => scrollTop,
          set: (value: number) => { scrollTop = value; },
        },
      });
      kmsList.addEventListener("scroll", () => {
        renderCredentials(credentialPages[scrollTop > 0 ? credentialPages.length - 1 : 0] ?? []);
      });
    }
    row.querySelector<HTMLElement>(".sensed-select-selector")?.addEventListener("click", () => {
      if (kmsList.children.length > 0) return;
      if (credentialPages) {
        renderCredentials(credentialPages[0] ?? []);
        return;
      }
      if (credentials.length === 0) {
        kmsList.innerHTML = '<div class="sensed-select-item-empty">暂无可用的凭据</div>';
        return;
      }
      renderCredentials(credentials);
    });
    row.querySelector<HTMLElement>(".manual-switch")?.addEventListener("click", () => {
      authFields.innerHTML = `
        <label for="ak_${index}">Access Key ID（AK）</label><input id="ak_${index}" placeholder="请输入 Access Key ID">
        <label for="sk_${index}">Secret Access Key（SK）</label><input id="sk_${index}" type="password" placeholder="请输入 Secret Access Key">
        <a>切换为从密钥管理KMS获取AK/SK</a>
      `;
    });
    document.body.insertBefore(row, document.querySelector("#add-aoss")?.closest("article") ?? null);
  });

  return document.querySelector<HTMLButtonElement>("#confirm")!;
}

describe("fillDevelopmentForm", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it("fills queue, RDMA, AFS, and AOSS with an exact KMS credential without submitting", async () => {
    const confirm = installFixture();
    const submit = vi.fn();
    confirm.addEventListener("click", submit);

    const report = await fillDevelopmentForm(structuredClone(profile), document, pageUrl);

    expect(report.ok).toBe(true);
    expect(report.fatal).toBe(false);
    expect(document.querySelector<HTMLInputElement>("#queue-target")?.checked).toBe(true);
    expect(document.querySelector<HTMLInputElement>('input[placeholder="如 data、data/model或data/data/txt"]')?.value).toBe("models");
    expect(document.querySelector<HTMLInputElement>('input[placeholder="请输入如 /data 或 /data/data.txt"]')?.value).toBe("/data/models");
    expect(document.querySelector<HTMLInputElement>('input[placeholder="请输入http(s)://yourdomain.xxx"]')?.value).toBe("https://objects.example");
    expect(document.querySelector<HTMLInputElement>('input[placeholder="请输入路径，如 /data"]')?.value).toBe("/data/objects");
    expect(document.querySelector('.sensed-select-selection-item')?.textContent).toBe("bucket-example");
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(submit).not.toHaveBeenCalled();
    expect(report.items.find((item) => item.key === "mount.afs.0")?.status).toBe("warning");
    expect(report.items.find((item) => item.key === "mount.aoss.0")).toMatchObject({
      status: "filled",
      message: "已填充对象存储字段，并选择同名 KMS 凭据",
    });
  });

  it("switches to manual AK/SK when no exact KMS credential exists", async () => {
    installFixture({ kmsCredentials: [["bucket-other"]] });
    const onlyAoss = structuredClone(profile);
    onlyAoss.mounts = [onlyAoss.mounts[1]];

    const report = await fillDevelopmentForm(onlyAoss, document, pageUrl);

    expect(report.ok).toBe(true);
    expect(document.querySelector<HTMLInputElement>('input[placeholder="请输入 Access Key ID"]')?.value)
      .toBe("OBJECT_ACCESS_KEY");
    expect(document.querySelector<HTMLInputElement>('input[placeholder="请输入 Secret Access Key"]')?.value)
      .toBe("OBJECT_SECRET_VALUE");
    expect(report.items.find((item) => item.key === "mount.aoss.0")).toMatchObject({
      status: "filled",
      message: "未找到同名 KMS 凭据，已切换并手动填写 AK/SK",
    });
    expect(JSON.stringify(report)).not.toContain("OBJECT_SECRET_VALUE");
  });

  it("finds an exact KMS credential after scrolling a virtualized dropdown", async () => {
    installFixture({ kmsCredentialPages: [[
      ["bucket-first-page"],
      ["bucket-example"],
    ]] });
    const onlyAoss = structuredClone(profile);
    onlyAoss.mounts = [onlyAoss.mounts[1]];

    const report = await fillDevelopmentForm(onlyAoss, document, pageUrl);

    expect(report.ok).toBe(true);
    expect(document.querySelector('.sensed-select-selection-item')?.textContent).toBe("bucket-example");
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(report.items.find((item) => item.key === "mount.aoss.0")?.status).toBe("filled");
  });

  it("falls back to manual AK/SK with a warning for duplicate KMS names", async () => {
    installFixture({ kmsCredentials: [["bucket-example", "bucket-example"]] });
    const onlyAoss = structuredClone(profile);
    onlyAoss.mounts = [onlyAoss.mounts[1]];

    const report = await fillDevelopmentForm(onlyAoss, document, pageUrl);

    expect(report.ok).toBe(true);
    expect(document.querySelector<HTMLInputElement>('input[type="password"]')?.value)
      .toBe("OBJECT_SECRET_VALUE");
    expect(report.items.find((item) => item.key === "mount.aoss.0")).toMatchObject({
      status: "warning",
      message: "存在多个同名 KMS 凭据，已拒绝猜测并手动填写 AK/SK；请复核",
    });
  });

  it("leaves all image controls untouched for manual selection", async () => {
    installFixture({ customImage: false });
    const withoutMounts = structuredClone(profile);
    withoutMounts.mounts = [];

    const report = await fillDevelopmentForm(withoutMounts, document, pageUrl);

    expect(report.ok).toBe(true);
    expect(report.items.find((item) => item.key === "image")).toMatchObject({
      status: "skipped",
      message: "保持页面当前值，请手动选择",
    });
    expect(document.querySelector("#official-tab")?.getAttribute("aria-selected")).toBe("true");
    expect(document.querySelector('input[placeholder="请输入镜像地址"]')).toBeNull();
    expect(report.items.find((item) => item.key === "manual-review")).toMatchObject({
      status: "skipped",
      message: "请手动调整实例规格，并确认共享内存、WebIDE、SSH 访问和优先级",
    });
  });

  it("fails closed before interacting when workspace differs", async () => {
    installFixture();
    const radio = document.querySelector<HTMLInputElement>("#queue-target")!;
    const wrongUrl = pageUrl.replace("ws-example", "ws-other");

    const report = await fillDevelopmentForm(structuredClone(profile), document, wrongUrl);

    expect(report).toMatchObject({ ok: false, fatal: true });
    expect(radio.checked).toBe(false);
    expect(document.querySelector('input[placeholder="请输入镜像地址"]')).toBeNull();
  });

  it("retries when selecting the queue asynchronously replaces the RDMA control", async () => {
    installFixture({ refreshRdma: true });
    const withoutMounts = structuredClone(profile);
    withoutMounts.mounts = [];

    const report = await fillDevelopmentForm(withoutMounts, document, pageUrl);

    expect(report.ok).toBe(true);
    expect(report.items.find((item) => item.key === "rdma")?.status).toBe("filled");
  }, 5_000);

  it("fills multiple object mounts even though the page repeats field ids", async () => {
    installFixture({ kmsCredentials: [["bucket-example"], []] });
    const multiple = structuredClone(profile);
    const objectMount = multiple.mounts.find((mount) => mount.type === "PV_AOSS")!;
    multiple.mounts = [
      objectMount,
      {
        ...objectMount,
        name: "bucket-second",
        mountPath: "/data/objects-second",
        accessKey: "SECOND_ACCESS_KEY",
        secretKey: "SECOND_SECRET_VALUE",
      },
    ];

    const report = await fillDevelopmentForm(multiple, document, pageUrl);

    expect(report.items.filter((item) => item.status === "error")).toEqual([]);
    expect(report.ok).toBe(true);
    expect(report.items.filter((item) => item.key.startsWith("mount.aoss"))).toHaveLength(2);
    expect(Array.from(document.querySelectorAll<HTMLInputElement>('input[placeholder="请输入路径，如 /data"]')).map((input) => input.value))
      .toEqual(["/data/objects", "/data/objects-second"]);
    expect(Array.from(document.querySelectorAll<HTMLElement>('.sensed-select-selection-item')).map((item) => item.textContent))
      .toEqual(["bucket-example"]);
    expect(Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"]')).map((input) => input.value))
      .toEqual(["SECOND_SECRET_VALUE"]);
  });

  it("scopes retained AFS options to the current mount row", async () => {
    installFixture();
    const multiple = structuredClone(profile);
    multiple.mounts = [
      { type: "PV_AFS", id: "uuid-models", subdir: "models", mountPath: "/data/models" },
      { type: "PV_AFS", id: "uuid-archive", subdir: "archive", mountPath: "/data/archive" },
    ];

    const report = await fillDevelopmentForm(multiple, document, pageUrl);

    expect(report.ok).toBe(true);
    expect(report.items.filter((item) => item.key.startsWith("mount.afs"))).toHaveLength(2);
    expect(Array.from(
      document.querySelectorAll<HTMLInputElement>('input[placeholder="请输入如 /data 或 /data/data.txt"]'),
    ).map((input) => input.value)).toEqual(["/data/models", "/data/archive"]);
  });

  it("selects an AFS name obtained from the resource UUID catalog", async () => {
    installFixture();
    const onlyAfs = structuredClone(profile);
    onlyAfs.mounts = [onlyAfs.mounts[0]];

    const report = await fillDevelopmentForm(onlyAfs, document, pageUrl, {
      "afs-example": "afs-team-models",
    });

    expect(report.ok).toBe(true);
    expect(report.items.find((item) => item.key === "mount.afs.0")).toMatchObject({
      status: "filled",
      message: "已通过资源 UUID 精确匹配并设为读写模式",
    });
  });

  it("does not add duplicate mounts when fill is run repeatedly", async () => {
    installFixture();

    const first = await fillDevelopmentForm(structuredClone(profile), document, pageUrl);
    const afsCount = document.querySelectorAll('input[id^="dataSource_"][id$="_afs"]').length;
    const aossCount = document.querySelectorAll('input[placeholder="请输入路径，如 /data"]').length;
    const second = await fillDevelopmentForm(structuredClone(profile), document, pageUrl);

    expect(first.ok).toBe(true);
    expect(second.ok).toBe(true);
    expect(document.querySelectorAll('input[id^="dataSource_"][id$="_afs"]')).toHaveLength(afsCount);
    expect(document.querySelectorAll('input[placeholder="请输入路径，如 /data"]')).toHaveLength(aossCount);
    expect(second.items.find((item) => item.key === "mount.afs.0")).toMatchObject({
      status: "skipped",
      message: "页面已存在相同挂载，未重复添加",
    });
    expect(second.items.find((item) => item.key === "mount.aoss.0")).toMatchObject({
      status: "skipped",
      message: "页面已存在相同挂载，并已选择同名 KMS 凭据，未重复添加",
    });
  });

  it("does not duplicate an existing manual object mount", async () => {
    installFixture({ kmsCredentials: [[]] });
    const onlyAoss = structuredClone(profile);
    onlyAoss.mounts = [onlyAoss.mounts[1]];

    const first = await fillDevelopmentForm(structuredClone(onlyAoss), document, pageUrl);
    const aossCount = document.querySelectorAll('input[placeholder="请输入路径，如 /data"]').length;
    const second = await fillDevelopmentForm(structuredClone(onlyAoss), document, pageUrl);

    expect(first.ok).toBe(true);
    expect(second.ok).toBe(true);
    expect(document.querySelectorAll('input[placeholder="请输入路径，如 /data"]')).toHaveLength(aossCount);
    expect(second.items.find((item) => item.key === "mount.aoss.0")).toMatchObject({
      status: "skipped",
      message: "页面已存在相同挂载，并使用手动 AK/SK，未重复添加",
    });
  });

  it("refuses to overwrite a conflicting object mount", async () => {
    installFixture();
    const onlyAoss = structuredClone(profile);
    onlyAoss.mounts = [onlyAoss.mounts[1]];
    const first = await fillDevelopmentForm(structuredClone(onlyAoss), document, pageUrl);
    const endpoint = document.querySelector<HTMLInputElement>('input[placeholder="请输入http(s)://yourdomain.xxx"]')!;
    endpoint.value = "https://conflicting.example";

    const second = await fillDevelopmentForm(structuredClone(onlyAoss), document, pageUrl);

    expect(first.ok).toBe(true);
    expect(second.ok).toBe(false);
    expect(endpoint.value).toBe("https://conflicting.example");
    expect(second.items.find((item) => item.key === "mount.aoss.0")).toMatchObject({
      status: "error",
      message: "挂载路径已存在，但对象存储字段与配置不一致，已拒绝覆盖",
    });
  });

  it("treats a missing queue as fatal before image or secret fields", async () => {
    installFixture();
    const missing = structuredClone(profile);
    missing.queue = "queue-missing";

    const report = await fillDevelopmentForm(missing, document, pageUrl);

    expect(report).toMatchObject({ ok: false, fatal: true });
    expect(document.querySelector('input[placeholder="请选择密钥凭据"]')).toBeNull();
    expect(document.querySelector("#custom-image")).toBeNull();
  });

  it("keeps successful fields and reports an unavailable AFS mount", async () => {
    installFixture({ afsOption: false });
    const onlyAfs = structuredClone(profile);
    onlyAfs.mounts = [onlyAfs.mounts[0]];

    const report = await fillDevelopmentForm(onlyAfs, document, pageUrl);

    expect(report.ok).toBe(false);
    expect(report.fatal).toBe(false);
    expect(report.items.find((item) => item.key === "queue")?.status).toBe("filled");
    expect(report.items.find((item) => item.key === "mount.afs.0")?.status).toBe("error");
  }, 7_000);
});
