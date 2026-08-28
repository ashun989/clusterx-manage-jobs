// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { FreshnessBadge } from "./App";
import type { PlanResult, PolicyFinding, PolicyResponse, Snapshot, Telemetry, Workload } from "./types";

const quotaFinding: PolicyFinding = {
  code: "quota.gpu", category: "quota", status: "violation", message: "group GPU usage exceeds quota",
  tags: ["quota", "gpu"], observed: { gpu: 8 }, limit: { gpu_quota: 4 },
};

const telemetry = (allocated: number, util: number | null, watts: number | null): Telemetry => ({
  allocated_gpu_count: allocated, reported_gpu_count: allocated, gpu_compute_util_avg_pct: util,
  compute_reported_gpu_count: util == null ? 0 : allocated,
  memory_reported_gpu_count: util == null ? 0 : allocated,
  power_reported_gpu_count: watts == null ? 0 : allocated,
  gpu_memory_util_avg_pct: util == null ? null : util + 5, gpu_power_total_w: watts,
  gpu_power_avg_w: watts == null || allocated === 0 ? null : watts / allocated,
});

const workload = (id: string, name: string, user: string, group: string, gpu: number, node: string): Workload => ({
  workload_id: id, workload_name: name, user, group, type: "trainingJob", workspace: "workspace",
  create_time: "2026-08-14T00:00:00Z", resource_create_time: "2026-08-13T23:50:00Z",
  priority: "NORMAL",
  start_time: "2026-08-14T00:10:00Z", runtime_anchor_time: "2026-08-14T00:10:00Z",
  runtime_source: "training_status_start", runtime_quality: "exact", runtime_hours: 2,
  runtime_estimated: false, total_gpu: gpu, total_cpu: gpu * 14, total_memory_gib: gpu * 240,
  resource_basis: "attributed", task_resources: [], policy_status: "compliant", policy_reasons: [],
  policy_findings: [], finding_categories: [], finding_codes: [], finding_tags: [],
  planning_eligible: true, planning_exclusion_reasons: [], planning_excluded_nodes: [],
  placements: [{ node, pod: `${name}-0`, gpu, cpu: gpu * 14, memory_gib: gpu * 240 }],
  gpus: [], telemetry: telemetry(gpu, 60, gpu * 250),
});

const trainA = workload("workload-a", "train-a", "alice", "group-a", 4, "node-a");
const trainB = workload("workload-b", "train-b", "bob", "group-b", 8, "node-b");

const baseSnapshot: Snapshot = {
  snapshot_id: "snapshot-1", generated_at: "2026-08-14T01:00:00Z", cluster: "cluster", queue: "a800",
  capacity: { bound_gpu: 512, allocated_gpu: 12, free_gpu: 500, default_gpu_quota: 504, planning_eligible_gpu: 512 },
  planning_profile: { default_cpu_per_gpu: 14, default_memory_gib_per_gpu: 240 },
  pending_pressure: { state: "inactive", eligible_jobs: 0 },
  telemetry: { ...telemetry(12, 70, 3600), reported_gpu_count: 10 }, telemetry_status: "partial",
  freshness: { stale: false, age_seconds: 3, last_error: null }, warnings: [], alerts: [
    { severity: "error", kind: "workload-policy", subject: "workload-b", message: "CPU exceeds policy", code: "resource.training.cpu_ratio", category: "resource-shape", subject_type: "workload", tags: ["cpu"], finding_categories: ["resource-shape"], finding_codes: ["resource.training.cpu_ratio"], finding_tags: ["cpu"] },
    { severity: "warning", kind: "telemetry", subject: "node-a", message: "partial telemetry", code: "telemetry.partial", category: "telemetry", subject_type: "node", tags: ["partial"], finding_categories: ["telemetry"], finding_codes: ["telemetry.partial"], finding_tags: ["partial"] },
  ],
  groups: [
    { group: "group-a", status: "compliant", gpu_quota: 16, cpu_quota: null, memory_quota_gib: null, allocated_gpu: 4, allocated_cpu: 56, allocated_memory_gib: 960, members: ["alice"], over_resources: [], policy_findings: [], finding_categories: [], finding_codes: [], finding_tags: [], telemetry: telemetry(4, 50, 1000) },
    { group: "group-b", status: "violation", gpu_quota: 4, cpu_quota: 56, memory_quota_gib: 960, allocated_gpu: 8, allocated_cpu: 112, allocated_memory_gib: 1920, members: ["bob"], over_resources: ["gpu", "cpu", "memory"], policy_findings: [quotaFinding], finding_categories: ["quota"], finding_codes: ["quota.gpu"], finding_tags: ["quota", "gpu"], telemetry: telemetry(8, 80, 2600) },
  ],
  users: [
    { user: "alice", group: "group-a", workload_count: 1, development_instance_count: 0, allocated_gpu: 4, allocated_cpu: 56, allocated_memory_gib: 960, status: "compliant", policy_findings: [], finding_categories: [], finding_codes: [], finding_tags: [], telemetry: telemetry(4, 50, 1000) },
    { user: "bob", group: "group-b", workload_count: 1, development_instance_count: 0, allocated_gpu: 8, allocated_cpu: 112, allocated_memory_gib: 1920, status: "violation", policy_findings: [quotaFinding], finding_categories: ["quota"], finding_codes: ["quota.gpu"], finding_tags: ["quota", "gpu"], telemetry: telemetry(8, 80, 2600) },
  ],
  nodes: [
    { node: "node-a", id: "node-id-a", host_ip: "10.0.0.1", state: "RUNNING", allocated_gpu: 4, total_gpu: 8, allocated_cpu: 56, total_cpu: 112, allocated_memory_gib: 960, total_memory_gib: 1920, workloads: { "workload-a": { gpu: 4, cpu: 56, memory_gib: 960 } }, unattributed: { gpu: 0, cpu: 0, memory_gib: 0 }, attribution_excess: { gpu: 0, cpu: 0, memory_gib: 0 }, planning_eligible: true, planning_exclusion_reasons: [], free_gpu: 4, effective_free_gpu: 4, stranded_gpu: 0, classification: "fragmented", telemetry: telemetry(4, 50, 1000) },
    { node: "node-b", id: "node-id-b", host_ip: "10.0.0.2", state: "RUNNING", allocated_gpu: 8, total_gpu: 8, allocated_cpu: 112, total_cpu: 112, allocated_memory_gib: 1920, total_memory_gib: 1920, workloads: { "workload-b": { gpu: 8, cpu: 112, memory_gib: 1920 } }, unattributed: { gpu: 0, cpu: 0, memory_gib: 0 }, attribution_excess: { gpu: 0, cpu: 0, memory_gib: 0 }, planning_eligible: true, planning_exclusion_reasons: [], free_gpu: 0, effective_free_gpu: 0, stranded_gpu: 0, classification: "gpu-full", telemetry: telemetry(8, 80, 2600) },
  ],
  workloads: [trainA, trainB], pending_workloads: [],
};

const policyResponse: PolicyResponse = {
  valid: true, using_last_known_good: false, error: null,
  status_definitions: { compliant: { description: "within limits", propagation: "no escalation" }, violation: { description: "rule violated", propagation: "workload to user" }, burst: "over quota without pressure", unknown: "insufficient data", pending: "queued" },
  rule_catalog: [{ code: "utilization.low_gpu_activity", category: "utilization", applies_to: "GPU workload", title: "Historical low GPU activity", description: "Both metrics are at or below thresholds." }],
  evaluation_behavior: { historical_scope: "Only current running GPU workloads are evaluated." },
  policy: {
    refresh_seconds: 30, telemetry_lookback_minutes: 5,
    pending_pressure: { min_wait_minutes: 10, min_jobs: 1 },
    development: { zero_gpu_max_cpu_per_node: 8, zero_gpu_max_memory_gib_per_node: 140, one_gpu_max_cpu_per_node: 14, one_gpu_max_memory_gib_per_node: 240, max_gpu: 1, max_instances_per_user: 1, one_gpu_max_runtime_hours: 72 },
    training: { cpu_per_gpu: 14, memory_gib_per_gpu: 240, zero_gpu_max_cpu_per_node: 14, zero_gpu_max_memory_gib_per_node: 240 },
    planning: { default_cpu_per_gpu: 14, default_memory_gib_per_gpu: 240 },
    low_utilization: { window_hours: 24, refresh_minutes: 5, min_observation_minutes: 60, gpu_compute_threshold_pct: 20, gpu_memory_threshold_pct: 20 },
    groups: {
      "group-a": { gpu_quota: 16, cpu_quota: null, memory_quota_gib: null, member_count: 1 },
      default: { gpu_quota: "remainder", cpu_quota: null, memory_quota_gib: null, member_count: 0 },
    },
  },
};
const adminResource = structuredClone(policyResponse.policy!) as Record<string, unknown>;
delete adminResource.groups;

const planResult: PlanResult = {
  snapshot_id: "snapshot-1", snapshot_generated_at: "2026-08-14T01:00:00Z", computed_at: "2026-08-14T01:01:00Z",
  requested_target: { nodes: 1, gpus_per_node: 8, cpus_per_node: null, memory_per_node_gib: null },
  resolved_target: { nodes: 1, gpus_per_node: 8, cpus_per_node: 112, memory_per_node_gib: 1920 },
  defaults_applied: ["cpus_per_node", "memory_per_node_gib"],
  planning_profile: { default_cpu_per_gpu: 14, default_memory_gib_per_gpu: 240 },
  planning_exclusions: { node_count: 0, workload_count: 0, reasons: [] },
  no_plan_reason: null,
  solver: { backend: "cp-sat", model_version: 2, status: "OPTIMAL", time_limit_seconds: 10, wall_time_seconds: 0.2, candidate_node_count: 2, candidate_workload_count: 2 },
  strategy_results: [
    { strategy: "min-gpu", status: "OPTIMAL", termination_reason: "alternatives-complete", top_k_complete: true, requested_alternatives: 1, returned_alternatives: 1, wall_time_seconds: 0.1, deterministic_time_seconds: 0.01, branches: 2, conflicts: 0, plans: [] },
    { strategy: "min-workloads", status: "OPTIMAL", termination_reason: "alternatives-complete", top_k_complete: true, requested_alternatives: 1, returned_alternatives: 1, wall_time_seconds: 0.1, deterministic_time_seconds: 0.01, branches: 2, conflicts: 0, plans: [] },
  ],
  optimality: "exact", superseded: false, search_elapsed_seconds: 0.2, plans: [
    { strategy: "min-gpu", rank: 1, rank_status: "OPTIMAL", rank_backend: "cp-sat", objective_value: 1, best_objective_bound: 1, workloads: ["workload-a"], workload_count: 1, users: 1, groups: 1, gpus: 4, cpus: 56, memory_gib: 960, freed_nodes: ["node-a"], target_nodes: ["node-a"], newly_schedulable_nodes: ["node-a"], workload_details: [trainA] },
    { strategy: "min-workloads", rank: 1, rank_status: "OPTIMAL", rank_backend: "cp-sat", objective_value: 1, best_objective_bound: 1, workloads: ["workload-b"], workload_count: 1, users: 1, groups: 1, gpus: 8, cpus: 112, memory_gib: 1920, freed_nodes: ["node-b"], target_nodes: ["node-b"], newly_schedulable_nodes: ["node-b"], workload_details: [trainB] },
  ],
};

let latestSnapshot: Snapshot;
let emitSnapshot: (() => void) | undefined;
let adminAuthenticated: boolean;
let adminResourceRevision: string;
let adminResourceText: string;
let logContent: string;
let logError: string;
const adminGroupsText = "schema_version: 1\ngroups:\n  group-a:\n    gpu_quota: 16\n    members: [alice]\n  default:\n    gpu_quota: remainder\n    members: []\n";

class FakeEventSource {
  addEventListener(name: string, callback: EventListener) { if (name === "snapshot") emitSnapshot = () => callback(new Event("snapshot")); }
  close() {}
  set onerror(_: ((event: Event) => void) | null) {}
}

describe("Clusterx monitor dashboard", () => {
  beforeEach(() => {
    latestSnapshot = structuredClone(baseSnapshot);
    emitSnapshot = undefined;
    adminAuthenticated = false;
    adminResourceRevision = "resource-r1";
    adminResourceText = JSON.stringify(adminResource, null, 2) + "\n";
    logContent = "first log line\nsecond log line";
    logError = "";
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/admin/session")) return adminAuthenticated ? { ok: true, status: 200, json: async () => ({ authenticated: true, username: "admin", csrf_token: "csrf-token", expires_at: "2026-08-14T12:00:00Z" }) } : { ok: false, status: 401, statusText: "Unauthorized", json: async () => ({ detail: "administrator authentication required" }) };
      if (path.endsWith("/admin/login")) { adminAuthenticated = true; return { ok: true, status: 200, json: async () => ({ authenticated: true, username: "admin", csrf_token: "csrf-token", expires_at: "2026-08-14T12:00:00Z" }) }; }
      if (path.endsWith("/admin/logout")) { adminAuthenticated = false; return { ok: true, status: 200, json: async () => ({ ok: true }) }; }
      if (path.endsWith("/admin/config") && !init?.method) return { ok: true, status: 200, json: async () => ({ configured: true, effective_config_valid: true, resource: { format: "json", text: adminResourceText, revision: adminResourceRevision, parse_error: null }, groups: { format: "yaml", text: adminGroupsText, revision: "group-r1", parse_error: null }, validation_error: null, audit_error: null }) };
      if (path.endsWith("/admin/config/resource") && init?.method === "PUT") { const body = JSON.parse(String(init.body)); adminResourceRevision = "resource-r2"; adminResourceText = body.text; return { ok: true, status: 200, json: async () => ({ configured: true, effective_config_valid: true, resource: { format: "json", text: adminResourceText, revision: adminResourceRevision, parse_error: null }, groups: { format: "yaml", text: adminGroupsText, revision: "group-r1", parse_error: null }, validation_error: null, audit_error: null }) }; }
      if (path.includes("/workloads/") && path.includes("/logs?")) {
        if (logError) return { ok: false, status: 502, statusText: "Bad Gateway", json: async () => ({ detail: logError }) };
        const url = new URL(path, "http://monitor.test");
        return { ok: true, status: 200, json: async () => ({ snapshot_id: url.searchParams.get("snapshot_id"), workload_id: "workload-a", worker: url.searchParams.get("worker"), lines: 200, content: logContent }) };
      }
      if (path.endsWith("/status")) return { ok: true, status: 200, json: async () => ({ service: "clusterx-monitor", version: "0.5.0", snapshot: { available: true, stale: false, age_seconds: 3, last_error: null }, collector: { running: true, skipped_refreshes: 0 }, policy: { valid: true, using_last_known_good: false, error: null, audit_error: null, setup_required: false } }) };
      if (path.includes("/history?")) return { ok: true, status: 200, json: async () => ({ retained_snapshots: 2, history_capacity: 2880, window_started_at: "2026-08-14T00:59:30Z", newest_at: "2026-08-14T01:00:00Z", points: [
        { snapshot_id: "snapshot-0", generated_at: "2026-08-14T00:59:30Z", bound_gpu: 512, planning_eligible_gpu: 512, allocated_gpu: 10, free_gpu: 502, pending_workloads: 1, pending_eligible_jobs: 0, alert_count: 1, critical_alert_count: 0, gpu_compute_util_avg_pct: 65, gpu_memory_util_avg_pct: 60, gpu_power_total_w: 3200, node_classifications: { fragmented: 1, "gpu-full": 1 } },
        { snapshot_id: "snapshot-1", generated_at: "2026-08-14T01:00:00Z", bound_gpu: 512, planning_eligible_gpu: 512, allocated_gpu: 12, free_gpu: 500, pending_workloads: 0, pending_eligible_jobs: 0, alert_count: 2, critical_alert_count: 1, gpu_compute_util_avg_pct: 70, gpu_memory_util_avg_pct: 65, gpu_power_total_w: 3600, node_classifications: { fragmented: 1, "gpu-full": 1 } },
      ] }) };
      const value = path.endsWith("/plans") && init?.method === "POST" ? planResult : path.endsWith("/policy") ? policyResponse : latestSnapshot;
      return { ok: true, status: 200, json: async () => structuredClone(value) };
    }));
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("updates the snapshot age every second without waiting for another snapshot", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-14T01:00:00Z"));
    render(<FreshnessBadge snapshotId="snapshot-1" freshness={{ stale: false, age_seconds: 3, last_error: null }} />);
    expect(screen.getByText("3s 前更新")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(2_000));

    expect(screen.getByText("5s 前更新")).toBeInTheDocument();
  });

  it("shows the current release notes from the version badge", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");

    fireEvent.click(screen.getByLabelText("查看 v0.5.0 更新内容"));

    expect(screen.getByText("本版更新")).toBeInTheDocument();
    expect(screen.getByText(/集群运行总览/)).toBeInTheDocument();
    expect(screen.getByText(/全局搜索/)).toBeInTheDocument();
    expect(screen.getByText(/明暗主题/)).toBeInTheDocument();
  });

  it("provides an operational overview and global entity search", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "overview" }));
    expect(screen.getByRole("heading", { name: "集群运行总览" })).toBeInTheDocument();
    expect(screen.getByText("较上一快照 +2")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /已分配 GPU趋势/ })).toBeInTheDocument();
    expect(screen.getByText("节点健康")).toBeInTheDocument();

    const search = screen.getByLabelText("全局搜索");
    fireEvent.focus(search);
    fireEvent.change(search, { target: { value: "node-b" } });
    fireEvent.click(await screen.findByRole("option", { name: /node-b/ }));
    expect(screen.getByRole("dialog", { name: "node-b 详情" })).toBeInTheDocument();
  });

  it("searches table text and lets operators hide columns", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.change(screen.getByLabelText("搜索当前表格"), { target: { value: "group-b" } });
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: "查看 group-a 详情" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("列", { selector: "summary" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "CPU quota" }));
    expect(screen.queryByRole("columnheader", { name: "CPU quota" })).not.toBeInTheDocument();
  });

  it("filters enum columns, sorts numeric columns and resets missing filter values", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    expect(screen.getByText("v0.5.0")).toBeInTheDocument();
    const table = screen.getByRole("table");
    const gpuSort = within(table).getByRole("button", { name: "排序 GPU" });
    fireEvent.click(gpuSort);
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("group-a");
    fireEvent.click(gpuSort);
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("group-b");

    fireEvent.click(screen.getByText("状态", { selector: "summary" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "violation" }));
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(within(table).queryByText("group-a")).not.toBeInTheDocument();

    latestSnapshot = { ...latestSnapshot, groups: latestSnapshot.groups.filter((group) => group.status === "compliant"), snapshot_id: "snapshot-2" };
    emitSnapshot?.();
    await waitFor(() => expect(screen.getByText("1/1")).toBeInTheDocument());
    expect(within(table).getByText("group-a")).toBeInTheDocument();
  });

  it("shows sortable workload totals and pending task request details", async () => {
    const pending: Workload = {
      ...workload("pending-a", "pending-a", "charlie", "default", 3, "unused"),
      policy_status: "pending", placements: [], gpus: [], telemetry: telemetry(0, null, null),
      total_gpu: 3, total_cpu: null, total_memory_gib: 700, resource_basis: "requested",
      priority: "HIGHEST", resource_create_time: "2026-08-14T00:30:00Z", queue_age_seconds: 1800,
      task_resources: [
        { name: "master", role: "PYTORCH_MASTER", replicas: 1, gpu_per_replica: 1, cpu_per_replica: null, memory_gib_per_replica: 100 },
        { name: "worker", role: "PYTORCH_WORKER", replicas: 1, gpu_per_replica: 2, cpu_per_replica: null, memory_gib_per_replica: 600 },
      ],
    };
    latestSnapshot = { ...latestSnapshot, pending_workloads: [pending] };
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "workloads" }));
    const table = screen.getByRole("table");
    expect(within(table).getByRole("button", { name: "排序 GPU 总量" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "排序 CPU 总量" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "排序 内存 GiB" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "排序 资源创建时间" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "排序 运行小时" })).toBeInTheDocument();
    expect(screen.getByText("优先级", { selector: "summary" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "HIGHEST" })).toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: "排序 CPU 总量" }));
    expect(within(table).getAllByRole("row").at(-1)).toHaveTextContent("pending-a");
    const pendingRow = within(table).getByRole("row", { name: "查看 pending-a 详情" });
    expect(pendingRow).toHaveTextContent("HIGHEST");
    expect(pendingRow).toHaveTextContent(new Date("2026-08-14T00:30:00Z").toLocaleString());
    expect(pendingRow).toHaveTextContent("—");

    fireEvent.click(pendingRow);
    const drawer = screen.getByRole("dialog", { name: "pending-a 详情" });
    expect(drawer).toHaveTextContent("申请资源");
    expect(drawer).toHaveTextContent("优先级");
    expect(drawer).toHaveTextContent("HIGHEST");
    expect(drawer).toHaveTextContent("运行开始时间");
    expect(drawer).toHaveTextContent("资源创建时间");
    expect(drawer).toHaveTextContent("已排队时长30 分钟");
    expect(drawer).toHaveTextContent("Pending 时以资源创建时间计算初始排队时长；重试或重新进入 Pending 不会重置。");
    expect(drawer).not.toHaveTextContent("排队开始时间（资源创建）");
    expect(drawer).toHaveTextContent(new Date("2026-08-14T00:30:00Z").toLocaleString());
    expect(drawer).toHaveTextContent("Task 请求");
    expect(within(drawer).getByRole("cell", { name: "master" })).toBeInTheDocument();
    expect(within(drawer).getByRole("cell", { name: "PYTORCH_WORKER" })).toBeInTheDocument();
    expect(drawer).not.toHaveTextContent("Placements（当前归属）");
  });

  it("loads logs only after an explicit Worker choice and keeps long details scrollable", async () => {
    logContent = Array.from({ length: 45 }, (_, index) => `log line ${index + 1}`).join("\n");
    latestSnapshot.workloads[0].placements.push({
      node: "node-b", pod: "train-a-1", gpu: 0, cpu: 1, memory_gib: 1,
    });
    latestSnapshot.workloads[0].gpus = [
      { node: "node-a", pod: "train-a-0", device_index: "0", gpu_compute_util_pct: 50 },
    ];
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "workloads" }));
    fireEvent.click(screen.getByRole("row", { name: "查看 train-a 详情" }));
    const drawer = screen.getByRole("dialog", { name: "train-a 详情" });
    const logCalls = () => vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/logs?"));
    expect(logCalls()).toHaveLength(0);
    expect(within(drawer).getByText("打开详情不会抓取日志；手动加载最近 200 行后在浏览器内分页。")).toBeInTheDocument();
    expect(within(drawer).getByLabelText("每页日志行数")).toHaveValue("20");
    const load = within(drawer).getByRole("button", { name: "加载日志" });
    expect(load).toBeDisabled();

    fireEvent.change(within(drawer).getByLabelText("日志 Worker"), { target: { value: "train-a-1" } });
    expect(logCalls()).toHaveLength(0);
    fireEvent.click(load);
    await within(drawer).findByText("45 行 · 第 3/3 页");
    expect(logCalls()).toHaveLength(1);
    expect(String(logCalls()[0][0])).toContain("snapshot_id=snapshot-1");
    expect(String(logCalls()[0][0])).toContain("worker=train-a-1");
    expect(String(logCalls()[0][0])).toContain("lines=200");
    expect(within(drawer).getByText(/log line 41/)).toBeInTheDocument();
    expect(within(drawer).queryByText(/log line 40/)).not.toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("button", { name: "日志上一页" }));
    expect(within(drawer).getByLabelText("日志页码")).toHaveValue("2");
    expect(within(drawer).getByText(/log line 21/)).toBeInTheDocument();
    expect(within(drawer).getByText(/log line 40/)).toBeInTheDocument();
    expect(logCalls()).toHaveLength(1);

    fireEvent.change(within(drawer).getByLabelText("每页日志行数"), { target: { value: "50" } });
    expect(within(drawer).getByText("45 行 · 第 1/1 页")).toBeInTheDocument();
    const panels = drawer.querySelectorAll("pre.detail-scroll-panel");
    expect(panels).toHaveLength(2);
    expect(panels[0]).toHaveClass("workload-log");
  });

  it("keeps log controls and page across snapshot and manual refreshes", async () => {
    logContent = Array.from({ length: 60 }, (_, index) => `initial ${index + 1}`).join("\n");
    latestSnapshot.workloads[0].placements.push({
      node: "node-b", pod: "train-a-1", gpu: 0, cpu: 1, memory_gib: 1,
    });
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "workloads" }));
    fireEvent.click(screen.getByRole("row", { name: "查看 train-a 详情" }));
    const drawer = screen.getByRole("dialog", { name: "train-a 详情" });
    const logCalls = () => vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/logs?"));
    fireEvent.change(within(drawer).getByLabelText("日志 Worker"), { target: { value: "train-a-1" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "加载日志" }));
    await within(drawer).findByText("60 行 · 第 3/3 页");
    fireEvent.click(within(drawer).getByRole("button", { name: "日志上一页" }));
    expect(within(drawer).getByLabelText("日志页码")).toHaveValue("2");
    fireEvent.change(within(drawer).getByLabelText("每页日志行数"), { target: { value: "50" } });
    expect(within(drawer).getByText("60 行 · 第 2/2 页")).toBeInTheDocument();

    latestSnapshot = { ...latestSnapshot, snapshot_id: "snapshot-2" };
    emitSnapshot?.();
    await waitFor(() => expect(within(drawer).getByLabelText("日志页码")).toHaveValue("2"));
    expect(within(drawer).getByLabelText("日志 Worker")).toHaveValue("train-a-1");
    expect(within(drawer).getByLabelText("每页日志行数")).toHaveValue("50");
    expect(logCalls()).toHaveLength(1);
    fireEvent.change(within(drawer).getByLabelText("每页日志行数"), { target: { value: "20" } });
    expect(within(drawer).getByText("60 行 · 第 2/3 页")).toBeInTheDocument();

    logContent = Array.from({ length: 80 }, (_, index) => `refreshed ${index + 1}`).join("\n");
    fireEvent.click(within(drawer).getByRole("button", { name: "刷新日志" }));
    await within(drawer).findByText("80 行 · 第 2/4 页");
    expect(String(logCalls()[1][0])).toContain("snapshot_id=snapshot-2");
    expect(within(drawer).getByText(/refreshed 21/)).toBeInTheDocument();

    fireEvent.change(within(drawer).getByLabelText("日志页码"), { target: { value: "4" } });
    logContent = Array.from({ length: 25 }, (_, index) => `short ${index + 1}`).join("\n");
    fireEvent.click(within(drawer).getByRole("button", { name: "刷新日志" }));
    await within(drawer).findByText("25 行 · 第 2/2 页");

    logError = "log refresh failed";
    fireEvent.click(within(drawer).getByRole("button", { name: "刷新日志" }));
    await within(drawer).findByText("log refresh failed");
    expect(within(drawer).getByText("25 行 · 第 2/2 页")).toBeInTheDocument();
    expect(within(drawer).getByText(/short 21/)).toBeInTheDocument();

    logError = "";
    latestSnapshot = {
      ...latestSnapshot,
      snapshot_id: "snapshot-3",
      workloads: latestSnapshot.workloads.map((item) => item.workload_id === "workload-a" ? {
        ...item, placements: item.placements.filter((placement) => placement.pod !== "train-a-1"),
      } : item),
    };
    emitSnapshot?.();
    await waitFor(() => expect(within(drawer).getByText("Worker: train-a-0")).toBeInTheDocument());
    await waitFor(() => expect(drawer.querySelector("pre.workload-log")).not.toBeInTheDocument());
    expect(logCalls()).toHaveLength(4);
  });

  it("shows the active development instance count in the user table", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "users" }));
    expect(within(screen.getByRole("table")).getByRole("button", { name: "排序 开发机数" })).toBeInTheDocument();
  });

  it("marks runtime quality in the list and shows lifecycle provenance in details", async () => {
    latestSnapshot.workloads[0].runtime_quality = "observed";
    latestSnapshot.workloads[0].runtime_source = "air_available_condition";
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "workloads" }));
    const table = screen.getByRole("table");
    expect(within(table).getByRole("row", { name: "查看 train-a 详情" })).toHaveTextContent("2（观测）");

    fireEvent.click(within(table).getByRole("row", { name: "查看 train-a 详情" }));
    const drawer = screen.getByRole("dialog", { name: "train-a 详情" });
    expect(drawer).toHaveTextContent("时间可信度观测");
    expect(drawer).toHaveTextContent("时间来源AIR Available 状态");
    expect(drawer).toHaveTextContent("运行开始时间");
    expect(drawer).toHaveTextContent("资源创建时间");
  });

  it("links every supported workload type to its official console detail", async () => {
    const urls = {
      trainingJob: "https://console.d.pjlab.org.cn/cn-pj-03/ssp/model/training/detail/?rid=%2Fsubscriptions%2Fs%2FresourceGroups%2Fdefault%2Fregions%2Fcn-pj-03%2Fworkspaces%2Fw%2FtrainingJobs%2Ftrain-a",
      aid: "https://console.d.pjlab.org.cn/cn-pj-03/ssp/model/development/detail?rid=%2Fsubscriptions%2Fs%2FresourceGroups%2Fdefault%2Fregions%2Fcn-pj-03%2Fworkspaces%2Fw%2Faids%2Fdev-a",
      air: "https://console.d.pjlab.org.cn/cn-pj-03/ssp/model/air/detail/?rid=%2Fsubscriptions%2Fs%2FresourceGroups%2Fdefault%2Fregions%2Fcn-pj-03%2Fworkspaces%2Fw%2Fairs%2Finfer-a",
    };
    latestSnapshot.workloads = [
      { ...trainA, console_url: urls.trainingJob },
      { ...trainB, workload_id: "aid-a", workload_name: "dev-a", type: "aid", console_url: urls.aid },
      { ...trainB, workload_id: "air-a", workload_name: "infer-a", type: "air", console_url: urls.air },
      { ...trainB, workload_id: "unknown-a", workload_name: "unknown-a", type: "unknown", console_url: undefined },
    ];
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "workloads" }));

    for (const [name, url] of [["train-a", urls.trainingJob], ["dev-a", urls.aid], ["infer-a", urls.air]]) {
      fireEvent.click(screen.getByRole("row", { name: `查看 ${name} 详情` }));
      const link = within(screen.getByRole("dialog", { name: `${name} 详情` })).getByRole("link", { name: "在官方控制台查看" });
      expect(link).toHaveAttribute("href", url);
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
      fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
    }

    fireEvent.click(screen.getByRole("row", { name: "查看 unknown-a 详情" }));
    expect(within(screen.getByRole("dialog", { name: "unknown-a 详情" })).queryByRole("link", { name: "在官方控制台查看" })).not.toBeInTheDocument();
  });

  it("opens all entity details, follows related objects, supports back and reports removed entities", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("row", { name: "查看 group-a 详情" }));
    const groupDrawer = screen.getByRole("dialog", { name: "group-a 详情" });
    expect(groupDrawer).toBeInTheDocument();
    expect(groupDrawer).toHaveTextContent("56 / 不限");
    expect(groupDrawer).toHaveTextContent("960 / 不限");
    fireEvent.click(screen.getByRole("button", { name: /train-a/ }));
    expect(screen.getByRole("heading", { name: "train-a" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回上一详情" }));
    expect(screen.getByRole("heading", { name: "group-a" })).toBeInTheDocument();

    latestSnapshot = { ...latestSnapshot, groups: latestSnapshot.groups.filter((group) => group.group !== "group-a"), snapshot_id: "snapshot-2" };
    emitSnapshot?.();
    await screen.findByText("该对象已不在最新快照中。");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "group-a 详情" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "alerts" }));
    fireEvent.click(screen.getByRole("row", { name: "查看 workload-b 详情" }));
    expect(within(screen.getByRole("dialog", { name: "workload-b 详情" })).getByText("CPU exceeds policy")).toBeInTheDocument();
  });

  it("uses a full-width node workspace with filtering, numeric ordering and node details", async () => {
    latestSnapshot.nodes[0].telemetry = telemetry(123456, 99.999, 123456789);
    latestSnapshot.nodes[0].planning_eligible = false;
    latestSnapshot.nodes[0].planning_exclusion_reasons = undefined;
    const { container } = render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "nodes" }));
    const workspace = container.querySelector(".workspace")!;
    expect(workspace).not.toHaveClass("nodes-workspace");
    expect(workspace.children[0]).toHaveClass("main-panel");
    expect(workspace.children).toHaveLength(1);
    const nodeCard = screen.getByRole("button", { name: "查看 node-a 详情" });
    expect(nodeCard).toBeInTheDocument();
    const telemetryCell = nodeCard.querySelector<HTMLElement>(".telemetry-cell")!;
    const telemetryItems = telemetryCell.querySelectorAll(":scope > span");
    expect(telemetryCell).toHaveTextContent("99.999% util");
    expect(telemetryCell).toHaveTextContent("123456/123456");
    expect(telemetryCell).toHaveTextContent("123456.8 kW");
    expect(telemetryItems).toHaveLength(3);
    expect(telemetryCell.querySelectorAll("small")).toHaveLength(3);

    fireEvent.click(screen.getByText("负载状态", { selector: "summary" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "fragmented" }));
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看 node-b 详情" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重置" }));
    fireEvent.change(screen.getByRole("combobox", { name: "节点排序字段" }), { target: { value: "allocated_gpu" } });
    const cards = container.querySelectorAll(".node-tile");
    expect(cards[0]).toHaveTextContent("node-b");

    fireEvent.click(screen.getByRole("button", { name: "查看 node-a 详情" }));
    expect(screen.getByRole("dialog", { name: "node-a 详情" })).toHaveTextContent("有效空闲 GPU");
    expect(screen.getByRole("dialog", { name: "node-a 详情" })).toHaveTextContent("不参与调度模拟");
  });

  it("auto-expands the first plan, expands alternatives and opens plan workload details", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "planner" }));
    const nodeInput = screen.getByLabelText("节点数") as HTMLInputElement;
    const gpuInput = screen.getByLabelText("每节点 GPU") as HTMLInputElement;
    const cpuInput = screen.getByLabelText("CPU 总量（可覆盖）") as HTMLInputElement;
    const memoryInput = screen.getByLabelText("内存总量 GiB（可覆盖）") as HTMLInputElement;
    expect(cpuInput).toHaveValue(224);
    expect(memoryInput).toHaveValue(3840);
    fireEvent.change(gpuInput, { target: { value: "4" } });
    expect(cpuInput).toHaveValue(112);
    expect(memoryInput).toHaveValue(1920);
    fireEvent.change(nodeInput, { target: { value: "3" } });
    expect(cpuInput).toHaveValue(168);
    expect(memoryInput).toHaveValue(2880);
    fireEvent.change(cpuInput, { target: { value: "150" } });
    fireEvent.change(memoryInput, { target: { value: "1000" } });
    fireEvent.change(gpuInput, { target: { value: "2" } });
    fireEvent.change(nodeInput, { target: { value: "4" } });
    expect(cpuInput).toHaveValue(150);
    expect(memoryInput).toHaveValue(1000);
    fireEvent.click(screen.getByRole("button", { name: "CPU 恢复默认比例" }));
    expect(cpuInput).toHaveValue(112);
    fireEvent.change(gpuInput, { target: { value: "4" } });
    expect(cpuInput).toHaveValue(224);
    expect(memoryInput).toHaveValue(1000);
    fireEvent.click(screen.getByRole("button", { name: "内存恢复默认比例" }));
    expect(memoryInput).toHaveValue(3840);
    fireEvent.click(screen.getByText("类型", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("类型：trainingJob"));
    fireEvent.click(screen.getByText("分组", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("分组：group-a"));
    fireEvent.click(screen.getByText("用户", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("用户：alice"));
    fireEvent.click(screen.getByText("指定 Workload", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("指定 Workload：train-a"));
    fireEvent.click(screen.getByText("排除用户", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("排除用户：bob"));
    fireEvent.click(screen.getByText("违规分类", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("违规分类：quota"));
    fireEvent.click(screen.getByText("规则代码", { selector: ".planner-multi > summary > span" }));
    fireEvent.click(screen.getByLabelText("规则代码：quota.gpu"));
    fireEvent.click(screen.getByRole("button", { name: "计算方案" }));
    const first = await screen.findByRole("button", { name: /min-gpu #1/ });
    const second = screen.getByRole("button", { name: /min-workloads #1/ });
    await waitFor(() => expect(first).toHaveAttribute("aria-expanded", "true"));
    expect(second).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("row", { name: "查看 train-a 详情" })).toBeInTheDocument();
    const planTable = screen.getByRole("row", { name: "查看 train-a 详情" }).closest("table")!;
    expect(within(planTable).getByRole("columnheader", { name: "CPU" })).toBeInTheDocument();
    expect(within(planTable).getByRole("columnheader", { name: "内存 GiB" })).toBeInTheDocument();
    expect(within(planTable).getByRole("row", { name: "查看 train-a 详情" })).toHaveTextContent("56");
    expect(within(planTable).getByRole("row", { name: "查看 train-a 详情" })).toHaveTextContent("960");
    fireEvent.click(second);
    expect(second).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("row", { name: "查看 train-b 详情" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("row", { name: "查看 train-b 详情" }));
    expect(screen.getByRole("dialog", { name: "train-b 详情" })).toBeInTheDocument();
    const planCall = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).endsWith("/plans") && init?.method === "POST");
    const payload = JSON.parse(String(planCall?.[1]?.body));
    expect(payload.target).toEqual({ nodes: 4, gpus_per_node: 4, cpus_per_node: 56, memory_per_node_gib: 960 });
    expect(payload.filters.workload_types).toEqual(["trainingJob"]);
    expect(payload.filters.groups).toEqual(["group-a"]);
    expect(payload.filters.users).toEqual(["alice"]);
    expect(payload.filters.workloads).toEqual(["workload-a"]);
    expect(payload.filters.exclude_users).toEqual(["bob"]);
    expect(payload.filters.violation_categories).toEqual(["quota"]);
    expect(payload.filters.violation_codes).toEqual(["quota.gpu"]);
  });

  it("filters array-valued finding facets and renders finding details", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    const categorySummary = screen.getByText("违规分类", { selector: "summary" });
    fireEvent.click(categorySummary);
    fireEvent.click(within(categorySummary.closest("details")!).getByRole("checkbox", { name: "quota" }));
    expect(screen.getByText("1/2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("row", { name: "查看 group-b 详情" }));
    expect(screen.getByRole("dialog", { name: "group-b 详情" })).toHaveTextContent("quota.gpu");
    expect(screen.getByRole("dialog", { name: "group-b 详情" })).toHaveTextContent("观测值");
  });

  it("renders dynamic status, rules, effective values and private group counts", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "rules" }));
    expect(screen.getByRole("heading", { name: "规则说明" })).toBeInTheDocument();
    expect(screen.getByText("utilization.low_gpu_activity")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "CPU quota" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "内存 quota GiB" })).toBeInTheDocument();
    expect(screen.getByText("workload to user")).toBeInTheDocument();
    expect(screen.getByText("window hours")).toBeInTheDocument();
    expect(screen.getByText(/remainder（当前 504）/)).toBeInTheDocument();
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });

  it("authenticates in the admin drawer and saves configuration with CSRF and revision", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "管理员配置" }));
    const username = await screen.findByLabelText("管理员用户名");
    fireEvent.change(username, { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "a-strong-test-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    const editor = await screen.findByLabelText("资源策略 JSON") as HTMLTextAreaElement;
    const resource = JSON.parse(editor.value);
    resource.refresh_seconds = 31;
    fireEvent.change(editor, { target: { value: JSON.stringify(resource, null, 2) } });
    fireEvent.click(screen.getByRole("button", { name: "校验并保存资源策略" }));
    await screen.findByText("资源策略已校验并写入本地配置。");
    const request = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).endsWith("/admin/config/resource") && init?.method === "PUT");
    expect(request?.[1]?.headers).toMatchObject({ "X-CSRF-Token": "csrf-token" });
    expect(JSON.parse(String(request?.[1]?.body)).revision).toBe("resource-r1");
    expect(screen.queryByLabelText("密码")).not.toBeInTheDocument();
  });
});
