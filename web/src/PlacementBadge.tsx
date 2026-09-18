import type { Placement, Workload } from "./types";

export type PlacementTone = "owned" | "foreign" | "mixed" | "unknown" | "unmanaged" | "outside" | "quota" | "empty";

const issueLabels: Record<string, string> = {
  outside_owned_pool: "有本组余量但使用其他组节点",
  quota_borrowed: "quota 满额借用",
  unknown: "节点归属未知",
};

const relationLabels: Record<string, string> = {
  owned: "本组节点",
  foreign: "其他组节点",
  unknown: "节点归属未知",
};

const ownershipLabels: Record<string, string> = {
  owned: "本组节点",
  foreign: "其他组节点",
  unknown: "节点归属未知",
  unmanaged: "归属约束未启用",
};

export function placementLabel(workload: Workload): string {
  const context = workload.placement_context;
  if (!context || context.mode === "unmanaged") return "未启用归属约束";
  if (context.issue && context.issue !== "none") return issueLabels[context.issue] ?? context.issue;
  if (context.relations.length === 0) return "无 placement";
  return context.relations.map((relation) => relationLabels[relation] ?? relation).join(" / ");
}

export function placementTone(workload: Workload): PlacementTone {
  const context = workload.placement_context;
  if (!context || context.mode === "unmanaged") return "unmanaged";
  if (context.issue === "outside_owned_pool") return "outside";
  if (context.issue === "quota_borrowed") return "quota";
  if (context.issue === "unknown") return "unknown";
  const relations = new Set(context.relations);
  if (relations.has("owned") && relations.has("foreign")) return "mixed";
  if (relations.has("foreign")) return "foreign";
  if (relations.has("owned")) return "owned";
  if (relations.has("unknown")) return "unknown";
  return "empty";
}

function ownerGroups(workload: Workload): string[] {
  return workload.placement_context?.owner_groups ?? [];
}

export function PlacementBadge({ workload, showOwner = true }: { workload: Workload; showOwner?: boolean }) {
  const owners = ownerGroups(workload);
  return <span className="placement-badge-group">
    <span className={`placement-badge placement-${placementTone(workload)}`}>{placementLabel(workload)}</span>
    {showOwner && owners.length > 0 && <small className="placement-owner">owner: {owners.join(", ")}</small>}
  </span>;
}

export function PlacementOwnershipBadge({ placement, showOwner = true }: { placement: Placement; showOwner?: boolean }) {
  const ownership = placement.ownership && ownershipLabels[placement.ownership] ? placement.ownership : "unknown";
  return <span className="placement-badge-group node-placement-badge">
    <span className={`placement-badge placement-${ownership}`}>{ownershipLabels[ownership]}</span>
    {showOwner && placement.owner_group && <small className="placement-owner">owner: {placement.owner_group}</small>}
  </span>;
}
