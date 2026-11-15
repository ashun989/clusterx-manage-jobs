"""Same-origin subscription API; Clusterx operations remain read-only."""

import hmac
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, Security
from fastapi.security import APIKeyCookie
from pydantic import BaseModel, ConfigDict, Field

from .rules import QUEUE_MINUTES, RULES, UID, default_events, identity, key_for
from .store import API, COOKIE, SESSION_TTL, RateLimit, email_address


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["trainingJob", "aid", "air"]
    uid: str = Field(pattern="^" + UID.pattern + "$", min_length=1, max_length=128)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[str] = Field(max_length=len(RULES))
    queue_minutes: Literal[30, 60, 120] = 60

    def selected(self, kind):
        if any(code not in RULES or kind not in RULES[code].kinds for code in self.events):
            raise HTTPException(422, "该任务类型不支持所选提醒。")
        return [key_for(code, self.queue_minutes) for code in self.events]


class Challenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=128)


class Verify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")
    code: str = Field(pattern=r"^[0-9]{6}$")
    target: Target | None = None


class Unsubscribe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(pattern=r"^[A-Za-z0-9_-]{32}\.[0-9a-f]{64}$")


class Followed(Target, Preferences):
    name: str
    state: str | None
    occurred_at: float | None


class Session(BaseModel):
    email_masked: str | None
    csrf: str | None
    follow_error: str | None = None


class Capability(BaseModel):
    code: str
    label: str
    kinds: list[str]
    configured: bool


class Capabilities(BaseModel):
    enabled: bool
    queue_minutes: list[int]
    events: list[Capability]


def router(store, runtime):
    routes = APIRouter(prefix=API, tags=["Task subscriptions"])
    scope = (runtime.collector.cluster_name, runtime.collector.queue)
    authenticated = [Security(APIKeyCookie(name=COOKIE, scheme_name="MonitorEmailSession", auto_error=False))]
    csrf_header = {"parameters": [{
        "name": "X-CSRF-Token", "in": "header", "required": True,
        "schema": {"type": "string"}, "description": "CSRF token from the current email session.",
    }]}

    def exposed(request):
        return bool(store.advertised and request.headers.get("host") == store.origin.split("://", 1)[1])

    def csrf(token):
        return store.digest("pt-csrf", token)

    def guard(request, *, verified=True, write=False):
        if not exposed(request):
            raise HTTPException(404, "关注功能暂未开放。")
        supplied_origin = request.headers.get("origin")
        if (write or supplied_origin is not None) and supplied_origin != store.origin:
            raise HTTPException(403, "请求来源无效。")
        token = request.cookies.get(COOKIE)
        email = store.session(token)
        if verified:
            if not email:
                raise HTTPException(401, "请重新验证邮箱。")
            if write and not hmac.compare_digest(csrf(token).encode(), request.headers.get("x-csrf-token", "").encode()):
                raise HTTPException(403, "会话已变化，请刷新后重试。")
        return token, email

    def mailbox(token, email, error=None):
        masked = email[0] + "***@pjlab.org.cn" if email else None
        return Session(email_masked=masked, csrf=csrf(token) if email else None, follow_error=error)

    def visible(kind, uid):
        snapshot = runtime.snapshots.latest()
        if snapshot is None or not runtime.policy.configured:
            return None
        freshness = runtime.snapshots.status(2 * runtime.policy.policy.refresh_seconds)
        if freshness["stale"]:
            return None
        return next((task for rows in (snapshot["workloads"], snapshot.get("pending_workloads", []))
                     for task in rows if identity(task) == (kind, uid)), None)

    def existing(email, kind, uid):
        return next((item for item in store.follows(email, scope)
                     if item["kind"] == kind and item["uid"] == uid), None)

    @routes.get("/capabilities", response_model=Capabilities)
    def capabilities(request: Request):
        policy = runtime.policy.policy if runtime.policy.configured else None
        enabled = exposed(request)
        return {"enabled": enabled, "queue_minutes": QUEUE_MINUTES, "events": [
            {"code": code, "label": rule.label, "kinds": rule.kinds,
             "configured": bool(code != "gpu.power" or policy and policy.low_utilization.gpu_power_threshold_pct is not None)
             and bool(not code.startswith("placement.") or policy and policy.node_allocation.enabled)}
            for code, rule in RULES.items()
        ] if enabled else []}

    @routes.post("/challenge", status_code=202)
    def challenge(payload: Challenge, request: Request) -> dict[str, str]:
        guard(request, verified=False, write=True)
        if store.paused:
            raise HTTPException(503, "验证码发送暂不可用，请稍后重试。")
        try:
            email = email_address(payload.email)
            # The ASGI connection address is used; browser-supplied forwarding headers are ignored.
            return {"challenge_id": store.challenge(email, request.client.host if request.client else "unknown")}
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except RateLimit:
            raise HTTPException(429, "验证码请求过于频繁，请稍后再试。", headers={"Retry-After": "60"}) from None

    @routes.post("/verify", response_model=Session)
    def verify(payload: Verify, request: Request, response: Response):
        old, _ = guard(request, verified=False, write=True)
        follow = None
        error = None
        if payload.target:
            kind, uid = payload.target.kind, payload.target.uid
            task = visible(kind, uid)
            if task:
                follow = (scope, task, default_events(kind))
            else:
                error = "已登录。任务暂时不可用，请稍后重新关注。"
        result = store.verify(payload.challenge_id, payload.code, old, follow)
        if not result:
            raise HTTPException(422, "验证码错误或已失效。")
        token, email, limited = result
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL, path=API, httponly=True,
                            samesite="strict", secure=store.origin.startswith("https://"))
        return mailbox(token, email, "最多可关注 100 个任务。" if limited else error)

    @routes.get("/session", response_model=Session)
    def session(request: Request):
        token, email = guard(request, verified=False)
        return mailbox(token, email)

    @routes.post("/logout", dependencies=authenticated, openapi_extra=csrf_header)
    def logout(request: Request, response: Response) -> dict[str, bool]:
        token, _ = guard(request, write=True)
        store.logout(token)
        response.delete_cookie(COOKIE, path=API, httponly=True, samesite="strict",
                               secure=store.origin.startswith("https://"))
        return {"ok": True}

    @routes.get("", response_model=list[Followed], dependencies=authenticated)
    def follows(request: Request):
        _, email = guard(request)
        return store.follows(email, scope)

    @routes.post("/unsubscribe")
    def unsubscribe(payload: Unsubscribe, request: Request) -> dict[str, bool]:
        guard(request, verified=False, write=True)
        if not store.unsubscribe(payload.token):
            raise HTTPException(422, "退订链接无效。")
        return {"ok": True}

    @routes.get("/{kind}/{uid}", response_model=Followed, dependencies=authenticated)
    def detail(kind: Literal["trainingJob", "aid", "air"], uid: str, request: Request):
        _, email = guard(request)
        item = existing(email, kind, uid)
        if not item:
            raise HTTPException(404, "找不到此关注。")
        return item

    @routes.put("/{kind}/{uid}", response_model=Followed, dependencies=authenticated, openapi_extra=csrf_header)
    def follow(kind: Literal["trainingJob", "aid", "air"], uid: str, request: Request, payload: Preferences | None = None):
        _, email = guard(request, write=True)
        with store.transaction() as db:
            task = visible(kind, uid)
            if task:
                rules = default_events(kind) if payload is None else payload.selected(kind)
                try:
                    store.save_follow(db, email, scope, task, rules, replace=payload is not None)
                except RateLimit:
                    raise HTTPException(429, "最多可关注 100 个任务。") from None
            else:
                row = db.execute("""SELECT s.id FROM subscription s JOIN target t ON s.target=t.id
                    WHERE s.email=? AND t.cluster=? AND t.queue=? AND t.kind=? AND t.uid=?""",
                                 (email, *scope, kind, uid)).fetchone()
                if not row:
                    raise HTTPException(404, "任务暂时不可用。")
                if payload is not None:
                    store.preferences(db, row["id"], payload.selected(kind), store.clock())
        item = existing(email, kind, uid)
        if item is None:
            raise HTTPException(409, "关注已在其他会话中取消。")
        return item

    @routes.delete("/{kind}/{uid}", dependencies=authenticated, openapi_extra=csrf_header)
    def delete(kind: Literal["trainingJob", "aid", "air"], uid: str, request: Request) -> dict[str, bool]:
        _, email = guard(request, write=True)
        store.unfollow(email, scope, kind, uid)
        return {"ok": True}

    return routes
