import { emptyTableState, type TableState } from "./Table";
import type { DetailKind, DetailRef } from "./types";

export type TableTab = "groups" | "users" | "workloads" | "alerts";
export type MonitorTab = TableTab | "overview" | "nodes" | "planner" | "rules";
/** Trend range is stored as an integer number of seconds. */
export type TrendRange = number;
export type NodeSortKey = "allocated_gpu" | "effective_free_gpu" | "stranded_gpu" | "gpu_util" | "gpu_mem" | "power";
export type NodeViewState = { classifications: string[]; states: string[]; sort: NodeSortKey | null; direction: "asc" | "desc" };
export type PlannerIntent = {
  sourceKind: DetailKind;
  sourceId: string;
  sourceLabel: string;
  note: string;
  nodes: number;
  gpusPerNode: number;
  cpusTotal: number | null;
  memoryTotalGib: number | null;
  targetNodes: string[];
  scope: "fragmented" | "full" | "all";
  groups: string[];
  users: string[];
  workloads: string[];
  workloadTypes: string[];
};

export type NavigationState = {
  tab: MonitorTab;
  details: DetailRef[];
  tableStates: Record<TableTab, TableState>;
  nodeState: NodeViewState;
  plannerIntent: PlannerIntent | null;
  trendRange: TrendRange;
};

export const monitorTabs: MonitorTab[] = ["overview", "groups", "users", "nodes", "workloads", "alerts", "planner", "rules"];
export const tableTabs: TableTab[] = ["groups", "users", "workloads", "alerts"];
const detailKinds: DetailKind[] = ["group", "user", "node", "workload", "alert"];
const nodeSortKeys: NodeSortKey[] = ["allocated_gpu", "effective_free_gpu", "stranded_gpu", "gpu_util", "gpu_mem", "power"];

// Keep the lower bound at one minute so the overview can inspect the most
// recent snapshots without pretending that the selected range is one hour.
// The slider still uses a logarithmic scale, which gives this short interval
// considerably more adjustment precision than the long end of the range.
export const MIN_TREND_RANGE_SECONDS = 60;
export const MAX_TREND_RANGE_SECONDS = 2_592_000;
export const DEFAULT_TREND_RANGE_SECONDS = 86_400;
export const TREND_SLIDER_STEPS = 1_000;
/** Number of slider positions around a reference mark that should snap. */
export const TREND_SLIDER_SNAP_STEPS = 18;

export const TREND_RANGE_MARKS = [
  { seconds: MIN_TREND_RANGE_SECONDS, label: "1 分钟" },
  { seconds: 3_600, label: "1 小时" },
  { seconds: 21_600, label: "6 小时" },
  { seconds: DEFAULT_TREND_RANGE_SECONDS, label: "24 小时" },
  { seconds: 604_800, label: "7 天" },
  { seconds: MAX_TREND_RANGE_SECONDS, label: "30 天" },
] as const;

const legacyTrendRanges: Record<string, number> = {
  "1m": MIN_TREND_RANGE_SECONDS,
  "1h": 3_600,
  "6h": 21_600,
  "24h": DEFAULT_TREND_RANGE_SECONDS,
  "7d": 604_800,
  "30d": MAX_TREND_RANGE_SECONDS,
  all: MAX_TREND_RANGE_SECONDS,
};

export function clampTrendRange(seconds: number): TrendRange {
  if (!Number.isFinite(seconds)) return DEFAULT_TREND_RANGE_SECONDS;
  return Math.min(MAX_TREND_RANGE_SECONDS, Math.max(MIN_TREND_RANGE_SECONDS, Math.round(seconds)));
}

function boundedSliderPosition(position: number): number {
  return Math.min(TREND_SLIDER_STEPS, Math.max(0, Math.round(Number(position))));
}

export function snapTrendSliderPosition(position: number): number {
  const boundedPosition = boundedSliderPosition(position);
  const nearest = TREND_RANGE_MARKS.reduce((best, mark) => {
    const markPosition = sliderPositionFromTrendRange(mark.seconds);
    return Math.abs(markPosition - boundedPosition) < Math.abs(best - boundedPosition) ? markPosition : best;
  }, sliderPositionFromTrendRange(TREND_RANGE_MARKS[0].seconds));
  return Math.abs(nearest - boundedPosition) <= TREND_SLIDER_SNAP_STEPS ? nearest : boundedPosition;
}

export function trendRangeFromSlider(position: number, snap = true): TrendRange {
  const boundedPosition = boundedSliderPosition(position);
  const resolvedPosition = snap ? snapTrendSliderPosition(boundedPosition) : boundedPosition;
  // Keep the visible reference marks exact even though the slider itself has
  // a finite number of positions.
  const mark = TREND_RANGE_MARKS.find((item) => sliderPositionFromTrendRange(item.seconds) === resolvedPosition);
  if (mark) return mark.seconds;
  const normalized = resolvedPosition / TREND_SLIDER_STEPS;
  const ratio = MAX_TREND_RANGE_SECONDS / MIN_TREND_RANGE_SECONDS;
  return clampTrendRange(MIN_TREND_RANGE_SECONDS * Math.pow(ratio, normalized));
}

export function sliderPositionFromTrendRange(seconds: TrendRange): number {
  const normalized = Math.log(clampTrendRange(seconds) / MIN_TREND_RANGE_SECONDS) / Math.log(MAX_TREND_RANGE_SECONDS / MIN_TREND_RANGE_SECONDS);
  return Math.round(Math.min(1, Math.max(0, normalized)) * TREND_SLIDER_STEPS);
}

export function formatTrendRange(seconds: TrendRange): string {
  // The slider position is quantized, so round display values to the nearest
  // minute to avoid showing 23h 59m for the exact 24-hour default/mark.
  const value = clampTrendRange(Math.round(clampTrendRange(seconds) / 60) * 60);
  const days = Math.floor(value / 86_400);
  const hours = Math.floor(value % 86_400 / 3_600);
  const minutes = Math.floor(value % 3_600 / 60);
  if (days >= 2) return `${days} 天${hours ? ` ${hours} 小时` : ""}${minutes ? ` ${minutes} 分钟` : ""}`;
  const totalHours = days * 24 + hours;
  if (totalHours > 0) return `${totalHours} 小时${minutes ? ` ${minutes} 分钟` : ""}`;
  return `${Math.max(1, minutes)} 分钟`;
}

function trendRangeUrlValue(seconds: TrendRange): string {
  const value = clampTrendRange(seconds);
  const known = Object.entries(legacyTrendRanges).find(([label, range]) => label !== "all" && range === value);
  return known?.[0] ?? String(value);
}

function readTrendRange(raw: string | null): TrendRange {
  if (!raw) return DEFAULT_TREND_RANGE_SECONDS;
  if (raw in legacyTrendRanges) return legacyTrendRanges[raw];
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? clampTrendRange(parsed) : DEFAULT_TREND_RANGE_SECONDS;
}

export const emptyNavigationState = (tab: MonitorTab = "overview"): NavigationState => ({
  tab,
  details: [],
  tableStates: {
    groups: emptyTableState(),
    users: emptyTableState(),
    workloads: emptyTableState(),
    alerts: emptyTableState(),
  },
  nodeState: { classifications: [], states: [], sort: null, direction: "desc" },
  plannerIntent: null,
  trendRange: DEFAULT_TREND_RANGE_SECONDS,
});

function positiveNumber(params: URLSearchParams, key: string): number | null {
  const value = Number(params.get(key));
  return Number.isFinite(value) && value > 0 ? value : null;
}

function readPlannerIntent(params: URLSearchParams): PlannerIntent | null {
  const source = params.get("plan.source") ?? "";
  const separator = source.indexOf(":");
  const sourceKind = source.slice(0, separator) as DetailKind;
  const sourceId = source.slice(separator + 1);
  const nodes = positiveNumber(params, "plan.nodes");
  const gpusPerNode = positiveNumber(params, "plan.gpus");
  if (separator <= 0 || !detailKinds.includes(sourceKind) || !sourceId || !nodes || !gpusPerNode) return null;
  const requestedScope = params.get("plan.scope");
  return {
    sourceKind,
    sourceId,
    sourceLabel: params.get("plan.label") || sourceId,
    note: params.get("plan.note") || "",
    nodes,
    gpusPerNode,
    cpusTotal: positiveNumber(params, "plan.cpus"),
    memoryTotalGib: positiveNumber(params, "plan.memory"),
    targetNodes: params.getAll("plan.target"),
    scope: requestedScope === "full" || requestedScope === "all" ? requestedScope : "fragmented",
    groups: params.getAll("plan.group"),
    users: params.getAll("plan.user"),
    workloads: params.getAll("plan.workload"),
    workloadTypes: params.getAll("plan.type"),
  };
}

function readTableState(params: URLSearchParams, tab: TableTab, previous?: TableState): TableState {
  const prefix = `f.${tab}.`;
  const filters: Record<string, string[]> = {};
  params.forEach((value, key) => {
    if (!key.startsWith(prefix)) return;
    const filterKey = key.slice(prefix.length);
    if (!filterKey) return;
    filters[filterKey] = [...(filters[filterKey] ?? []), value];
  });
  const rawSort = params.get(`s.${tab}`) ?? "";
  const separator = rawSort.lastIndexOf(":");
  const direction = separator >= 0 ? rawSort.slice(separator + 1) : "";
  const key = separator >= 0 ? rawSort.slice(0, separator) : "";
  return {
    filters,
    sort: key && (direction === "asc" || direction === "desc") ? { key, direction } : null,
    query: params.get(`q.${tab}`) ?? "",
    hiddenColumns: previous?.hiddenColumns ?? [],
    density: previous?.density ?? "comfortable",
  };
}

function readDetails(params: URLSearchParams): DetailRef[] {
  return params.getAll("detail").flatMap((raw) => {
    const separator = raw.indexOf(":");
    if (separator <= 0) return [];
    const kind = raw.slice(0, separator) as DetailKind;
    const id = raw.slice(separator + 1);
    return detailKinds.includes(kind) && id ? [{ kind, id, label: id }] : [];
  });
}

export function readNavigationState(fallbackTab: MonitorTab = "overview", previous?: NavigationState): NavigationState {
  const params = new URLSearchParams(window.location.search);
  const requestedTab = params.get("view") as MonitorTab | null;
  const tab = requestedTab && monitorTabs.includes(requestedTab) ? requestedTab : fallbackTab;
  const rawNodeSort = params.get("node.sort") ?? "";
  const separator = rawNodeSort.lastIndexOf(":");
  const sort = separator >= 0 ? rawNodeSort.slice(0, separator) as NodeSortKey : null;
  const direction = separator >= 0 && rawNodeSort.slice(separator + 1) === "asc" ? "asc" : "desc";
  return {
    tab,
    details: readDetails(params),
    tableStates: Object.fromEntries(tableTabs.map((name) => [name, readTableState(params, name, previous?.tableStates[name])])) as Record<TableTab, TableState>,
    nodeState: {
      classifications: params.getAll("node.class"),
      states: params.getAll("node.state"),
      sort: sort && nodeSortKeys.includes(sort) ? sort : null,
      direction,
    },
    plannerIntent: readPlannerIntent(params),
    trendRange: readTrendRange(params.get("range")),
  };
}

export function navigationUrl(state: NavigationState): string {
  const url = new URL(window.location.href);
  const params = new URLSearchParams();
  params.set("view", state.tab);
  if (state.trendRange !== DEFAULT_TREND_RANGE_SECONDS) params.set("range", trendRangeUrlValue(state.trendRange));
  for (const tab of tableTabs) {
    const table = state.tableStates[tab];
    if (table.query?.trim()) params.set(`q.${tab}`, table.query.trim());
    if (table.sort) params.set(`s.${tab}`, `${table.sort.key}:${table.sort.direction}`);
    for (const [key, values] of Object.entries(table.filters).sort(([left], [right]) => left.localeCompare(right))) {
      [...values].sort().forEach((value) => params.append(`f.${tab}.${key}`, value));
    }
  }
  state.nodeState.classifications.slice().sort().forEach((value) => params.append("node.class", value));
  state.nodeState.states.slice().sort().forEach((value) => params.append("node.state", value));
  if (state.nodeState.sort) params.set("node.sort", `${state.nodeState.sort}:${state.nodeState.direction}`);
  const intent = state.plannerIntent;
  if (intent) {
    params.set("plan.source", `${intent.sourceKind}:${intent.sourceId}`);
    if (intent.sourceLabel !== intent.sourceId) params.set("plan.label", intent.sourceLabel);
    if (intent.note) params.set("plan.note", intent.note);
    params.set("plan.nodes", String(intent.nodes));
    params.set("plan.gpus", String(intent.gpusPerNode));
    if (intent.cpusTotal != null) params.set("plan.cpus", String(intent.cpusTotal));
    if (intent.memoryTotalGib != null) params.set("plan.memory", String(intent.memoryTotalGib));
    if (intent.scope !== "fragmented") params.set("plan.scope", intent.scope);
    intent.targetNodes.forEach((value) => params.append("plan.target", value));
    intent.groups.forEach((value) => params.append("plan.group", value));
    intent.users.forEach((value) => params.append("plan.user", value));
    intent.workloads.forEach((value) => params.append("plan.workload", value));
    intent.workloadTypes.forEach((value) => params.append("plan.type", value));
  }
  state.details.forEach((detail) => params.append("detail", `${detail.kind}:${detail.id}`));
  url.search = params.toString();
  return `${url.pathname}${url.search}${url.hash}`;
}
