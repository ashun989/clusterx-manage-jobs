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
    { group: "group-a", status: "compliant", gpu_quota: 16, cpu_quota: 224, memory_quota_gib: 3840, allocated_gpu: 4, allocated_cpu: 56, allocated_memory_gib: 960, members: ["alice"], over_resources: [], policy_findings: [], finding_categories: [], finding_codes: [], finding_tags: [], telemetry: telemetry(4, 50, 1000) },
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
    groups: { "group-a": { gpu_quota: 16, member_count: 1 }, default: { gpu_quota: "remainder", member_count: 0 } },
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
  optimality: "exact", superseded: false, search_elapsed_seconds: 0.2, plans: [
    { strategy: "min-gpu", rank: 1, workloads: ["workload-a"], workload_count: 1, users: 1, groups: 1, gpus: 4, cpus: 56, memory_gib: 960, freed_nodes: ["node-a"], workload_details: [trainA] },
    { strategy: "min-workloads", rank: 1, workloads: ["workload-b"], workload_count: 1, users: 1, groups: 1, gpus: 8, cpus: 112, memory_gib: 1920, freed_nodes: ["node-b"], workload_details: [trainB] },
  ],
};

let latestSnapshot: Snapshot;
let emitSnapshot: (() => void) | undefined;
let adminAuthenticated: boolean;
let adminResourceRevision: string;
let adminResourceText: string;
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
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/admin/session")) return adminAuthenticated ? { ok: true, status: 200, json: async () => ({ authenticated: true, username: "admin", csrf_token: "csrf-token", expires_at: "2026-08-14T12:00:00Z" }) } : { ok: false, status: 401, statusText: "Unauthorized", json: async () => ({ detail: "administrator authentication required" }) };
      if (path.endsWith("/admin/login")) { adminAuthenticated = true; return { ok: true, status: 200, json: async () => ({ authenticated: true, username: "admin", csrf_token: "csrf-token", expires_at: "2026-08-14T12:00:00Z" }) }; }
      if (path.endsWith("/admin/logout")) { adminAuthenticated = false; return { ok: true, status: 200, json: async () => ({ ok: true }) }; }
      if (path.endsWith("/admin/config") && !init?.method) return { ok: true, status: 200, json: async () => ({ configured: true, effective_config_valid: true, resource: { format: "json", text: adminResourceText, revision: adminResourceRevision, parse_error: null }, groups: { format: "yaml", text: adminGroupsText, revision: "group-r1", parse_error: null }, validation_error: null, audit_error: null }) };
      if (path.endsWith("/admin/config/resource") && init?.method === "PUT") { const body = JSON.parse(String(init.body)); adminResourceRevision = "resource-r2"; adminResourceText = body.text; return { ok: true, status: 200, json: async () => ({ configured: true, effective_config_valid: true, resource: { format: "json", text: adminResourceText, revision: adminResourceRevision, parse_error: null }, groups: { format: "yaml", text: adminGroupsText, revision: "group-r1", parse_error: null }, validation_error: null, audit_error: null }) }; }
      if (path.endsWith("/status")) return { ok: true, status: 200, json: async () => ({ service: "ok", snapshot: { available: true, stale: false, age_seconds: 3, last_error: null }, collector: { running: true, skipped_refreshes: 0 }, policy: { valid: true, using_last_known_good: false, error: null, audit_error: null, setup_required: false } }) };
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

  it("filters enum columns, sorts numeric columns and resets missing filter values", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
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
    expect(within(table).getByRole("button", { name: "排序 运行小时" })).toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: "排序 CPU 总量" }));
    expect(within(table).getAllByRole("row").at(-1)).toHaveTextContent("pending-a");
    expect(within(table).getByRole("row", { name: "查看 pending-a 详情" })).toHaveTextContent("—");

    fireEvent.click(within(table).getByRole("row", { name: "查看 pending-a 详情" }));
    const drawer = screen.getByRole("complementary", { name: "pending-a 详情" });
    expect(drawer).toHaveTextContent("申请资源");
    expect(drawer).toHaveTextContent("Task 请求");
    expect(within(drawer).getByRole("cell", { name: "master" })).toBeInTheDocument();
    expect(within(drawer).getByRole("cell", { name: "PYTORCH_WORKER" })).toBeInTheDocument();
    expect(drawer).not.toHaveTextContent("Placements（当前归属）");
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
    const drawer = screen.getByRole("complementary", { name: "train-a 详情" });
    expect(drawer).toHaveTextContent("时间可信度观测");
    expect(drawer).toHaveTextContent("时间来源AIR Available 状态");
    expect(drawer).toHaveTextContent("开始时间");
    expect(drawer).toHaveTextContent("资源创建时间");
  });

  it("opens all entity details, follows related objects, supports back and reports removed entities", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("row", { name: "查看 group-a 详情" }));
    expect(screen.getByRole("complementary", { name: "group-a 详情" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /train-a/ }));
    expect(screen.getByRole("heading", { name: "train-a" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回上一详情" }));
    expect(screen.getByRole("heading", { name: "group-a" })).toBeInTheDocument();

    latestSnapshot = { ...latestSnapshot, groups: latestSnapshot.groups.filter((group) => group.group !== "group-a"), snapshot_id: "snapshot-2" };
    emitSnapshot?.();
    await screen.findByText("该对象已不在最新快照中。");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("complementary", { name: "group-a 详情" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "alerts" }));
    fireEvent.click(screen.getByRole("row", { name: "查看 workload-b 详情" }));
    expect(within(screen.getByRole("complementary", { name: "workload-b 详情" })).getByText("CPU exceeds policy")).toBeInTheDocument();
  });

  it("uses a full-width node workspace with filtering, numeric ordering and node details", async () => {
    latestSnapshot.nodes[0].telemetry = telemetry(123456, 99.999, 123456789);
    const { container } = render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "nodes" }));
    const workspace = container.querySelector(".workspace")!;
    expect(workspace).not.toHaveClass("nodes-workspace");
    expect(workspace.children[0]).toHaveClass("main-panel");
    expect(workspace.children[1]).toHaveClass("scheduler-panel");
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
    expect(screen.getByRole("complementary", { name: "node-a 详情" })).toHaveTextContent("有效空闲 GPU");
  });

  it("auto-expands the first plan, expands alternatives and opens plan workload details", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    const gpuInput = screen.getByLabelText("每节点 GPU") as HTMLInputElement;
    const cpuInput = screen.getByLabelText("CPU（可覆盖）") as HTMLInputElement;
    const memoryInput = screen.getByLabelText("内存 GiB（可覆盖）") as HTMLInputElement;
    expect(cpuInput).toHaveValue(112);
    expect(memoryInput).toHaveValue(1920);
    fireEvent.change(gpuInput, { target: { value: "4" } });
    expect(cpuInput).toHaveValue(56);
    expect(memoryInput).toHaveValue(960);
    fireEvent.change(cpuInput, { target: { value: "42" } });
    fireEvent.change(gpuInput, { target: { value: "2" } });
    expect(cpuInput).toHaveValue(42);
    expect(memoryInput).toHaveValue(480);
    fireEvent.change(screen.getByLabelText("违规分类（逗号分隔）"), { target: { value: "utilization" } });
    fireEvent.change(screen.getByLabelText("规则代码（逗号分隔）"), { target: { value: "utilization.low_gpu_activity" } });
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
    expect(screen.getByRole("complementary", { name: "train-b 详情" })).toBeInTheDocument();
    const planCall = vi.mocked(fetch).mock.calls.find(([input, init]) => String(input).endsWith("/plans") && init?.method === "POST");
    const payload = JSON.parse(String(planCall?.[1]?.body));
    expect(payload.target).toEqual({ nodes: 2, gpus_per_node: 2, cpus_per_node: 42, memory_per_node_gib: 480 });
    expect(payload.filters.violation_categories).toEqual(["utilization"]);
    expect(payload.filters.violation_codes).toEqual(["utilization.low_gpu_activity"]);
  });

  it("filters array-valued finding facets and renders finding details", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    const categorySummary = screen.getByText("违规分类", { selector: "summary" });
    fireEvent.click(categorySummary);
    fireEvent.click(within(categorySummary.closest("details")!).getByRole("checkbox", { name: "quota" }));
    expect(screen.getByText("1/2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("row", { name: "查看 group-b 详情" }));
    expect(screen.getByRole("complementary", { name: "group-b 详情" })).toHaveTextContent("quota.gpu");
    expect(screen.getByRole("complementary", { name: "group-b 详情" })).toHaveTextContent("观测值");
  });

  it("renders dynamic status, rules, effective values and private group counts", async () => {
    render(<App />);
    await screen.findByText("Queue Observatory");
    fireEvent.click(screen.getByRole("button", { name: "规则说明" }));
    expect(screen.getByRole("heading", { name: "规则说明" })).toBeInTheDocument();
    expect(screen.getByText("utilization.low_gpu_activity")).toBeInTheDocument();
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
