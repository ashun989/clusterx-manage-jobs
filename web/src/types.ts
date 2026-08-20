export type Telemetry = {
  allocated_gpu_count: number;
  reported_gpu_count: number;
  compute_reported_gpu_count: number;
  memory_reported_gpu_count: number;
  power_reported_gpu_count: number;
  gpu_compute_util_avg_pct: number | null;
  gpu_memory_util_avg_pct: number | null;
  gpu_power_total_w: number | null;
  gpu_power_avg_w: number | null;
};

export type ResourceUsage = {
  gpu: number;
  cpu: number;
  memory_gib: number;
};

export type Placement = {
  gpu: number;
  cpu: number | null;
  memory_gib: number | null;
  node: string;
  pod?: string;
};

export type TaskResource = {
  name: string;
  role: string;
  replicas: number;
  gpu_per_replica: number;
  cpu_per_replica: number | null;
  memory_gib_per_replica: number | null;
};

export type PolicyFinding = {
  code: string;
  category: string;
  status: "violation" | "burst" | "unknown" | string;
  message: string;
  tags: string[];
  observed: Record<string, unknown>;
  limit: Record<string, unknown>;
  window_hours?: number;
  source_type?: string;
  source_id?: string;
};

export type FindingFacets = {
  policy_findings?: PolicyFinding[];
  finding_categories?: string[];
  finding_codes?: string[];
  finding_tags?: string[];
};

export type HistoricalTelemetry = {
  window_hours: number;
  fetched_at: string | null;
  collection_status: "available" | "unavailable" | string;
  evaluation_status: "evaluated" | "warming-up" | "unavailable" | "not-applicable" | string;
  gpu_compute_util_avg_pct: number | null;
  gpu_memory_util_avg_pct: number | null;
  compute_sample_count: number;
  memory_sample_count: number;
};

export type Workload = FindingFacets & {
  workload_id: string;
  workload_name: string;
  resource_name?: string;
  user: string;
  group: string;
  type: string;
  workspace?: string;
  console_url?: string;
  create_time?: string | null;
  resource_create_time?: string | null;
  queue_age_seconds?: number | null;
  start_time?: string | null;
  runtime_anchor_time?: string | null;
  runtime_source?: "training_status_start" | "aid_pod_started_event" | "air_available_condition" | "pod_create_time" | "resource_create_time" | null;
  runtime_quality?: "exact" | "observed" | "estimated" | "unavailable";
  runtime_hours?: number | null;
  runtime_estimated?: boolean;
  total_gpu: number;
  total_cpu: number | null;
  total_memory_gib: number | null;
  resource_basis: "requested" | "attributed";
  task_resources?: TaskResource[];
  num_nodes?: number;
  gpus_per_node?: number | null;
  cpus_per_node?: number | null;
  memory_per_node_gib?: number | null;
  policy_status: string;
  policy_reasons: string[];
  historical_telemetry?: HistoricalTelemetry;
  placements: Placement[];
  gpus: Array<Record<string, string | number | null>>;
  telemetry: Telemetry;
  planning_eligible?: boolean;
  planning_exclusion_reasons?: string[];
  planning_excluded_nodes?: string[];
};

export type WorkloadLogResponse = {
  snapshot_id: string;
  workload_id: string;
  worker: string;
  lines: number;
  content: string;
};

export type GroupSummary = FindingFacets & {
  group: string;
  status: string;
  gpu_quota: number | null;
  cpu_quota: number | null;
  memory_quota_gib: number | null;
  allocated_gpu: number;
  allocated_cpu: number | null;
  allocated_memory_gib: number | null;
  members: string[];
  over_resources: string[];
  telemetry: Telemetry;
};

export type UserSummary = FindingFacets & {
  user: string;
  group: string;
  workload_count: number;
  development_instance_count: number;
  allocated_gpu: number;
  allocated_cpu: number | null;
  allocated_memory_gib: number | null;
  status: string;
  telemetry: Telemetry;
};

export type NodeSummary = {
  node: string;
  id: string;
  host_ip: string;
  state: string;
  allocated_gpu: number;
  total_gpu: number;
  allocated_cpu: number;
  total_cpu: number;
  allocated_memory_gib: number;
  total_memory_gib: number;
  workloads: Record<string, ResourceUsage>;
  unattributed: ResourceUsage;
  attribution_excess: ResourceUsage;
  free_gpu: number;
  effective_free_gpu: number;
  stranded_gpu: number;
  classification: string;
  telemetry: Telemetry;
  planning_eligible: boolean;
  planning_exclusion_reasons: string[];
};

export type Alert = {
  severity: string;
  kind: string;
  subject: string;
  message: string;
  code?: string;
  category?: string;
  subject_type?: string;
  tags?: string[];
  finding_categories?: string[];
  finding_codes?: string[];
  finding_tags?: string[];
};

export type PolicyResponse = {
  valid: boolean;
  using_last_known_good: boolean;
  error: string | null;
  audit_error?: string | null;
  status_definitions: Record<string, string | { description: string; propagation: string }>;
  rule_catalog: Array<{ code: string; category: string; applies_to: string; title: string; description?: string }>;
  evaluation_behavior: Record<string, string>;
  policy: {
    refresh_seconds: number;
    telemetry_lookback_minutes: number;
    pending_pressure: { min_wait_minutes: number; min_jobs: number };
    development: Record<string, number>;
    training: Record<string, number>;
    planning: { default_cpu_per_gpu: number; default_memory_gib_per_gpu: number };
    low_utilization: {
      window_hours: number;
      refresh_minutes: number;
      min_observation_minutes: number;
      gpu_compute_threshold_pct: number;
      gpu_memory_threshold_pct: number;
    };
    groups: Record<string, { gpu_quota: number | "remainder"; member_count: number }>;
  } | null;
};

export type Snapshot = {
  snapshot_id: string;
  generated_at: string;
  cluster: string;
  queue: string;
  capacity: Record<string, number>;
  pending_pressure: Record<string, string | number>;
  telemetry: Telemetry;
  telemetry_status?: "available" | "partial" | "unavailable";
  historical_telemetry_status?: "available" | "unavailable";
  planning_profile: { default_cpu_per_gpu: number; default_memory_gib_per_gpu: number };
  freshness: { stale: boolean; age_seconds: number; last_error: string | null };
  policy_config?: { valid: boolean; using_last_known_good: boolean; error: string | null };
  warnings: string[];
  alerts: Alert[];
  users: UserSummary[];
  groups: GroupSummary[];
  nodes: NodeSummary[];
  workloads: Workload[];
  pending_workloads?: Workload[];
};

export type PlanItem = {
  strategy: string;
  rank: number;
  workloads: string[];
  workload_count: number;
  users: number;
  groups: number;
  gpus: number;
  cpus: number | null;
  memory_gib: number | null;
  freed_nodes: string[];
  workload_details: Workload[];
};

export type PlanResult = {
  snapshot_id: string;
  snapshot_generated_at: string;
  computed_at?: string;
  cache_hit?: boolean;
  optimality: "exact" | "heuristic" | "not-needed" | string;
  superseded: boolean;
  search_elapsed_seconds: number;
  currently_schedulable_nodes?: string[];
  requested_target: { nodes: number; gpus_per_node: number; cpus_per_node: number | null; memory_per_node_gib: number | null };
  resolved_target: { nodes: number; gpus_per_node: number; cpus_per_node: number; memory_per_node_gib: number };
  defaults_applied: string[];
  planning_profile: { default_cpu_per_gpu: number; default_memory_gib_per_gpu: number };
  planning_exclusions: {
    node_count: number;
    workload_count: number;
    reasons: string[];
    nodes?: Array<{ node: string; reasons: string[] }>;
    workloads?: Array<{ workload_id: string; reasons: string[]; nodes: string[] }>;
  };
  no_plan_reason?: string | null;
  plans: PlanItem[];
};

export type ServiceStatus = {
  service: string;
  version: string;
  collecting: boolean;
  skipped_refreshes: number;
  setup_required: boolean;
  admin_enabled: boolean;
  admin_configured: boolean;
  snapshot: { ready: boolean; stale: boolean; last_error: string | null };
  policy: { valid: boolean; error: string | null; audit_error?: string | null };
};

export type DetailKind = "group" | "user" | "node" | "workload" | "alert";

export type DetailRef = {
  kind: DetailKind;
  id: string;
  label: string;
};
