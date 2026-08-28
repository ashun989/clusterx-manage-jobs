import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api";
import type { HistoryResponse, PolicyResponse, ServiceStatus, Snapshot } from "./types";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "polling";

export function useMonitorData() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [policy, setPolicy] = useState<PolicyResponse | null>(null);
  const [serviceStatus, setServiceStatus] = useState<ServiceStatus | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState("");
  const [policyError, setPolicyError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [refreshing, setRefreshing] = useState(false);
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const requestGeneration = ++generation.current;
    setRefreshing(true);
    const [snapshotResult, policyResult, statusResult, historyResult] = await Promise.allSettled([
      api<Snapshot>("/snapshots/latest"),
      api<PolicyResponse>("/policy"),
      api<ServiceStatus>("/status"),
      api<HistoryResponse>("/history?limit=240"),
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
    return () => { generation.current += 1; stream.close(); window.clearInterval(timer); };
  }, [refresh]);

  return {
    snapshot, policy, serviceStatus, history,
    error, policyError, statusError,
    connection, refreshing, lastSuccessfulAt,
    refresh,
  };
}
