import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { monitorApiUrl } from "./api";
import { useDialogFocus } from "./useDialogFocus";

type AdminSession = {
  authenticated: boolean;
  username: string;
  csrf_token: string;
  expires_at: string;
};

type AdminConfig = {
  configured: boolean;
  effective_config_valid: boolean;
  resource: AdminFile;
  groups: AdminFile;
  validation_error: string | null;
  audit_error: string | null;
  backups?: Record<"resource" | "groups", BackupInfo>;
  audit?: AuditRecord[];
  groups_structured?: StructuredGroups | null;
  snapshot_id?: string | null;
  snapshot_generated_at?: string | null;
  node_options?: AdminNode[];
  member_options?: string[];
};

type AdminFile = { format: "json" | "yaml"; text: string; revision: string; parse_error: string | null };
type BackupInfo = { available: boolean; revision: string | null; updated_at: string | null };
type AuditRecord = { timestamp: string; actor: string; kind: string; action?: string; before_revision: string; after_revision: string };
type GroupDraft = { gpu_quota: number | "remainder" | null; cpu_quota: number | null; memory_quota_gib: number | null; members: string[]; nodes: string[]; effective_member_count?: number };
type StructuredGroups = { node_allocation: { enabled: boolean }; groups: Record<string, GroupDraft>; revision?: string };
type DraftGroup = GroupDraft & { id: string; name: string };
type GroupDraftState = { node_allocation: { enabled: boolean }; groups: DraftGroup[] };
type DraftIssue = { id: string; groupId?: string; message: string };
type AdminNode = { node: string; id: string; state: string; total_gpu: number; allocated_gpu: number; assigned_group?: string | null };
type AllocationProposal = {
  strategy: "workload-first" | "gpu-first" | "user-first" | string;
  status: string;
  feasible: boolean;
  groups: Record<string, GroupDraft>;
  assignment: Array<{ node: string; group: string }>;
  metrics: { affected_workloads: number | null; affected_gpu: number | null; affected_users: number | null };
  capacity_coverage: Array<{ group: string; quota: number; assigned_gpu: number; deficit: number; covered: boolean }>;
  capacity_deficits: Array<{ group: string; quota: number; assigned_gpu: number; deficit: number; covered: boolean }>;
  diagnostics: string[];
};
type AllocationPlanResponse = {
  snapshot_id: string;
  groups_revision: string;
  node_allocation_enabled: boolean;
  superseded?: boolean;
  cache_hit?: boolean;
  proposals: AllocationProposal[];
};
type EditorState = AdminFile & { dirty: boolean };
const emptyEditor = (format: "json" | "yaml"): EditorState => ({ format, text: "", revision: "", parse_error: null, dirty: false });
const editorFrom = (value: AdminFile): EditorState => ({ ...value, dirty: false });
let nextDraftGroupId = 1;
const draftGroupId = (name: string) => `draft-group-${nextDraftGroupId++}-${name}`;
const structuredFrom = (value: StructuredGroups): GroupDraftState => ({
  node_allocation: { enabled: Boolean(value.node_allocation?.enabled) },
  groups: Object.entries(value.groups ?? {}).map(([name, group]) => ({
    id: draftGroupId(name), name,
    gpu_quota: group.gpu_quota ?? null, cpu_quota: group.cpu_quota ?? null,
    memory_quota_gib: group.memory_quota_gib ?? null,
    members: name === "default" ? [] : [...(group.members ?? [])], nodes: [...(group.nodes ?? [])],
  })),
});
const structuredPayload = (draft: GroupDraftState): StructuredGroups => ({
  node_allocation: { enabled: draft.node_allocation.enabled },
  groups: Object.fromEntries(draft.groups.map(({ id: _id, name, ...group }) => [name, {
    gpu_quota: group.gpu_quota, cpu_quota: group.cpu_quota,
    memory_quota_gib: group.memory_quota_gib,
    members: name === "default" ? [] : [...group.members], nodes: [...group.nodes],
  }])),
});
const validateGroupDraft = (draft: GroupDraftState): DraftIssue[] => {
  const issues: DraftIssue[] = [];
  const names = new Map<string, DraftGroup[]>();
  const members = new Map<string, DraftGroup[]>();
  const nodes = new Map<string, DraftGroup[]>();
  const add = (groupId: string | undefined, id: string, message: string) => issues.push({ id, groupId, message });
  draft.groups.forEach((group) => {
    const normalizedName = group.name.trim();
    if (!normalizedName) add(group.id, `name:${group.id}`, "组名不能为空");
    if (normalizedName !== group.name) add(group.id, `name-whitespace:${group.id}`, "组名不能以空白字符开头或结尾");
    const sameName = names.get(normalizedName) ?? [];
    sameName.push(group); names.set(normalizedName, sameName);
    if (group.gpu_quota !== null && group.gpu_quota !== "remainder" && (!Number.isFinite(group.gpu_quota) || group.gpu_quota < 0)) add(group.id, `gpu:${group.id}`, "GPU quota 必须是非负有限数值");
    if (group.gpu_quota !== null && group.gpu_quota !== "remainder" && Number.isFinite(group.gpu_quota) && group.gpu_quota % 8 !== 0) add(group.id, `gpu-alignment:${group.id}`, "GPU quota 必须是 8 的倍数");
    if (group.cpu_quota !== null && (!Number.isFinite(group.cpu_quota) || group.cpu_quota < 0)) add(group.id, `cpu:${group.id}`, "CPU quota 必须是非负有限数值");
    if (group.memory_quota_gib !== null && (!Number.isFinite(group.memory_quota_gib) || group.memory_quota_gib < 0)) add(group.id, `memory:${group.id}`, "内存 quota 必须是非负有限数值");
    if (group.gpu_quota === "remainder" && group.name !== "default") add(group.id, `remainder:${group.id}`, "只有 default 组可以使用 remainder quota");
    if (group.name === "default" && group.members.length) add(group.id, `default-members:${group.id}`, "default 组成员由剩余用户自动派生，不能显式编辑");
    group.members.forEach((member) => {
      const values = members.get(member) ?? []; values.push(group); members.set(member, values);
    });
    group.nodes.forEach((node) => {
      const values = nodes.get(node) ?? []; values.push(group); nodes.set(node, values);
    });
  });
  const defaultGroups = draft.groups.filter((group) => group.name === "default");
  if (defaultGroups.length !== 1) add(undefined, "default-group", "必须且只能有一个 default 组");
  names.forEach((groups, name) => { if (name && groups.length > 1) groups.forEach((group) => add(group.id, `duplicate-name:${group.id}`, `组名“${name}”重复`)); });
  members.forEach((groups, member) => { const explicitGroups = groups.filter((group) => group.name !== "default"); if (explicitGroups.length > 1) explicitGroups.forEach((group) => add(group.id, `duplicate-member:${member}:${group.id}`, `成员“${member}”同时属于：${explicitGroups.map((item) => item.name || "未命名组").join("、")}`)); });
  nodes.forEach((groups, node) => { if (groups.length > 1) groups.forEach((group) => add(group.id, `duplicate-node:${node}:${group.id}`, `节点“${node}”同时属于：${groups.map((item) => item.name || "未命名组").join("、")}`)); });
  return issues;
};

type GuidedField = { path: string; label: string; unit?: string; min: number; max: number; step?: number; nullable?: boolean; defaultValue?: number };
const guidedSections: Array<{ title: string; fields: GuidedField[] }> = [
  { title: "采集与排队", fields: [
    { path: "refresh_seconds", label: "快照刷新间隔", unit: "秒", min: 10, max: 3600 },
    { path: "telemetry_lookback_minutes", label: "实时遥测窗口", unit: "分钟", min: 1, max: 60 },
    { path: "pending_pressure.min_wait_minutes", label: "Pending 压力等待阈值", unit: "分钟", min: 0, max: 1440 },
    { path: "pending_pressure.min_jobs", label: "Pending 压力任务数", unit: "个", min: 1, max: 1000 },
  ] },
  { title: "训练与调度画像", fields: [
    { path: "training.cpu_per_gpu", label: "训练每 GPU CPU 上限", unit: "CPU", min: 0.1, max: 1024, step: 0.1 },
    { path: "training.memory_gib_per_gpu", label: "训练每 GPU 内存上限", unit: "GiB", min: 0.1, max: 16384, step: 0.1 },
    { path: "planning.default_cpu_per_gpu", label: "调度默认 CPU/GPU", unit: "CPU", min: 0.1, max: 1024, step: 0.1 },
    { path: "planning.default_memory_gib_per_gpu", label: "调度默认内存/GPU", unit: "GiB", min: 0.1, max: 16384, step: 0.1 },
  ] },
  { title: "低利用率规则", fields: [
    { path: "low_utilization.gpu_power_limit_w", label: "每卡功率上限（计算基准）", unit: "W", min: 0.1, max: Infinity, step: 0.1, defaultValue: 400 },
    { path: "low_utilization.gpu_power_threshold_pct", label: "功率阈值（留空关闭）", unit: "%", min: 0, max: 100, step: 0.1, nullable: true },
    { path: "low_utilization.window_hours", label: "评估窗口", unit: "小时", min: 1, max: 168 },
    { path: "low_utilization.min_observation_minutes", label: "最短观测时间", unit: "分钟", min: 0, max: 10080 },
    { path: "low_utilization.gpu_compute_threshold_pct", label: "GPU Compute 阈值", unit: "%", min: 0, max: 100, step: 0.1 },
    { path: "low_utilization.gpu_memory_threshold_pct", label: "GPU Memory 阈值", unit: "%", min: 0, max: 100, step: 0.1 },
  ] },
];

const parseResource = (text: string): Record<string, unknown> | null => {
  try { const value = JSON.parse(text); return value && typeof value === "object" && !Array.isArray(value) ? value : null; }
  catch { return null; }
};
const readPath = (value: Record<string, unknown>, path: string) => path.split(".").reduce<unknown>((current, key) => current && typeof current === "object" ? (current as Record<string, unknown>)[key] : undefined, value);
const writePath = (value: Record<string, unknown>, path: string, next: number | null) => {
  const result = structuredClone(value);
  const parts = path.split(".");
  let cursor = result;
  parts.slice(0, -1).forEach((key) => {
    if (!cursor[key] || typeof cursor[key] !== "object") cursor[key] = {};
    cursor = cursor[key] as Record<string, unknown>;
  });
  cursor[parts.at(-1)!] = next;
  return result;
};
const flatten = (value: unknown, prefix = "", result: Record<string, unknown> = {}) => {
  if (value && typeof value === "object" && !Array.isArray(value)) Object.entries(value as Record<string, unknown>).forEach(([key, child]) => flatten(child, prefix ? `${prefix}.${key}` : key, result));
  else result[prefix] = value;
  return result;
};

class AdminApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

async function adminApi<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(monitorApiUrl(`/admin${path}`), { ...init, credentials: "include" });
  if (!response.ok) {
    let message = response.statusText;
    try { message = (await response.json()).detail ?? message; } catch { /* response has no JSON body */ }
    throw new AdminApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function AssetPicker({
  title, options, selected, owners, renderOption, onChange, readOnly = false,
}: {
  title: string;
  options: string[];
  selected: string[];
  owners: Map<string, string[]>;
  renderOption?: (value: string) => string;
  onChange: (values: string[]) => void;
  readOnly?: boolean;
}) {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const visibleOptions = options.filter((value) => !normalizedQuery || value.toLowerCase().includes(normalizedQuery));
  const toggle = (value: string) => onChange(selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]);
  return <fieldset className="asset-picker">
    <legend>{title} <span>{selected.length}</span></legend>
    <input aria-label={`${title}搜索`} placeholder={`搜索${title}`} value={query} onChange={(event) => setQuery(event.target.value)} />
    <div className="asset-chips">{selected.length ? selected.map((value) => readOnly ? <span className="asset-chip" key={value}>{value}</span> : <button type="button" className="asset-chip" key={value} onClick={() => toggle(value)}>{value} ×</button>) : <span className="muted">尚未选择</span>}</div>
    <div className="asset-options" aria-label={`${title}候选项`}>
      {visibleOptions.map((value) => {
        const otherOwners = (owners.get(value) ?? []).filter((owner) => owner !== "当前组");
        return <label className="asset-option" key={value}>
          <input type="checkbox" aria-label={`${title}：${value}`} checked={selected.includes(value)} disabled={readOnly} onChange={() => toggle(value)} />
          <span>{renderOption?.(value) ?? value}</span>
          {otherOwners.length > 0 && <small>草稿中已在：{otherOwners.join("、")}</small>}
        </label>;
      })}
      {!visibleOptions.length && <span className="muted">没有匹配项</span>}
    </div>
  </fieldset>;
}

function GroupAllocationEditor({
  draft, nodes, members, issues, activeGroupId, onSelectGroup, onChange, onSuggest, suggestionBusy,
}: {
  draft: GroupDraftState;
  nodes: AdminNode[];
  members: string[];
  issues: DraftIssue[];
  activeGroupId: string | null;
  onSelectGroup: (id: string) => void;
  onChange: (value: GroupDraftState) => void;
  onSuggest: () => void;
  suggestionBusy: boolean;
}) {
  const nodeMap = new Map(nodes.map((node) => [node.node, node]));
  const nodeOptions = [...new Set([...nodes.map((node) => node.node), ...draft.groups.flatMap((group) => group.nodes)])].sort();
  const memberOptions = [...new Set([...members, ...draft.groups.flatMap((group) => group.members)])].sort();
  const explicitMembers = new Set(draft.groups.filter((group) => group.name !== "default").flatMap((group) => group.members));
  const defaultMembers = memberOptions.filter((member) => !explicitMembers.has(member));
  const activeGroup = draft.groups.find((group) => group.id === activeGroupId) ?? draft.groups[0];
  const owners = (field: "members" | "nodes") => {
    const result = new Map<string, string[]>();
    draft.groups.filter((group) => field !== "members" || group.name !== "default").forEach((group) => group[field].forEach((value) => result.set(value, [...(result.get(value) ?? []), group.name || "未命名组"] )));
    return result;
  };
  const memberOwners = owners("members");
  const nodeOwners = owners("nodes");
  const groupIssues = (groupId: string) => issues.filter((issue) => issue.groupId === groupId);
  const updateGroup = (id: string, value: Partial<GroupDraft> & { name?: string }) => onChange({
    ...draft, groups: draft.groups.map((group) => group.id === id ? { ...group, ...value } : group),
  });
  const addGroup = () => {
    const usedNames = new Set(draft.groups.map((group) => group.name));
    let index = 1; let name = `新组 ${index}`;
    while (usedNames.has(name)) name = `新组 ${++index}`;
    const group: DraftGroup = { id: draftGroupId(name), name, gpu_quota: null, cpu_quota: null, memory_quota_gib: null, members: [], nodes: [] };
    onChange({ ...draft, groups: [...draft.groups, group] });
    onSelectGroup(group.id);
  };
  const removeGroup = (id: string) => {
    const group = draft.groups.find((item) => item.id === id);
    const fallback = draft.groups.find((item) => item.name === "default");
    if (!group || group.name === "default" || !fallback) return;
    const groups = draft.groups.filter((item) => item.id !== id).map((item) => item.id === fallback.id ? {
      ...item,
      nodes: [...new Set([...item.nodes, ...group.nodes])],
    } : item);
    onChange({ ...draft, groups });
    if (activeGroupId === id) onSelectGroup(fallback.id);
  };
  return <div className="group-allocation-editor">
    <label className="allocation-toggle"><input type="checkbox" checked={draft.node_allocation.enabled} onChange={(event) => onChange({ ...draft, node_allocation: { enabled: event.target.checked } })} /><span>启用节点归属约束</span><small>{draft.node_allocation.enabled ? "启用后 Skill 提交前会检查组节点并产生 placement 告警。" : "关闭时所有 queue 节点可用，Skill 不添加节点限制。"}</small></label>
    <div className="admin-editor-actions"><button type="button" onClick={addGroup}>新增组</button><button type="button" onClick={onSuggest} disabled={suggestionBusy || !draft.groups.length || !nodes.length}>{suggestionBusy ? "计算冷启动方案…" : "计算冷启动分配建议"}</button></div>
    <div className="draft-editor-status"><b>{issues.length ? `草稿有 ${issues.length} 项待应用校验` : "草稿当前没有发现冲突"}</b><span>编辑不会立即写入配置，确认应用时才会提交。</span></div>
    <div className="group-editor-layout">
      <nav className="group-list" aria-label="组列表">{draft.groups.map((group) => <button type="button" key={group.id} className={group.id === activeGroup?.id ? "active" : ""} onClick={() => onSelectGroup(group.id)}><span>{group.name || "未命名组"}</span><small>{group.name === "default" ? defaultMembers.length : group.members.length} 成员 · {group.nodes.length} 节点{groupIssues(group.id).length ? ` · ${groupIssues(group.id).length} 项` : ""}</small></button>)}<button type="button" className="group-list-add" onClick={addGroup}>＋ 新增组</button></nav>
      {activeGroup && <article className="group-edit-pane">
        <header><div><span className="eyebrow">Draft group</span><h4>{activeGroup.name || "未命名组"}</h4></div><button type="button" className="danger-link" disabled={activeGroup.name === "default"} onClick={() => removeGroup(activeGroup.id)}>删除此组</button></header>
        <label className="group-name-field">组名<input aria-label="组名" value={activeGroup.name} onChange={(event) => updateGroup(activeGroup.id, { name: event.target.value })} readOnly={activeGroup.name === "default"} /></label>
        <div className="group-quota-fields">
          <label>GPU quota<select value={activeGroup.gpu_quota === "remainder" ? "remainder" : activeGroup.gpu_quota == null ? "unlimited" : "number"} onChange={(event) => updateGroup(activeGroup.id, { gpu_quota: event.target.value === "remainder" ? "remainder" : event.target.value === "unlimited" ? null : 0 })}><option value="number">指定数值</option><option value="remainder">remainder</option><option value="unlimited">不限</option></select>{activeGroup.gpu_quota !== "remainder" && activeGroup.gpu_quota !== null && <input type="number" min="0" step="8" value={activeGroup.gpu_quota} onChange={(event) => updateGroup(activeGroup.id, { gpu_quota: Number(event.target.value) })} />}</label>
          <label>CPU quota<input type="number" value={activeGroup.cpu_quota ?? ""} placeholder="不限" onChange={(event) => updateGroup(activeGroup.id, { cpu_quota: event.target.value === "" ? null : Number(event.target.value) })} /></label>
          <label>内存 quota GiB<input type="number" value={activeGroup.memory_quota_gib ?? ""} placeholder="不限" onChange={(event) => updateGroup(activeGroup.id, { memory_quota_gib: event.target.value === "" ? null : Number(event.target.value) })} /></label>
        </div>
        <AssetPicker title="成员" options={memberOptions} selected={activeGroup.name === "default" ? defaultMembers : activeGroup.members} owners={memberOwners} readOnly={activeGroup.name === "default"} onChange={(values) => updateGroup(activeGroup.id, { members: values })} />
        <AssetPicker title="节点" options={nodeOptions} selected={activeGroup.nodes} owners={nodeOwners} renderOption={(value) => { const node = nodeMap.get(value); return node ? `${value} · ${node.state} · GPU ${node.allocated_gpu}/${node.total_gpu}` : `${value} · 草稿节点`; }} onChange={(values) => updateGroup(activeGroup.id, { nodes: values })} />
        {groupIssues(activeGroup.id).length > 0 && <div className="draft-issues">{groupIssues(activeGroup.id).map((issue) => <p key={issue.id}>{issue.message}</p>)}</div>}
      </article>}
    </div>
    <p className="muted">冷启动建议会忽略当前显式节点分配，重新覆盖当前快照的 queue 节点；未显式分配的节点有效归属 default。default 成员是当前已知用户中未分配到其他组的剩余用户，只读显示。成员和节点可在草稿阶段暂时重复，应用时必须互斥。</p>
  </div>;
}

export function AdminPanel({ close, onConfigured }: { close: () => void; onConfigured: () => void }) {
  const [session, setSession] = useState<AdminSession | null>(null);
  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [resourceEditor, setResourceEditor] = useState<EditorState>(() => emptyEditor("json"));
  const [groupEditor, setGroupEditor] = useState<EditorState>(() => emptyEditor("yaml"));
  const [groupDraft, setGroupDraft] = useState<GroupDraftState | null>(null);
  const [groupDirty, setGroupDirty] = useState(false);
  const [activeGroupId, setActiveGroupId] = useState<string | null>(null);
  const [applyReviewOpen, setApplyReviewOpen] = useState(false);
  const [applyErrors, setApplyErrors] = useState<string[]>([]);
  const [allocationPlans, setAllocationPlans] = useState<AllocationPlanResponse | null>(null);
  const [allocationBusy, setAllocationBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [resourceMode, setResourceMode] = useState<"guided" | "source">("guided");
  const panel = useRef<HTMLElement>(null);

  const loadConfig = async () => {
    const value = await adminApi<AdminConfig>("/config");
    setConfig(value);
    setResourceEditor((current) => current.dirty ? current : editorFrom(value.resource));
    setGroupEditor((current) => current.dirty ? current : editorFrom(value.groups));
    if (!groupDirty && value.groups_structured) {
      const nextDraft = structuredFrom(value.groups_structured);
      setGroupDraft(nextDraft);
      setActiveGroupId(nextDraft.groups[0]?.id ?? null);
    }
    return value;
  };

  useEffect(() => {
    adminApi<AdminSession>("/session").then(async (value) => {
      setSession(value); await loadConfig();
    }).catch((value) => {
      if (!(value instanceof AdminApiError) || value.status !== 401) setError(value instanceof Error ? value.message : String(value));
    });
  }, []);

  const login = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setBusy(true); setError(""); setNotice("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const value = await adminApi<AdminSession>("/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
      });
      form.reset(); setSession(value); await loadConfig();
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };

  const save = async (kind: "resource" | "groups") => {
    if (!session || !config) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const editor = kind === "resource" ? resourceEditor : groupEditor;
      const value = await adminApi<AdminConfig>(`/config/${kind}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: JSON.stringify({
          revision: editor.revision,
          text: editor.text,
        }),
      });
      setConfig(value);
      if (kind === "resource") {
        setResourceEditor(editorFrom(value.resource));
        setGroupEditor((current) => current.dirty ? current : editorFrom(value.groups));
        if (!groupDirty && value.groups_structured) {
          const nextDraft = structuredFrom(value.groups_structured);
          setGroupDraft(nextDraft);
          setActiveGroupId(nextDraft.groups[0]?.id ?? null);
        }
      } else {
        setGroupEditor(editorFrom(value.groups));
        setResourceEditor((current) => current.dirty ? current : editorFrom(value.resource));
      }
      setNotice(`${kind === "resource" ? "资源策略" : "私有分组"}已校验并写入本地配置。`);
      if (value.configured) onConfigured();
    } catch (value) {
      if (value instanceof AdminApiError && value.status === 401) setSession(null);
      setError(value instanceof Error ? value.message : String(value));
    }
    finally { setBusy(false); }
  };

  const saveGroupsStructured = async () => {
    if (!session || !config || !groupDraft) return;
    if (groupIssues.length > 0) {
      setApplyErrors(groupIssues.map((issue) => issue.message));
      setError("");
      return;
    }
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await adminApi<AdminConfig>("/config/groups-structured", {
        method: "PUT", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: JSON.stringify({ revision: config.groups.revision, ...structuredPayload(groupDraft) }),
      });
      setConfig(value);
      setGroupEditor(editorFrom(value.groups));
      if (value.groups_structured) {
        const nextDraft = structuredFrom(value.groups_structured);
        setGroupDraft(nextDraft);
        setActiveGroupId(nextDraft.groups[0]?.id ?? null);
      }
      setGroupDirty(false);
      setApplyReviewOpen(false);
      setApplyErrors([]);
      setNotice("私有分组与节点分配已校验并写入本地配置。");
      if (value.configured) onConfigured();
    } catch (value) {
      if (value instanceof AdminApiError && value.status === 401) setSession(null);
      setApplyErrors([value instanceof Error ? value.message : String(value)]);
      setError(value instanceof Error ? value.message : String(value));
    } finally { setBusy(false); }
  };

  const groupIssues = useMemo(() => groupDraft ? validateGroupDraft(groupDraft) : [], [groupDraft]);
  const openApplyReview = () => {
    setError(""); setNotice(""); setApplyErrors([]); setApplyReviewOpen(true);
  };

  const requestAllocationPlans = async () => {
    if (!session || !config || !groupDraft || !config.snapshot_id) return;
    setAllocationBusy(true); setError(""); setNotice("");
    try {
      const value = await adminApi<AllocationPlanResponse>("/node-allocation/plans", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: JSON.stringify({ snapshot_id: config.snapshot_id, groups_revision: config.groups.revision, search_seconds: 10 }),
      });
      setAllocationPlans(value);
      if (value.superseded) setNotice("建议基于较旧快照计算，仅可查看；请重新加载配置后再应用。 ");
    } catch (value) {
      if (value instanceof AdminApiError && value.status === 401) setSession(null);
      setError(value instanceof Error ? value.message : String(value));
    } finally { setAllocationBusy(false); }
  };

  const applyAllocationProposal = (proposal: AllocationProposal) => {
    if (!groupDraft || !allocationPlans || allocationPlans.superseded || !proposal.feasible) return;
    const groups = groupDraft.groups.map((group) => ({
      ...group,
      nodes: [...(proposal.groups[group.name]?.nodes ?? [])],
    }));
    setGroupDraft({ ...groupDraft, groups });
    setGroupDirty(true);
    setAllocationPlans(null);
    setNotice(`已将“${proposal.strategy}”方案回填到草稿；请核对后点击“确认应用组与节点分配”。`);
  };

  const rollback = async (kind: "resource" | "groups") => {
    if (!session || !config) return;
    const editor = kind === "resource" ? resourceEditor : groupEditor;
    const backup = config.backups?.[kind];
    if (!backup?.available || !backup.revision) return;
    if (!window.confirm(`将${kind === "resource" ? "资源策略" : "私有分组"}恢复为上一备份版本。当前版本会成为新的备份，确定继续吗？`)) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const value = await adminApi<AdminConfig>(`/config/${kind}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: JSON.stringify({ revision: editor.revision, backup_revision: backup.revision }),
      });
      setConfig(value); setResourceEditor(editorFrom(value.resource)); setGroupEditor(editorFrom(value.groups));
      if (value.groups_structured) {
        const nextDraft = structuredFrom(value.groups_structured);
        setGroupDraft(nextDraft);
        setActiveGroupId(nextDraft.groups[0]?.id ?? null);
      }
      setGroupDirty(false);
      setApplyReviewOpen(false);
      setApplyErrors([]);
      setNotice(`${kind === "resource" ? "资源策略" : "私有分组"}已恢复为上一备份版本。`);
      onConfigured();
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };

  const logout = async () => {
    if (!session) return;
    try {
      await adminApi("/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: "{}" });
    } finally { setSession(null); setConfig(null); setResourceEditor(emptyEditor("json")); setGroupEditor(emptyEditor("yaml")); setGroupDraft(null); setGroupDirty(false); setActiveGroupId(null); setApplyReviewOpen(false); setApplyErrors([]); }
  };

  const dirty = resourceEditor.dirty || groupEditor.dirty || groupDirty;
  const requestClose = () => { if (!dirty || window.confirm("存在未保存的配置修改，确定关闭吗？")) close(); };
  useDialogFocus(panel, requestClose);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener("beforeunload", beforeUnload);
    return () => { window.removeEventListener("beforeunload", beforeUnload); };
  }, [dirty]);

  const reloadEditor = async (kind: "resource" | "groups") => {
    const editor = kind === "resource" ? resourceEditor : groupEditor;
    if (editor.dirty && !window.confirm("重新加载会丢弃该编辑器中的未保存内容，确定继续吗？")) return;
    setBusy(true); setError("");
    try {
      const value = await adminApi<AdminConfig>("/config"); setConfig(value);
      if (kind === "resource") setResourceEditor(editorFrom(value.resource));
      else {
        setGroupEditor(editorFrom(value.groups));
        if (value.groups_structured) {
          const nextDraft = structuredFrom(value.groups_structured);
          setGroupDraft(nextDraft);
          setActiveGroupId(nextDraft.groups[0]?.id ?? null);
        }
        setGroupDirty(false); setApplyReviewOpen(false); setApplyErrors([]);
      }
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };

  const parsedResource = useMemo(() => parseResource(resourceEditor.text), [resourceEditor.text]);
  const resourceChanges = useMemo(() => {
    if (!config || !parsedResource) return [];
    const before = flatten(parseResource(config.resource.text) ?? {});
    const after = flatten(parsedResource);
    return [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key])).map((key) => ({ key, before: before[key], after: after[key] }));
  }, [config, parsedResource]);
  const powerThreshold = parsedResource ? readPath(parsedResource, "low_utilization.gpu_power_threshold_pct") : null;
  const powerLimit = Number(parsedResource ? readPath(parsedResource, "low_utilization.gpu_power_limit_w") ?? 400 : 400);
  const powerWatts = Number((Number(powerThreshold) * powerLimit / 100).toFixed(2));
  const effectiveDefaultMemberCount = useMemo(() => {
    if (!groupDraft) return 0;
    const explicitMembers = new Set(groupDraft.groups.filter((group) => group.name !== "default").flatMap((group) => group.members));
    return (config?.member_options ?? []).filter((member) => !explicitMembers.has(member)).length;
  }, [config?.member_options, groupDraft]);
  const updateGuided = (field: GuidedField, raw: string) => {
    if (!parsedResource) return;
    const value = field.nullable && raw.trim() === "" ? null : Number(raw);
    if (value !== null && !Number.isFinite(value)) return;
    const next = writePath(parsedResource, field.path, value);
    setResourceEditor((current) => ({ ...current, text: JSON.stringify(next, null, 2) + "\n", dirty: true, parse_error: null }));
  };

  return <div className="admin-backdrop" onClick={requestClose}><aside ref={panel} className="admin-panel" role="dialog" aria-modal="true" aria-label="管理员配置" onClick={(event) => event.stopPropagation()}>
    <header><div><span className="eyebrow">Server-side administration</span><h2>管理员配置</h2></div><button type="button" className="admin-close" onClick={requestClose} aria-label="关闭管理员配置">×</button></header>
    {!session ? <form className="admin-login" onSubmit={login}>
      <p>凭据只提交给本机 monitor 服务，不会保存到浏览器存储。</p>
      <label>管理员用户名<input name="username" autoComplete="username" required maxLength={64} /></label>
      <label>密码<input name="password" type="password" autoComplete="current-password" required maxLength={1024} /></label>
      <button disabled={busy}>{busy ? "登录中…" : "登录"}</button>
    </form> : <>
      <div className="admin-session"><span>已登录：<b>{session.username}</b> · 到期 {new Date(session.expires_at).toLocaleString()}</span><button type="button" onClick={logout}>退出</button></div>
      {config && <div className="admin-editors">
        {!config.effective_config_valid && <div className="banner">当前处于 setup-required 或 last-known-good。两份磁盘配置均有效后会自动按新配置采集。</div>}
        {config.validation_error && <div className="banner">{config.validation_error}</div>}
        {config.audit_error && <div className="banner">审计日志降级：{config.audit_error}</div>}
        <section><div className="admin-section-heading"><div><h3>资源策略</h3><small>常用参数可视化编辑；高级模式仍提供完整 JSON。</small></div><div className="segmented"><button type="button" className={resourceMode === "guided" ? "active" : ""} onClick={() => setResourceMode("guided")}>常用设置</button><button type="button" className={resourceMode === "source" ? "active" : ""} onClick={() => setResourceMode("source")}>JSON 源码</button></div></div>
          {resourceEditor.parse_error && <p className="admin-error">{resourceEditor.parse_error}</p>}
          {!parsedResource && <p className="admin-error">JSON 无法解析，请切换到源码模式修复。</p>}
          <div className="guided-config" hidden={resourceMode !== "guided"}>{parsedResource && guidedSections.map((section) => <fieldset key={section.title}><legend>{section.title}</legend><div>{section.fields.map((field) => <label key={field.path}><span>{field.label}</span><span className="number-input"><input type="number" min={field.min} max={Number.isFinite(field.max) ? field.max : undefined} step={field.step ?? 1} value={String(readPath(parsedResource, field.path) ?? field.defaultValue ?? "")} onChange={(event) => updateGuided(field, event.target.value)} /><em>{field.unit}</em></span></label>)}</div></fieldset>)}</div>
          {parsedResource && <p aria-label="功率阈值换算">{powerThreshold == null
            ? "功率判定已关闭"
            : `功率阈值：${Number(powerThreshold)}% × ${powerLimit} W = ${powerWatts} W/卡`}</p>}
          <div hidden={resourceMode !== "source"}><textarea aria-label="资源策略 JSON" value={resourceEditor.text} onChange={(event) => setResourceEditor((current) => ({ ...current, text: event.target.value, dirty: true }))} spellCheck={false} /></div>
          {resourceEditor.dirty && <details className="change-preview"><summary>查看变更预览 <span>{resourceChanges.length}</span></summary><div>{resourceChanges.length ? resourceChanges.map((item) => <p key={item.key}><code>{item.key}</code><del>{String(item.before ?? "—")}</del><ins>{String(item.after ?? "—")}</ins></p>) : <p>源码格式发生变化，结构化值未改变。</p>}</div></details>}
          <div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => reloadEditor("resource")}>重新加载</button><button type="button" disabled={busy || !config.backups?.resource.available} onClick={() => rollback("resource")}>恢复上一版本</button><button type="button" disabled={busy || !resourceEditor.dirty || !parsedResource} onClick={() => save("resource")}>校验并保存资源策略</button></div></section>
        <section><div><h3>组与节点分配</h3><small>先在草稿中编辑组、成员和节点；节点归属信息对所有 Monitor 使用者公开。未显式分配的在线节点有效归属 default。</small>{groupEditor.parse_error && <p className="admin-error">{groupEditor.parse_error}</p>}</div>{groupDraft ? <GroupAllocationEditor draft={groupDraft} nodes={config.node_options ?? []} members={config.member_options ?? []} issues={groupIssues} activeGroupId={activeGroupId} onSelectGroup={setActiveGroupId} onSuggest={requestAllocationPlans} suggestionBusy={allocationBusy} onChange={(value) => { setGroupDraft(value); setGroupDirty(true); setAllocationPlans(null); setApplyErrors([]); }} /> : <p className="muted">当前配置未就绪，请先完成资源和分组配置。</p>}
          {allocationPlans && <div className="allocation-proposals" aria-label="冷启动分配建议"><header><div><h4>冷启动分配建议</h4><small>快照 {allocationPlans.snapshot_id.slice(0, 12)} · 三种目标均为预览，不会自动写入配置。</small></div><button type="button" onClick={() => setAllocationPlans(null)}>关闭</button></header>{allocationPlans.superseded && <p className="banner">快照已经更新，建议仅供参考；重新加载组配置后再计算。</p>}<div className="allocation-proposal-cards">{allocationPlans.proposals.map((proposal) => <article key={proposal.strategy} className={!proposal.feasible ? "proposal-infeasible" : ""}><h5>{{ "workload-first": "workload 优先", "gpu-first": "GPU 影响优先", "user-first": "用户影响优先" }[proposal.strategy] ?? proposal.strategy}</h5>{proposal.feasible ? <><p>受影响 workload <b>{proposal.metrics.affected_workloads ?? "—"}</b> · GPU <b>{proposal.metrics.affected_gpu ?? "—"}</b> · 用户 <b>{proposal.metrics.affected_users ?? "—"}</b></p><p>节点分配：{proposal.assignment.map((item) => `${item.node}→${item.group}`).join("，") || "无当前节点"}</p>{proposal.capacity_coverage.length > 0 && <p>quota 精确匹配：{proposal.capacity_coverage.filter((item) => item.covered).length}/{proposal.capacity_coverage.length}</p>}<button type="button" disabled={Boolean(allocationPlans.superseded)} onClick={() => applyAllocationProposal(proposal)}>回填此方案</button></> : <><p>不可行：{proposal.diagnostics.join("；") || proposal.status}</p><button type="button" disabled>不能应用</button></>}</article>)}</div></div>}
          <div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => reloadEditor("groups")}>重新加载并放弃草稿</button><button type="button" disabled={busy || !config.backups?.groups.available} onClick={() => rollback("groups")}>恢复上一版本</button><button type="button" disabled={busy || !groupDirty || !groupDraft} onClick={openApplyReview}>确认应用组与节点分配</button></div><details className="source-preview"><summary>查看当前持久化 YAML（只读）</summary><pre>{groupEditor.text}</pre></details></section>
        <details className="audit-panel"><summary>配置审计记录 <span>{config.audit?.length ?? 0}</span></summary><div>{config.audit?.map((record) => <article key={`${record.timestamp}:${record.after_revision}`}><span className={record.action === "rollback" ? "status status-warning" : "status status-info"}>{record.action === "rollback" ? "回滚" : "更新"}</span><p><b>{record.kind === "resource" ? "资源策略" : "私有分组"}</b><small>{record.actor} · {new Date(record.timestamp).toLocaleString()}</small></p><code>{record.after_revision.slice(0, 12)}</code></article>)}{!config.audit?.length && <p className="muted">暂无审计记录</p>}</div></details>
      </div>}
    </>}
    {config && applyReviewOpen && groupDraft && <div className="admin-confirm-backdrop" role="presentation" onClick={() => { if (!busy) setApplyReviewOpen(false); }}><section className="admin-confirm-dialog" role="dialog" aria-modal="true" aria-label="确认应用组与节点分配" onClick={(event) => event.stopPropagation()}>
      <header><div><span className="eyebrow">Review draft</span><h3>确认应用组与节点分配</h3></div><button type="button" onClick={() => setApplyReviewOpen(false)} disabled={busy} aria-label="关闭确认面板">×</button></header>
      <p className="muted">提交时将使用 revision <code>{config.groups.revision.slice(0, 12)}</code>，服务端会再次校验并在成功后原子写入。</p>
      <div className="apply-summary"><p><b>节点归属约束：</b>{groupDraft.node_allocation.enabled ? "启用" : "关闭"}</p><p><b>草稿组：</b>{groupDraft.groups.length} 个</p>{groupDraft.groups.map((group) => <article key={group.id}><b>{group.name || "未命名组"}</b><span>GPU quota：{group.gpu_quota === "remainder" ? "remainder" : group.gpu_quota == null ? "不限" : group.gpu_quota}</span><span>{group.name === "default" ? effectiveDefaultMemberCount : group.members.length} 个成员 · {group.nodes.length} 个节点</span></article>)}</div>
      {(groupIssues.length > 0 || applyErrors.length > 0) && <div className="draft-issues apply-errors"><strong>应用前校验未通过，草稿仍保留：</strong>{[...groupIssues.map((issue) => issue.message), ...applyErrors].filter((message, index, values) => values.indexOf(message) === index).map((message) => <p key={message}>{message}</p>)}</div>}
      <div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => setApplyReviewOpen(false)}>返回继续编辑</button><button type="button" disabled={busy} onClick={saveGroupsStructured}>{busy ? "校验并应用中…" : "确认并应用"}</button></div>
    </section></div>}
    <div aria-live="polite">{error && <p className="admin-error">{error}</p>}{notice && <p className="admin-notice">{notice}</p>}</div>
  </aside></div>;
}
