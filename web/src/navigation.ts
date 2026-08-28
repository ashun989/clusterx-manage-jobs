import { emptyTableState, type TableState } from "./Table";
import type { DetailKind, DetailRef } from "./types";

export type TableTab = "groups" | "users" | "workloads" | "alerts";
export type MonitorTab = TableTab | "overview" | "nodes" | "planner" | "rules";
export type TrendRange = "1h" | "6h" | "24h" | "7d" | "30d" | "all";
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
const trendRanges: TrendRange[] = ["1h", "6h", "24h", "7d", "30d", "all"];

export const emptyNavigationState = (tab: MonitorTab = "groups"): NavigationState => ({
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
  trendRange: "24h",
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

export function readNavigationState(fallbackTab: MonitorTab = "groups", previous?: NavigationState): NavigationState {
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
    trendRange: trendRanges.includes(params.get("range") as TrendRange) ? params.get("range") as TrendRange : "24h",
  };
}

export function navigationUrl(state: NavigationState): string {
  const url = new URL(window.location.href);
  const params = new URLSearchParams();
  params.set("view", state.tab);
  if (state.trendRange !== "24h") params.set("range", state.trendRange);
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
