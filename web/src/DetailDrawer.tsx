import type { ReactNode } from "react";
import { statusClass } from "./Table";
import type { Alert, DetailRef, GroupSummary, NodeSummary, PolicyFinding, Snapshot, Telemetry, UserSummary, Workload } from "./types";

const number = (value: unknown, suffix = "") => value == null ? "—" : `${Number(value).toLocaleString()}${suffix}`;
const power = (watts: number | null) => watts == null ? "—" : watts >= 1000 ? `${(watts / 1000).toFixed(1)} kW` : `${watts.toFixed(0)} W`;
const allWorkloads = (snapshot: Snapshot) => [...snapshot.workloads, ...(snapshot.pending_workloads ?? [])];
const workloadResources = (workload: Workload) => `${number(workload.total_gpu)} GPU · ${number(workload.total_cpu)} CPU · ${number(workload.total_memory_gib)} GiB`;
const runtimeQuality = (value: Workload["runtime_quality"]) => ({ exact: "精确", observed: "观测", estimated: "估算", unavailable: "不可用" })[value ?? "unavailable"];
const runtimeSourceLabels: Record<string, string> = {
  training_status_start: "TrainingJob 状态",
  aid_pod_started_event: "AID Pod 启动事件",
  air_available_condition: "AIR Available 状态",
  pod_create_time: "Pod 创建时间",
  resource_create_time: "资源创建时间",
};
const runtimeSource = (value: Workload["runtime_source"]) => runtimeSourceLabels[value ?? ""] ?? "—";

export const alertIdentity = (alert: Alert) => [alert.severity, alert.kind, alert.subject, alert.message].join("\u0000");

function Metrics({ children }: { children: ReactNode }) {
  return <div className="detail-metrics">{children}</div>;
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return <div><small>{label}</small><strong>{value}</strong></div>;
}

function TelemetryPanel({ telemetry }: { telemetry: Telemetry }) {
  return <section className="detail-section"><h3>最近 5 分钟遥测</h3><Metrics>
    <Metric label="GPU Util" value={number(telemetry.gpu_compute_util_avg_pct, "%")} />
    <Metric label="GPU Memory" value={number(telemetry.gpu_memory_util_avg_pct, "%")} />
    <Metric label="总功率" value={power(telemetry.gpu_power_total_w)} />
    <Metric label="平均每卡" value={power(telemetry.gpu_power_avg_w)} />
    <Metric label="Compute 覆盖" value={`${telemetry.compute_reported_gpu_count}/${telemetry.allocated_gpu_count}`} />
    <Metric label="显存覆盖" value={`${telemetry.memory_reported_gpu_count}/${telemetry.allocated_gpu_count}`} />
    <Metric label="功率覆盖" value={`${telemetry.power_reported_gpu_count}/${telemetry.allocated_gpu_count}`} />
  </Metrics></section>;
}

function FindingsPanel({ findings }: { findings?: PolicyFinding[] }) {
  const rows = findings ?? [];
  return <section className="detail-section"><h3>策略发现<span className="section-count">{rows.length}</span></h3>{rows.length === 0 ? <p className="muted">无</p> : <div className="finding-list">{rows.map((finding, index) => <article key={`${finding.code}:${finding.source_id ?? "self"}:${index}`}>
    <header><code>{finding.code}</code><span className={statusClass(finding.status)}>{finding.status}</span></header>
    <p>{finding.message}</p><div className="tag-list"><span>{finding.category}</span>{finding.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
    <dl><div><dt>观测值</dt><dd>{JSON.stringify(finding.observed)}</dd></div><div><dt>限制值</dt><dd>{JSON.stringify(finding.limit)}</dd></div>{finding.window_hours != null && <div><dt>窗口</dt><dd>{finding.window_hours}h</dd></div>}{finding.source_type && <div><dt>来源</dt><dd>{finding.source_type} · {finding.source_id}</dd></div>}</dl>
  </article>)}</div>}</section>;
}

function RelatedList({ title, items, kind, open }: { title: string; items: Array<{ id: string; label: string; meta?: string }>; kind: DetailRef["kind"]; open: (ref: DetailRef) => void }) {
  return <section className="detail-section"><h3>{title}<span className="section-count">{items.length}</span></h3>
    {items.length === 0 ? <p className="muted">无</p> : <div className="related-list">{items.map((item) => <button type="button" key={item.id} onClick={() => open({ kind, id: item.id, label: item.label })}><span>{item.label}</span>{item.meta && <small>{item.meta}</small>}<i>›</i></button>)}</div>}
  </section>;
}

function GroupDetail({ group, snapshot, open }: { group: GroupSummary; snapshot: Snapshot; open: (ref: DetailRef) => void }) {
  const workloads = allWorkloads(snapshot).filter((item) => item.group === group.group);
  return <>
    <span className="eyebrow">Group</span><h2>{group.group}</h2><span className={statusClass(group.status)}>{group.status}</span>
    <Metrics>
      <Metric label="GPU" value={`${number(group.allocated_gpu)} / ${number(group.gpu_quota)}`} />
      <Metric label="CPU" value={`${number(group.allocated_cpu)} / ${number(group.cpu_quota)}`} />
      <Metric label="内存 GiB" value={`${number(group.allocated_memory_gib)} / ${number(group.memory_quota_gib)}`} />
      <Metric label="超限资源" value={group.over_resources.length ? group.over_resources.join(", ") : "无"} />
    </Metrics>
    <RelatedList title="当前活跃用户" kind="user" open={open} items={group.members.map((user) => ({ id: user, label: user, meta: snapshot.users.some((item) => item.user === user) ? undefined : "当前无资源" }))} />
    <RelatedList title="关联 Workload" kind="workload" open={open} items={workloads.map((item) => ({ id: item.workload_id, label: item.workload_name, meta: `${item.user} · ${workloadResources(item)}` }))} />
    <FindingsPanel findings={group.policy_findings} />
    <TelemetryPanel telemetry={group.telemetry} />
  </>;
}

function UserDetail({ user, snapshot, open }: { user: UserSummary; snapshot: Snapshot; open: (ref: DetailRef) => void }) {
  const workloads = allWorkloads(snapshot).filter((item) => item.user === user.user);
  return <>
    <span className="eyebrow">User</span><h2>{user.user}</h2><p><button className="inline-link" type="button" onClick={() => open({ kind: "group", id: user.group, label: user.group })}>{user.group}</button></p><span className={statusClass(user.status)}>{user.status}</span>
    <Metrics>
      <Metric label="Workloads" value={number(user.workload_count)} />
      <Metric label="GPU" value={number(user.allocated_gpu)} />
      <Metric label="CPU" value={number(user.allocated_cpu)} />
      <Metric label="内存 GiB" value={number(user.allocated_memory_gib)} />
    </Metrics>
    <RelatedList title="关联 Workload" kind="workload" open={open} items={workloads.map((item) => ({ id: item.workload_id, label: item.workload_name, meta: `${item.type} · ${workloadResources(item)}` }))} />
    <FindingsPanel findings={user.policy_findings} />
    <TelemetryPanel telemetry={user.telemetry} />
  </>;
}

function NodeDetail({ node, snapshot, open }: { node: NodeSummary; snapshot: Snapshot; open: (ref: DetailRef) => void }) {
  const workloadIds = new Set(Object.keys(node.workloads));
  const workloads = allWorkloads(snapshot).filter((item) => workloadIds.has(item.workload_id) || item.placements.some((placement) => placement.node === node.node));
  return <>
    <span className="eyebrow">Node · {node.state}</span><h2>{node.node}</h2><span className={statusClass(node.classification)}>{node.classification}</span><p className="muted">{node.host_ip || "无 Host IP"}</p>
    {!node.planning_eligible && <p className="banner">该节点资源归属不一致，仅用于监控，不参与调度模拟。{node.planning_exclusion_reasons.join(", ")}</p>}
    <Metrics>
      <Metric label="GPU" value={`${number(node.allocated_gpu)} / ${number(node.total_gpu)}`} />
      <Metric label="CPU" value={`${number(node.allocated_cpu)} / ${number(node.total_cpu)}`} />
      <Metric label="内存 GiB" value={`${number(node.allocated_memory_gib)} / ${number(node.total_memory_gib)}`} />
      <Metric label="有效空闲 GPU" value={number(node.effective_free_gpu)} />
      <Metric label="受阻 GPU" value={number(node.stranded_gpu)} />
      <Metric label="原始空闲 GPU" value={number(node.free_gpu)} />
    </Metrics>
    <section className="detail-section"><h3>资源归属</h3><Metrics>
      <Metric label="未归属 GPU / CPU / GiB" value={`${number(node.unattributed.gpu)} / ${number(node.unattributed.cpu)} / ${number(node.unattributed.memory_gib)}`} />
      <Metric label="归属超额 GPU / CPU / GiB" value={`${number(node.attribution_excess.gpu)} / ${number(node.attribution_excess.cpu)} / ${number(node.attribution_excess.memory_gib)}`} />
    </Metrics></section>
    <RelatedList title="节点 Workload" kind="workload" open={open} items={workloads.map((item) => ({ id: item.workload_id, label: item.workload_name, meta: `${item.user} · ${workloadResources(item)}` }))} />
    <TelemetryPanel telemetry={node.telemetry} />
  </>;
}

function WorkloadDetail({ workload, open }: { workload: Workload; open: (ref: DetailRef) => void }) {
  return <>
    <span className="eyebrow">{workload.type}</span><h2>{workload.workload_name}</h2><p><button className="inline-link" type="button" onClick={() => open({ kind: "user", id: workload.user, label: workload.user })}>{workload.user}</button> · <button className="inline-link" type="button" onClick={() => open({ kind: "group", id: workload.group, label: workload.group })}>{workload.group}</button></p><span className={statusClass(workload.policy_status)}>{workload.policy_status}</span>
    {workload.planning_eligible === false && <p className="banner">该 Workload 接触归属异常节点，不作为调度释放候选。</p>}
    {(workload.policy_reasons ?? []).map((reason) => <p className="error" key={reason}>{reason}</p>)}
    <Metrics>
      <Metric label="GPU" value={number(workload.total_gpu)} />
      <Metric label="CPU" value={number(workload.total_cpu)} />
      <Metric label="内存 GiB" value={number(workload.total_memory_gib)} />
      <Metric label="资源口径" value={workload.resource_basis === "requested" ? "申请资源" : "Pod 归属资源"} />
      <Metric label="运行时间" value={workload.runtime_hours == null ? "—" : `${number(workload.runtime_hours)}h${workload.runtime_quality === "observed" ? "（观测）" : workload.runtime_quality === "estimated" || workload.runtime_estimated ? "（估算）" : ""}`} />
      <Metric label="时间可信度" value={runtimeQuality(workload.runtime_quality)} />
      <Metric label="时间来源" value={runtimeSource(workload.runtime_source)} />
      <Metric label="开始时间" value={workload.start_time ? new Date(workload.start_time).toLocaleString() : "—"} />
      <Metric label="资源创建时间" value={workload.resource_create_time ? new Date(workload.resource_create_time).toLocaleString() : "—"} />
      <Metric label="Workspace" value={workload.workspace || "—"} />
    </Metrics>
    {workload.resource_basis === "requested" && <section className="detail-section"><h3>Task 请求<span className="section-count">{workload.task_resources?.length ?? 0}</span></h3>{workload.task_resources?.length ? <div className="plan-workloads"><table><thead><tr><th>Task</th><th>角色</th><th>副本数</th><th>每副本 GPU</th><th>每副本 CPU</th><th>每副本内存 GiB</th></tr></thead><tbody>{workload.task_resources.map((task, index) => <tr key={`${task.name}:${task.role}:${index}`}><td>{task.name}</td><td>{task.role || "—"}</td><td>{number(task.replicas)}</td><td>{number(task.gpu_per_replica)}</td><td>{number(task.cpu_per_replica)}</td><td>{number(task.memory_gib_per_replica)}</td></tr>)}</tbody></table></div> : <p className="muted">无 Task 资源明细</p>}</section>}
    {workload.resource_basis === "attributed" && <section className="detail-section"><h3>Placements（当前归属）<span className="section-count">{workload.placements.length}</span></h3><div className="placement-list">{workload.placements.map((placement, index) => <button type="button" key={`${placement.node}-${placement.pod ?? index}`} onClick={() => open({ kind: "node", id: placement.node, label: placement.node })}><span><b>{placement.node}</b><small>{placement.pod || "—"}</small></span><em>{number(placement.gpu)} GPU · {number(placement.cpu)} CPU · {number(placement.memory_gib)} GiB</em></button>)}</div></section>}
    <TelemetryPanel telemetry={workload.telemetry} />
    {workload.historical_telemetry && <section className="detail-section"><h3>历史 GPU 遥测</h3><Metrics>
      <Metric label="评估状态" value={<span className={statusClass(workload.historical_telemetry.evaluation_status)}>{workload.historical_telemetry.evaluation_status}</span>} />
      <Metric label="查询状态" value={workload.historical_telemetry.collection_status} />
      <Metric label="窗口" value={`${number(workload.historical_telemetry.window_hours)}h`} />
      <Metric label="GPU Util 平均" value={number(workload.historical_telemetry.gpu_compute_util_avg_pct, "%")} />
      <Metric label="显存 Util 平均" value={number(workload.historical_telemetry.gpu_memory_util_avg_pct, "%")} />
      <Metric label="样本（compute / memory）" value={`${number(workload.historical_telemetry.compute_sample_count)} / ${number(workload.historical_telemetry.memory_sample_count)}`} />
      <Metric label="抓取时间" value={workload.historical_telemetry.fetched_at ? new Date(workload.historical_telemetry.fetched_at).toLocaleString() : "—"} />
    </Metrics></section>}
    <FindingsPanel findings={workload.policy_findings} />
    <section className="detail-section"><h3>逐卡遥测<span className="section-count">{workload.gpus.length}</span></h3>{workload.gpus.length ? <pre>{JSON.stringify(workload.gpus, null, 2)}</pre> : <p className="muted">无逐卡遥测</p>}</section>
  </>;
}

function relatedAlertRef(alert: Alert, snapshot: Snapshot): DetailRef | null {
  const workloads = allWorkloads(snapshot);
  const workload = workloads.find((item) => item.workload_id === alert.subject || item.workload_name === alert.subject);
  if (workload) return { kind: "workload", id: workload.workload_id, label: workload.workload_name };
  if (snapshot.users.some((item) => item.user === alert.subject)) return { kind: "user", id: alert.subject, label: alert.subject };
  if (snapshot.groups.some((item) => item.group === alert.subject)) return { kind: "group", id: alert.subject, label: alert.subject };
  if (snapshot.nodes.some((item) => item.node === alert.subject)) return { kind: "node", id: alert.subject, label: alert.subject };
  return null;
}

function AlertDetail({ alert, snapshot, open }: { alert: Alert; snapshot: Snapshot; open: (ref: DetailRef) => void }) {
  const related = relatedAlertRef(alert, snapshot);
  return <>
    <span className="eyebrow">Alert · {alert.kind}</span><h2>{alert.subject}</h2><span className={statusClass(alert.severity)}>{alert.severity}</span>
    <section className="detail-section"><h3>说明</h3><p className="alert-message">{alert.message}</p></section>
    <Metrics><Metric label="规则代码" value={alert.code || "—"} /><Metric label="分类" value={alert.category || "—"} /><Metric label="对象类型" value={alert.subject_type || "—"} /><Metric label="标签" value={(alert.tags ?? []).join(", ") || "—"} /></Metrics>
    {related && <RelatedList title="关联对象" kind={related.kind} open={open} items={[{ id: related.id, label: related.label, meta: related.kind }]} />}
  </>;
}

function resolveDetail(ref: DetailRef, snapshot: Snapshot) {
  if (ref.kind === "group") return snapshot.groups.find((item) => item.group === ref.id);
  if (ref.kind === "user") return snapshot.users.find((item) => item.user === ref.id);
  if (ref.kind === "node") return snapshot.nodes.find((item) => item.node === ref.id);
  if (ref.kind === "workload") return allWorkloads(snapshot).find((item) => item.workload_id === ref.id);
  return snapshot.alerts.find((item) => alertIdentity(item) === ref.id);
}

export function DetailDrawer({ stack, snapshot, open, back, close }: { stack: DetailRef[]; snapshot: Snapshot; open: (ref: DetailRef) => void; back: () => void; close: () => void }) {
  const ref = stack.at(-1);
  if (!ref) return null;
  const value = resolveDetail(ref, snapshot);
  return <div className="drawer-backdrop" onClick={close}><aside className="drawer" aria-label={`${ref.label} 详情`} onClick={(event) => event.stopPropagation()}>
    <div className="drawer-actions">{stack.length > 1 ? <button type="button" onClick={back} aria-label="返回上一详情">← 返回</button> : <span />}<button type="button" className="close" onClick={close} aria-label="关闭详情">×</button></div>
    {!value ? <div className="missing-detail"><span className="eyebrow">{ref.kind}</span><h2>{ref.label}</h2><p>该对象已不在最新快照中。</p></div> : ref.kind === "group" ? <GroupDetail group={value as GroupSummary} snapshot={snapshot} open={open} /> : ref.kind === "user" ? <UserDetail user={value as UserSummary} snapshot={snapshot} open={open} /> : ref.kind === "node" ? <NodeDetail node={value as NodeSummary} snapshot={snapshot} open={open} /> : ref.kind === "workload" ? <WorkloadDetail workload={value as Workload} open={open} /> : <AlertDetail alert={value as Alert} snapshot={snapshot} open={open} />}
  </aside></div>;
}
