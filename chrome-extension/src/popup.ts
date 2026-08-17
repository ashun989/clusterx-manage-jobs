import { ConfigError, parseClusterxConfig } from "./config";
import type {
  AfsCatalogRequest,
  AfsCatalogResponse,
  FillReport,
  FillRequest,
  ResolvedClusterxProfile,
} from "./types";
import { scrubProfileSecrets } from "./types";

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error("Popup markup is incomplete");
  return element;
}

const fileInput = requiredElement<HTMLInputElement>("#config-file");
const chooseFileButton = requiredElement<HTMLButtonElement>("#choose-file");
const fillButton = requiredElement<HTMLButtonElement>("#fill");
const state = requiredElement<HTMLElement>("#state");
const results = requiredElement<HTMLUListElement>("#results");
const version = requiredElement<HTMLElement>("#extension-version");

version.textContent = typeof chrome !== "undefined" && chrome.runtime?.getManifest
  ? chrome.runtime.getManifest().version
  : "dev";

let selectedFile: File | undefined;

type RememberingFilePicker = (options: {
  id: string;
  multiple: boolean;
  types: Array<{ description: string; accept: Record<string, string[]> }>;
}) => Promise<Array<{ getFile(): Promise<File> }>>;

function selectFile(file: File | undefined): void {
  selectedFile = file;
  fillButton.disabled = !file;
  fillButton.textContent = file ? "填充当前创建页" : "选择配置后填充";
  setState(file ? `已选择 ${file.name}` : "尚未选择配置文件");
  results.replaceChildren();
}

function setState(message: string, error = false): void {
  state.textContent = message;
  state.classList.toggle("error", error);
}

function renderReport(report: FillReport): void {
  results.replaceChildren();
  for (const item of report.items) {
    const row = document.createElement("li");
    row.className = item.status;
    row.textContent = `${item.label}：${item.message}`;
    results.append(row);
  }
  setState(report.ok ? "填充完成，请复核表单" : report.fatal ? "未填充：安全检查未通过" : "部分内容未能填充", !report.ok);
}

function readLocalText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(typeof reader.result === "string" ? reader.result : ""));
    reader.addEventListener("error", () => reject(new Error("无法读取本地配置文件")));
    reader.readAsText(file, "utf-8");
  });
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function loadAfsCatalog(region: string, ids: string[]): Promise<Record<string, string>> {
  if (ids.length === 0) return {};
  const url = new URL(`/${encodeURIComponent(region)}/afs/list`, "https://console.d.pjlab.org.cn");
  const tab = await chrome.tabs.create({ url: url.href, active: false });
  if (!tab.id) throw new Error("无法打开文件存储列表");
  try {
    const request: AfsCatalogRequest = { type: "clusterx.read-afs-catalog", ids };
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      try {
        const response = (await chrome.tabs.sendMessage(tab.id, request)) as AfsCatalogResponse;
        if (!response?.ok || !response.catalog || typeof response.catalog !== "object") {
          throw new Error("无法读取文件存储 UUID 映射");
        }
        return response.catalog;
      } catch (error) {
        if (error instanceof Error && !/Receiving end does not exist/i.test(error.message)) throw error;
        await delay(250);
      }
    }
    throw new Error("文件存储列表加载超时");
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => undefined);
  }
}

chooseFileButton.addEventListener("click", async () => {
  const picker = (window as Window & { showOpenFilePicker?: RememberingFilePicker }).showOpenFilePicker;
  if (!picker) {
    fileInput.click();
    return;
  }
  try {
    const [handle] = await picker.call(window, {
      id: "clusterx-config-yaml",
      multiple: false,
      types: [{
        description: "ClusterX YAML",
        accept: { "application/yaml": [".yaml", ".yml"] },
      }],
    });
    selectFile(await handle?.getFile());
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return;
    setState("无法打开配置文件选择器", true);
  }
});

fileInput.addEventListener("change", () => selectFile(fileInput.files?.[0]));

fillButton.disabled = true;
fillButton.addEventListener("click", async () => {
  const file = selectedFile;
  if (!file) return;

  fillButton.disabled = true;
  setState("正在解析并检查配置…");
  let profile: ResolvedClusterxProfile | undefined;
  let yamlSource = "";
  try {
    yamlSource = await readLocalText(file);
    profile = parseClusterxConfig(yamlSource);
    yamlSource = "";
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw new Error("无法访问当前标签页");
    const afsIds = profile.mounts
      .filter((mount) => mount.type === "PV_AFS")
      .map((mount) => mount.id.toLowerCase());
    let afsCatalog: Record<string, string> = {};
    if (afsIds.length > 0) {
      setState("正在读取文件存储 UUID 映射…");
      try {
        afsCatalog = await loadAfsCatalog(profile.scope.region, afsIds);
      } catch {
        // The form filler retains a unique mount-path fallback and reports it for review.
      }
    }
    const request: FillRequest = { type: "clusterx.fill-development", profile, afsCatalog };
    const report = (await chrome.tabs.sendMessage(tab.id, request)) as FillReport;
    renderReport(report);
  } catch (error) {
    const message = error instanceof ConfigError
      ? error.message
      : error instanceof Error && /Receiving end does not exist/i.test(error.message)
        ? "请打开受支持的开发机创建页；扩展刚安装时需要刷新页面"
        : error instanceof Error
          ? error.message
          : "填充失败";
    results.replaceChildren();
    setState(message, true);
  } finally {
    yamlSource = "";
    if (profile) scrubProfileSecrets(profile);
    selectedFile = undefined;
    fileInput.value = "";
    fillButton.textContent = "选择配置后填充";
    fillButton.disabled = true;
  }
});
