import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api";
import type { HistoryResponse, PolicyResponse, ServiceStatus, Snapshot } from "./types";
import type { TrendRange } from "./navigation";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "polling";

const historyPath = (range: TrendRange) => {
  const durations: Partial<Record<TrendRange, number>> = { "1h": 3_600, "6h": 21_600, "24h": 86_400, "7d": 604_800, "30d": 2_592_000 };
  const seconds = durations[range];
  const since = new Date(seconds ? Date.now() - seconds * 1_000 : 0).toISOString();
  return `/history?limit=800&since=${encodeURIComponent(since)}`;
};

export function useMonitorData(historyRange: TrendRange) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [policy, setPolicy] = useState<PolicyResponse | null>(null);
  const [serviceStatus, setServiceStatus] = useState<ServiceStatus | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState("");
  const [policyError, setPolicyError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [refreshing, setRefreshing] = useState(false);
  const [historyRefreshing, setHistoryRefreshing] = useState(false);
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const generation = useRef(0);
  const historyGeneration = useRef(0);
  const historyRangeRef = useRef(historyRange);

  const refreshHistory = useCallback(async (range: TrendRange) => {
    const requestGeneration = ++historyGeneration.current;
    setHistoryRefreshing(true);
    try {
      const value = await api<HistoryResponse>(historyPath(range));
      if (requestGeneration === historyGeneration.current && Array.isArray(value.points)) setHistory(value);
    } catch {
      // Preserve the last successfully loaded trend while the live snapshot continues.
    } finally {
      if (requestGeneration === historyGeneration.current) setHistoryRefreshing(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    const requestGeneration = ++generation.current;
    setRefreshing(true);
    const [snapshotResult, policyResult, statusResult, historyResult] = await Promise.allSettled([
      api<Snapshot>("/snapshots/latest"),
      api<PolicyResponse>("/policy"),
      api<ServiceStatus>("/status"),
      api<HistoryResponse>(historyPath(historyRangeRef.current)),
    ]);
    if (requestGeneration !== generation.current) return;

    if (snapshotResult.status === "fulfilled") {
      setSnapshot((current) => {
        if (!current) return snapshotResult.value;
        const incoming = Date.parse(snapshotResult.value.generated_at);
        const existing = Date.parse(current.generated_at);
        return Number.isNaN(incoming) || Number.isNaN(existing) || incoming >= existing ? snapshotResult.value : current;
      });
      setError("");
      setLastSuccessfulAt(Date.now());
    } else setError(errorMessage(snapshotResult.reason));

    if (policyResult.status === "fulfilled") { setPolicy(policyResult.value); setPolicyError(""); }
    else setPolicyError(errorMessage(policyResult.reason));
    if (statusResult.status === "fulfilled") { setServiceStatus(statusResult.value); setStatusError(""); }
    else setStatusError(errorMessage(statusResult.reason));
    if (historyResult.status === "fulfilled" && Array.isArray(historyResult.value.points)) setHistory(historyResult.value);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    if (historyRangeRef.current === historyRange) return;
    historyRangeRef.current = historyRange;
    void refreshHistory(historyRange);
  }, [historyRange, refreshHistory]);

  useEffect(() => {
    void refresh();
    const stream = new EventSource("/api/v1/events");
    stream.onopen = () => setConnection("live");
    stream.addEventListener("snapshot", () => { setConnection("live"); void refresh(); });
    stream.onerror = () => {
      setConnection("reconnecting");
      setError("实时连接已断开，正在通过轮询保持更新");
    };
    const timer = window.setInterval(() => {
      setConnection((current) => current === "live" ? current : "polling");
      void refresh();
    }, 30_000);
    return () => { generation.current += 1; historyGeneration.current += 1; stream.close(); window.clearInterval(timer); };
  }, [refresh]);

  return {
    snapshot, policy, serviceStatus, history,
    error, policyError, statusError,
    connection, refreshing, historyRefreshing, lastSuccessfulAt,
    refresh,
  };
}
