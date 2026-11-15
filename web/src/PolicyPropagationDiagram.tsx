type FlowTone = "source" | "finding" | "propagation" | "warning";

type FlowNode = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  title: string;
  detail: string[];
  tone: FlowTone;
};

type FlowEdge = {
  from: string;
  to: string;
  label?: string;
  tone?: "normal" | "warning";
  lane?: number;
};

// The lanes mirror apply_policy(): evidence becomes a finding first, then
// findings update the stable workload/user/group targets and alert facets.
const nodes: FlowNode[] = [
  { id: "pressure", x: 24, y: 54, width: 238, height: 82, title: "Queue pending pressure", detail: ["等待时间 + 任务数", "complete / unknown"], tone: "source" },
  { id: "usage", x: 24, y: 170, width: 238, height: 82, title: "Group resource usage", detail: ["GPU / CPU / memory", "quota 与 over 状态"], tone: "source" },
  { id: "running", x: 24, y: 286, width: 238, height: 82, title: "Running workload", detail: ["资源形状 / runtime", "遥测与历史利用率"], tone: "source" },
  { id: "ownership", x: 24, y: 402, width: 238, height: 82, title: "Placement context", detail: ["节点 owner + quota", "owned / foreign / unknown"], tone: "source" },

  { id: "queue-finding", x: 330, y: 54, width: 270, height: 82, title: "quota.pending_pressure", detail: ["queue / group pressure", "active / inactive / unknown"], tone: "finding" },
  { id: "quota-finding", x: 330, y: 170, width: 270, height: 82, title: "quota.*", detail: ["group quota finding", "compliant / burst / violation"], tone: "finding" },
  { id: "workload-finding", x: 330, y: 286, width: 270, height: 82, title: "resource.* / runtime.*", detail: ["workload finding", "violation / unknown"], tone: "finding" },
  { id: "placement-finding", x: 330, y: 402, width: 270, height: 82, title: "placement.*", detail: ["outside_owned_pool", "quota_borrowed / owner_unknown"], tone: "warning" },

  { id: "workload-status", x: 670, y: 108, width: 258, height: 100, title: "Workload policy_status", detail: ["pending > violation > warning", "> unknown > compliant", "placement finding 直接挂到 workload"], tone: "propagation" },
  { id: "summary-status", x: 670, y: 262, width: 258, height: 100, title: "User / Group summary", detail: ["workload findings → user", "quota findings → group + user", "节点归属 badge 使用同一上下文"], tone: "propagation" },
  { id: "alerts", x: 670, y: 416, width: 258, height: 100, title: "Alerts & filters", detail: ["subject = stable workload_id", "warning / error + finding facets", "category / code / tag 可筛选"], tone: "propagation" },

  { id: "boundary", x: 978, y: 108, width: 232, height: 248, title: "Evaluation boundary", detail: ["Pending Workload", "不产生 placement finding。", "", "所有 foreign 都产生 finding：", "quota 可覆盖 → outside_owned_pool；", "quota 不足 → quota_borrowed；", "owner pressure active 时升级 violation。"], tone: "warning" },
];

const edges: FlowEdge[] = [
  { from: "pressure", to: "queue-finding" },
  { from: "usage", to: "quota-finding" },
  { from: "running", to: "workload-finding" },
  { from: "ownership", to: "placement-finding" },
  { from: "queue-finding", to: "placement-finding", label: "owner pressure", tone: "warning", lane: 30 },
  { from: "quota-finding", to: "summary-status", label: "quota → summary" },
  { from: "workload-finding", to: "workload-status", label: "finding → status" },
  { from: "placement-finding", to: "workload-status", label: "placement → status", tone: "warning" },
  { from: "workload-status", to: "summary-status", label: "propagate", lane: 22 },
  { from: "workload-status", to: "alerts", label: "emit", lane: 48 },
  { from: "placement-finding", to: "alerts", label: "placement alert", tone: "warning" },
];

const nodeById = new Map(nodes.map((node) => [node.id, node]));

const anchor = (node: FlowNode, side: "left" | "right") => ({
  x: side === "left" ? node.x : node.x + node.width,
  y: node.y + node.height / 2,
});

function edgeGeometry(edge: FlowEdge, from: FlowNode, to: FlowNode) {
  const forward = to.x > from.x;
  const start = anchor(from, "right");
  const end = anchor(to, forward ? "left" : "right");
  if (!forward) {
    const laneX = Math.max(from.x + from.width, to.x + to.width) + 22 + (edge.lane ?? 0);
    return {
      path: `M ${start.x} ${start.y} C ${laneX} ${start.y}, ${laneX} ${end.y}, ${end.x} ${end.y}`,
      labelX: laneX + 3,
      labelY: (start.y + end.y) / 2,
    };
  }
  const bend = Math.max(30, (end.x - start.x) / 2);
  return {
    path: `M ${start.x} ${start.y} C ${start.x + bend} ${start.y}, ${end.x - bend} ${end.y}, ${end.x} ${end.y}`,
    labelX: start.x + (end.x - start.x) / 2,
    labelY: (start.y + end.y) / 2,
  };
}

export function PolicyPropagationDiagram() {
  return <div className="policy-flow">
    <svg className="policy-flow-svg" viewBox="0 0 1240 570" role="img" aria-label="Monitor 规则产生层级与传播" aria-labelledby="policy-flow-title policy-flow-description">
      <title id="policy-flow-title">Monitor 规则产生层级与传播</title>
      <desc id="policy-flow-description">四列展示输入证据、策略发现、Workload 与用户分组状态传播、告警筛选。placement finding 直接挂到 Workload；owner group 的 pending pressure 会把 warning 升级为 violation；pending Workload 不产生 placement finding。</desc>
      <defs>
        <marker id="policy-flow-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" />
        </marker>
      </defs>
      <g className="policy-flow-column-heads" aria-hidden="true">
        <text x="24" y="25">1 · 输入证据</text>
        <text x="330" y="25">2 · Policy findings</text>
        <text x="670" y="25">3 · 状态传播</text>
        <text x="978" y="25">4 · 边界与语义</text>
      </g>
      <g className="policy-flow-edges">
        {edges.map((edge) => {
          const from = nodeById.get(edge.from);
          const to = nodeById.get(edge.to);
          if (!from || !to) return null;
          const geometry = edgeGeometry(edge, from, to);
          return <g key={`${edge.from}-${edge.to}`}>
            <path className={`policy-flow-edge ${edge.tone === "warning" ? "warning" : ""}`} d={geometry.path} markerEnd="url(#policy-flow-arrow)" />
            {edge.label && <text className="policy-flow-edge-label" x={geometry.labelX} y={geometry.labelY - 6} textAnchor="middle">{edge.label}</text>}
          </g>;
        })}
      </g>
      <g className="policy-flow-nodes">
        {nodes.map((node) => <g className={`policy-flow-node ${node.tone}`} key={node.id} transform={`translate(${node.x} ${node.y})`}>
          <rect width={node.width} height={node.height} rx="12" />
          <text className="policy-flow-node-title" x="16" y="27">{node.title}</text>
          {node.detail.map((line, index) => <text className="policy-flow-node-detail" key={`${node.id}:${index}`} x="16" y={48 + index * 16}>{line}</text>)}
        </g>)}
      </g>
    </svg>
    <p className="policy-flow-legend"><span className="policy-flow-legend-item source">输入</span><span className="policy-flow-legend-item finding">Finding</span><span className="policy-flow-legend-item propagation">状态传播</span><span className="policy-flow-legend-item warning">升级条件</span><span className="policy-flow-legend-note">Workload 状态按 pending → violation → warning → unknown → compliant 取最高优先级</span></p>
  </div>;
}
