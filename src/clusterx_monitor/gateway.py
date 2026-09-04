"""Small, testable boundary around the Clusterx SDK.

The collector owns the current Clusterx response contract; this facade only
applies request policy and classifies failures.  Keeping those concerns here
means the collector does not need release-specific request shims.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
from typing import Any, Callable, TypeVar

from . import collector

T = TypeVar("T")


@dataclass(frozen=True)
class GatewayTimeouts:
    connect: float = 5.0
    read: float = 20.0
    collection_deadline: float = 90.0
    retries: int = 2


class GatewayError(RuntimeError):
    """Stable error category for failures at the external API boundary."""

    def __init__(self, message: str, *, category: str = "external_api", cause: Exception | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.cause = cause


class ClusterxGateway:
    """Adapter exposing all monitor read operations in one interface.

    The adapter delegates to the collector's current-version helpers and does
    not retain mutable SDK request state, making it safe to use from the
    collector's worker threads.
    """

    def __init__(self, cluster: Any, *, timeouts: GatewayTimeouts | None = None) -> None:
        self.cluster = cluster
        self.timeouts = timeouts or GatewayTimeouts()

    def _call(self, operation: str, fn: Callable[[], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(self.timeouts.retries + 1):
            try:
                return self._bounded_call(fn)
            except GatewayError:
                raise
            except RuntimeError:
                # Preserve structural collector errors (for example allocation
                # changes) so the runtime can apply its existing retry policy.
                raise
            except Exception as error:
                last_error = error
                name = type(error).__name__.lower()
                transient = isinstance(error, (TimeoutError, ConnectionError)) or "timeout" in name or "connection" in name
                if not transient or attempt >= self.timeouts.retries:
                    category = "timeout" if transient else "external_api"
                    message = "timed out" if transient else "failed"
                    raise GatewayError(f"{operation} request {message}", category=category, cause=error) from error
        assert last_error is not None
        raise GatewayError(f"{operation} request failed", cause=last_error) from last_error

    def _bounded_call(self, fn: Callable[[], T]) -> T:
        """Run SDK calls with a hard wall-clock bound.

        The installed Clusterx SDK hard-codes its requests timeout and does not
        expose a timeout parameter. A daemon worker lets the monitor return
        promptly while preserving the SDK's normal request path.
        """
        result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, fn()))
            except Exception as error:
                result.put((False, error))

        thread = threading.Thread(target=invoke, name="clusterx-gateway-request", daemon=True)
        thread.start()
        try:
            ok, value = result.get(timeout=self.timeouts.connect + self.timeouts.read)
        except queue.Empty as error:
            raise TimeoutError("Clusterx API request exceeded read deadline") from error
        if ok:
            return value  # type: ignore[return-value]
        raise value  # type: ignore[misc]

    def list_queue_nodes(self, cluster_name: str, queue: str) -> list[dict[str, Any]]:
        return self._call("list_queue_nodes", lambda: collector._list_queue_nodes(self.cluster, cluster_name, queue))

    def list_node_pods(self, cluster_name: str, queue: str, node_id: str) -> list[dict[str, Any]]:
        return self._call("list_node_pods", lambda: collector._list_node_pods(self.cluster, cluster_name, queue, node_id))

    def running_training_lifecycle(self, queue: str) -> tuple[dict[str, dict[str, Any]], bool]:
        return self._call("running_training_lifecycle", lambda: collector._running_training_lifecycle(self.cluster, queue))

    def running_aid_lifecycle(self) -> tuple[dict[str, dict[str, Any]], bool]:
        return self._call("running_aid_lifecycle", lambda: collector._running_aid_lifecycle(self.cluster))

    def running_air_lifecycle(self) -> tuple[dict[str, dict[str, Any]], bool]:
        return self._call("running_air_lifecycle", lambda: collector._running_air_lifecycle(self.cluster))

    def pending_workloads(self, queue: str) -> tuple[list[dict[str, Any]], bool]:
        return self._call("pending_workloads", lambda: collector._pending_workloads(self.cluster, queue))

    def query_gpu_telemetry(self, queue: str, cluster_name: str, minutes: int) -> list[dict[str, Any]]:
        return self._call("query_gpu_telemetry", lambda: collector._query_gpu_telemetry(self.cluster, queue, cluster_name, minutes))

    def query_workload_history(self, queue: str, cluster_name: str, window_hours: int) -> dict[str, dict[str, float]]:
        return self._call("query_workload_history", lambda: collector._query_workload_history(self.cluster, queue, cluster_name, window_hours))

    def realtime_log(self, resource_name: str, worker: str, lines: int) -> str:
        return self._call("realtime_log", lambda: self.cluster.get_log(resource_name, worker=worker, lines=lines))
