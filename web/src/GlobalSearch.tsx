import { useEffect, useMemo, useRef, useState } from "react";
import type { DetailRef, Snapshot } from "./types";

type SearchItem = DetailRef & { meta: string; search: string };

export function GlobalSearch({ snapshot, open }: { snapshot: Snapshot; open: (ref: DetailRef) => void }) {
  const [query, setQuery] = useState("");
  const [focused, setFocused] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const items = useMemo<SearchItem[]>(() => [
    ...snapshot.groups.map((item) => ({ kind: "group" as const, id: item.group, label: item.group, meta: `分组 · ${item.status}`, search: `${item.group} ${item.status}` })),
    ...snapshot.users.map((item) => ({ kind: "user" as const, id: item.user, label: item.user, meta: `用户 · ${item.group}`, search: `${item.user} ${item.group} ${item.status}` })),
    ...snapshot.nodes.map((item) => ({ kind: "node" as const, id: item.node, label: item.node, meta: `节点 · ${item.classification}`, search: `${item.node} ${item.host_ip} ${item.classification} ${item.state}` })),
    ...[...snapshot.workloads, ...(snapshot.pending_workloads ?? [])].map((item) => ({ kind: "workload" as const, id: item.workload_id, label: item.workload_name, meta: `${item.type} · ${item.user}`, search: `${item.workload_name} ${item.user} ${item.group} ${item.type} ${item.priority ?? ""}` })),
  ], [snapshot]);
  const normalized = query.trim().toLocaleLowerCase();
  const results = normalized ? items.filter((item) => item.search.toLocaleLowerCase().includes(normalized)).slice(0, 8) : [];

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (event.key === "/" && !(event.target instanceof HTMLInputElement) && !(event.target instanceof HTMLTextAreaElement)) {
        event.preventDefault(); input.current?.focus();
      }
      if (event.key === "Escape") { setQuery(""); input.current?.blur(); }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);

  const choose = (item: SearchItem) => { open(item); setQuery(""); setFocused(false); input.current?.blur(); };
  return <div className="global-search">
    <span aria-hidden="true">⌕</span><input ref={input} type="search" aria-label="全局搜索" placeholder="搜索用户、节点或 Workload" value={query} onChange={(event) => setQuery(event.target.value)} onFocus={() => setFocused(true)} onBlur={() => window.setTimeout(() => setFocused(false), 120)} /><kbd>/</kbd>
    {focused && normalized && <div className="global-search-results" role="listbox" aria-label="全局搜索结果">
      {results.map((item) => <button type="button" role="option" aria-selected="false" key={`${item.kind}:${item.id}`} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(item)}><span><b>{item.label}</b><small>{item.meta}</small></span><em>打开</em></button>)}
      {!results.length && <p>没有匹配对象</p>}
    </div>}
  </div>;
}
