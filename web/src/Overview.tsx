import { alertIdentity } from "./DetailDrawer";
import { statusClass } from "./Table";
import type { DetailRef, HistoryPoint, HistoryResponse, Snapshot, Workload } from "./types";

const number = (value: unknown, suffix = "") => value == null ? "—" : `${Number(value).toLocaleString()}${suffix}`;
const percent = (part: unknown, total: unknown) => Number(total) > 0 ? Math.round(Number(part) / Number(total) * 100) : 0;
const queueAge = (seconds: number | null | undefined) => {
  if (seconds == null) return "—";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  return `${Math.floor(minutes / 1440)} 天 ${Math.floor(minutes % 1440 / 60)} 小时`;
};

function delta(points: HistoryPoint[], key: keyof HistoryPoint) {
  if (points.length < 2) return null;
  const current = points.at(-1)?.[key];
  const previous = points.at(-2)?.[key];
  if (typeof current !== "number" || typeof previous !== "number") return null;
  return current - previous;
}

function Delta({ value }: { value: number | null }) {
  if (value == null || value === 0) return <small className="metric-delta neutral">较上一快照持平</small>;
  return <small className={value > 0 ? "metric-delta up" : "metric-delta down"}>较上一快照 {value > 0 ? "+" : ""}{number(value)}</small>;
}

function Sparkline({ points, field, label }: { points: HistoryPoint[]; field: keyof HistoryPoint; label: string }) {
  const values = points.map((point) => point[field]).filter((value): value is number => typeof value === "number").slice(-60);
  if (values.length < 2) return <div className="sparkline-empty">积累两个快照后显示趋势</div>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const path = values.map((value, index) => `${index ? "L" : "M"} ${index / (values.length - 1) * 100} ${36 - (value - min) / range * 30}`).join(" ");
  return <svg className="sparkline" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={`${label}趋势，当前 ${values.at(-1)}`}>
    <path className="sparkline-area" d={`${path} L 100 40 L 0 40 Z`} />
    <path className="sparkline-line" d={path} />
  </svg>;
}

function InsightList({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return <section className="overview-panel"><header><h3>{title}</h3>{action}</header><div className="insight-list">{children}</div></section>;
}

export function Overview({ snapshot, history, open, navigate }: {
  snapshot: Snapshot;
  history: HistoryResponse | null;
  open: (ref: DetailRef) => void;
  navigate: (tab: "nodes" | "workloads" | "alerts") => void;
}) {
  const points = history?.points ?? [];
  const capacity = snapshot.capacity;
  const pending = [...(snapshot.pending_workloads ?? [])].sort((a, b) => Number(b.queue_age_seconds ?? 0) - Number(a.queue_age_seconds ?? 0));
  const lowUtilization = snapshot.workloads.filter((workload) => workload.finding_codes?.includes("utilization.low_gpu_activity"));
  const nodeCounts = snapshot.nodes.reduce<Record<string, number>>((result, node) => ({ ...result, [node.classification]: (result[node.classification] ?? 0) + 1 }), {});
  const attentionNodes = snapshot.nodes.filter((node) => ["fragmented", "cpu-memory-blocked", "unavailable"].includes(node.classification)).slice(0, 5);
  const severeAlerts = snapshot.alerts.filter((alert) => ["critical", "error", "warning"].includes(alert.severity)).slice(0, 5);
  const openWorkload = (workload: Workload) => open({ kind: "workload", id: workload.workload_id, label: workload.workload_name });

  return <div className="overview-page">
    <section className="overview-hero">
      <div><span className="eyebrow">Operational overview</span><h2>集群运行总览</h2><p>从容量、排队、节点健康与策略异常快速定位当前最值得关注的问题。</p></div>
      <div className="capacity-ring" style={{ "--value": `${percent(capacity.allocated_gpu, capacity.bound_gpu)}%` } as React.CSSProperties} aria-label={`GPU 分配率 ${percent(capacity.allocated_gpu, capacity.bound_gpu)}%`}><strong>{percent(capacity.allocated_gpu, capacity.bound_gpu)}%</strong><small>GPU 分配率</small></div>
    </section>

    <section className="overview-metrics">
      <article><span>已分配 GPU</span><strong>{number(capacity.allocated_gpu)}<small> / {number(capacity.bound_gpu)}</small></strong><Delta value={delta(points, "allocated_gpu")} /><Sparkline points={points} field="allocated_gpu" label="已分配 GPU" /></article>
      <article><span>有效空闲 GPU</span><strong>{number(capacity.planning_eligible_free_gpu ?? capacity.free_gpu)}</strong><small className="metric-caption">原始空闲 {number(capacity.free_gpu)}</small><Sparkline points={points} field="free_gpu" label="空闲 GPU" /></article>
      <article><span>Pending Workload</span><strong>{number(pending.length)}</strong><Delta value={delta(points, "pending_workloads")} /><Sparkline points={points} field="pending_workloads" label="Pending Workload" /></article>
      <article><span>需关注告警</span><strong className={severeAlerts.length ? "attention" : "healthy"}>{number(severeAlerts.length)}</strong><small className="metric-caption">共 {number(snapshot.alerts.length)} 条告警</small><Sparkline points={points} field="alert_count" label="告警数" /></article>
    </section>

    <section className="overview-grid">
      <InsightList title="排队焦点" action={<button type="button" onClick={() => navigate("workloads")}>查看全部</button>}>
        {pending.slice(0, 5).map((workload) => <button type="button" key={workload.workload_id} onClick={() => openWorkload(workload)}><span><b>{workload.workload_name}</b><small>{workload.user} · {workload.group}</small></span><em><b>{queueAge(workload.queue_age_seconds)}</b><small>{number(workload.total_gpu)} GPU · {workload.priority ?? "无优先级"}</small></em></button>)}
        {!pending.length && <p className="overview-empty">当前没有 Pending Workload</p>}
      </InsightList>

      <InsightList title="节点健康" action={<button type="button" onClick={() => navigate("nodes")}>节点视图</button>}>
        <div className="node-distribution">{Object.entries(nodeCounts).map(([name, count]) => <span key={name} className={statusClass(name)}>{name}<b>{count}</b></span>)}</div>
        {attentionNodes.map((node) => <button type="button" key={node.node} onClick={() => open({ kind: "node", id: node.node, label: node.node })}><span><b>{node.node}</b><small>{node.classification}</small></span><em><b>{number(node.stranded_gpu)} blocked</b><small>{number(node.effective_free_gpu)} effective free</small></em></button>)}
        {!attentionNodes.length && <p className="overview-empty">节点状态良好</p>}
      </InsightList>

      <InsightList title="策略与利用率" action={<button type="button" onClick={() => navigate("workloads")}>Workload 视图</button>}>
        {lowUtilization.slice(0, 5).map((workload) => <button type="button" key={workload.workload_id} onClick={() => openWorkload(workload)}><span><b>{workload.workload_name}</b><small>{workload.user} · 低 GPU 活跃度</small></span><em><b>{number(workload.telemetry.gpu_compute_util_avg_pct, "%")}</b><small>最近 5 分钟</small></em></button>)}
        {!lowUtilization.length && <p className="overview-empty">当前没有低利用率策略发现</p>}
      </InsightList>

      <InsightList title="最新告警" action={<button type="button" onClick={() => navigate("alerts")}>告警中心</button>}>
        {severeAlerts.map((alert) => <button type="button" key={alertIdentity(alert)} onClick={() => open({ kind: "alert", id: alertIdentity(alert), label: alert.subject })}><span><b>{alert.subject}</b><small>{alert.code ?? alert.kind}</small></span><em><span className={statusClass(alert.severity)}>{alert.severity}</span></em></button>)}
        {!severeAlerts.length && <p className="overview-empty">当前没有需要关注的告警</p>}
      </InsightList>
    </section>
  </div>;
}
