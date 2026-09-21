import type { NodeSummary } from "./types";

export type NodeIdentity = Pick<NodeSummary, "node"> & Partial<Pick<NodeSummary, "hostname" | "host_ip">>;

export function nodePrimaryLabel(node: NodeIdentity): string {
  return node.hostname?.trim() || node.node;
}

export function nodeSecondaryLabel(node: NodeIdentity): string {
  if (!node.hostname?.trim()) return node.host_ip ? `${node.node} · ${node.host_ip} · hostname 不可用` : `${node.node} · hostname 不可用`;
  return `${node.node} · ${node.host_ip || "无 Host IP"}`;
}

export function nodeByName(nodes: NodeIdentity[], nodeName: string): NodeIdentity | undefined {
  return nodes.find((node) => node.node === nodeName);
}

export function nodeNameWithInternal(nodes: NodeIdentity[], nodeName: string): string {
  const node = nodeByName(nodes, nodeName);
  if (!node) return nodeName;
  const primary = nodePrimaryLabel(node);
  return primary === node.node ? primary : `${primary} (${node.node})`;
}
