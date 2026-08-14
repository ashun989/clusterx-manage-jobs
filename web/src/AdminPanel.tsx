import { FormEvent, useEffect, useState } from "react";

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
};

type AdminFile = { format: "json" | "yaml"; text: string; revision: string; parse_error: string | null };
type EditorState = AdminFile & { dirty: boolean };
const emptyEditor = (format: "json" | "yaml"): EditorState => ({ format, text: "", revision: "", parse_error: null, dirty: false });
const editorFrom = (value: AdminFile): EditorState => ({ ...value, dirty: false });

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

  const logout = async () => {
    if (!session) return;
    try {
      await adminApi("/logout", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: "{}" });
    } finally { setSession(null); setConfig(null); setResourceEditor(emptyEditor("json")); setGroupEditor(emptyEditor("yaml")); }
  };

  const dirty = resourceEditor.dirty || groupEditor.dirty;
  const requestClose = () => { if (!dirty || window.confirm("存在未保存的配置修改，确定关闭吗？")) close(); };
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") requestClose(); };
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener("keydown", keydown);
    window.addEventListener("beforeunload", beforeUnload);
    return () => { window.removeEventListener("keydown", keydown); window.removeEventListener("beforeunload", beforeUnload); };
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

  return <div className="admin-backdrop" onClick={requestClose}><aside className="admin-panel" aria-label="管理员配置" onClick={(event) => event.stopPropagation()}>
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
        <section><div><h3>资源策略</h3><small>原始 JSON；monitor 立即采用。提交包装器仅在 `--resource-policy` 或 `CLUSTERX_RESOURCE_POLICY` 指向同一路径时同步采用。</small>{resourceEditor.parse_error && <p className="admin-error">{resourceEditor.parse_error}</p>}</div><textarea aria-label="资源策略 JSON" value={resourceEditor.text} onChange={(event) => setResourceEditor((current) => ({ ...current, text: event.target.value, dirty: true }))} spellCheck={false} /><div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => reloadEditor("resource")}>重新加载</button><button type="button" disabled={busy || !resourceEditor.dirty} onClick={() => save("resource")}>校验并保存资源策略</button></div></section>
        <section><div><h3>私有分组</h3><small>原始 YAML，包含分组、quota 和拼音用户名；公共策略接口不会返回成员名单。</small>{groupEditor.parse_error && <p className="admin-error">{groupEditor.parse_error}</p>}</div><textarea aria-label="私有分组 YAML" value={groupEditor.text} onChange={(event) => setGroupEditor((current) => ({ ...current, text: event.target.value, dirty: true }))} spellCheck={false} /><div className="admin-editor-actions"><button type="button" disabled={busy} onClick={() => reloadEditor("groups")}>重新加载</button><button type="button" disabled={busy || !groupEditor.dirty} onClick={() => save("groups")}>校验并保存私有分组</button></div></section>
      </div>}
    </>}
    {error && <p className="admin-error">{error}</p>}{notice && <p className="admin-notice">{notice}</p>}
  </aside></div>;
}
