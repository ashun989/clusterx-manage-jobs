const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CATALOG_TIMEOUT_MS = 10_000;

function normalizedText(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

function storageName(cell: HTMLElement): string | undefined {
  const candidates = [
    cell.querySelector<HTMLElement>(".showNameItem-highlight")?.textContent,
    ...Array.from(cell.querySelectorAll<HTMLElement>("[title], [name]"))
      .flatMap((element) => [element.getAttribute("title"), element.getAttribute("name")]),
    ...Array.from(cell.querySelectorAll<HTMLElement>("*")).filter((element) =>
      element.children.length === 0 && element.getAttribute("role") !== "img",
    ).map((element) => element.textContent),
    cell.children.length === 0 ? cell.textContent : undefined,
  ];
  return candidates
    .map(normalizedText)
    .find((candidate) => candidate !== "" && candidate !== "--" && !UUID_PATTERN.test(candidate));
}

export function parseAfsCatalog(
  document: Document,
  requestedIds: readonly string[],
): Record<string, string> {
  const requested = new Set(requestedIds);
  const catalog: Record<string, string> = {};
  for (const row of Array.from(document.querySelectorAll<HTMLTableRowElement>("tr"))) {
    const cells = Array.from(row.querySelectorAll<HTMLElement>("td"));
    const uuidIndex = cells.findIndex((cell) => UUID_PATTERN.test(normalizedText(cell.textContent)));
    if (uuidIndex < 0) continue;
    const uuid = normalizedText(cells[uuidIndex].textContent).toLowerCase();
    if (!requested.has(uuid)) continue;
    const name = cells.slice(0, uuidIndex).map(storageName).find(Boolean);
    if (name) catalog[uuid] = name;
  }
  return catalog;
}

export async function readAfsCatalog(
  document: Document,
  requestedIds: readonly string[],
  timeoutMs = CATALOG_TIMEOUT_MS,
): Promise<Record<string, string>> {
  const ids = [...new Set(requestedIds.map((id) => id.toLowerCase()))];
  const inspect = (): Record<string, string> | undefined => {
    const catalog = parseAfsCatalog(document, ids);
    return ids.every((id) => catalog[id]) ? catalog : undefined;
  };
  const immediate = inspect();
  if (immediate) return immediate;

  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      observer.disconnect();
      reject(new Error("文件存储列表加载超时"));
    }, timeoutMs);
    const observer = new MutationObserver(() => {
      const catalog = inspect();
      if (!catalog) return;
      window.clearTimeout(timer);
      observer.disconnect();
      resolve(catalog);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  });
}
