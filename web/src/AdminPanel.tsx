import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
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
};

type AdminFile = { format: "json" | "yaml"; text: string; revision: string; parse_error: string | null };
type BackupInfo = { available: boolean; revision: string | null; updated_at: string | null };
type AuditRecord = { timestamp: string; actor: string; kind: string; action?: string; before_revision: string; after_revision: string };
type EditorState = AdminFile & { dirty: boolean };
const emptyEditor = (format: "json" | "yaml"): EditorState => ({ format, text: "", revision: "", parse_error: null, dirty: false });
const editorFrom = (value: AdminFile): EditorState => ({ ...value, dirty: false });

type GuidedField = { path: string; label: string; unit?: string; min: number; max: number; step?: number };
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
const writePath = (value: Record<string, unknown>, path: string, next: number) => {
  const result = structuredClone(value);
  const parts = path.split(".");
  let cursor = result;
  parts.slice(0, -1).forEach((key) => { cursor = cursor[key] as Record<string, unknown>; });
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
  const response = await fetch(`/api/v1/admin${path}`, { ...init, credentials: "same-origin" });
  if (!response.ok) {
    let message = response.statusText;
    try { message = (await response.json()).detail ?? message; } catch { /* response has no JSON body */ }
    throw new AdminApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export function AdminPanel({ close, onConfigured }: { close: () => void; onConfigured: () => void }) {
  const [session, setSession] = useState<AdminSession | null>(null);
  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [resourceEditor, setResourceEditor] = useState<EditorState>(() => emptyEditor("json"));
  const [groupEditor, setGroupEditor] = useState<EditorState>(() => emptyEditor("yaml"));
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
      setNotice(`${kind === "resource" ? "资源策略" : "私有分组"}已恢复为上一备份版本。`);
      onConfigured();
    } catch (value) { setError(value instanceof Error ? value.message : String(value)); }
    finally { setBusy(false); }
  };

  const logout = async () => {
    if (!session) return;
    try {
      await adminApi("/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: "{}" });
    } finally { setSession(null); setConfig(null); setResourceEditor(emptyEditor("json")); setGroupEditor(emptyEditor("yaml")); }
  };

  const dirty = resourceEditor.dirty || groupEditor.dirty;
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
      else setGroupEditor(editorFrom(value.groups));
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
  const updateGuided = (field: GuidedField, raw: string) => {
    if (!parsedResource) return;
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
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
          <div className="guided-config" hidden={resourceMode !== "guided"}>{parsedResource && guidedSections.map((section) => <fieldset key={section.title}><legend>{section.title}</legend><div>{section.fields.map((field) => <label key={field.path}><span>{field.label}</span><span className="number-input"><input type="number" min={field.min} max={field.max} step={field.step ?? 1} value={String(readPath(parsedResource, field.path) ?? "")} onChange={(event) => updateGuided(field, event.target.value)} /><em>{field.unit}</em></span></label>)}</div></fieldset>)}</div>
          <div hidden={resourceMode !== "source"}><textarea aria-label="资源策略 JSON" value={resourceEditor.text} onChange={(event) => setResourceEditor((current) => ({ ...current, text: event.target.value, dirty: true }))} spellCheck={false} /></div>
          {resourceEditor.dirty && <details className="change-preview"><summary>查看变更预览 <span>{resourceChanges.length}</span></summary><div>{resourceChanges.length ? resourceChanges.map((item) => <p key={item.key}><code>{item.key}</code><del>{String(item.before ?? "—")}</del><ins>{String(item.after ?? "—")}</ins></p>) : <p>源码格式发生变化，结构化值未改变。</p>}</div></details>}
          <div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => reloadEditor("resource")}>重新加载</button><button type="button" disabled={busy || !config.backups?.resource.available} onClick={() => rollback("resource")}>恢复上一版本</button><button type="button" disabled={busy || !resourceEditor.dirty || !parsedResource} onClick={() => save("resource")}>校验并保存资源策略</button></div></section>
        <section><div><h3>私有分组</h3><small>原始 YAML；GPU、CPU、内存 quota 独立，缺省表示不限。公共策略接口不会返回成员名单。</small>{groupEditor.parse_error && <p className="admin-error">{groupEditor.parse_error}</p>}</div><textarea aria-label="私有分组 YAML" value={groupEditor.text} onChange={(event) => setGroupEditor((current) => ({ ...current, text: event.target.value, dirty: true }))} spellCheck={false} /><div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => reloadEditor("groups")}>重新加载</button><button type="button" disabled={busy || !config.backups?.groups.available} onClick={() => rollback("groups")}>恢复上一版本</button><button type="button" disabled={busy || !groupEditor.dirty} onClick={() => save("groups")}>校验并保存私有分组</button></div></section>
        <details className="audit-panel"><summary>配置审计记录 <span>{config.audit?.length ?? 0}</span></summary><div>{config.audit?.map((record) => <article key={`${record.timestamp}:${record.after_revision}`}><span className={record.action === "rollback" ? "status status-warning" : "status status-info"}>{record.action === "rollback" ? "回滚" : "更新"}</span><p><b>{record.kind === "resource" ? "资源策略" : "私有分组"}</b><small>{record.actor} · {new Date(record.timestamp).toLocaleString()}</small></p><code>{record.after_revision.slice(0, 12)}</code></article>)}{!config.audit?.length && <p className="muted">暂无审计记录</p>}</div></details>
      </div>}
    </>}
    <div aria-live="polite">{error && <p className="admin-error">{error}</p>}{notice && <p className="admin-notice">{notice}</p>}</div>
  </aside></div>;
}
