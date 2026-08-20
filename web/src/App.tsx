import { FormEvent, useEffect, useMemo, useState } from "react";
import { AdminPanel } from "./AdminPanel";
import { alertIdentity, DetailDrawer } from "./DetailDrawer";
import { DataTable, emptyTableState, formatPower, statusClass, TelemetryCell } from "./Table";
import type { ColumnDef, TableState } from "./Table";
import type { Alert, DetailRef, GroupSummary, NodeSummary, PlanItem, PlanResult, PolicyResponse, ServiceStatus, Snapshot, UserSummary, Workload } from "./types";

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`/api/v1${path}`, init);
  if (!response.ok) throw new Error((await response.json()).detail ?? response.statusText);
  return response.json() as Promise<T>;
};

const number = (value: unknown, suffix = "") => value == null ? "—" : `${Number(value).toLocaleString()}${suffix}`;
const power = (watts: number | null) => watts == null ? "—" : watts >= 1000 ? `${(watts / 1000).toFixed(1)} kW` : `${watts.toFixed(0)} W`;
const runtimeMark = (row: Workload) => row.runtime_quality === "observed" ? "（观测）" : row.runtime_quality === "estimated" || row.runtime_estimated ? "（估算）" : "";

function Brand({ compact = false, version }: { compact?: boolean; version?: string }) {
  return <div className={compact ? "brand brand-compact" : "brand"}>
    <img className="brand-icon" src="/clusterx-icon.svg" alt="" />
    <div>{!compact && <span className="eyebrow">Clusterx</span>}<div className="brand-title"><h1>{compact ? "Clusterx Monitor" : "Queue Observatory"}</h1>{version && <span className="app-version">v{version}</span>}</div></div>
  </div>;
}

export function FreshnessBadge({ snapshotId, freshness }: { snapshotId: string; freshness: Snapshot["freshness"] }) {
  const [anchor, setAnchor] = useState(() => ({ ageSeconds: freshness.age_seconds, receivedAt: Date.now() }));
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const receivedAt = Date.now();
    setAnchor({ ageSeconds: freshness.age_seconds, receivedAt });
    setNow(receivedAt);
  }, [snapshotId, freshness.age_seconds, freshness.stale]);
  useEffect(() => {
    if (freshness.stale) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [freshness.stale]);
  const ageSeconds = Math.max(0, Math.round(anchor.ageSeconds + (now - anchor.receivedAt) / 1_000));
  return <div className={freshness.stale ? "fresh stale" : "fresh"}><span />{freshness.stale ? "数据过期" : `${ageSeconds}s 前更新`}</div>;
}

const telemetryColumns = <T extends { telemetry: Snapshot["telemetry"] }>(): ColumnDef<T>[] => [
  { key: "gpu_util", label: "GPU Util", kind: "number", value: (row) => row.telemetry.gpu_compute_util_avg_pct, format: (value) => number(value, "%") },
  { key: "gpu_mem", label: "GPU Mem", kind: "number", value: (row) => row.telemetry.gpu_memory_util_avg_pct, format: (value) => number(value, "%") },
  { key: "power", label: "功率", kind: "number", value: (row) => row.telemetry.gpu_power_total_w, format: formatPower },
];

const groupColumns: ColumnDef<GroupSummary>[] = [
  { key: "group", label: "分组", kind: "text", value: (row) => row.group },
  { key: "status", label: "状态", kind: "enum", value: (row) => row.status },
  { key: "gpu_quota", label: "Quota", kind: "number", value: (row) => row.gpu_quota },
  { key: "allocated_gpu", label: "GPU", kind: "number", value: (row) => row.allocated_gpu },
  { key: "allocated_cpu", label: "CPU", kind: "number", value: (row) => row.allocated_cpu },
  { key: "allocated_memory_gib", label: "内存 GiB", kind: "number", value: (row) => row.allocated_memory_gib },
  { key: "finding_categories", label: "违规分类", kind: "enum", value: (row) => row.finding_categories, hidden: true },
  { key: "finding_codes", label: "规则代码", kind: "enum", value: (row) => row.finding_codes, hidden: true },
  { key: "finding_tags", label: "标签", kind: "enum", value: (row) => row.finding_tags, hidden: true },
  ...telemetryColumns<GroupSummary>(),
];

const userColumns: ColumnDef<UserSummary>[] = [
  { key: "user", label: "用户", kind: "text", value: (row) => row.user },
  { key: "group", label: "分组", kind: "enum", value: (row) => row.group },
  { key: "status", label: "状态", kind: "enum", value: (row) => row.status },
  { key: "workload_count", label: "任务数", kind: "number", value: (row) => row.workload_count },
  { key: "development_instance_count", label: "开发机数", kind: "number", value: (row) => row.development_instance_count },
  { key: "allocated_gpu", label: "GPU", kind: "number", value: (row) => row.allocated_gpu },
  { key: "allocated_cpu", label: "CPU", kind: "number", value: (row) => row.allocated_cpu },
  { key: "allocated_memory_gib", label: "内存 GiB", kind: "number", value: (row) => row.allocated_memory_gib },
  { key: "finding_categories", label: "违规分类", kind: "enum", value: (row) => row.finding_categories, hidden: true },
  { key: "finding_codes", label: "规则代码", kind: "enum", value: (row) => row.finding_codes, hidden: true },
  { key: "finding_tags", label: "标签", kind: "enum", value: (row) => row.finding_tags, hidden: true },
  ...telemetryColumns<UserSummary>(),
];

const workloadColumns: ColumnDef<Workload>[] = [
  { key: "workload_name", label: "Workload", kind: "text", value: (row) => row.workload_name },
  { key: "user", label: "用户", kind: "enum", value: (row) => row.user },
  { key: "group", label: "分组", kind: "enum", value: (row) => row.group },
  { key: "type", label: "类型", kind: "enum", value: (row) => row.type },
  { key: "policy_status", label: "状态", kind: "enum", value: (row) => row.policy_status },
  { key: "total_gpu", label: "GPU 总量", kind: "number", value: (row) => row.total_gpu },
  { key: "total_cpu", label: "CPU 总量", kind: "number", value: (row) => row.total_cpu },
  { key: "total_memory_gib", label: "内存 GiB", kind: "number", value: (row) => row.total_memory_gib },
  { key: "runtime_hours", label: "运行小时", kind: "number", value: (row) => row.runtime_hours, format: (value, row) => value == null ? "—" : `${number(value)}${runtimeMark(row)}` },
  { key: "finding_categories", label: "违规分类", kind: "enum", value: (row) => row.finding_categories, hidden: true },
  { key: "finding_codes", label: "规则代码", kind: "enum", value: (row) => row.finding_codes, hidden: true },
  { key: "finding_tags", label: "标签", kind: "enum", value: (row) => row.finding_tags, hidden: true },
  ...telemetryColumns<Workload>(),
];

const alertColumns: ColumnDef<Alert>[] = [
  { key: "severity", label: "级别", kind: "enum", value: (row) => row.severity, format: (value) => <span className={statusClass(value)}>{String(value)}</span> },
  { key: "kind", label: "类型", kind: "enum", value: (row) => row.kind },
  { key: "category", label: "分类", kind: "enum", value: (row) => row.category },
  { key: "code", label: "规则代码", kind: "enum", value: (row) => row.code },
  { key: "subject_type", label: "对象类型", kind: "enum", value: (row) => row.subject_type, hidden: true },
  { key: "finding_tags", label: "标签", kind: "enum", value: (row) => row.finding_tags, hidden: true },
  { key: "subject", label: "对象", kind: "text", value: (row) => row.subject },
  { key: "message", label: "说明", kind: "text", value: (row) => row.message },
];

type TableTab = "groups" | "users" | "workloads" | "alerts";
type Tab = TableTab | "nodes" | "rules";
type NodeSortKey = "allocated_gpu" | "effective_free_gpu" | "stranded_gpu" | "gpu_util" | "gpu_mem" | "power";
type NodeViewState = { classifications: string[]; states: string[]; sort: NodeSortKey | null; direction: "asc" | "desc" };

function MultiFilter({ label, options, selected, onChange }: { label: string; options: string[]; selected: string[]; onChange: (values: string[]) => void }) {
  return <details className="filter-menu"><summary>{label}{selected.length > 0 && <span>{selected.length}</span>}</summary><div className="filter-popover">{options.map((option) => <label key={`${label}:${option}`}><input type="checkbox" checked={selected.includes(option)} onChange={(event) => onChange(event.target.checked ? [...selected, option] : selected.filter((item) => item !== option))} /><span>{option}</span></label>)}</div></details>;
}

function nodeSortValue(node: NodeSummary, key: NodeSortKey) {
  if (key === "gpu_util") return node.telemetry.gpu_compute_util_avg_pct;
  if (key === "gpu_mem") return node.telemetry.gpu_memory_util_avg_pct;
  if (key === "power") return node.telemetry.gpu_power_total_w;
  return node[key];
}

function NodeHeatmap({ nodes, state, onState, onNode }: { nodes: NodeSummary[]; state: NodeViewState; onState: (value: NodeViewState) => void; onNode: (node: NodeSummary) => void }) {
  const classifications = useMemo(() => [...new Set(nodes.map((node) => node.classification).filter(Boolean))].sort(), [nodes]);
  const states = useMemo(() => [...new Set(nodes.map((node) => node.state).filter(Boolean))].sort(), [nodes]);
  const validClassifications = state.classifications.filter((item) => classifications.includes(item));
  const validStates = state.states.filter((item) => states.includes(item));
  const nodeOptionSignature = `${classifications.join("\u0000")}|${states.join("\u0000")}`;
  const nodeFilterSignature = `${state.classifications.join("\u0000")}|${state.states.join("\u0000")}`;
  useEffect(() => {
    if (validClassifications.length !== state.classifications.length || validStates.length !== state.states.length) onState({ ...state, classifications: validClassifications, states: validStates });
  }, [nodeOptionSignature, nodeFilterSignature]);
  const visibleNodes = useMemo(() => {
    const filtered = nodes.filter((node) => (!validClassifications.length || validClassifications.includes(node.classification)) && (!validStates.length || validStates.includes(node.state)));
    if (!state.sort) return filtered;
    return filtered.map((node, index) => ({ node, index })).sort((left, right) => {
      const a = nodeSortValue(left.node, state.sort!);
      const b = nodeSortValue(right.node, state.sort!);
      const aMissing = a == null || Number.isNaN(Number(a));
      const bMissing = b == null || Number.isNaN(Number(b));
      if (aMissing || bMissing) return aMissing === bMissing ? left.index - right.index : aMissing ? 1 : -1;
      return (Number(a) - Number(b)) * (state.direction === "asc" ? 1 : -1) || left.index - right.index;
    }).map(({ node }) => node);
  }, [nodes, state, validClassifications.join("\u0000"), validStates.join("\u0000")]);
  const reset = () => onState({ classifications: [], states: [], sort: null, direction: "desc" });
  const hasControls = state.classifications.length > 0 || state.states.length > 0 || state.sort;
  return <>
    <div className="table-tools node-tools"><div className="filter-list"><MultiFilter label="负载状态" options={classifications} selected={validClassifications} onChange={(values) => onState({ ...state, classifications: values })} /><MultiFilter label="节点状态" options={states} selected={validStates} onChange={(values) => onState({ ...state, states: values })} /><label className="sort-select"><span>排序</span><select aria-label="节点排序字段" value={state.sort ?? ""} onChange={(event) => onState({ ...state, sort: event.target.value ? event.target.value as NodeSortKey : null })}><option value="">默认顺序</option><option value="allocated_gpu">GPU 已用</option><option value="effective_free_gpu">有效空闲 GPU</option><option value="stranded_gpu">受阻 GPU</option><option value="gpu_util">GPU Util</option><option value="gpu_mem">GPU Mem</option><option value="power">功率</option></select></label>{state.sort && <button type="button" className="direction-button" onClick={() => onState({ ...state, direction: state.direction === "asc" ? "desc" : "asc" })}>{state.direction === "asc" ? "升序 ↑" : "降序 ↓"}</button>}</div><div className="result-count"><span>{visibleNodes.length}/{nodes.length}</span>{hasControls && <button type="button" onClick={reset}>重置</button>}</div></div>
    <div className="node-grid">{visibleNodes.map((node) => {
      const ratio = node.total_gpu > 0 ? Math.min(100, node.allocated_gpu / node.total_gpu * 100) : 0;
      const open = () => onNode(node);
      return <article role="button" tabIndex={0} aria-label={`查看 ${node.node} 详情`} className={`node-tile node-${node.classification}`} key={node.node} onClick={open} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } }}>
        <header><b title={node.node}>{node.node}</b><span>{node.classification}</span></header>
        <div className="gpu-bar"><i style={{ width: `${ratio}%` }} /></div>
        <p><strong>{number(node.allocated_gpu)}/{number(node.total_gpu)}</strong> GPU</p>
        <small>{number(node.effective_free_gpu)} effective free · {number(node.stranded_gpu)} blocked</small>
        <TelemetryCell data={node.telemetry} />
        {Object.values(node.unattributed).some((value) => Number(value) > 0) && <em>含未归属资源</em>}
        {!node.planning_eligible && <em>仅监控 · 不参与调度</em>}
      </article>;
    })}{visibleNodes.length === 0 && <p className="empty-nodes">没有匹配的节点</p>}</div>
  </>;
}

function Planner({ snapshot, onResult }: { snapshot: Snapshot; onResult: (value: PlanResult) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [gpus, setGpus] = useState("8");
  const defaultFor = (perGpu: number) => String(Number(gpus) * perGpu);
  const [cpus, setCpus] = useState(() => String(8 * snapshot.planning_profile.default_cpu_per_gpu));
  const [memory, setMemory] = useState(() => String(8 * snapshot.planning_profile.default_memory_gib_per_gpu));
  const [cpusUseDefault, setCpusUseDefault] = useState(true);
  const [memoryUsesDefault, setMemoryUsesDefault] = useState(true);
  useEffect(() => {
    if (cpusUseDefault) setCpus(defaultFor(snapshot.planning_profile.default_cpu_per_gpu));
    if (memoryUsesDefault) setMemory(defaultFor(snapshot.planning_profile.default_memory_gib_per_gpu));
  }, [gpus, snapshot.planning_profile.default_cpu_per_gpu, snapshot.planning_profile.default_memory_gib_per_gpu, cpusUseDefault, memoryUsesDefault]);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true); setError("");
    const data = new FormData(event.currentTarget);
    const strategies = data.getAll("strategy") as string[];
    const split = (name: string) => String(data.get(name) ?? "").split(",").map((value) => value.trim()).filter(Boolean);
    const optional = (name: string) => data.get(name) ? Number(data.get(name)) : null;
    try {
      const result = await api<PlanResult>("/plans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        snapshot_id: snapshot.snapshot_id,
        target: { nodes: Number(data.get("nodes")), gpus_per_node: Number(data.get("gpus")), cpus_per_node: optional("cpus"), memory_per_node_gib: optional("memory") },
        strategies: strategies.length ? strategies : ["min-gpu"], candidate_scope: data.get("scope"), alternatives: Number(data.get("alternatives")), search_seconds: Number(data.get("searchSeconds")),
        filters: {
          workload_types: split("types"), groups: split("groups"), users: split("users"), workloads: split("workloads"),
          exclude_workloads: split("excludeWorkloads"), exclude_users: split("excludeUsers"), over_quota_only: data.get("overQuota") === "on",
          violation_categories: split("violationCategories"), violation_codes: split("violationCodes"), violation_tags: split("violationTags"),
        },
      }) });
      onResult(result);
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); } finally { setBusy(false); }
  };
  return <form className="planner" onSubmit={submit}>
    <label>节点数<input name="nodes" type="number" min="1" max="1024" defaultValue="2" /></label><label>每节点 GPU<input name="gpus" type="number" min="1" max="1024" value={gpus} onChange={(event) => setGpus(event.target.value)} /></label>
    <label>CPU（可覆盖）<input name="cpus" type="number" min="1" max="1000000" value={cpus} onChange={(event) => { setCpusUseDefault(false); setCpus(event.target.value); }} /></label><label>内存 GiB（可覆盖）<input name="memory" type="number" min="1" max="10000000" value={memory} onChange={(event) => { setMemoryUsesDefault(false); setMemory(event.target.value); }} /></label>
    <label>候选范围<select name="scope" defaultValue="fragmented"><option value="fragmented">碎片节点</option><option value="full">满 GPU 节点</option><option value="all">全部</option></select></label><label>备选数<input name="alternatives" type="number" min="1" max="10" defaultValue="1" /></label>
    <label>搜索秒数<input name="searchSeconds" type="number" min="1" max="30" defaultValue="10" /></label>
    <label>类型（逗号分隔）<input name="types" placeholder="trainingJob,aid" /></label><label>分组（逗号分隔）<input name="groups" placeholder="example-team" /></label>
    <label>用户（逗号分隔）<input name="users" placeholder="alice" /></label><label>指定 workload ID<input name="workloads" placeholder="id-a,id-b" /></label>
    <label>排除 workload ID<input name="excludeWorkloads" placeholder="id-c" /></label><label>排除用户<input name="excludeUsers" placeholder="username" /></label>
    <label>违规分类（逗号分隔）<input name="violationCategories" placeholder="utilization,quota" /></label><label>规则代码（逗号分隔）<input name="violationCodes" placeholder="utilization.low_gpu_activity" /></label>
    <label>违规标签（逗号分隔）<input name="violationTags" placeholder="low-utilization,gpu" /></label>
    <fieldset><legend>策略</legend>{[["min-gpu", "最少 GPU"], ["min-workloads", "最少任务"], ["min-users", "最少用户"]].map(([value, label]) => <label className="check" key={value}><input name="strategy" value={value} type="checkbox" defaultChecked />{label}</label>)}</fieldset>
    <label className="check"><input name="overQuota" type="checkbox" />仅超 quota 分组</label><button disabled={busy}>{busy ? "计算中…" : "计算方案"}</button>{error && <p className="error planner-error">{error}</p>}
  </form>;
}

const planKey = (item: PlanItem) => `${item.strategy}:${item.rank}:${item.workloads.join(",")}`;

function PlanResults({ plan, currentSnapshotId, onWorkload }: { plan: PlanResult | null; currentSnapshotId: string; onWorkload: (workload: Workload) => void }) {
  const [expanded, setExpanded] = useState<string[]>([]);
  const signature = plan ? `${plan.snapshot_id}:${plan.computed_at ?? ""}:${plan.plans.map(planKey).join("|")}` : "";
  useEffect(() => setExpanded(plan?.plans[0] ? [planKey(plan.plans[0])] : []), [signature]);
  if (!plan) return <div className="plan-empty"><p>提交查询后，可在这里比较调度方案。</p></div>;
  const superseded = plan.superseded || plan.snapshot_id !== currentSnapshotId;
  const reasonLabels: Record<string, string> = { "attribution-excluded": "归属异常节点及关联 Workload 已被安全排除。", "no-candidates-after-filters": "筛选后没有可协调的 Workload。", "insufficient-releasable-resources": "候选 Workload 可释放的资源不足。" };
  return <div className="plan-results">
    <div className="plan-result-heading"><div><h3>{plan.optimality}</h3><span>{plan.search_elapsed_seconds}s · snapshot {plan.snapshot_id.slice(0, 12)}</span></div>{superseded && <span className="status status-warning">快照已更新</span>}</div>
    <p className="plan-timestamp">快照时间 {new Date(plan.snapshot_generated_at).toLocaleString()}{plan.cache_hit ? " · cache hit" : ""}</p>
    <div className="plan-resolved"><b>实际资源画像</b><span>{number(plan.resolved_target.nodes)} × {number(plan.resolved_target.gpus_per_node)} GPU / {number(plan.resolved_target.cpus_per_node)} CPU / {number(plan.resolved_target.memory_per_node_gib)} GiB</span>{plan.defaults_applied.length > 0 && <small>已应用默认值：{plan.defaults_applied.join(", ")}</small>}<small>排除 {plan.planning_exclusions.node_count} 节点 / {plan.planning_exclusions.workload_count} Workload{plan.planning_exclusions.reasons.length ? ` · ${plan.planning_exclusions.reasons.join(", ")}` : ""}</small>{plan.planning_exclusions.nodes?.map((item) => <small key={`node:${item.node}`}>节点 {item.node}: {item.reasons.join(", ")}</small>)}{plan.planning_exclusions.workloads?.map((item) => <small key={`workload:${item.workload_id}`}>Workload {item.workload_id}: {item.reasons.join(", ")} ({item.nodes.join(", ")})</small>)}</div>
    {plan.plans.length === 0 ? plan.optimality === "not-needed" ? <div className="plan-notice"><b>无需协调 Workload</b><p>当前可调度节点：{plan.currently_schedulable_nodes?.join(", ") || "—"}</p></div> : <div className="plan-notice"><b>没有可行方案</b><p>{reasonLabels[plan.no_plan_reason ?? ""] ?? "当前候选范围不足以释放目标资源。"}</p></div> : plan.plans.map((item) => {
      const key = planKey(item); const open = expanded.includes(key);
      return <article className={open ? "plan-card expanded" : "plan-card"} key={key}><button type="button" className="plan-summary" aria-expanded={open} onClick={() => setExpanded(open ? expanded.filter((value) => value !== key) : [...expanded, key])}><span><b>{item.strategy} #{item.rank}</b><small>{number(item.gpus)} GPU · {number(item.workload_count)} workloads</small></span><span className="freed-summary">{item.freed_nodes.join(", ") || "无释放节点"}</span><i>{open ? "−" : "+"}</i></button>
        {open && <div className="plan-detail"><div className="plan-metrics"><span><small>GPU</small><b>{number(item.gpus)}</b></span><span><small>CPU</small><b>{number(item.cpus)}</b></span><span><small>内存 GiB</small><b>{number(item.memory_gib)}</b></span><span><small>用户</small><b>{number(item.users)}</b></span><span><small>分组</small><b>{number(item.groups)}</b></span><span><small>Workloads</small><b>{number(item.workload_count)}</b></span></div>
          <h4>可释放节点</h4><div className="tag-list">{item.freed_nodes.map((node) => <span key={node}>{node}</span>)}</div>
          <h4>需要协调的 Workload</h4><div className="plan-workloads"><table><thead><tr><th>Workload</th><th>用户 / 分组</th><th>类型</th><th>GPU</th><th>CPU</th><th>内存 GiB</th><th>Placements</th></tr></thead><tbody>{item.workload_details.map((workload) => <tr tabIndex={0} className="clickable" aria-label={`查看 ${workload.workload_name} 详情`} key={workload.workload_id} onClick={() => onWorkload(workload)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onWorkload(workload); } }}><td>{workload.workload_name}</td><td>{workload.user}<small>{workload.group}</small></td><td>{workload.type}</td><td>{number(workload.total_gpu)}</td><td>{number(workload.total_cpu)}</td><td>{number(workload.total_memory_gib)}</td><td>{workload.placements.map((placement) => `${placement.node} (${number(placement.gpu)}G/${number(placement.cpu)}C/${number(placement.memory_gib)}GiB)`).join(", ")}</td></tr>)}</tbody></table></div>
        </div>}
      </article>;
    })}
  </div>;
}

function SchedulerPanel({ snapshot, plan, onResult, onWorkload }: { snapshot: Snapshot; plan: PlanResult | null; onResult: (result: PlanResult) => void; onWorkload: (workload: Workload) => void }) {
  return <aside className="scheduler-panel"><div className="planner-column"><h2>调度模拟器</h2><p>固定使用当前缓存快照，不访问集群。</p><Planner snapshot={snapshot} onResult={onResult} /></div><PlanResults plan={plan} currentSnapshotId={snapshot.snapshot_id} onWorkload={onWorkload} /></aside>;
}

const policyLabel = (value: string) => value.replaceAll("_", " ");

function RulesPage({ response, snapshot, error }: { response: PolicyResponse | null; snapshot: Snapshot; error: string }) {
  if (!response?.policy) return <div className="rules-empty"><h2>规则说明</h2><p>{error || "策略配置暂不可用。"}</p></div>;
  const policy = response.policy;
  const sections = [
    ["开发机（aid）", policy.development],
    ["训练任务（每 task / 节点）", policy.training],
    ["标准调度画像（每 GPU）", policy.planning],
    ["低 GPU 利用率", policy.low_utilization],
    ["Pending pressure", policy.pending_pressure],
  ] as const;
  return <div className="rules-page">
    <header className="rules-heading"><div><span className="eyebrow">Effective policy</span><h2>规则说明</h2><p>本页内容来自 <code>/api/v1/policy</code>，显示当前实际生效值。</p></div><span className={response.valid ? statusClass("compliant") : statusClass("warning")}>{response.valid ? "配置有效" : response.using_last_known_good ? "使用 last-known-good" : "配置无效"}</span></header>
    {response.error && <p className="banner">{response.error}</p>}
    {response.audit_error && <p className="banner">审计日志降级：{response.audit_error}</p>}
    <section className="rules-section"><h3>状态含义与传播</h3><div className="status-definition-grid">{Object.entries(response.status_definitions).map(([status, definition]) => {
      const detail = typeof definition === "string" ? { description: definition, propagation: "" } : definition;
      return <article key={status}><span className={statusClass(status)}>{status}</span><p>{detail.description}</p>{detail.propagation && <small>{detail.propagation}</small>}</article>;
    })}</div></section>
    <section className="rules-section"><h3>当前配置</h3><p className="rule-note">采集间隔 {number(policy.refresh_seconds, "s")} · 逐卡遥测窗口 {number(policy.telemetry_lookback_minutes, "min")} · 当前 default quota {number(snapshot.capacity.default_gpu_quota)} GPU</p><p className="rule-note">Monitor 对 Clusterx 只读；认证管理员只能写入本机资源和分组配置。节点 effective/blocked 均相对于标准调度画像。</p><div className="config-grid">{sections.map(([title, values]) => <article key={title}><h4>{title}</h4><dl>{Object.entries(values).map(([key, value]) => <div key={key}><dt>{policyLabel(key)}</dt><dd>{number(value)}</dd></div>)}</dl></article>)}</div></section>
    <section className="rules-section"><h3>规则目录</h3><div className="rule-catalog">{response.rule_catalog.map((rule) => <article key={rule.code}><div><code>{rule.code}</code><span>{rule.category}</span><span>{rule.applies_to}</span></div><h4>{rule.title}</h4>{rule.description && <p>{rule.description}</p>}</article>)}</div></section>
    <section className="rules-section"><h3>分组 Quota</h3><div className="policy-groups"><table><thead><tr><th>Group ID</th><th>GPU quota</th><th>成员数</th></tr></thead><tbody>{Object.entries(policy.groups).map(([group, config]) => <tr key={group}><td>{group}</td><td>{config.gpu_quota === "remainder" ? `remainder（当前 ${number(snapshot.capacity.default_gpu_quota)}）` : number(config.gpu_quota)}</td><td>{number(config.member_count)}</td></tr>)}</tbody></table></div></section>
    <section className="rules-section"><h3>失败与缺失数据</h3><div className="behavior-list">{Object.entries(response.evaluation_behavior).map(([key, description]) => <article key={key}><code>{policyLabel(key)}</code><p>{description}</p></article>)}</div></section>
  </div>;
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("groups");
  const [details, setDetails] = useState<DetailRef[]>([]);
  const [plan, setPlan] = useState<PlanResult | null>(null);
  const [policy, setPolicy] = useState<PolicyResponse | null>(null);
  const [policyError, setPolicyError] = useState("");
  const [serviceStatus, setServiceStatus] = useState<ServiceStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [adminOpen, setAdminOpen] = useState(false);
  const [tableStates, setTableStates] = useState<Record<TableTab, TableState>>({ groups: emptyTableState(), users: emptyTableState(), workloads: emptyTableState(), alerts: emptyTableState() });
  const [nodeState, setNodeState] = useState<NodeViewState>({ classifications: [], states: [], sort: null, direction: "desc" });
  const refresh = () => {
    api<Snapshot>("/snapshots/latest").then((value) => { setSnapshot(value); setError(""); }).catch((value) => setError(value instanceof Error ? value.message : String(value)));
    api<PolicyResponse>("/policy").then((value) => { setPolicy(value); setPolicyError(""); }).catch((value) => setPolicyError(value instanceof Error ? value.message : String(value)));
    api<ServiceStatus>("/status").then((value) => { setServiceStatus(value); setStatusError(""); }).catch((value) => setStatusError(value instanceof Error ? value.message : String(value)));
  };
  useEffect(() => {
    refresh();
    const stream = new EventSource("/api/v1/events");
    stream.addEventListener("snapshot", refresh);
    stream.onerror = () => setError("实时连接已断开，正在等待重连");
    const timer = window.setInterval(refresh, 30_000);
    return () => { stream.close(); window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (!details.length) return;
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape") setDetails([]); };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [details.length]);
  const workloads = useMemo(() => snapshot ? [...snapshot.workloads, ...(snapshot.pending_workloads ?? [])] : [], [snapshot]);
  const open = (ref: DetailRef) => setDetails((current) => [...current, ref]);
  const updateTable = (name: TableTab, state: TableState) => setTableStates((current) => ({ ...current, [name]: state }));
  if (!snapshot) {
    const loadingMessages = [error, statusError, policyError, serviceStatus?.setup_required ? "服务处于 setup-required，请由管理员补全本地配置。" : "", serviceStatus?.snapshot.last_error, serviceStatus?.policy.error, serviceStatus?.policy.audit_error].filter(Boolean);
    return <main className="loading"><Brand compact version={serviceStatus?.version} /><div className="pulse" /><p>{loadingMessages.join(" · ") || "正在等待第一份完整快照…"}</p><button type="button" className="admin-entry" onClick={() => setAdminOpen(true)}>管理员配置</button>{adminOpen && <AdminPanel close={() => setAdminOpen(false)} onConfigured={refresh} />}</main>;
  }
  const telemetryCoverage = snapshot.telemetry;
  const coverageMessages = [["compute", telemetryCoverage.compute_reported_gpu_count], ["memory", telemetryCoverage.memory_reported_gpu_count], ["power", telemetryCoverage.power_reported_gpu_count]].filter(([, count]) => Number(count) < telemetryCoverage.allocated_gpu_count).map(([metric, count]) => `${metric} 遥测覆盖 ${count}/${telemetryCoverage.allocated_gpu_count}`);
  const bannerMessages = [error, policyError, statusError, policy?.error, policy?.audit_error, serviceStatus?.policy.audit_error, serviceStatus?.snapshot.last_error, serviceStatus?.setup_required ? "服务处于 setup-required" : "", serviceStatus?.skipped_refreshes ? `已跳过 ${serviceStatus.skipped_refreshes} 次刷新` : "", snapshot.policy_config?.error, ...snapshot.warnings, ...coverageMessages, snapshot.historical_telemetry_status === "unavailable" ? "历史 GPU 遥测不可用，低利用率规则本轮未评估" : ""].filter(Boolean);
  const workloadRef = (workload: Workload): DetailRef => ({ kind: "workload", id: workload.workload_id, label: workload.workload_name });
  return <div className="app">
    <header className="app-header"><div className="brand-block"><Brand version={serviceStatus?.version} /><span className="cluster-context">{snapshot.cluster} / {snapshot.queue}</span></div><div className="header-actions"><FreshnessBadge snapshotId={snapshot.snapshot_id} freshness={snapshot.freshness} /><button type="button" className="admin-entry" onClick={() => setAdminOpen(true)}>管理员配置</button></div></header>
    <section className="cards"><article><label>绑定容量</label><strong>{number(snapshot.capacity.bound_gpu)}</strong><small>{number(snapshot.capacity.planning_eligible_gpu)} planning eligible</small></article><article><label>已分配</label><strong>{number(snapshot.capacity.allocated_gpu)}</strong><small>{number(snapshot.capacity.free_gpu)} free</small></article><article><label>Pending pressure</label><strong className={statusClass(snapshot.pending_pressure.state)}>{String(snapshot.pending_pressure.state)}</strong><small>{number(snapshot.pending_pressure.eligible_jobs)} eligible jobs</small></article><article><label>GPU Power</label><strong>{power(snapshot.telemetry.gpu_power_total_w)}</strong><small>{snapshot.telemetry.power_reported_gpu_count}/{snapshot.telemetry.allocated_gpu_count} power covered · {snapshot.telemetry_status ?? "unknown"}</small></article></section>
    {bannerMessages.length > 0 && <div className="banner">{bannerMessages.join(" · ")}</div>}
    <section className="workspace"><div className="main-panel"><nav>{(["groups", "users", "nodes", "workloads", "alerts", "rules"] as Tab[]).map((name) => <button className={tab === name ? "active" : ""} key={name} onClick={() => setTab(name)}>{name === "rules" ? "规则说明" : name}</button>)}</nav>
      {tab === "groups" && <DataTable rows={snapshot.groups} columns={groupColumns} state={tableStates.groups} onState={(state) => updateTable("groups", state)} rowKey={(row) => row.group} rowLabel={(row) => row.group} onRow={(row) => open({ kind: "group", id: row.group, label: row.group })} />}
      {tab === "users" && <DataTable rows={snapshot.users} columns={userColumns} state={tableStates.users} onState={(state) => updateTable("users", state)} rowKey={(row) => row.user} rowLabel={(row) => row.user} onRow={(row) => open({ kind: "user", id: row.user, label: row.user })} />}
      {tab === "nodes" && <NodeHeatmap nodes={snapshot.nodes} state={nodeState} onState={setNodeState} onNode={(row) => open({ kind: "node", id: row.node, label: row.node })} />}
      {tab === "workloads" && <DataTable rows={workloads} columns={workloadColumns} state={tableStates.workloads} onState={(state) => updateTable("workloads", state)} rowKey={(row) => row.workload_id} rowLabel={(row) => row.workload_name} onRow={(row) => open(workloadRef(row))} />}
      {tab === "alerts" && <DataTable rows={snapshot.alerts} columns={alertColumns} state={tableStates.alerts} onState={(state) => updateTable("alerts", state)} rowKey={(row, index) => `${alertIdentity(row)}:${index}`} rowLabel={(row) => row.subject} onRow={(row) => open({ kind: "alert", id: alertIdentity(row), label: row.subject })} />}
      {tab === "rules" && <RulesPage response={policy} snapshot={snapshot} error={policyError} />}
    </div><SchedulerPanel snapshot={snapshot} plan={plan} onResult={setPlan} onWorkload={(workload) => open(workloadRef(workload))} /></section>
    <DetailDrawer stack={details} snapshot={snapshot} open={open} back={() => setDetails((current) => current.slice(0, -1))} close={() => setDetails([])} />
    {adminOpen && <AdminPanel close={() => setAdminOpen(false)} onConfigured={refresh} />}
  </div>;
}
