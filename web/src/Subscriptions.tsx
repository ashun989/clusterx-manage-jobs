import { createContext, useCallback, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError, monitorApiUrl } from "./api";
import type { Workload } from "./types";
import { useDialogFocus } from "./useDialogFocus";

const root = "/subscriptions";
const refreshKey = "clusterx-monitor:pt-email-changed";
type Target = { kind: string; uid: string; name: string };
type Preferences = { events: string[]; queue_minutes: number };
type Followed = Target & Preferences & { state: string | null; occurred_at: number | null };
type Session = { email_masked: string | null; csrf: string | null; follow_error?: string | null };
type Capabilities = { enabled: boolean; queue_minutes: number[]; events: { code: string; label: string; kinds: string[]; configured: boolean }[] };
type Panel = { view: "login" | "list" | "follow" | "settings" | "summary" | "unsubscribe"; target?: Target; token?: string };

const key = (target: Target) => `${target.kind}:${target.uid}`;
const targetPath = (target: Target) => `${root}/${encodeURIComponent(target.kind)}/${encodeURIComponent(target.uid)}`;
const defaults = (target: Target): Preferences => ({ events: target.kind === "trainingJob" ? ["job.failed"] : [], queue_minutes: 60 });
const states: Record<string, string> = { PENDING: "排队中", RUNNING: "运行中", FAILED: "失败", COMPLETED: "已完成", TERMINATED: "已停止" };

export function followTarget(workload: Workload): Target | undefined {
  return workload.workload_uid && ["trainingJob", "aid", "air"].includes(workload.type)
    ? { kind: workload.type, uid: workload.workload_uid, name: workload.workload_name }
    : undefined;
}

function initialPanel(): Panel | undefined {
  const url = new URL(location.href);
  if (url.hash.startsWith("#unsubscribe=")) return { view: "unsubscribe", token: url.hash.slice(13) };
  const code = url.searchParams.get("followTask");
  const [kind, uid] = code?.split(":") ?? [];
  if (["trainingJob", "aid", "air"].includes(kind) && uid && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(uid)) {
    return { view: "summary", target: { kind, uid, name: "" } };
  }
}

async function request<T>(path: string, method = "GET", body?: object, csrf?: string | null) {
  const signal = AbortSignal.timeout(12000);
  try {
    return await api<T>(path, { method, body: body ? JSON.stringify(body) : undefined,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
      signal, cache: "no-store", credentials: "same-origin" });
  } catch (error) {
    if (signal.aborted) throw new Error("请求响应超时。请刷新确认关注状态后再试。");
    throw error;
  }
}

type Controls = {
  enabled: boolean;
  ready: boolean;
  email: string | null;
  busy: boolean;
  panelOpen: boolean;
  following: Map<string, Followed>;
  open: (value: Panel) => void;
  toggle: (target: Target) => void;
};
const Context = createContext<Controls | undefined>(undefined);
export const useSubscriptions = () => useContext(Context)!;

export function SubscriptionsProvider({ children }: { children: ReactNode }) {
  const [caps, setCaps] = useState<Capabilities>();
  const [session, setSession] = useState<Session>();
  const [following, setFollowing] = useState(new Map<string, Followed>());
  const [ready, setReady] = useState(false);
  const [panel, setPanel] = useState<Panel | undefined>(initialPanel);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [emailInput, setEmailInput] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState("");
  const [prefs, setPrefs] = useState<Preferences>();
  const panelRef = useRef<HTMLElement>(null);
  const activity = useRef({ busy: false, revision: 0 });

  const reload = useCallback(async () => {
    if (activity.current.busy) return;
    const revision = ++activity.current.revision;
    try {
      const current = await request<Session>(root + "/session");
      const rows = current.email_masked ? await request<Followed[]>(root) : [];
      if (activity.current.revision === revision) {
        setSession(current);
        setFollowing(new Map(rows.map(item => [key(item), item])));
        setReady(true);
      }
    } catch (failure) {
      if (activity.current.revision !== revision) return;
      if (failure instanceof ApiError && failure.status === 401) {
        setSession(undefined);
        setFollowing(new Map());
      } else {
        setFeedback("暂时无法读取邮箱会话，请刷新后重试。");
      }
      setReady(true);
    }
  }, []);

  useEffect(() => {
    if (new URL(monitorApiUrl(root), location.href).origin !== location.origin) return;
    let cancelled = false;
    void request<Capabilities>(root + "/capabilities").then(value => {
      if (cancelled) return;
      setCaps(value);
      if (value.enabled) void reload();
    }).catch(() => {});
    return () => { cancelled = true; ++activity.current.revision; };
  }, [reload]);

  useEffect(() => {
    if (!caps?.enabled) return;
    const visibility = () => { if (document.visibilityState === "visible") void reload(); };
    const sync = (event: StorageEvent) => { if (event.key === refreshKey) void reload(); };
    window.addEventListener("focus", visibility);
    window.addEventListener("storage", sync);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("focus", visibility);
      window.removeEventListener("storage", sync);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [caps?.enabled, reload]);

  function syncTabs() {
    try { localStorage.setItem(refreshKey, String(Date.now())); } catch { /* Session refresh on focus remains available. */ }
  }

  function open(value: Panel) {
    if (activity.current.busy) return;
    setError("");
    setFeedback("");
    setCode("");
    setChallenge("");
    setEmailInput("");
    setPrefs(value.target ? following.get(key(value.target)) ?? defaults(value.target) : undefined);
    setPanel(value);
  }

  function close() {
    if (activity.current.busy) return;
    setPanel(undefined);
    const url = new URL(location.href);
    if (url.hash.startsWith("#unsubscribe=")) url.hash = "";
    url.searchParams.delete("followTask");
    if (url.href !== location.href) history.replaceState(history.state, "", url);
  }

  const panelOpen = Boolean(caps?.enabled && panel);
  useDialogFocus(panelRef, close, panelOpen);

  async function operate(action: () => Promise<void>, inline?: { target: Target; removing: boolean }) {
    if (activity.current.busy) return;
    activity.current.busy = true;
    ++activity.current.revision;
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      await action();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) {
        setSession(undefined);
        setFollowing(new Map());
        setPanel(inline && !inline.removing ? { view: "follow", target: inline.target } : { view: "login" });
      }
      const message = failure instanceof Error ? failure.message : "操作失败，请重试。";
      if (inline) setFeedback(message);
      else setError(message);
    } finally {
      activity.current.busy = false;
      setBusy(false);
    }
  }

  function updateItem(item: Followed) {
    setFollowing(current => new Map(current).set(key(item), item));
  }

  function toggle(target: Target) {
    if (!session?.email_masked) { open({ view: "follow", target }); return; }
    const removing = following.has(key(target));
    void operate(async () => {
      if (removing) {
        await request(targetPath(target), "DELETE", undefined, session.csrf);
        setFollowing(current => {
          const next = new Map(current);
          next.delete(key(target));
          return next;
        });
      } else {
        updateItem(await request<Followed>(targetPath(target), "PUT", undefined, session.csrf));
      }
      setFeedback(removing ? `已取消关注 ${target.name}` : `已关注 ${target.name}`);
      syncTabs();
    }, { target, removing });
  }

  function sendCode(event: FormEvent) {
    event.preventDefault();
    void operate(async () => {
      const response = await request<{ challenge_id: string }>(root + "/challenge", "POST", { email: emailInput });
      setChallenge(response.challenge_id);
      setFeedback("验证码已进入发送队列，10 分钟内有效。");
    });
  }

  function verify(event: FormEvent) {
    event.preventDefault();
    void operate(async () => {
      const target = panel?.view === "follow" ? panel.target : undefined;
      const current = await request<Session>(root + "/verify", "POST", {
        challenge_id: challenge, code, target: target ? { kind: target.kind, uid: target.uid } : undefined,
      });
      setSession(current);
      setFollowing(new Map());
      setChallenge("");
      setCode("");
      setEmailInput("");
      syncTabs();
      setPanel(panel?.view === "summary" || panel?.view === "settings" ? panel : { view: "list" });
      setFeedback(current.follow_error || (target ? `已关注 ${target.name}` : "邮箱已登录。"));
      const rows = await request<Followed[]>(root);
      setFollowing(new Map(rows.map(item => [key(item), item])));
    });
  }

  function logout(change: boolean) {
    void operate(async () => {
      await request(root + "/logout", "POST", undefined, session?.csrf);
      setSession(undefined);
      setFollowing(new Map());
      setPanel(change ? { view: "login" } : undefined);
      syncTabs();
      setFeedback(change ? "" : "已退出邮箱登录。");
    });
  }

  function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (!panel?.target || !prefs || !session?.csrf) return;
    const target = panel.target;
    void operate(async () => {
      const { events, queue_minutes } = prefs;
      updateItem(await request<Followed>(targetPath(target), "PUT", { events, queue_minutes }, session.csrf));
      setFeedback("提醒设置已保存。");
      syncTabs();
    });
  }

  const item = panel?.target && following.get(key(panel.target));
  const login = !session?.email_masked && panel?.view !== "unsubscribe";
  const title = panel?.view === "unsubscribe" ? "退订任务提醒" : login ? "邮箱登录" :
    panel?.view === "settings" ? "提醒设置" : panel?.view === "summary" ? "任务状态摘要" : "我的关注";

  return <Context.Provider value={{ enabled: Boolean(caps?.enabled), ready, email: session?.email_masked ?? null, busy, panelOpen, following, open, toggle }}>
    {children}
    {caps?.enabled && !panelOpen && feedback && <div className="subscription-toast" role="status">{feedback}<button onClick={() => setFeedback("")} aria-label="关闭提示">×</button></div>}
    {panelOpen && panel && <div className="subscription-backdrop" onClick={close}>
      <section className="subscription-dialog" ref={panelRef} role="dialog" aria-modal="true" aria-label={title} onClick={event => event.stopPropagation()}>
        <header><h2>{title}</h2><button type="button" onClick={close} aria-label="关闭邮箱面板">×</button></header>
        {panel.view === "unsubscribe" ? <>
          <p>确认取消关注邮件所对应的任务？</p>
          <button type="button" disabled={busy || Boolean(feedback)} onClick={() => void operate(async () => {
            await request(root + "/unsubscribe", "POST", { token: panel.token });
            setFeedback("已取消关注。");
            if (session?.email_masked) {
              const rows = await request<Followed[]>(root);
              setFollowing(new Map(rows.map(row => [key(row), row])));
            }
            syncTabs();
          })}>确认退订</button>
        </> : login ? <>
          {panel.target?.name && <p>{panel.view === "follow" ? "验证后自动关注 " : ""}<b>{panel.target.name}</b></p>}
          {!challenge ? <form onSubmit={sendCode}><label>PJLab 邮箱<input type="email" required value={emailInput} autoComplete="email" placeholder="name@pjlab.org.cn" onChange={event => setEmailInput(event.target.value)} /></label><button disabled={busy}>发送验证码</button></form>
            : <form onSubmit={verify}><label>邮箱验证码<input required inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} value={code} onChange={event => setCode(event.target.value)} /></label><div className="subscription-actions"><button disabled={busy}>{panel.view === "follow" ? "验证并关注" : "验证邮箱"}</button><button type="button" disabled={busy} onClick={() => { setChallenge(""); setCode(""); setFeedback(""); setError(""); }}>重新填写邮箱</button></div></form>}
        </> : panel.view === "settings" && prefs && panel.target ? <>
          <p><b>{panel.target.name}</b></p>
          {panel.target.kind !== "trainingJob" && <p className="muted">失败提醒暂不可用。仍可关注任务并选择下列提醒。</p>}
          <form onSubmit={saveSettings}>
            {panel.target.kind === "trainingJob" && <div className="subscription-actions">
              <button type="button" onClick={() => setPrefs({ ...prefs, events: ["job.failed"] })}>仅失败</button>
              <button type="button" onClick={() => setPrefs({ ...prefs, events: ["job.started", "job.failed", "job.completed"] })}>开跑、失败、完成</button>
            </div>}
            <fieldset><legend>发送邮件的事件</legend>{caps?.events.filter(rule => rule.kinds.includes(panel.target!.kind)).map(rule =>
              <label className="subscription-checkbox" key={rule.code}><input type="checkbox" checked={prefs.events.includes(rule.code)} disabled={!rule.configured && !prefs.events.includes(rule.code)}
                onChange={event => setPrefs({ ...prefs, events: event.target.checked ? [...prefs.events, rule.code] : prefs.events.filter(code => code !== rule.code) })} />
                {rule.label}{!rule.configured && <small>平台规则未启用</small>}</label>)}</fieldset>
            {prefs.events.includes("queue.wait") && <label>排队时长<select value={prefs.queue_minutes} onChange={event => setPrefs({ ...prefs, queue_minutes: Number(event.target.value) })}>
              {caps?.queue_minutes.map(minutes => <option key={minutes} value={minutes}>{minutes} 分钟</option>)}</select></label>}
            <button disabled={busy}>保存提醒设置</button>
          </form>
        </> : panel.view === "summary" ? item ? <>
          <h3>{item.name}</h3><p>{states[item.state ?? ""] ?? "状态未知"} {item.occurred_at ? new Date(item.occurred_at * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" }) + "（北京时间）" : ""}</p>
          <button type="button" onClick={() => open({ view: "settings", target: item })}>提醒设置</button>
        </> : <p>当前邮箱的关注列表中没有该任务。</p> : <>
          <div className="subscription-actions"><b>{session?.email_masked}</b><button type="button" disabled={busy} onClick={() => logout(true)}>更换邮箱</button><button type="button" disabled={busy} onClick={() => logout(false)}>退出登录</button></div>
          {!following.size && <p className="muted">还没有关注的任务。</p>}
          <ul className="subscription-list">{[...following.values()].map(row => <li key={key(row)}>
            <button type="button" title={row.name} onClick={() => open({ view: "summary", target: row })}>{row.name}</button>
            <div className="subscription-actions"><button type="button" onClick={() => open({ view: "settings", target: row })}>提醒设置</button>
              <button type="button" disabled={busy} onClick={() => toggle(row)}>取消关注</button></div>
          </li>)}</ul>
        </>}
        {error && <p role="alert" className="subscription-error">{error}</p>}
        {feedback && <p role="status">{feedback}</p>}
      </section>
    </div>}
  </Context.Provider>;
}

export function FollowActions({ workload }: { workload: Workload }) {
  const { enabled, ready, busy, following, toggle, open } = useSubscriptions();
  if (!enabled) return null;
  const target = followTarget(workload);
  if (!target) return <small className="muted">任务 UID 暂不可用</small>;
  const followed = following.has(key(target));
  return <span className="subscription-actions" onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}>
    <button type="button" aria-label={`${followed ? "取消关注" : "关注"} ${target.name}`} aria-pressed={followed} disabled={!ready || busy}
      onClick={() => toggle(target)}>{followed ? "★ 已关注" : "☆ 关注"}</button>
    <button type="button" aria-label={`提醒设置 ${target.name}`} disabled={!ready || busy} onClick={() => open({ view: "settings", target })}>提醒设置</button>
  </span>;
}

export function isFollowed(following: Map<string, Followed>, workload: Workload) {
  const target = followTarget(workload);
  return Boolean(target && following.has(key(target)));
}
