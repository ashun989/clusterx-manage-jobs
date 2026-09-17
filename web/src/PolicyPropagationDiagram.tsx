type FlowTone = "source" | "finding" | "propagation" | "warning";

type FlowNode = {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  title: string;
  detail: string;
  tone: FlowTone;
};

type FlowEdge = {
  from: string;
  to: string;
  label?: string;
  tone?: "normal" | "warning";
};

// Keep the flow as data so adding a rule or changing propagation does not
// require hand-editing a large SVG path tree.
const nodes: FlowNode[] = [
  { id: "pressure", x: 24, y: 30, width: 220, height: 76, title: "Queue pending pressure", detail: "等待时间 + 任务数阈值", tone: "source" },
  { id: "usage", x: 24, y: 142, width: 220, height: 76, title: "Group resource usage", detail: "GPU / CPU / memory quota", tone: "source" },
  { id: "running", x: 24, y: 254, width: 220, height: 76, title: "Running workload", detail: "资源形状 + 运行时 + 遥测", tone: "source" },
  { id: "ownership", x: 24, y: 366, width: 220, height: 76, title: "Node ownership", detail: "owned / borrowed / outside pool", tone: "source" },
  { id: "queue-finding", x: 330, y: 30, width: 238, height: 76, title: "quota.pending_pressure", detail: "queue: active / inactive / unknown", tone: "finding" },
  { id: "quota-finding", x: 330, y: 142, width: 238, height: 76, title: "quota.*", detail: "group: compliant / burst / violation", tone: "finding" },
  { id: "workload-finding", x: 330, y: 254, width: 238, height: 76, title: "resource.* / runtime.*", detail: "workload: violation / unknown", tone: "finding" },
  { id: "placement-finding", x: 330, y: 366, width: 238, height: 76, title: "placement.*", detail: "warning → violation on owner pressure", tone: "warning" },
  { id: "workload-status", x: 660, y: 142, width: 196, height: 76, title: "Workload status", detail: "任一 violation 即升级", tone: "propagation" },
  { id: "user-status", x: 660, y: 254, width: 196, height: 76, title: "User / group summary", detail: "quota findings → users", tone: "propagation" },
  { id: "alerts", x: 660, y: 366, width: 196, height: 76, title: "Alerts", detail: "warning / error 通知", tone: "propagation" },
  { id: "note", x: 900, y: 142, width: 176, height: 188, title: "Boundary", detail: "Pending workload 不产生 placement finding。\n借用节点不豁免 quota；\nowner group 有 pending pressure\n时，借用 warning 升级为 violation。", tone: "warning" },
];

const edges: FlowEdge[] = [
  { from: "pressure", to: "queue-finding" },
  { from: "usage", to: "quota-finding" },
  { from: "running", to: "workload-finding" },
  { from: "ownership", to: "placement-finding" },
  { from: "queue-finding", to: "placement-finding", label: "owner pressure", tone: "warning" },
  { from: "quota-finding", to: "user-status", label: "propagate" },
  { from: "workload-finding", to: "workload-status" },
  { from: "placement-finding", to: "workload-status" },
  { from: "workload-status", to: "user-status" },
  { from: "workload-status", to: "alerts" },
  { from: "placement-finding", to: "alerts" },
];

const anchor = (node: FlowNode, side: "left" | "right") => ({
  x: side === "left" ? node.x : node.x + node.width,
  y: node.y + node.height / 2,
});

export function PolicyPropagationDiagram() {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  return <div className="policy-flow">
    <svg className="policy-flow-svg" viewBox="0 0 1100 472" role="img" aria-label="Monitor 规则产生层级与传播" aria-labelledby="policy-flow-title policy-flow-description">
      <title id="policy-flow-title">Monitor 规则产生层级与传播</title>
      <desc id="policy-flow-description">输入数据分别产生队列、组、工作负载和节点归属 findings；owner group 的 pending pressure 会把借用节点 warning 升级为 violation，最终传播到 workload、user、group summary 和 alerts。</desc>
      <defs>
        <marker id="policy-flow-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" />
        </marker>
      </defs>
      <g className="policy-flow-edges">
        {edges.map((edge) => {
          const from = nodeById.get(edge.from);
          const to = nodeById.get(edge.to);
          if (!from || !to) return null;
          const start = anchor(from, "right");
          const end = anchor(to, "left");
          const bend = Math.max(24, (end.x - start.x) / 2);
          const labelX = start.x + (end.x - start.x) / 2;
          return <g key={`${edge.from}-${edge.to}`}>
            <path className={`policy-flow-edge ${edge.tone === "warning" ? "warning" : ""}`} d={`M ${start.x} ${start.y} C ${start.x + bend} ${start.y}, ${end.x - bend} ${end.y}, ${end.x} ${end.y}`} markerEnd="url(#policy-flow-arrow)" />
            {edge.label && <text className="policy-flow-edge-label" x={labelX} y={(start.y + end.y) / 2 - 5} textAnchor="middle">{edge.label}</text>}
          </g>;
        })}
      </g>
      <g className="policy-flow-nodes">
        {nodes.map((node) => <g className={`policy-flow-node ${node.tone}`} key={node.id} transform={`translate(${node.x} ${node.y})`}>
          <rect width={node.width} height={node.height} rx="12" />
          <text className="policy-flow-node-title" x="16" y="28">{node.title}</text>
          {node.detail.split("\n").map((line, index) => <text className="policy-flow-node-detail" key={line} x="16" y={49 + index * 17}>{line}</text>)}
        </g>)}
      </g>
    </svg>
    <p className="policy-flow-legend"><span className="policy-flow-legend-item source">输入</span><span className="policy-flow-legend-item finding">Finding</span><span className="policy-flow-legend-item propagation">状态传播</span><span className="policy-flow-legend-item warning">升级条件</span></p>
  </div>;
}
