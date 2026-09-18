import { useEffect, useRef, useState } from "react";
import { alertIdentity } from "./DetailDrawer";
import { formatPowerPercent, statusClass } from "./Table";
import type { Alert, DetailRef, HistoryPoint, HistoryResponse, PolicyFinding, Snapshot, Workload } from "./types";
import {
  DEFAULT_TREND_RANGE_SECONDS,
  TREND_RANGE_MARKS,
  TREND_SLIDER_STEPS,
  formatTrendRange,
  snapTrendSliderPosition,
  sliderPositionFromTrendRange,
  trendRangeFromSlider,
  type TrendRange,
} from "./navigation";

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

function Delta({ value, aggregate }: { value: number | null; aggregate: boolean }) {
  const comparison = aggregate ? "较上一聚合点" : "较上一快照";
  const formatted = value == null ? "" : number(Math.round(value * 10) / 10);
  if (value == null || value === 0) return <small className="metric-delta neutral">{comparison}持平</small>;
  return <small className={value > 0 ? "metric-delta up" : "metric-delta down"}>{comparison} {value > 0 ? "+" : ""}{formatted}</small>;
}

function axisLabel(value: string, spanMs: number) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  if (spanMs <= 48 * 60 * 60 * 1_000) return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (spanMs <= 90 * 24 * 60 * 60 * 1_000) return date.toLocaleDateString([], { month: "numeric", day: "numeric" });
  return date.toLocaleDateString([], { year: "2-digit", month: "numeric", day: "numeric" });
}

const trendNumber = (value: unknown) => typeof value === "number" ? number(Math.round(value * 10) / 10) : "—";
function Sparkline({ points, field, label, activeSnapshotId, onActivate }: {
  points: HistoryPoint[];
  field: keyof HistoryPoint;
  label: string;
  activeSnapshotId: string | null;
  onActivate: (snapshotId: string) => void;
}) {
  // Keep missing values in the original point positions so every metric
  // remains aligned to the same history timestamp.
  const samples = points.flatMap((point, pointIndex) => typeof point[field] === "number" ? [{ value: point[field] as number, pointIndex }] : []);
  const hasTrend = samples.length >= 2;
  const values = samples.map((sample) => sample.value);
  const min = hasTrend ? Math.min(...values) : 0;
  const max = hasTrend ? Math.max(...values) : 0;
  const flat = max === min;
  const range = max - min || 1;
  const xAt = (index: number) => points.length < 2 ? 0 : index / (points.length - 1) * 100;
  const yAt = (value: number) => flat ? 20 : 36 - (value - min) / range * 30;
  const path = samples.map((sample, index) => `${index ? "L" : "M"} ${xAt(sample.pointIndex)} ${yAt(sample.value)}`).join(" ");
  const areaPath = hasTrend ? `${path} L ${xAt(samples.at(-1)!.pointIndex)} 39 L ${xAt(samples[0].pointIndex)} 39 Z` : "";
  const latest = values.at(-1);
  const startedAt = new Date(points[0]?.generated_at ?? "").getTime();
  const endedAt = new Date(points.at(-1)?.generated_at ?? "").getTime();
  const spanMs = Number.isFinite(startedAt) && Number.isFinite(endedAt) ? Math.max(0, endedAt - startedAt) : 0;
  const axisSamples = points.length > 2 ? [points[0], points[Math.floor((points.length - 1) / 2)], points.at(-1)!] : [points[0], points.at(-1)!];
  const activeIndex = activeSnapshotId == null ? -1 : points.findIndex((point) => point.snapshot_id === activeSnapshotId);
  const activePoint = activeIndex >= 0 ? points[activeIndex] : null;
  const activeValue = activePoint?.[field];
  const activeX = activePoint ? xAt(activeIndex) : null;
  const interactive = points.length >= 2;
  const activateFromPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0) return;
    const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    onActivate(points[Math.round(ratio * (points.length - 1))].snapshot_id);
  };
  const activateFromKeyboard = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!interactive || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = activeIndex >= 0 ? activeIndex : points.length - 1;
    const next = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1 : event.key === "ArrowLeft" ? Math.max(0, current - 1) : Math.min(points.length - 1, current + 1);
    onActivate(points[next].snapshot_id);
  };

  return <div
    className="sparkline-chart"
    role="group"
    aria-label={`${label}趋势交互`}
    tabIndex={interactive ? 0 : undefined}
    onPointerMove={interactive ? activateFromPointer : undefined}
    onFocus={() => { if (interactive && !activePoint) onActivate(points.at(-1)!.snapshot_id); }}
    onKeyDown={activateFromKeyboard}
  >
    <svg className="sparkline" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={`${label}趋势，当前 ${trendNumber(latest)}`}>
      <line className="sparkline-axis" x1="0" y1="39" x2="100" y2="39" />
      {hasTrend && <path className="sparkline-area" d={areaPath} />}
      {hasTrend && <path className="sparkline-line" d={path} />}
      {activeX != null && <line className="sparkline-guide" x1={activeX} y1="1" x2={activeX} y2="39" />}
      {activeX != null && typeof activeValue === "number" && <circle className="sparkline-point" cx={activeX} cy={yAt(activeValue)} r="1.8" />}
    </svg>
    {activeX != null && <output className={`sparkline-readout ${activeX > 50 ? "left" : "right"}`} aria-label={`${label}选中值`}><small>{label}</small><strong>{trendNumber(activeValue)}</strong></output>}
    {hasTrend ? <div className={`sparkline-labels ${axisSamples.length === 2 ? "two" : ""}`} aria-label={`${label}时间轴`}>
      {axisSamples.map((sample, index) => <time key={`${sample.generated_at}:${index}`} dateTime={sample.generated_at} title={new Date(sample.generated_at).toLocaleString()}>{axisLabel(sample.generated_at, spanMs)}</time>)}
    </div> : <div className="sparkline-empty">积累两个快照后显示趋势</div>}
  </div>;
}

function InsightList({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return <section className="overview-panel"><header><h3>{title}</h3>{action}</header><div className="insight-list" role="region" aria-label={`${title}列表`} tabIndex={0}>{children}</div></section>;
}

type PolicyPanelKey = "resource" | "utilization" | "placement";
type PolicyInsight = {
  id: string;
  subjectType: string;
  subjectId: string;
  subjectLabel: string;
  meta: string;
  finding: PolicyFinding;
  detail?: string;
  alert?: Alert;
};

const policyPanelDefinitions: Record<PolicyPanelKey, { title: string; categories: string[] }> = {
  resource: { title: "资源与配额策略", categories: ["resource-shape", "quota", "runtime"] },
  utilization: { title: "利用率与观测策略", categories: ["utilization", "telemetry"] },
  placement: { title: "节点归属策略", categories: ["placement", "attribution"] },
};

function findingCategory(code: string) {
  if (code.startsWith("resource.")) return "resource-shape";
  if (code.startsWith("quota.")) return "quota";
  if (code.startsWith("utilization.")) return "utilization";
  if (code.startsWith("telemetry.")) return "telemetry";
  if (code.startsWith("placement.")) return "placement";
  if (code.startsWith("attribution.")) return "attribution";
  if (code.startsWith("runtime.")) return "runtime";
  return "";
}

function fallbackFindings(entity: { policy_findings?: PolicyFinding[]; finding_codes?: string[]; finding_tags?: string[]; status?: string }) {
  if (entity.policy_findings?.length) return entity.policy_findings;
  return (entity.finding_codes ?? []).map((code): PolicyFinding => ({
    code,
    category: findingCategory(code),
    status: entity.status === "violation" ? "violation" : entity.status === "unknown" ? "unknown" : "warning",
    message: code,
    tags: entity.finding_tags ?? [],
    observed: {},
    limit: {},
  }));
}

function alertStatus(severity: string) {
  if (["critical", "error"].includes(severity)) return "violation";
  if (severity === "warning") return "warning";
  return severity || "unknown";
}

function collectPolicyInsights(snapshot: Snapshot, panel: PolicyPanelKey): PolicyInsight[] {
  const categories = new Set(policyPanelDefinitions[panel].categories);
  const entries: PolicyInsight[] = [];
  const seen = new Set<string>();
  const add = (subjectType: string, subjectId: string, subjectLabel: string, meta: string, finding: PolicyFinding, alert?: Alert, detail?: string) => {
    if (!categories.has(finding.category)) return;
    const key = `${finding.category}:${finding.code}:${subjectType}:${subjectId}`;
    if (seen.has(key)) return;
    seen.add(key);
    entries.push({ id: key, subjectType, subjectId, subjectLabel, meta, finding, alert, detail });
  };

  for (const workload of [...snapshot.workloads, ...(snapshot.pending_workloads ?? [])]) {
    for (const finding of fallbackFindings(workload)) {
      const detail = finding.code === "utilization.low_gpu_activity"
        ? `最近 ${number(workload.historical_telemetry?.window_hours)} 小时 · GPU ${number(workload.historical_telemetry?.gpu_compute_util_avg_pct, "%")} · 显存 ${number(workload.historical_telemetry?.gpu_memory_util_avg_pct, "%")} · 功率 ${formatPowerPercent(workload.historical_telemetry?.gpu_power_util_avg_pct)}`
        : undefined;
      add("workload", workload.workload_id, workload.workload_name, `${workload.user} · ${workload.group}`, finding, undefined, detail);
    }
  }
  for (const group of snapshot.groups) {
    for (const finding of fallbackFindings(group)) add("group", group.group, group.group, `${group.members.length} 个成员`, finding);
  }
  for (const user of snapshot.users) {
    for (const finding of fallbackFindings(user)) {
      // User summaries copy workload/group findings; retain only user-local findings here.
      if (finding.source_type) continue;
      add("user", user.user, user.user, user.group, finding);
    }
  }
  for (const alert of snapshot.alerts) {
    const category = alert.category ?? alert.finding_categories?.[0] ?? findingCategory(alert.code ?? "");
    if (!category || !categories.has(category)) continue;
    const finding: PolicyFinding = {
      code: alert.code ?? alert.kind,
      category,
      status: alertStatus(alert.severity),
      message: alert.message,
      tags: alert.tags ?? alert.finding_tags ?? [],
      observed: alert.observed ?? {},
      limit: alert.limit ?? {},
    };
    add(alert.subject_type ?? "alert", String(alert.subject), String(alert.subject), alert.subject_type ?? "队列", finding, alert);
  }
  const statusRank: Record<string, number> = { violation: 0, warning: 1, burst: 2, unknown: 3 };
  return entries.sort((left, right) => (statusRank[left.finding.status] ?? 4) - (statusRank[right.finding.status] ?? 4) || left.subjectLabel.localeCompare(right.subjectLabel) || left.finding.code.localeCompare(right.finding.code));
}

function TrendRangeSlider({ range, onRange }: { range: TrendRange; onRange: (range: TrendRange) => void }) {
  const [position, setPosition] = useState(() => sliderPositionFromTrendRange(range));
  const positionRef = useRef(position);
  const timerRef = useRef<number | null>(null);
  const pointerActiveRef = useRef(false);

  useEffect(() => {
    const next = sliderPositionFromTrendRange(range);
    positionRef.current = next;
    setPosition(next);
  }, [range]);

  useEffect(() => () => {
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
  }, []);

  const commit = (next: number, immediate = false, snap = false) => {
    const bounded = snap ? snapTrendSliderPosition(next) : Math.min(TREND_SLIDER_STEPS, Math.max(0, Math.round(next)));
    positionRef.current = bounded;
    setPosition(bounded);
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    const submit = () => onRange(trendRangeFromSlider(positionRef.current, snap));
    if (immediate) submit();
    else timerRef.current = window.setTimeout(() => { timerRef.current = null; submit(); }, 220);
  };

  const commitCurrent = (snap = false) => commit(positionRef.current, true, snap);
  const previewRange = trendRangeFromSlider(position, pointerActiveRef.current);

  return <div className="trend-range-control">
    <div className="trend-range-heading"><label htmlFor="trend-range">趋势范围</label><output htmlFor="trend-range">{formatTrendRange(previewRange)}</output></div>
    <input
      id="trend-range"
      type="range"
      min={0}
      max={TREND_SLIDER_STEPS}
      step={1}
      value={position}
      aria-label="趋势时间范围"
      aria-valuetext={formatTrendRange(previewRange)}
      onChange={(event) => commit(Number(event.target.value), false, pointerActiveRef.current)}
      onPointerDown={() => { pointerActiveRef.current = true; }}
      onPointerUp={() => { pointerActiveRef.current = false; commitCurrent(true); }}
      onBlur={() => { pointerActiveRef.current = false; commitCurrent(true); }}
      onKeyDown={() => { pointerActiveRef.current = false; }}
      onKeyUp={() => commitCurrent(false)}
    />
    <div className="trend-range-marks" aria-hidden="true">
      {TREND_RANGE_MARKS.map((mark) => <span key={mark.seconds} style={{ left: `${sliderPositionFromTrendRange(mark.seconds) / TREND_SLIDER_STEPS * 100}%` }}>{mark.label}</span>)}
    </div>
  </div>;
}

export function Overview({ snapshot, history, open, navigate, range, onRange, historyRefreshing }: {
  snapshot: Snapshot;
  history: HistoryResponse | null;
  open: (ref: DetailRef) => void;
  navigate: (tab: "nodes" | "workloads" | "alerts") => void;
  range: TrendRange;
  onRange: (range: TrendRange) => void;
  historyRefreshing: boolean;
}) {
  const points = history?.points ?? [];
  const [activeSnapshotId, setActiveSnapshotId] = useState<string | null>(null);
  const capacity = snapshot.capacity;
  const pending = [...(snapshot.pending_workloads ?? [])].sort((a, b) => Number(b.queue_age_seconds ?? 0) - Number(a.queue_age_seconds ?? 0));
  const policyInsights = {
    resource: collectPolicyInsights(snapshot, "resource"),
    utilization: collectPolicyInsights(snapshot, "utilization"),
    placement: collectPolicyInsights(snapshot, "placement"),
  };
  const severeAlerts = snapshot.alerts.filter((alert) => ["critical", "error", "warning"].includes(alert.severity));
  const openWorkload = (workload: Workload) => open({ kind: "workload", id: workload.workload_id, label: workload.workload_name });
  const openPolicyInsight = (insight: PolicyInsight) => {
    if (insight.subjectType === "workload") {
      const workload = [...snapshot.workloads, ...(snapshot.pending_workloads ?? [])].find((item) => item.workload_id === insight.subjectId);
      if (workload) return openWorkload(workload);
    }
    if (insight.subjectType === "group" && snapshot.groups.some((item) => item.group === insight.subjectId)) return open({ kind: "group", id: insight.subjectId, label: insight.subjectLabel });
    if (insight.subjectType === "user" && snapshot.users.some((item) => item.user === insight.subjectId)) return open({ kind: "user", id: insight.subjectId, label: insight.subjectLabel });
    if (insight.subjectType === "node" && snapshot.nodes.some((item) => item.node === insight.subjectId)) return open({ kind: "node", id: insight.subjectId, label: insight.subjectLabel });
    if (insight.alert) open({ kind: "alert", id: alertIdentity(insight.alert), label: insight.subjectLabel });
  };
  const resolution = history?.resolution_seconds ?? 1;
  const resolutionLabel = resolution < 60 ? "原始点" : resolution < 3600 ? `${Math.round(resolution / 60)} 分钟聚合` : resolution < 86_400 ? `${Math.round(resolution / 3600)} 小时聚合` : `${Math.round(resolution / 86_400)} 天聚合`;
  const activePoint = activeSnapshotId == null ? null : points.find((point) => point.snapshot_id === activeSnapshotId) ?? null;
  const historySummary = `${history?.storage === "sqlite" ? "SQLite 历史" : "内存历史"} · ${number(points.length)} 个展示点 · ${resolutionLabel}${history?.retention_days ? ` · 最多保留 ${history.retention_days} 天` : ""}`;
  const activeDate = activePoint ? new Date(activePoint.generated_at) : null;
  const activeTime = activeDate && !Number.isNaN(activeDate.getTime()) ? activeDate.toLocaleString() : "—";

  useEffect(() => setActiveSnapshotId(null), [range]);
  useEffect(() => {
    if (activeSnapshotId != null && !points.some((point) => point.snapshot_id === activeSnapshotId)) setActiveSnapshotId(null);
  }, [activeSnapshotId, points]);

  return <div className="overview-page">
    <section className="overview-hero">
      <div><span className="eyebrow">Operational overview</span><h2>集群运行总览</h2><p>从容量、排队与策略异常快速定位当前最值得关注的问题。</p></div>
      <div className="capacity-ring" style={{ "--value": `${percent(capacity.allocated_gpu, capacity.bound_gpu)}%` } as React.CSSProperties} aria-label={`GPU 分配率 ${percent(capacity.allocated_gpu, capacity.bound_gpu)}%`}><strong>{percent(capacity.allocated_gpu, capacity.bound_gpu)}%</strong><small>GPU 分配率</small></div>
    </section>

    <section className="trend-toolbar" aria-label="趋势时间范围">
      <TrendRangeSlider range={range || DEFAULT_TREND_RANGE_SECONDS} onRange={onRange} />
      <small>{activePoint ? <><b>{resolution > 1 ? "聚合点截至" : "快照时间"} {activeTime}</b><span> · {historySummary}</span></> : historyRefreshing ? "正在加载历史…" : historySummary}</small>
    </section>

    <section className="overview-metrics" onPointerLeave={() => setActiveSnapshotId(null)} onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setActiveSnapshotId(null); }}>
      <article><span>已分配 GPU</span><strong>{number(capacity.allocated_gpu)}<small> / {number(capacity.bound_gpu)}</small></strong><Delta value={delta(points, "allocated_gpu")} aggregate={resolution > 1} /><Sparkline points={points} field="allocated_gpu" label="已分配 GPU" activeSnapshotId={activeSnapshotId} onActivate={setActiveSnapshotId} /></article>
      <article><span>有效空闲 GPU</span><strong>{number(capacity.planning_eligible_free_gpu ?? capacity.free_gpu)}</strong><small className="metric-caption">原始空闲 {number(capacity.free_gpu)}</small><Sparkline points={points} field="free_gpu" label="空闲 GPU" activeSnapshotId={activeSnapshotId} onActivate={setActiveSnapshotId} /></article>
      <article><span>Pending Workload</span><strong>{number(pending.length)}</strong><Delta value={delta(points, "pending_workloads")} aggregate={resolution > 1} /><Sparkline points={points} field="pending_workloads" label="Pending Workload" activeSnapshotId={activeSnapshotId} onActivate={setActiveSnapshotId} /></article>
      <article><span>需关注告警</span><strong className={severeAlerts.length ? "attention" : "healthy"}>{number(severeAlerts.length)}</strong><small className="metric-caption">共 {number(snapshot.alerts.length)} 条告警</small><Sparkline points={points} field="alert_count" label="告警数" activeSnapshotId={activeSnapshotId} onActivate={setActiveSnapshotId} /></article>
    </section>

    <section className="overview-grid">
      <InsightList title="排队焦点" action={<button type="button" onClick={() => navigate("workloads")}>查看全部</button>}>
        {pending.map((workload) => <button type="button" key={workload.workload_id} onClick={() => openWorkload(workload)}><span><b>{workload.workload_name}</b><small>{workload.user} · {workload.group}</small></span><em><b>{queueAge(workload.queue_age_seconds)}</b><small>{number(workload.total_gpu)} GPU · {workload.priority ?? "无优先级"}</small></em></button>)}
        {!pending.length && <p className="overview-empty">当前没有 Pending Workload</p>}
      </InsightList>

      {(Object.entries(policyPanelDefinitions) as Array<[PolicyPanelKey, typeof policyPanelDefinitions[PolicyPanelKey]]>).map(([key, definition]) => {
        const insights = policyInsights[key];
        return <InsightList key={key} title={definition.title} action={<small className="overview-panel-count">{insights.length} 项</small>}>
          {insights.map((insight) => <button type="button" className="policy-insight-item" key={insight.id} onClick={() => openPolicyInsight(insight)}>
            <span><b>{insight.subjectLabel}</b><small>{insight.meta}</small><small>{insight.finding.message}</small>{insight.detail && <small role="group" aria-label="GPU、显存与功率预览">{insight.detail}</small>}</span>
            <em><span className={statusClass(insight.finding.status)}>{insight.finding.status}</span><small>{insight.finding.code}</small></em>
          </button>)}
          {!insights.length && <p className="overview-empty">当前没有相关策略问题</p>}
        </InsightList>;
      })}
    </section>
  </div>;
}
