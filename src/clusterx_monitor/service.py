from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Message, Receive, Scope, Send
import yaml

from . import __version__
from .auth import AdminAuth, AdminSession, SESSION_COOKIE
from .collector import ClusterCollector
from .models import PlanRequest
from .planner import solve_plan
from .planning.cp_sat import configured_solver_workers
from .planning.domain import MODEL_VERSION as PLANNER_MODEL_VERSION
from .policy import ConfigConflictError, PolicyManager, apply_policy
from .store import PlanCache, SnapshotStore, SQLiteHistoryStore

logger = logging.getLogger("clusterx_monitor")


class RequestRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        events = self._events[key]
        while events and now - events[0] >= self.window_seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        if len(self._events) > self.max_keys:
            oldest = min(self._events, key=lambda name: self._events[name][-1] if self._events[name] else 0)
            self._events.pop(oldest, None)
        return True


class MonitorRuntime:
    def __init__(
        self, collector: ClusterCollector, policy: PolicyManager, *,
        history_db: str | Path | None = None, history_retention_days: int = 30,
        history_max_points: int = 100_000, history_max_db_mib: int = 256,
    ) -> None:
        # Fail fast on an operator typo rather than discovering it after the
        # first planner request.
        configured_solver_workers()
        self.collector = collector
        self.policy = policy
        self.history_init_error: str | None = None
        persistent_history = None
        if history_db is not None:
            try:
                persistent_history = SQLiteHistoryStore(
                    history_db, retention_days=history_retention_days,
                    max_points=history_max_points, max_db_mib=history_max_db_mib,
                )
            except Exception as error:
                self.history_init_error = f"{type(error).__name__}: history storage unavailable"
                logger.exception("sqlite history initialization failed; using memory history")
        self.snapshots = SnapshotStore(capacity=5, persistent_history=persistent_history)
        self.snapshots.history_error = self.history_init_error
        self.plans = PlanCache(capacity=128)
        self.executor = ProcessPoolExecutor(max_workers=1)
        self.stop_event = asyncio.Event()
        self.config_changed = asyncio.Event()
        self.snapshot_event = asyncio.Condition()
        self.collecting = False
        self.skipped_refreshes = 0
        self._plan_lock = asyncio.Lock()
        self._plan_key: str | None = None
        self._plan_task: asyncio.Task[dict[str, Any]] | None = None
        self.collection_deadline_seconds = 90.0
        self.metrics: dict[str, float] = {
            "collection_success_total": 0, "collection_failure_total": 0,
            "collection_timeout_total": 0, "collection_duration_seconds": 0,
            "planner_requests_total": 0, "planner_rejections_total": 0,
            "planner_failures_total": 0, "planner_duration_seconds": 0,
            "refresh_skipped_total": 0, "realtime_log_requests_total": 0,
            "realtime_log_failures_total": 0,
        }

    def _apply_policy(self, raw: dict[str, Any], policy=None) -> dict[str, Any]:
        snapshot = apply_policy(raw, policy or self.policy.policy)
        config_error = self.policy.error
        snapshot["policy_config"] = {
            "valid": config_error is None,
            "using_last_known_good": config_error is not None,
            "error": config_error,
        }
        if config_error:
            snapshot.setdefault("alerts", []).append({
                "severity": "warning",
                "kind": "policy-config",
                "subject": "policy-config",
                "message": f"invalid policy configuration; using last-known-good: {config_error}",
                "code": "configuration.last_known_good",
                "category": "configuration",
                "subject_type": "configuration",
                "tags": ["configuration", "last-known-good"],
                "finding_categories": ["configuration"],
                "finding_codes": ["configuration.last_known_good"],
                "finding_tags": ["configuration", "last-known-good"],
            })
        if self.policy.audit_error:
            snapshot.setdefault("alerts", []).append({
                "severity": "warning",
                "kind": "audit",
                "subject": "admin-audit",
                "message": f"administrator audit log is degraded: {self.policy.audit_error}",
                "code": "configuration.audit_degraded",
                "category": "configuration",
                "subject_type": "configuration",
                "tags": ["configuration", "audit", "degraded"],
                "finding_categories": ["configuration"],
                "finding_codes": ["configuration.audit_degraded"],
                "finding_tags": ["configuration", "audit", "degraded"],
            })
        return snapshot

    async def _collect(self, policy) -> dict[str, Any]:
        def invoke() -> dict[str, Any]:
            kwargs = {
                "telemetry_minutes": policy.telemetry_lookback_minutes,
                "historical_window_hours": policy.low_utilization.window_hours,
                "historical_refresh_minutes": policy.low_utilization.refresh_minutes,
                "deadline_seconds": self.collection_deadline_seconds,
            }
            try:
                return self.collector.collect(**kwargs)
            except TypeError as error:
                if "deadline_seconds" not in str(error):
                    raise
                kwargs.pop("deadline_seconds")
                return self.collector.collect(**kwargs)
        return await asyncio.wait_for(asyncio.to_thread(invoke), timeout=self.collection_deadline_seconds)

    async def refresh(self) -> None:
        if self.collecting:
            self.skipped_refreshes += 1
            self.metrics["refresh_skipped_total"] += 1
            return
        self.collecting = True
        started = time.monotonic()
        try:
            self.policy.reload()
            if not self.policy.configured:
                return
            policy = self.policy.policy
            policy_revision = self.policy.policy_revision
            raw = await self._collect(policy)
            self.policy.reload()
            if policy_revision != self.policy.policy_revision:
                self.config_changed.set()
                return
            snapshot = self._apply_policy(raw, policy)
            await asyncio.to_thread(self.snapshots.publish, snapshot)
            self.metrics["collection_success_total"] += 1
            async with self.snapshot_event:
                self.snapshot_event.notify_all()
        except asyncio.TimeoutError:
            self.metrics["collection_timeout_total"] += 1
            self.metrics["collection_failure_total"] += 1
            self.snapshots.record_failure("collection deadline exceeded")
            logger.warning("collection deadline exceeded", extra={"event": "collection_timeout"})
        except RuntimeError as error:
            if "queue node allocation changed" in str(error):
                try:
                    policy = self.policy.policy
                    policy_revision = self.policy.policy_revision
                    raw = await self._collect(policy)
                    self.policy.reload()
                    if policy_revision != self.policy.policy_revision:
                        self.config_changed.set()
                        return
                    raw.setdefault("warnings", []).append(
                        "node allocation changed during the first collection; one retry was used"
                    )
                    await asyncio.to_thread(self.snapshots.publish, self._apply_policy(raw, policy))
                    async with self.snapshot_event:
                        self.snapshot_event.notify_all()
                    self.metrics["collection_success_total"] += 1
                    return
                except Exception as retry_error:
                    self.snapshots.record_failure(retry_error)
                    self.metrics["collection_failure_total"] += 1
                    logger.exception("collection retry failed")
            else:
                self.snapshots.record_failure(error)
                self.metrics["collection_failure_total"] += 1
                logger.exception("collection failed")
        except Exception as error:
            self.snapshots.record_failure(error)
            self.metrics["collection_failure_total"] += 1
            logger.exception("collection failed")
        finally:
            self.metrics["collection_duration_seconds"] = time.monotonic() - started
            self.collecting = False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        while not self.stop_event.is_set():
            await self.refresh()
            interval = self.policy.policy.refresh_seconds if self.policy.configured else 2
            deadline += interval
            now = loop.time()
            if now > deadline:
                skipped = int((now - deadline) // interval) + 1
                deadline += skipped * interval
                self.skipped_refreshes += skipped
            stop_task = asyncio.create_task(self.stop_event.wait())
            config_task = asyncio.create_task(self.config_changed.wait())
            done, pending = await asyncio.wait(
                {stop_task, config_task}, timeout=max(0, deadline - now),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if config_task in done and self.config_changed.is_set():
                self.config_changed.clear()
                deadline = loop.time()

    async def close(self) -> None:
        self.stop_event.set()
        if self._plan_task is not None and not self._plan_task.done():
            self._plan_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True),
                timeout=5,
            )
        except asyncio.TimeoutError:
            # A solver already executing native code cannot be force-cancelled;
            # ensure queued work is dropped and let the process pool terminate
            # asynchronously instead of blocking service shutdown.
            self.executor.shutdown(wait=False, cancel_futures=True)

    def request_config_refresh(self) -> None:
        self.config_changed.set()

    async def coordinate_plan(
        self, key: str, compute: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._plan_lock:
            if self._plan_task is not None and not self._plan_task.done():
                if self._plan_key != key:
                    raise RuntimeError("another scheduling plan is already being computed")
                task = self._plan_task
            else:
                task = asyncio.create_task(compute())
                self._plan_key = key
                self._plan_task = task
        try:
            return deepcopy(await asyncio.shield(task))
        finally:
            if task.done():
                async with self._plan_lock:
                    if self._plan_task is task:
                        self._plan_task = None
                        self._plan_key = None


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=1_048_576)


class ConfigRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    backup_revision: str = Field(min_length=1, max_length=128)


class SnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: int | None = None
    snapshot_id: str | None = None
    generated_at: str | None = None


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    points: list[dict[str, Any]] = Field(default_factory=list)
    storage: str | None = None


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str | None = None
    strategy: str | None = None
    cache_hit: bool | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    detail: str
    error_code: str | None = None
    request_id: str | None = None


class RequestBodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET").upper()
        limit = (
            1_048_576 if path.startswith("/api/v1/admin/") and method in {"POST", "PUT", "PATCH"}
            else 262_144 if path == "/api/v1/plans" and method == "POST"
            else None
        )
        if limit is None:
            await self.app(scope, receive, send)
            return
        request_id = next((value.decode("latin1") for key, value in scope.get("headers", []) if key.lower() == b"x-request-id"), None) or uuid.uuid4().hex
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        if declared > limit:
            await JSONResponse({"detail": "request body is too large", "error_code": "request_too_large", "request_id": request_id[:128]}, status_code=413, headers={"X-Request-ID": request_id[:128]})(scope, receive, send)
            return
        consumed = 0
        buffered: list[Message] = []
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    await JSONResponse({"detail": "request body is too large", "error_code": "request_too_large", "request_id": request_id[:128]}, status_code=413, headers={"X-Request-ID": request_id[:128]})(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


def create_app(
    runtime: MonitorRuntime, *, static_dir: str | Path | None = None,
    auth: AdminAuth | None = None, allowed_hosts: list[str] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(runtime.run(), name="clusterx-monitor-refresh")
        try:
            yield
        finally:
            await runtime.close()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Clusterx Monitor", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts or ["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id[:128]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        detail = error.detail if isinstance(error.detail, str) else "request failed"
        response = JSONResponse({
            "detail": detail,
            "error_code": f"http_{error.status_code}",
            "request_id": getattr(request.state, "request_id", None),
        }, status_code=error.status_code, headers=error.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError):
        return JSONResponse({
            "detail": "request validation failed",
            "error_code": "validation_error",
            "request_id": getattr(request.state, "request_id", None),
        }, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception):
        logger.exception("unhandled monitor request error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse({
            "detail": "internal server error",
            "error_code": "internal_error",
            "request_id": getattr(request.state, "request_id", None),
        }, status_code=500)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        raw_path = request.scope.get("raw_path", request.url.path.encode("utf-8", errors="replace"))
        decoded_path = raw_path.decode("utf-8", errors="replace")
        for _ in range(2):
            decoded_path = unquote(decoded_path)
        normalized = decoded_path.replace("\\", "/")
        if normalized.startswith("//") or ".." in normalized.split("/") or "\\" in decoded_path:
            response = JSONResponse({"detail": "not found"}, status_code=404)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if (
            request.url.path.startswith("/api/v1/admin/")
            or request.url.path.startswith("/api/v1/workloads/")
        ):
            response.headers["Cache-Control"] = "no-store"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "clusterx-monitor", "version": __version__}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        interval = runtime.policy.policy.refresh_seconds if runtime.policy.configured else 30
        state = runtime.snapshots.status(interval * 2)
        ready = bool(runtime.policy.configured and state["ready"] and not state["stale"])
        if not ready:
            response.status_code = 503
        return {"status": "ready" if ready else "not_ready", "snapshot": state}

    @app.get("/metrics")
    async def metrics() -> Response:
        status_data = runtime.snapshots.status(
            runtime.policy.policy.refresh_seconds * 2 if runtime.policy.configured else 60,
        )
        values = dict(runtime.metrics)
        values["snapshot_age_seconds"] = float(status_data.get("age_seconds") or 0)
        values["snapshot_stale"] = 1.0 if status_data.get("stale") else 0.0
        history = status_data.get("history") or {}
        values["sqlite_database_bytes"] = float(history.get("database_bytes") or 0)
        lines = []
        for key, value in sorted(values.items()):
            metric_type = "counter" if key.endswith("_total") else "gauge"
            lines.append(f"# TYPE clusterx_monitor_{key} {metric_type}")
            lines.append(f"clusterx_monitor_{key} {float(value):.6f}")
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/status")
    async def status() -> dict[str, Any]:
        interval = runtime.policy.policy.refresh_seconds if runtime.policy.configured else 30
        return {
            "service": "clusterx-monitor",
            "version": __version__,
            "collecting": runtime.collecting,
            "skipped_refreshes": runtime.skipped_refreshes,
            "snapshot": runtime.snapshots.status(interval * 2),
            "policy": {
                "valid": runtime.policy.public_status()["valid"],
                "error": runtime.policy.error,
                "audit_error": runtime.policy.audit_error,
            },
            "setup_required": not runtime.policy.configured,
            "admin_enabled": auth is not None,
            "admin_configured": bool(auth and auth.configured),
            "history_init_error": runtime.history_init_error,
        }

    def snapshot_or_404(snapshot_id: str | None = None) -> dict[str, Any]:
        snapshot = runtime.snapshots.latest() if snapshot_id is None else runtime.snapshots.get(snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="complete snapshot is unavailable")
        status_data = runtime.snapshots.status(runtime.policy.policy.refresh_seconds * 2)
        snapshot["freshness"] = {
            "age_seconds": status_data["age_seconds"],
            "stale": status_data["stale"],
            "last_error": status_data["last_error"],
        }
        return snapshot

    @app.get("/api/v1/snapshots/latest", response_model=SnapshotResponse)
    async def latest_snapshot(request: Request, response: Response) -> dict[str, Any] | Response:
        snapshot = snapshot_or_404()
        immutable = {key: value for key, value in snapshot.items() if key != "freshness"}
        etag = hashlib.sha256(json.dumps(immutable, sort_keys=True, default=str).encode()).hexdigest()
        response.headers["ETag"] = f'"{etag}"'
        if request.headers.get("if-none-match") == f'"{etag}"':
            return Response(status_code=304, headers={"ETag": f'"{etag}"'})
        return snapshot

    @app.get("/api/v1/snapshots")
    async def snapshot_index() -> dict[str, Any]:
        return {"snapshots": runtime.snapshots.index()}

    @app.get("/api/v1/snapshots/compare")
    async def compare_snapshots(from_snapshot_id: str, to_snapshot_id: str) -> dict[str, Any]:
        comparison = runtime.snapshots.compare(from_snapshot_id, to_snapshot_id)
        if comparison is None:
            raise HTTPException(status_code=404, detail="one or both snapshots are not retained")
        return comparison

    @app.get("/api/v1/snapshots/{snapshot_id}", response_model=SnapshotResponse)
    async def get_snapshot(snapshot_id: str, request: Request, response: Response) -> dict[str, Any] | Response:
        snapshot = snapshot_or_404(snapshot_id)
        immutable = {key: value for key, value in snapshot.items() if key != "freshness"}
        etag = hashlib.sha256(json.dumps(immutable, sort_keys=True, default=str).encode()).hexdigest()
        response.headers["ETag"] = f'"{etag}"'
        if request.headers.get("if-none-match") == f'"{etag}"':
            return Response(status_code=304, headers={"ETag": f'"{etag}"'})
        return snapshot

    @app.get("/api/v1/snapshots/latest/workloads")
    async def snapshot_workloads(
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        snapshot = snapshot_or_404()
        rows = list(snapshot.get("workloads") or [])
        return {"snapshot_id": snapshot["snapshot_id"], "offset": offset, "limit": limit,
                "total": len(rows), "workloads": rows[offset:offset + limit]}

    @app.get("/api/v1/history", response_model=HistoryResponse)
    async def history(
        request: Request, response: Response,
        limit: int = Query(default=240, ge=2, le=800),
        since: str | None = Query(default=None), until: str | None = Query(default=None),
        resolution_seconds: int | None = Query(default=None, ge=1, le=604_800),
    ) -> dict[str, Any]:
        try:
            since_dt = None
            until_dt = None
            if since:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if until:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if since_dt and since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            if until_dt and until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=timezone.utc)
            if since_dt and until_dt and since_dt > until_dt:
                raise ValueError("since must not be after until")
        except ValueError as error:
            raise HTTPException(status_code=422, detail="since and until must be ISO-8601 timestamps") from error
        result = await asyncio.to_thread(
            runtime.snapshots.history, limit, since=since, until=until,
            resolution_seconds=resolution_seconds,
        )
        etag = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
        response.headers["ETag"] = f'"{etag}"'
        if request.headers.get("if-none-match") == f'"{etag}"':
            return Response(status_code=304, headers={"ETag": f'"{etag}"'})
        return result

    @app.get("/api/v1/policy")
    async def policy() -> dict[str, Any]:
        return runtime.policy.public_status()

    def require_auth(request: Request) -> AdminSession:
        if auth is None:
            raise HTTPException(status_code=503, detail="administrator authentication is not configured")
        try:
            session = auth.session(request.cookies.get(SESSION_COOKIE))
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail="administrator authentication is not initialized or is invalid") from error
        if session is None:
            raise HTTPException(status_code=401, detail="administrator authentication required")
        return session

    log_fetch_slots = asyncio.Semaphore(4)

    @app.get("/api/v1/workloads/{workload_id}/logs")
    async def workload_logs(
        workload_id: str,
        snapshot_id: str,
        worker: str,
        lines: int = Query(default=200, ge=1, le=2000),
    ) -> dict[str, Any]:
        runtime.metrics["realtime_log_requests_total"] += 1
        snapshot = runtime.snapshots.get(snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="snapshot is not retained")
        workload = next(
            (
                item for item in [
                    *(snapshot.get("workloads") or []),
                    *(snapshot.get("pending_workloads") or []),
                ]
                if str(item.get("workload_id") or "") == workload_id
            ),
            None,
        )
        if workload is None:
            raise HTTPException(status_code=404, detail="workload is not in the snapshot")
        if workload.get("type") != "trainingJob" or workload.get("resource_basis") != "attributed":
            raise HTTPException(status_code=422, detail="realtime logs require a running training workload")
        resource_name = str(workload.get("resource_name") or "")
        allowed_workers = {
            str(item.get("pod") or "")
            for item in workload.get("placements") or []
            if item.get("pod")
        }
        if not resource_name:
            raise HTTPException(status_code=422, detail="workload log target is unavailable")
        if worker not in allowed_workers:
            raise HTTPException(status_code=422, detail="worker is not part of this snapshot workload")
        try:
            async with log_fetch_slots:
                content = await asyncio.wait_for(
                    asyncio.to_thread(
                        runtime.collector.get_realtime_log,
                        resource_name,
                        worker,
                        lines,
                    ),
                    timeout=30,
                )
        except TimeoutError as error:
            runtime.metrics["realtime_log_failures_total"] += 1
            raise HTTPException(status_code=504, detail="workload log request timed out") from error
        except ValueError as error:
            runtime.metrics["realtime_log_failures_total"] += 1
            raise HTTPException(status_code=422, detail="workload log request is invalid") from error
        except Exception as error:
            runtime.metrics["realtime_log_failures_total"] += 1
            raise HTTPException(status_code=502, detail="workload log is unavailable") from error
        return {
            "snapshot_id": snapshot_id,
            "workload_id": workload_id,
            "worker": worker,
            "lines": lines,
            "content": str(content or ""),
        }

    def require_same_origin_json(request: Request) -> None:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if not origin or origin.rstrip("/") != expected.rstrip("/"):
            raise HTTPException(status_code=403, detail="same-origin request required")
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site and fetch_site != "same-origin":
            raise HTTPException(status_code=403, detail="cross-site request rejected")
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            raise HTTPException(status_code=415, detail="application/json is required")

    def require_admin_write(
        request: Request, session: AdminSession = Depends(require_auth),
    ) -> AdminSession:
        require_same_origin_json(request)
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token,
        ):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        return session

    @app.post("/api/v1/admin/login")
    async def admin_login(payload: AdminLoginRequest, request: Request, response: Response) -> dict[str, Any]:
        if auth is None:
            raise HTTPException(status_code=503, detail="administrator authentication is not configured")
        require_same_origin_json(request)
        client = request.client.host if request.client else "unknown"
        try:
            token, session = await asyncio.to_thread(
                auth.login, payload.username, payload.password, client,
            )
        except PermissionError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except (OSError, RuntimeError) as error:
            raise HTTPException(status_code=503, detail="administrator authentication is not initialized or is invalid") from error
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, secure=request.url.scheme == "https",
            samesite="strict", path="/", max_age=auth.session_ttl_hours * 3600,
        )
        return auth.public_session(session)

    @app.get("/api/v1/admin/session")
    async def admin_session(session: AdminSession = Depends(require_auth)) -> dict[str, Any]:
        assert auth is not None
        return auth.public_session(session)

    @app.post("/api/v1/admin/logout")
    async def admin_logout(
        request: Request, response: Response,
        _: AdminSession = Depends(require_admin_write),
    ) -> dict[str, bool]:
        assert auth is not None
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/v1/admin/config")
    async def admin_config(_: AdminSession = Depends(require_auth)) -> dict[str, Any]:
        return runtime.policy.admin_config()

    async def update_config(kind: str, payload: ConfigUpdateRequest, session: AdminSession) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                runtime.policy.update_config, kind, payload.text,
                payload.revision, actor=session.username,
            )
        except ConfigConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if runtime.policy.configured:
            runtime.request_config_refresh()
        return result

    @app.put("/api/v1/admin/config/resource")
    async def update_resource_config(
        payload: ConfigUpdateRequest,
        session: AdminSession = Depends(require_admin_write),
    ) -> dict[str, Any]:
        return await update_config("resource", payload, session)

    @app.put("/api/v1/admin/config/groups")
    async def update_group_config(
        payload: ConfigUpdateRequest,
        session: AdminSession = Depends(require_admin_write),
    ) -> dict[str, Any]:
        return await update_config("groups", payload, session)

    async def rollback_config(
        kind: str, payload: ConfigRollbackRequest, session: AdminSession,
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                runtime.policy.rollback_config, kind, payload.revision,
                payload.backup_revision, actor=session.username,
            )
        except ConfigConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        runtime.request_config_refresh()
        return result

    @app.post("/api/v1/admin/config/resource/rollback")
    async def rollback_resource_config(
        payload: ConfigRollbackRequest,
        session: AdminSession = Depends(require_admin_write),
    ) -> dict[str, Any]:
        return await rollback_config("resource", payload, session)

    @app.post("/api/v1/admin/config/groups/rollback")
    async def rollback_group_config(
        payload: ConfigRollbackRequest,
        session: AdminSession = Depends(require_admin_write),
    ) -> dict[str, Any]:
        return await rollback_config("groups", payload, session)

    sse_slots = asyncio.Semaphore(100)
    plan_limiter = RequestRateLimiter(limit=6, window_seconds=60)

    @app.get("/api/v1/events")
    async def events() -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            await sse_slots.acquire()
            last_id: str | None = None
            try:
                while True:
                    latest = runtime.snapshots.latest()
                    current = str(latest["snapshot_id"]) if latest else None
                    if current and current != last_id:
                        last_id = current
                        yield "event: snapshot\ndata: " + json.dumps({
                            "snapshot_id": current,
                            "generated_at": latest["generated_at"],
                        }) + "\n\n"
                    try:
                        async with runtime.snapshot_event:
                            await asyncio.wait_for(runtime.snapshot_event.wait(), 15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                sse_slots.release()
        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/plans", response_model=PlanResponse)
    async def plan(request: PlanRequest, http_request: Request) -> dict[str, Any]:
        runtime.metrics["planner_requests_total"] += 1
        snapshot = runtime.snapshots.get(request.snapshot_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="snapshot is not retained")
        payload = request.model_dump(mode="json")
        key = hashlib.sha256(json.dumps({
            "planner_model_version": PLANNER_MODEL_VERSION,
            "request": payload,
        }, sort_keys=True).encode()).hexdigest()
        cached = runtime.plans.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            latest = runtime.snapshots.latest()
            cached["superseded"] = bool(latest and latest["snapshot_id"] != request.snapshot_id)
            return cached
        client_key = http_request.client.host if http_request.client else "unknown"
        if not plan_limiter.allow(client_key):
            runtime.metrics["planner_rejections_total"] += 1
            raise HTTPException(status_code=429, detail="planner request rate limit exceeded", headers={"Retry-After": "10"})
        loop = asyncio.get_running_loop()
        async def compute() -> dict[str, Any]:
            return await loop.run_in_executor(runtime.executor, solve_plan, snapshot, payload)

        try:
            started = time.monotonic()
            result = await runtime.coordinate_plan(key, compute)
        except RuntimeError as error:
            runtime.metrics["planner_rejections_total"] += 1
            raise HTTPException(
                status_code=429, detail=str(error), headers={"Retry-After": "1"},
            ) from error
        except Exception as error:
            runtime.metrics["planner_failures_total"] += 1
            logger.exception("planner failed")
            raise HTTPException(status_code=503, detail="planner is temporarily unavailable") from error
        else:
            runtime.metrics["planner_duration_seconds"] = time.monotonic() - started
        latest = runtime.snapshots.latest()
        result["superseded"] = bool(latest and latest["snapshot_id"] != request.snapshot_id)
        result["computed_at"] = datetime.now(timezone.utc).isoformat()
        result["cache_hit"] = False
        if result.get("optimality") in {"exact", "not-needed"}:
            runtime.plans.put(key, result)
        return result

    root = Path(static_dir) if static_dir else Path(__file__).with_name("static")
    assets = root / "assets"
    if assets.is_dir() and not assets.is_symlink():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index_file = root / "index.html"
    if index_file.is_file() and not index_file.is_symlink():
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(index_file)

        @app.get("/clusterx-icon.svg", include_in_schema=False)
        async def clusterx_icon():
            icon_file = root / "clusterx-icon.svg"
            if not icon_file.is_file() or icon_file.is_symlink():
                return JSONResponse({"detail": "not found"}, status_code=404)
            return FileResponse(icon_file, media_type="image/svg+xml")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            parts = Path(path.replace("\\", "/")).parts
            if path == "assets" or path.startswith(("api/", "assets/", "/")) or ".." in parts or "\\" in path:
                return JSONResponse({"detail": "not found"}, status_code=404)
            return FileResponse(index_file)
    return app
