import { useEffect, useMemo } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import type { Telemetry } from "./types";
import { DismissibleDetails } from "./useDismissibleMenu";

export type ColumnKind = "text" | "enum" | "number" | "status" | "telemetry";

export type ColumnDef<T> = {
  key: string;
  label: string;
  kind: ColumnKind;
  value: (row: T) => unknown;
  format?: (value: unknown, row: T) => ReactNode;
  filterable?: boolean;
  hidden?: boolean;
};

export type SortState = { key: string; direction: "asc" | "desc" } | null;
export type TableState = {
  filters: Record<string, string[]>;
  sort: SortState;
  query?: string;
  hiddenColumns?: string[];
  density?: "comfortable" | "compact";
};

export const emptyTableState = (): TableState => ({ filters: {}, sort: null, query: "", hiddenColumns: [], density: "comfortable" });

const display = (value: unknown) => value == null ? "—" : Array.isArray(value) ? value.join(", ") || "—" : typeof value === "number" ? value.toLocaleString() : String(value);
const number = (value: unknown, suffix = "") => value == null ? "—" : `${Number(value).toLocaleString()}${suffix}`;
const power = (watts: number | null) => watts == null ? "—" : watts >= 1000 ? `${(watts / 1000).toFixed(1)} kW` : `${watts.toFixed(0)} W`;
export const statusClass = (status: unknown) => `status status-${String(status ?? "unknown")}`;

export function TelemetryCell({ data }: { data: Telemetry }) {
  return <div className="telemetry-cell">
    <span>{number(data.gpu_compute_util_avg_pct, "%")} util <small>{data.compute_reported_gpu_count}/{data.allocated_gpu_count}</small></span>
    <span>{number(data.gpu_memory_util_avg_pct, "%")} mem <small>{data.memory_reported_gpu_count}/{data.allocated_gpu_count}</small></span>
    <span>{power(data.gpu_power_total_w)} <small>{data.power_reported_gpu_count}/{data.allocated_gpu_count}</small></span>
  </div>;
}

const valueString = (value: unknown) => value == null ? "" : String(value);
const valueStrings = (value: unknown) => Array.isArray(value) ? value.map(valueString) : [valueString(value)];

function cycleSort(current: SortState, key: string): SortState {
  if (!current || current.key !== key) return { key, direction: "asc" };
  if (current.direction === "asc") return { key, direction: "desc" };
  return null;
}

function compareValues(left: unknown, right: unknown, direction: "asc" | "desc") {
  const leftMissing = left == null || (typeof left === "number" && Number.isNaN(left));
  const rightMissing = right == null || (typeof right === "number" && Number.isNaN(right));
  if (leftMissing || rightMissing) {
    if (leftMissing && rightMissing) return 0;
    return leftMissing ? 1 : -1;
  }
  const result = Number(left) - Number(right);
  return direction === "asc" ? result : -result;
}

function FilterMenu<T>({ column, options, selected, onChange }: {
  column: ColumnDef<T>;
  options: string[];
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  return <DismissibleDetails className="filter-menu" summary={<>{column.label}{selected.length > 0 && <span>{selected.length}</span>}</>}>
    <div className="filter-popover">
      {options.length === 0 ? <small>暂无选项</small> : options.map((option) => <label key={option}>
        <input type="checkbox" checked={selected.includes(option)} onChange={(event) => onChange(event.target.checked ? [...selected, option] : selected.filter((value) => value !== option))} />
        <span>{option || "(empty)"}</span>
      </label>)}
    </div>
  </DismissibleDetails>;
}

export function DataTable<T>({ rows, columns, state, onState, rowKey, rowLabel, onRow, emptyMessage = "没有匹配的数据" }: {
  rows: T[];
  columns: ColumnDef<T>[];
  state: TableState;
  onState: (state: TableState) => void;
  rowKey: (row: T, index: number) => string;
  rowLabel: (row: T) => string;
  onRow: (row: T) => void;
  emptyMessage?: string;
}) {
  const enumColumns = columns.filter((column) => column.kind === "enum" || column.filterable);
  const configurableColumns = columns.filter((column) => !column.hidden);
  const visibleColumns = configurableColumns.filter((column) => !(state.hiddenColumns ?? []).includes(column.key));
  const optionMap = useMemo(() => Object.fromEntries(enumColumns.map((column) => [column.key, [...new Set(rows.flatMap((row) => valueStrings(column.value(row))).filter(Boolean))].sort((a, b) => a.localeCompare(b))])), [rows, columns]);
  const optionSignature = JSON.stringify(optionMap);
  const filterSignature = JSON.stringify(state.filters);

  useEffect(() => {
    const filters = Object.fromEntries(Object.entries(state.filters).map(([key, values]) => [key, values.filter((value) => optionMap[key]?.includes(value))]).filter(([, values]) => values.length > 0));
    if (JSON.stringify(filters) !== filterSignature) onState({ ...state, filters });
  }, [optionSignature, filterSignature]);

  const visibleRows = useMemo(() => {
    const normalizedQuery = (state.query ?? "").trim().toLocaleLowerCase();
    const filtered = rows.filter((row) => (!normalizedQuery || columns.some((column) => valueStrings(column.value(row)).some((value) => value.toLocaleLowerCase().includes(normalizedQuery)))) && Object.entries(state.filters).every(([key, selected]) => {
      if (selected.length === 0) return true;
      const column = columns.find((item) => item.key === key);
      return column ? valueStrings(column.value(row)).some((value) => selected.includes(value)) : true;
    }));
    if (!state.sort) return filtered;
    const column = columns.find((item) => item.key === state.sort?.key);
    if (!column) return filtered;
    return filtered.map((row, index) => ({ row, index })).sort((a, b) => compareValues(column.value(a.row), column.value(b.row), state.sort!.direction) || a.index - b.index).map(({ row }) => row);
  }, [rows, columns, state]);

  const activeFilterCount = Object.values(state.filters).reduce((sum, values) => sum + values.length, 0);
  const reset = () => onState(emptyTableState());
  const exportCsv = () => {
    const escape = (value: unknown) => {
      const text = display(value);
      // Excel interprets leading formula characters even inside quoted CSV
      // cells. Prefix a tab so exported operational data remains inert.
      const safe = /^[=+\-@]/.test(text) ? `\t${text}` : text;
      return `"${safe.replaceAll('"', '""')}"`;
    };
    const lines = [visibleColumns.map((column) => escape(column.label)).join(","), ...visibleRows.map((row) => visibleColumns.map((column) => escape(column.value(row))).join(","))];
    const url = URL.createObjectURL(new Blob(["\ufeff", lines.join("\n")], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `clusterx-monitor-${new Date().toISOString().slice(0, 10)}.csv`; anchor.click();
    URL.revokeObjectURL(url);
  };
  const keyOpen = (event: KeyboardEvent<HTMLTableRowElement>, row: T) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onRow(row);
    }
  };

  return <>
    <div className="table-tools">
      <div className="table-search"><span aria-hidden="true">⌕</span><input type="search" aria-label="搜索当前表格" placeholder="搜索当前列表" value={state.query ?? ""} onChange={(event) => onState({ ...state, query: event.target.value })} /></div>
      <div className="filter-list">{enumColumns.map((column) => <FilterMenu key={column.key} column={column} options={optionMap[column.key] ?? []} selected={state.filters[column.key] ?? []} onChange={(values) => onState({ ...state, filters: { ...state.filters, [column.key]: values } })} />)}</div>
      <div className="table-view-actions">
        <DismissibleDetails className="filter-menu column-menu" summary="列"><div className="filter-popover">{configurableColumns.map((column) => <label key={column.key}><input type="checkbox" checked={!(state.hiddenColumns ?? []).includes(column.key)} onChange={(event) => onState({ ...state, hiddenColumns: event.target.checked ? (state.hiddenColumns ?? []).filter((key) => key !== column.key) : [...(state.hiddenColumns ?? []), column.key] })} /><span>{column.label}</span></label>)}</div></DismissibleDetails>
        <button type="button" aria-label="切换表格密度" onClick={() => onState({ ...state, density: state.density === "compact" ? "comfortable" : "compact" })}>{state.density === "compact" ? "舒适" : "紧凑"}</button>
        <button type="button" onClick={exportCsv}>导出</button>
      </div>
      <div className="result-count"><span>{visibleRows.length}/{rows.length}</span>{(activeFilterCount > 0 || state.sort || state.query || (state.hiddenColumns?.length ?? 0) > 0) && <button type="button" onClick={reset}>重置</button>}</div>
    </div>
    <div className={`table-wrap data-table-wrap density-${state.density ?? "comfortable"}`}><table className="data-table"><thead><tr>{visibleColumns.map((column) => <th key={column.key}>{column.kind === "number" ? <button type="button" aria-label={`排序 ${column.label}`} className={state.sort?.key === column.key ? "sort active" : "sort"} onClick={() => onState({ ...state, sort: cycleSort(state.sort, column.key) })}>{column.label}<span>{state.sort?.key === column.key ? state.sort.direction === "asc" ? "↑" : "↓" : "↕"}</span></button> : column.label}</th>)}</tr></thead>
      <tbody>{visibleRows.map((row, index) => <tr key={rowKey(row, index)} className="clickable" tabIndex={0} aria-label={`查看 ${rowLabel(row)} 详情`} onClick={() => onRow(row)} onKeyDown={(event) => keyOpen(event, row)}>
        {visibleColumns.map((column) => {
          const value = column.value(row);
          return <td key={column.key} data-label={column.label}>{column.format ? column.format(value, row) : column.kind === "telemetry" ? <TelemetryCell data={value as Telemetry} /> : column.kind === "status" || column.key === "status" || column.key === "policy_status" ? <span className={statusClass(value)}>{display(value)}</span> : display(value)}</td>;
        })}
      </tr>)}{visibleRows.length === 0 && <tr><td className="empty-table" colSpan={visibleColumns.length}>{emptyMessage}</td></tr>}</tbody></table></div>
  </>;
}

export const formatPower = (value: unknown) => power(value == null ? null : Number(value));
