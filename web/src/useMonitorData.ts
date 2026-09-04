import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api";
import type { HistoryResponse, PolicyResponse, ServiceStatus, Snapshot } from "./types";
import type { TrendRange } from "./navigation";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "polling";

const historyPath = (range: TrendRange) => {
  const since = new Date(Date.now() - range * 1_000).toISOString();
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
  const [historyError, setHistoryError] = useState("");
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [refreshing, setRefreshing] = useState(false);
  const [historyRefreshing, setHistoryRefreshing] = useState(false);
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const generation = useRef(0);
  const historyGeneration = useRef(0);
  const historyRangeRef = useRef(historyRange);
  const refreshPromise = useRef<Promise<void> | null>(null);
  const refreshAbort = useRef<AbortController | null>(null);
  const historyAbort = useRef<AbortController | null>(null);

  const refreshHistory = useCallback(async (range: TrendRange) => {
    const requestGeneration = ++historyGeneration.current;
    historyAbort.current?.abort();
    const controller = new AbortController();
    historyAbort.current = controller;
    setHistoryRefreshing(true);
    try {
      const value = await api<HistoryResponse>(historyPath(range), { signal: controller.signal });
      if (requestGeneration === historyGeneration.current && Array.isArray(value.points)) {
        setHistory(value);
        setHistoryError("");
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (requestGeneration === historyGeneration.current) setHistoryError(errorMessage(reason));
    } finally {
      if (requestGeneration === historyGeneration.current) setHistoryRefreshing(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    if (refreshPromise.current) return refreshPromise.current;
    const requestGeneration = ++generation.current;
    refreshAbort.current?.abort();
    const controller = new AbortController();
    refreshAbort.current = controller;
    setRefreshing(true);
    const work = (async () => {
      const [snapshotResult, policyResult, statusResult, historyResult] = await Promise.allSettled([
        api<Snapshot>("/snapshots/latest", { signal: controller.signal }),
        api<PolicyResponse>("/policy", { signal: controller.signal }),
        api<ServiceStatus>("/status", { signal: controller.signal }),
        api<HistoryResponse>(historyPath(historyRangeRef.current), { signal: controller.signal }),
      ]);
      if (requestGeneration !== generation.current || controller.signal.aborted) return;

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
      if (historyResult.status === "fulfilled" && Array.isArray(historyResult.value.points)) {
        setHistory(historyResult.value);
        setHistoryError("");
      } else if (historyResult.status === "rejected" && !controller.signal.aborted) {
        setHistoryError(errorMessage(historyResult.reason));
      }
    })();
    refreshPromise.current = work;
    try { await work; } finally {
      if (refreshPromise.current === work) refreshPromise.current = null;
      if (requestGeneration === generation.current) setRefreshing(false);
    }
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
      if (stream.readyState === EventSource.OPEN) return;
      setConnection((current) => current === "live" ? current : "polling");
      void refresh();
    }, 30_000);
    return () => {
      generation.current += 1;
      historyGeneration.current += 1;
      refreshAbort.current?.abort();
      historyAbort.current?.abort();
      stream.close();
      window.clearInterval(timer);
    };
  }, [refresh]);

  return {
    snapshot, policy, serviceStatus, history,
    error, policyError, statusError, historyError,
    connection, refreshing, historyRefreshing, lastSuccessfulAt,
    refresh,
  };
}
