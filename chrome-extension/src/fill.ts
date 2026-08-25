import { validateDevelopmentUrl } from "./scope";
import type {
  AfsMount,
  AossMount,
  FillItemResult,
  FillReport,
  ResolvedClusterxProfile,
} from "./types";

const DEFAULT_TIMEOUT_MS = 5_000;

class PageControlError extends Error {}

function text(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

function isUsable(element: Element): boolean {
  if (!(element instanceof HTMLElement)) return false;
  if (element.hidden || element.getAttribute("aria-hidden") === "true") return false;
  if (
    element.hasAttribute("disabled")
    || element.getAttribute("aria-disabled") === "true"
    || element.classList.contains("sensed-typography-disabled")
  ) return false;
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    if (current.hidden || current.getAttribute("aria-hidden") === "true") return false;
    if (
      current.style.display === "none"
      || current.style.visibility === "hidden"
      || current.style.pointerEvents === "none"
      || current.classList.contains("sensed-select-dropdown-hidden")
    ) return false;
  }
  return true;
}

async function waitFor<T>(
  document: Document,
  find: () => T | undefined,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const immediate = find();
  if (immediate !== undefined) return immediate;

  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      observer.disconnect();
      reject(new PageControlError("页面控件等待超时"));
    }, timeoutMs);
    const observer = new MutationObserver(() => {
      let value: T | undefined;
      try {
        value = find();
      } catch (error) {
        window.clearTimeout(timer);
        observer.disconnect();
        reject(error);
        return;
      }
      if (value === undefined) return;
      window.clearTimeout(timer);
      observer.disconnect();
      resolve(value);
    });
    observer.observe(document.documentElement, {
      attributes: true,
      childList: true,
      subtree: true,
    });
  });
}

function unique<T>(values: T[], missingMessage: string, ambiguousMessage: string): T {
  if (values.length === 0) throw new PageControlError(missingMessage);
  if (values.length > 1) throw new PageControlError(ambiguousMessage);
  return values[0];
}

function inputSetter(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (!setter) throw new PageControlError("浏览器不支持设置输入框");
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

function exactPlaceholder(root: ParentNode, placeholder: string): HTMLInputElement {
  const matches = Array.from(root.querySelectorAll<HTMLInputElement>("input"))
    .filter((input) => isUsable(input) && input.placeholder === placeholder);
  return unique(matches, `页面缺少“${placeholder}”输入框`, `页面存在多个“${placeholder}”输入框`);
}

function optionCandidates(option: HTMLElement): string[] {
  const values = new Set<string>();
  for (const value of [
    option.textContent,
    option.getAttribute("aria-label"),
    option.getAttribute("title"),
    option.getAttribute("name"),
    option.getAttribute("value"),
    option.getAttribute("data-value"),
  ]) {
    const normalized = text(value);
    if (normalized) values.add(normalized);
  }
  for (const descendant of Array.from(option.querySelectorAll<HTMLElement>("*"))) {
    if (descendant.children.length === 0) {
      const normalized = text(descendant.textContent);
      if (normalized) values.add(normalized);
    }
  }
  return [...values];
}

function activateCombobox(control: HTMLElement): void {
  const select = control.closest<HTMLElement>(".sensed-select");
  const target = select?.querySelector<HTMLElement>(".sensed-select-selector") ?? control;
  target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, button: 0, buttons: 1 }));
  target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, button: 0 }));
  target.click();
}

function optionRootsForControl(document: Document, control?: HTMLElement): HTMLElement[] {
  if (control?.getAttribute("aria-expanded") === "true") {
    const dropdowns = Array.from(
      document.querySelectorAll<HTMLElement>(".sensed-select-dropdown"),
    ).filter((element) =>
      isUsable(element)
      && !element.classList.contains("sensed-select-dropdown-hidden"),
    );
    if (dropdowns.length > 0) return dropdowns;
  }

  const ids = [control?.getAttribute("aria-controls"), control?.getAttribute("aria-owns")]
    .flatMap((value) => (value ?? "").split(/\s+/))
    .filter(Boolean);
  return [...new Set(ids)]
    .map((id) => document.getElementById(id))
    .filter((element): element is HTMLElement => element !== null);
}

interface OptionGroups {
  visual: HTMLElement[];
  accessible: HTMLElement[];
}

function optionGroupsForControl(document: Document, control?: HTMLElement): OptionGroups {
  const roots = optionRootsForControl(document, control);
  const queryRoots: ParentNode[] = roots.length > 0 ? roots : [document];
  const visualOptions = queryRoots.flatMap((root) =>
    Array.from(root.querySelectorAll<HTMLElement>(".sensed-select-item-option")),
  ).filter(isUsable);
  const accessibleOptions = queryRoots.flatMap((root) =>
    Array.from(root.querySelectorAll<HTMLElement>('[role="option"]')),
  ).filter(isUsable);
  return {
    visual: [...new Set(visualOptions)],
    accessible: [...new Set(accessibleOptions)],
  };
}

function optionsForControl(document: Document, control?: HTMLElement): HTMLElement[] {
  const groups = optionGroupsForControl(document, control);
  return groups.visual.length > 0 ? groups.visual : groups.accessible;
}

function matchingOptions(document: Document, value: string, control?: HTMLElement): HTMLElement[] {
  const groups = optionGroupsForControl(document, control);
  const visualMatches = groups.visual.filter((option) => optionCandidates(option).includes(value));
  if (visualMatches.length > 0 || groups.visual.length === 0) {
    return visualMatches.length > 0
      ? visualMatches
      : groups.accessible.filter((option) => optionCandidates(option).includes(value));
  }

  const accessibleMatches = groups.accessible
    .filter((option) => optionCandidates(option).includes(value));
  const visibleNames = new Set(accessibleMatches.flatMap((option) => [
    option.getAttribute("aria-label"),
    option.getAttribute("title"),
    option.getAttribute("name"),
  ].map(text).filter(Boolean)));
  const mappedVisual = groups.visual.filter((option) =>
    optionCandidates(option).some((candidate) => visibleNames.has(candidate)),
  );
  return mappedVisual.length > 0 ? mappedVisual : accessibleMatches;
}

async function chooseOption(
  document: Document,
  control: HTMLElement,
  value: string,
  missingMessage: string,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<void> {
  activateCombobox(control);
  const option = await waitFor(document, () => {
    const matches = matchingOptions(document, value, control);
    if (matches.length > 1) throw new PageControlError("下拉列表存在多个精确匹配项");
    return matches[0];
  }, timeoutMs).catch((error: unknown) => {
    if (error instanceof PageControlError && error.message.includes("多个")) throw error;
    throw new PageControlError(missingMessage);
  });
  option.click();
}

type CredentialChoice = "selected" | "missing" | "ambiguous";

function dropdownIsReady(roots: HTMLElement[], options: HTMLElement[]): boolean {
  if (options.length > 0) return true;
  return roots.some((root) =>
    !!root.querySelector('[class*="select-item-empty"]')
    || text(root.textContent).includes("暂无可用的凭据"),
  );
}

function scrollContainerFor(roots: HTMLElement[]): HTMLElement | undefined {
  const candidates = new Set<HTMLElement>();
  for (const root of roots) {
    candidates.add(root);
    root.querySelectorAll<HTMLElement>("*").forEach((element) => candidates.add(element));
    for (let current = root.parentElement; current && current !== root.ownerDocument.body; current = current.parentElement) {
      candidates.add(current);
    }
  }
  return [...candidates]
    .filter((element) => element.scrollHeight > element.clientHeight)
    .sort((left, right) => {
      const scrollPriority = (element: HTMLElement): number => {
        const overflow = element.style.overflowY;
        return /virtual-list-holder|select-list-holder/.test(element.className)
          || overflow === "auto"
          || overflow === "scroll"
          ? 1
          : 0;
      };
      return scrollPriority(right) - scrollPriority(left)
        || (right.scrollHeight - right.clientHeight) - (left.scrollHeight - left.clientHeight);
    })[0];
}

function afterDropdownScroll(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 20));
}

async function chooseCredentialOption(
  document: Document,
  control: HTMLElement,
  credentialName: string,
): Promise<CredentialChoice> {
  activateCombobox(control);
  let roots: HTMLElement[];
  try {
    roots = await waitFor(document, () => {
      const currentRoots = optionRootsForControl(document, control);
      if (currentRoots.length === 0) return undefined;
      const options = optionsForControl(document, control);
      return dropdownIsReady(currentRoots, options) ? currentRoots : undefined;
    });
  } catch {
    return "missing";
  }

  const scrollContainer = scrollContainerFor(roots);
  const matchedIdentities = new Set<string>();
  const matchedElements = new Set<HTMLElement>();
  let matchedScrollTop = scrollContainer?.scrollTop ?? 0;
  const matchCount = (): number => matchedIdentities.size + matchedElements.size;
  const collectMatches = (): CredentialChoice | undefined => {
    const matches = matchingOptions(document, credentialName, control);
    if (matches.length > 1) return "ambiguous";
    if (matches.length === 1) {
      if (matchCount() === 0) matchedScrollTop = scrollContainer?.scrollTop ?? 0;
      const option = matches[0];
      const identity = ["data-value", "value", "id", "aria-posinset"]
        .map((attribute) => option.getAttribute(attribute))
        .find((value) => !!value);
      if (identity) matchedIdentities.add(identity);
      else matchedElements.add(option);
      if (matchCount() > 1) return "ambiguous";
    }
    return undefined;
  };

  if (collectMatches() === "ambiguous") return "ambiguous";
  if (scrollContainer) {
    const maxScrollTop = scrollContainer.scrollHeight - scrollContainer.clientHeight;
    const step = Math.max(scrollContainer.clientHeight - 24, 1);
    let attempts = 0;
    while (scrollContainer.scrollTop < maxScrollTop && attempts < 100) {
      const next = Math.min(maxScrollTop, scrollContainer.scrollTop + step);
      if (next === scrollContainer.scrollTop) break;
      scrollContainer.scrollTop = next;
      scrollContainer.dispatchEvent(new Event("scroll", { bubbles: true }));
      await afterDropdownScroll();
      if (collectMatches() === "ambiguous") return "ambiguous";
      attempts += 1;
    }
  }

  if (matchCount() === 0) return "missing";
  if (scrollContainer && scrollContainer.scrollTop !== matchedScrollTop) {
    scrollContainer.scrollTop = matchedScrollTop;
    scrollContainer.dispatchEvent(new Event("scroll", { bubbles: true }));
    await afterDropdownScroll();
  }
  const visibleMatches = matchingOptions(document, credentialName, control);
  if (visibleMatches.length !== 1) {
    return visibleMatches.length > 1 ? "ambiguous" : "missing";
  }
  visibleMatches[0].click();
  return "selected";
}

async function selectQueue(document: Document, queue: string): Promise<void> {
  const rows = Array.from(document.querySelectorAll<HTMLTableRowElement>("table tbody tr"));
  const matches = rows.filter((row) => Array.from(row.querySelectorAll<HTMLElement>("td, td *"))
    .some((element) => text(element.textContent) === queue));
  const row = unique(matches, "配置中的队列不在当前页面", "页面存在多个同名队列");
  const radio = row.querySelector<HTMLInputElement>('input[type="radio"]');
  if (!radio) throw new PageControlError("队列行缺少选择控件");
  if (!radio.checked) radio.click();
}

async function chooseAfsOption(
  document: Document,
  control: HTMLElement,
  mount: AfsMount,
  afsCatalog: Readonly<Record<string, string>>,
): Promise<"id" | "catalog" | "mount-path"> {
  activateCombobox(control);
  const options = await waitFor(document, () => {
    const visible = optionsForControl(document, control);
    return visible.length > 0 ? visible : undefined;
  });
  const exact = matchingOptions(document, mount.id, control);
  if (exact.length === 1) {
    exact[0].click();
    return "id";
  }
  if (exact.length > 1) throw new PageControlError("页面存在多个相同 ID 的文件存储卷");

  const catalogName = afsCatalog[mount.id.toLowerCase()];
  if (catalogName) {
    const catalogMatches = matchingOptions(document, catalogName, control);
    if (catalogMatches.length !== 1) {
      throw new PageControlError(
        catalogMatches.length > 1
          ? "资源 UUID 对应多个同名文件存储卷，已拒绝猜测"
          : "资源 UUID 对应的文件存储卷不在当前工作空间",
      );
    }
    catalogMatches[0].click();
    return "catalog";
  }

  const basename = mount.mountPath.split("/").filter(Boolean).at(-1)?.toLowerCase();
  if (!basename) throw new PageControlError("配置中的文件存储卷不在当前工作空间");
  const suffixMatches = options.filter((option) => optionCandidates(option).some((candidate) => {
    const normalized = candidate.toLowerCase();
    return normalized === basename
      || normalized.endsWith(`-${basename}`)
      || normalized.endsWith(`_${basename}`);
  }));
  if (suffixMatches.length !== 1) {
    throw new PageControlError(
      suffixMatches.length > 1
        ? "挂载路径对应多个文件存储卷，已拒绝猜测"
        : "配置中的文件存储卷不在当前工作空间",
    );
  }
  suffixMatches[0].click();
  return "mount-path";
}

async function selectRdma(document: Document, rdmaName: string): Promise<void> {
  let lastError: unknown;
  // Changing the queue can replace the RDMA selector asynchronously. Retry with
  // a freshly queried control instead of holding a detached input reference.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const control = await waitFor(document, () =>
      document.querySelector<HTMLElement>('#rdma_name[role="combobox"]') ?? undefined,
    );
    const selectedContainer = control.parentElement?.parentElement;
    if (selectedContainer && text(selectedContainer.textContent) === rdmaName) return;
    try {
      await chooseOption(
        document,
        control,
        rdmaName,
        "配置中的 RDMA 网络不在当前页面",
        1_500,
      );
      return;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError ?? new PageControlError("配置中的 RDMA 网络不在当前页面");
}

function exactTextElement(root: ParentNode, value: string): HTMLElement {
  const matches = Array.from(root.querySelectorAll<HTMLElement>("a, article, button, span, div"))
    .filter((element) => isUsable(element) && text(element.textContent) === value)
    .filter((element) => !Array.from(element.children).some((child) => text(child.textContent) === value));
  return unique(matches, `页面缺少“${value}”入口`, `页面存在多个“${value}”入口`);
}

function clickableFor(element: HTMLElement): HTMLElement {
  return element.closest<HTMLElement>("a, button, article, [role=button]") ?? element;
}

function closestContainer(start: HTMLElement, predicate: (element: HTMLElement) => boolean): HTMLElement {
  const body = start.ownerDocument.body;
  for (let current = start.parentElement; current && current !== body; current = current.parentElement) {
    if (predicate(current)) return current;
  }
  throw new PageControlError("无法定位新添加的挂载配置行");
}

function afsContainerFromSource(source: HTMLInputElement): HTMLElement {
  return closestContainer(source, (element) =>
    element.querySelectorAll('[role="combobox"]').length >= 2
    && !!element.querySelector('input[placeholder="请输入如 /data 或 /data/data.txt"]'),
  );
}

interface AfsRow {
  source: HTMLInputElement;
  container: HTMLElement;
  subdir: HTMLInputElement;
  mountPath: HTMLInputElement;
}

function afsRows(document: Document): AfsRow[] {
  const rows: AfsRow[] = [];
  for (const source of Array.from(
    document.querySelectorAll<HTMLInputElement>('input[id^="dataSource_"][id$="_afs"]'),
  )) {
    try {
      const container = afsContainerFromSource(source);
      const subdir = exactPlaceholder(container, "如 data、data/model或data/data/txt");
      const mountPath = exactPlaceholder(container, "请输入如 /data 或 /data/data.txt");
      rows.push({ source, container, subdir, mountPath });
    } catch {
      // Ignore unrelated or incomplete controls; the normal add flow will report them.
    }
  }
  return rows;
}

function existingAfsMount(
  document: Document,
  mount: AfsMount,
  index: number,
): FillItemResult | undefined {
  const matches = afsRows(document).filter((row) => row.mountPath.value === mount.mountPath);
  if (matches.length > 1) {
    throw new PageControlError("页面已存在多个相同挂载路径的文件存储卷");
  }
  const existing = matches[0];
  if (!existing) return undefined;
  if (existing.subdir.value !== (mount.subdir ?? "")) {
    throw new PageControlError("挂载路径已存在，但文件存储子目录与配置不一致，已拒绝覆盖");
  }
  return {
    key: `mount.afs.${index}`,
    label: `文件存储挂载 ${index + 1}`,
    status: "skipped",
    message: "页面已存在相同挂载，未重复添加",
  };
}

function labeledInput(container: HTMLElement, labelText: string): HTMLInputElement | undefined {
  for (const label of Array.from(container.querySelectorAll<HTMLLabelElement>("label"))) {
    const normalizedLabel = text(label.textContent)
      .replace(/^[*＊]\s*/, "")
      .replace(/\s*[:：]$/, "");
    if (normalizedLabel !== labelText) continue;
    if (label.control instanceof HTMLInputElement && container.contains(label.control)) {
      return label.control;
    }
    const target = label.htmlFor
      ? Array.from(container.querySelectorAll<HTMLInputElement>("input"))
        .find((input) => input.id === label.htmlFor) ?? null
      : null;
    if (target) return target;
    for (let current = label.parentElement; current && current !== container; current = current.parentElement) {
      const inputs = current.querySelectorAll<HTMLInputElement>("input");
      if (inputs.length === 1) return inputs[0];
    }
  }
  return undefined;
}

async function addAfsMount(
  document: Document,
  mount: AfsMount,
  index: number,
  afsCatalog: Readonly<Record<string, string>>,
): Promise<FillItemResult> {
  const existing = existingAfsMount(document, mount, index);
  if (existing) return existing;

  const reusable = afsRows(document).find((row) =>
    row.mountPath.value === "" && row.subdir.value === "",
  );
  let source: HTMLInputElement;
  let container: HTMLElement;
  if (reusable) {
    ({ source, container } = reusable);
  } else {
    const before = document.querySelectorAll<HTMLInputElement>('input[id^="dataSource_"][id$="_afs"]').length;
    clickableFor(exactTextElement(document, "添加文件存储卷挂载")).click();
    source = await waitFor(document, () => {
      const controls = document.querySelectorAll<HTMLInputElement>('input[id^="dataSource_"][id$="_afs"]');
      return controls.length > before ? controls[controls.length - 1] : undefined;
    });
    container = afsContainerFromSource(source);
  }

  const matchedBy = await chooseAfsOption(document, source, mount, afsCatalog);
  const accessMode = Array.from(container.querySelectorAll<HTMLElement>('[role="combobox"]'))
    .find((control) => control !== source);
  if (!accessMode) throw new PageControlError("文件存储挂载缺少访问模式");
  await chooseOption(
    document,
    accessMode,
    "VOLUME_ACCESS_MODE_READ_WRITE",
    "文件存储卷不支持读写访问模式",
  );
  if (mount.subdir) inputSetter(exactPlaceholder(container, "如 data、data/model或data/data/txt"), mount.subdir);
  inputSetter(exactPlaceholder(container, "请输入如 /data 或 /data/data.txt"), mount.mountPath);
  return {
    key: `mount.afs.${index}`,
    label: `文件存储挂载 ${index + 1}`,
    status: matchedBy === "mount-path" ? "warning" : "filled",
    message: matchedBy === "catalog"
      ? "已通过资源 UUID 精确匹配并设为读写模式"
      : matchedBy === "id"
        ? "已按资源 UUID 精确匹配并设为读写模式"
        : "未获得 UUID 映射，已通过挂载路径唯一匹配并设为读写模式；请复核",
  };
}

const AOSS_KMS_LABEL = "Access Key ID（AK） / Secret Access Key（SK）";
const AOSS_AK_LABEL = "Access Key ID（AK）";
const AOSS_SK_LABEL = "Secret Access Key（SK）";
const AOSS_BUCKET_LABEL = "Bucket名称";
const AOSS_KMS_PLACEHOLDER = "请选择密钥凭据";
const AOSS_MANUAL_SWITCH = "切换为手动填写AK/SK";

type AossAuthFields =
  | { mode: "kms"; control: HTMLInputElement }
  | { mode: "manual"; accessKey: HTMLInputElement; secretKey: HTMLInputElement };

const AOSS_MOUNT_PATH_SELECTOR = 'input[placeholder="请输入路径，如 /data"]';

function usableInputs(container: ParentNode): HTMLInputElement[] {
  return Array.from(container.querySelectorAll<HTMLInputElement>("input")).filter(isUsable);
}

function aossCommonContainerFromMountInput(mountInput: HTMLInputElement): HTMLElement {
  const body = mountInput.ownerDocument.body;
  for (let current = mountInput.parentElement; current && current !== body; current = current.parentElement) {
    const mountInputs = Array.from(
      current.querySelectorAll<HTMLInputElement>(AOSS_MOUNT_PATH_SELECTOR),
    ).filter(isUsable);
    if (mountInputs.length === 1 && mountInputs[0] === mountInput && usableInputs(current).length >= 4) {
      return current;
    }
  }
  throw new PageControlError("无法定位新添加的挂载配置行");
}

function aossContainerFromMountInput(
  mountInput: HTMLInputElement,
  commonContainer: HTMLElement,
): HTMLElement {
  const body = mountInput.ownerDocument.body;
  for (let current: HTMLElement | null = commonContainer; current && current !== body; current = current.parentElement) {
    const hasAuthFields = !!labeledInput(current, AOSS_KMS_LABEL)
      || usableInputs(current).some((input) => input.getAttribute("role") === "combobox")
      || (!!labeledInput(current, AOSS_AK_LABEL) && !!labeledInput(current, AOSS_SK_LABEL))
      || (usableInputs(current).some((input) => /access\s*key\s*id|\bAK\b/i.test(input.placeholder))
        && usableInputs(current).some((input) => /secret\s*access\s*key|\bSK\b/i.test(input.placeholder)));
    if (hasAuthFields) return current;
  }
  throw new PageControlError("对象存储挂载行缺少凭据控件");
}

function uniqueMatchingInput(
  container: ParentNode,
  predicate: (input: HTMLInputElement) => boolean,
  missingMessage: string,
  ambiguousMessage: string,
): HTMLInputElement {
  return unique(usableInputs(container).filter(predicate), missingMessage, ambiguousMessage);
}

function aossBucketInput(commonContainer: HTMLElement): HTMLInputElement | undefined {
  const labeled = labeledInput(commonContainer, AOSS_BUCKET_LABEL);
  if (labeled) return labeled;

  const byId = Array.from(commonContainer.querySelectorAll<HTMLInputElement>(
    'input[id^="dataSource_"][id$="_aoss"]',
  )).filter(isUsable);
  if (byId.length > 1) {
    throw new PageControlError("对象存储挂载存在多个 Bucket 输入框");
  }
  if (byId[0]) return byId[0];

  const byPlaceholder = usableInputs(commonContainer)
    .filter((input) => /bucket/i.test(input.placeholder));
  if (byPlaceholder.length > 1) {
    throw new PageControlError("对象存储挂载存在多个候选 Bucket 输入框");
  }
  if (byPlaceholder[0]) return byPlaceholder[0];

  const generic = Array.from(commonContainer.querySelectorAll<HTMLInputElement>(
    'input[placeholder="请输入"]',
  )).filter(isUsable);
  if (generic.length > 1) {
    throw new PageControlError("对象存储挂载存在多个候选 Bucket 输入框");
  }
  return generic[0];
}

interface AossFields {
  container: HTMLElement;
  auth: AossAuthFields;
  bucket: HTMLInputElement;
  endpoint: HTMLInputElement;
  subdir?: HTMLInputElement;
  mountPath: HTMLInputElement;
}

function aossFields(mountInput: HTMLInputElement): AossFields {
  const commonContainer = aossCommonContainerFromMountInput(mountInput);
  const container = aossContainerFromMountInput(mountInput, commonContainer);
  const unlabeledComboboxes = usableInputs(container).filter((input) =>
    input.getAttribute("role") === "combobox" && input !== mountInput,
  );
  const kmsControl = labeledInput(container, AOSS_KMS_LABEL)
    ?? container.querySelector<HTMLInputElement>(
      `input[role="combobox"][placeholder="${AOSS_KMS_PLACEHOLDER}"]`,
    )
    ?? (unlabeledComboboxes.length === 1 ? unlabeledComboboxes[0] : undefined)
    ?? undefined;
  const accessKey = labeledInput(container, AOSS_AK_LABEL)
    ?? usableInputs(container).find((input) => /access\s*key\s*id|\bAK\b/i.test(input.placeholder))
    ?? undefined;
  const secretKey = labeledInput(container, AOSS_SK_LABEL)
    ?? usableInputs(container).find((input) => /secret\s*access\s*key|\bSK\b/i.test(input.placeholder))
    ?? undefined;
  const bucket = aossBucketInput(commonContainer);
  const endpoint = uniqueMatchingInput(
    commonContainer,
    (input) => /https?|endpoint/i.test(input.placeholder),
    "对象存储挂载缺少 Endpoint 输入框",
    "对象存储挂载存在多个 Endpoint 输入框",
  );
  const subdirMatches = usableInputs(commonContainer)
    .filter((input) => /存储目录|子目录|bucket.*目录/i.test(input.placeholder));
  if (subdirMatches.length > 1) {
    throw new PageControlError("对象存储挂载存在多个存储目录输入框");
  }
  const subdir = subdirMatches[0];
  let auth: AossAuthFields;
  if (kmsControl?.getAttribute("role") === "combobox") {
    auth = { mode: "kms", control: kmsControl };
  } else if (accessKey && secretKey) {
    auth = { mode: "manual", accessKey, secretKey };
  } else {
    throw new PageControlError("对象存储凭据字段结构不完整");
  }
  if (!bucket) {
    throw new PageControlError("对象存储挂载字段结构不完整");
  }
  return { container, auth, bucket, endpoint, subdir, mountPath: mountInput };
}

function selectedComboboxValues(control: HTMLInputElement): string[] {
  const select = control.closest<HTMLElement>(".sensed-select") ?? control.parentElement;
  const values = new Set<string>();
  if (control.value) values.add(text(control.value));
  select?.querySelectorAll<HTMLElement>(
    '.sensed-select-selection-item, [class*="select-selection-item"]',
  ).forEach((element) => {
    for (const candidate of optionCandidates(element)) values.add(candidate);
  });
  values.delete("");
  values.delete(AOSS_KMS_PLACEHOLDER);
  return [...values];
}

function aossAuthMatches(auth: AossAuthFields, mount: AossMount): boolean {
  if (auth.mode === "kms") {
    return selectedComboboxValues(auth.control).includes(mount.name);
  }
  return auth.accessKey.value === mount.accessKey && auth.secretKey.value === mount.secretKey;
}

function aossFieldsAreEmpty(fields: AossFields): boolean {
  const commonFieldsEmpty = fields.bucket.value === ""
    && fields.endpoint.value === ""
    && (fields.subdir?.value ?? "") === ""
    && fields.mountPath.value === "";
  if (!commonFieldsEmpty) return false;
  return fields.auth.mode === "kms"
    ? selectedComboboxValues(fields.auth.control).length === 0
    : fields.auth.accessKey.value === "" && fields.auth.secretKey.value === "";
}

function aossFieldsByMountPath(document: Document, mountPath: string): AossFields | undefined {
  const pathInputs = Array.from(
    document.querySelectorAll<HTMLInputElement>('input[placeholder="请输入路径，如 /data"]'),
  ).filter((input) => input.value === mountPath);
  if (pathInputs.length > 1) {
    throw new PageControlError("页面已存在多个相同挂载路径的对象存储");
  }
  return pathInputs[0] ? aossFields(pathInputs[0]) : undefined;
}

function existingAossMount(
  document: Document,
  mount: AossMount,
  index: number,
): FillItemResult | undefined {
  const fields = aossFieldsByMountPath(document, mount.mountPath);
  if (!fields) return undefined;
  if (mount.subdir && !fields.subdir) {
    throw new PageControlError("配置包含对象存储 subdir，但当前页面没有存储目录输入框");
  }
  const matches = fields.bucket.value === mount.name
    && fields.endpoint.value === mount.endpoint
    && (fields.subdir?.value ?? "") === (mount.subdir ?? "")
    && aossAuthMatches(fields.auth, mount);
  if (!matches) {
    throw new PageControlError("挂载路径已存在，但对象存储字段与配置不一致，已拒绝覆盖");
  }
  return {
    key: `mount.aoss.${index}`,
    label: `对象存储挂载 ${index + 1}`,
    status: "skipped",
    message: fields.auth.mode === "kms"
      ? "页面已存在相同挂载，并已选择同名 KMS 凭据，未重复添加"
      : "页面已存在相同挂载，并使用手动 AK/SK，未重复添加",
  };
}

async function manualAossFields(
  document: Document,
  mount: AossMount,
): Promise<Extract<AossAuthFields, { mode: "manual" }>> {
  return waitFor(document, () => {
    const fields = aossFieldsByMountPath(document, mount.mountPath);
    return fields?.auth.mode === "manual" ? fields.auth : undefined;
  });
}

async function configureAossAuth(
  document: Document,
  fields: AossFields,
  mount: AossMount,
): Promise<{ mode: "kms" | "manual"; ambiguous: boolean }> {
  if (fields.auth.mode === "manual") {
    inputSetter(fields.auth.accessKey, mount.accessKey);
    inputSetter(fields.auth.secretKey, mount.secretKey);
    return { mode: "manual", ambiguous: false };
  }

  const choice = await chooseCredentialOption(document, fields.auth.control, mount.name);
  if (choice === "selected") return { mode: "kms", ambiguous: false };

  clickableFor(exactTextElement(fields.container, AOSS_MANUAL_SWITCH)).click();
  const manual = await manualAossFields(document, mount);
  inputSetter(manual.accessKey, mount.accessKey);
  inputSetter(manual.secretKey, mount.secretKey);
  return { mode: "manual", ambiguous: choice === "ambiguous" };
}

function populateAossCommonFields(fields: AossFields, mount: AossMount): void {
  inputSetter(fields.bucket, mount.name);
  inputSetter(fields.endpoint, mount.endpoint);
  if (fields.subdir) {
    inputSetter(fields.subdir, mount.subdir ?? "");
  } else if (mount.subdir) {
    throw new PageControlError("配置包含对象存储 subdir，但当前页面没有存储目录输入框");
  }
  inputSetter(fields.mountPath, mount.mountPath);
}

function aossAuthAnchor(fields: AossFields): HTMLElement {
  return fields.auth.mode === "kms" ? fields.auth.control : fields.auth.accessKey;
}

async function stableAossFields(document: Document, mountPath: string): Promise<AossFields> {
  let previousAnchor: HTMLElement | undefined;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await afterDropdownScroll();
    const fields = aossFieldsByMountPath(document, mountPath);
    if (!fields) continue;
    const anchor = aossAuthAnchor(fields);
    if (anchor.isConnected && anchor === previousAnchor) return fields;
    previousAnchor = anchor;
  }
  throw new PageControlError("对象存储字段更新后凭据控件仍不稳定");
}

async function addAossMount(document: Document, mount: AossMount, index: number): Promise<FillItemResult> {
  const existing = existingAossMount(document, mount, index);
  if (existing) return existing;

  const selector = 'input[placeholder="请输入路径，如 /data"]';
  let mountInput = Array.from(document.querySelectorAll<HTMLInputElement>(selector)).find((input) => {
    if (input.value !== "") return false;
    try {
      return aossFieldsAreEmpty(aossFields(input));
    } catch {
      return false;
    }
  });
  if (!mountInput) {
    const before = document.querySelectorAll<HTMLInputElement>(selector).length;
    clickableFor(exactTextElement(document, "添加对象存储挂载")).click();
    mountInput = await waitFor(document, () => {
      const inputs = document.querySelectorAll<HTMLInputElement>(selector);
      return inputs.length > before ? inputs[inputs.length - 1] : undefined;
    });
  }
  const fields = aossFields(mountInput);
  populateAossCommonFields(fields, mount);
  const populatedFields = await stableAossFields(document, mount.mountPath);
  const authResult = await configureAossAuth(document, populatedFields, mount);
  const currentFields = await waitFor(document, () => {
    const current = aossFieldsByMountPath(document, mount.mountPath);
    return current && aossAuthMatches(current.auth, mount) ? current : undefined;
  }).catch(() => {
    throw new PageControlError("对象存储凭据选择后未保持生效");
  });
  populateAossCommonFields(currentFields, mount);
  return {
    key: `mount.aoss.${index}`,
    label: `对象存储挂载 ${index + 1}`,
    status: authResult.ambiguous ? "warning" : "filled",
    message: authResult.mode === "kms"
      ? "已填充对象存储字段，并选择同名 KMS 凭据"
      : authResult.ambiguous
        ? "存在多个同名 KMS 凭据，已拒绝猜测并手动填写 AK/SK；请复核"
        : "未找到同名 KMS 凭据，已切换并手动填写 AK/SK",
  };
}

function failure(key: string, label: string, error: unknown): FillItemResult {
  const message = error instanceof PageControlError ? error.message : "页面结构与预期不一致";
  return { key, label, status: "error", message };
}

export async function fillDevelopmentForm(
  profile: ResolvedClusterxProfile,
  document: Document = window.document,
  pageUrl: string = window.location.href,
  afsCatalog: Readonly<Record<string, string>> = {},
): Promise<FillReport> {
  const scopeError = validateDevelopmentUrl(pageUrl, profile.scope);
  if (scopeError) {
    return {
      ok: false,
      fatal: true,
      items: [{ key: "scope", label: "页面范围", status: "error", message: scopeError }],
    };
  }

  const items: FillItemResult[] = [];
  try {
    await selectQueue(document, profile.queue);
    items.push({ key: "queue", label: "队列", status: "filled", message: "已按配置精确选择" });
  } catch (error) {
    items.push(failure("queue", "队列", error));
    return { ok: false, fatal: true, items };
  }
  try {
    await selectRdma(document, profile.rdmaName);
    items.push({ key: "rdma", label: "RDMA 网络", status: "filled", message: "已按配置精确选择" });
  } catch (error) {
    items.push(failure("rdma", "RDMA 网络", error));
    return { ok: false, fatal: true, items };
  }

  items.push({
    key: "image",
    label: "镜像",
    status: "skipped",
    message: "保持页面当前值，请手动选择",
  });
  items.push({
    key: "manual-review",
    label: "人工设置",
    status: "skipped",
    message: "请手动调整实例规格，并确认共享内存、WebIDE、SSH 访问和优先级",
  });

  const afsMounts = profile.mounts.filter((mount): mount is AfsMount => mount.type === "PV_AFS");
  const aossMounts = profile.mounts.filter((mount): mount is AossMount => mount.type === "PV_AOSS");
  if (profile.mounts.length === 0) {
    items.push({ key: "mounts", label: "存储挂载", status: "skipped", message: "配置中没有挂载" });
  }
  for (const [index, mount] of afsMounts.entries()) {
    try {
      items.push(await addAfsMount(document, mount, index, afsCatalog));
    } catch (error) {
      items.push(failure(`mount.afs.${index}`, `文件存储挂载 ${index + 1}`, error));
    }
  }
  for (const [index, mount] of aossMounts.entries()) {
    try {
      items.push(await addAossMount(document, mount, index));
    } catch (error) {
      items.push(failure(`mount.aoss.${index}`, `对象存储挂载 ${index + 1}`, error));
    }
  }

  return {
    ok: !items.some((item) => item.status === "error"),
    fatal: false,
    items,
  };
}
