"""Grouproxy control plane entry point.

The first implementation keeps the HTTP API deliberately small, but the
contracts are explicit: browser management endpoints live under ``/api/v1``
and node endpoints live under ``/agent/v1``.  The monitor owns all host-side
changes; this service only computes and records desired state.
"""

import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response

from app.config import Settings, get_settings
from app.db import Database
from app.models import (
    AdminUser,
    AuditEvent,
    ConfigDraft,
    ConfigRelease,
    CrossSiteAllow,
    DesiredRelease,
    DestinationBlacklist,
    HeartbeatLatest,
    HeartbeatSample,
    ManagementSession,
    Node,
    Site,
    SiteCIDR,
    SiteSubscription,
    SubscriptionSource,
    SubscriptionVersion,
    Task,
    TravelException,
    utcnow,
)
from app.models import (
    AgentAck as AgentAckDocument,
)
from app.schemas import (
    AgentAck,
    AgentAckOut,
    AgentHeartbeat,
    AuditEventOut,
    AuthActionResponse,
    CIDRCreate,
    CIDROut,
    CIDRPreviewRequest,
    CIDRPreviewResponse,
    CrossSiteAllowOut,
    CrossSiteAllowUpdate,
    DesiredResponse,
    DestinationBlacklistCreate,
    DestinationBlacklistOut,
    DraftCreate,
    DraftOut,
    GQuanLoginRequest,
    LoginRequest,
    LoginResponse,
    NodeCreate,
    NodeCreateResponse,
    NodeOut,
    PasswordChangeRequest,
    RegistrationRequest,
    ReleaseCreate,
    ReleaseOut,
    SiteOut,
    SiteSubscriptionOut,
    SubscriptionCatalogOut,
    SubscriptionPublishOut,
    SubscriptionPublishRequest,
    SubscriptionRefreshResponse,
    SubscriptionSourceCreate,
    SubscriptionSourceOut,
    SubscriptionUploadResponse,
    SubscriptionVersionOut,
    TaskOut,
    TravelExceptionCreate,
    TravelExceptionOut,
    VerificationCodeRequest,
    VerificationCodeResponse,
)
from app.services.audit import append_audit, verify_audit_chain
from app.services.auth import (
    AuthError,
    consume_verification_code,
    create_registered_user,
    create_session,
    find_user_by_itcode,
    hash_password,
    normalize_itcode,
    request_verification_code,
    resolve_session,
    revoke_session_token,
    revoke_user_sessions,
    validate_password,
    verify_password,
)
from app.services.bundles import create_desired_release, latest_release
from app.services.cidr import effective_cidrs, match_source_ip, normalize_cidr, normalize_source_ip
from app.services.subscription_worker import SubscriptionWorker, enqueue_refresh_task
from app.services.subscriptions import (
    SubscriptionError,
    normalize_source_url,
    record_uploaded_subscription,
    source_url_hint,
)
from app.services.tasks import create_task

DEFAULT_SITES = [
    ("north", "North Region"),
    ("east", "East Region"),
    ("south", "South Region"),
    ("west", "West Region"),
    ("central", "Central Region"),
]

_management_actor: ContextVar[str] = ContextVar("management_actor", default="")


def _settings() -> Settings:
    return get_settings()


def _actor() -> str:
    """Identity established by ``require_management`` for audit ownership."""

    return _management_actor.get() or _settings().admin_username


def _model_id(value: Any) -> str:
    return str(value.id)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_error(value: str, limit: int = 512) -> str:
    return " ".join(value.split())[:limit]


def _site_out(site: Site) -> SiteOut:
    return SiteOut(
        id=_model_id(site),
        slug=site.slug,
        name=site.name,
        dns_note=site.dns_note,
        proxy_auth_required=site.proxy_auth_required,
        http_port=site.http_port,
        shutdown=site.shutdown,
        config_revision=site.config_revision,
    )


def _node_out(node: Node) -> NodeOut:
    return NodeOut(
        id=_model_id(node),
        site_id=node.site_id,
        name=node.name,
        agent_id=node.agent_id,
        advertise_ip=node.advertise_ip,
        monitor_version=node.monitor_version,
        singbox_version=node.singbox_version,
        last_seen_at=node.last_seen_at,
        desired_version=node.desired_version,
        applied_version=node.applied_version,
        applied_hash=node.applied_hash,
        liveness_status=node.liveness_status,
        config_status=node.config_status,
        service_status=node.service_status,
        subscription_status=node.subscription_status,
        last_error=node.last_error,
    )


def _draft_out(draft: ConfigDraft) -> DraftOut:
    return DraftOut(
        id=_model_id(draft),
        site_id=draft.site_id,
        node_ids=draft.node_ids,
        source_revision=draft.source_revision,
        diff=draft.diff,
        validation=draft.validation,
        risk_level=draft.risk_level,
        status=draft.status,
        expires_at=draft.expires_at,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _release_out(release: ConfigRelease) -> ReleaseOut:
    return ReleaseOut(
        release_id=release.release_id,
        site_id=release.site_id,
        node_ids=release.node_ids,
        desired_release_id=release.desired_release_id,
        previous_release_id=release.previous_release_id,
        task_id=release.task_id,
        status=release.status,
        stage=release.stage,
        progress=release.progress,
        error=release.error,
        rollback_reason=release.rollback_reason,
        started_at=release.started_at,
        finished_at=release.finished_at,
        created_at=release.created_at,
    )


def _task_out(task: Task) -> TaskOut:
    return TaskOut(
        task_id=task.task_id,
        task_type=task.task_type,
        target_type=task.target_type,
        target_id=task.target_id,
        status=task.status,
        progress=task.progress,
        stage=task.stage,
        progress_message=task.progress_message,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        error=task.error,
        result=task.result,
        created_at=task.created_at,
        finished_at=task.finished_at,
        next_run_at=task.next_run_at,
        locked_by=task.locked_by,
        lease_expires_at=task.lease_expires_at,
    )


def _exception_out(item: TravelException) -> TravelExceptionOut:
    return TravelExceptionOut(
        id=_model_id(item),
        cidr=item.cidr,
        comment=item.comment,
        owner=item.owner,
        expires_at=item.expires_at,
        enabled=item.enabled,
        created_at=item.created_at,
    )


def _cross_site_out(item: CrossSiteAllow) -> CrossSiteAllowOut:
    return CrossSiteAllowOut(
        id=_model_id(item),
        from_site_id=item.from_site_id,
        to_site_id=item.to_site_id,
        enabled=item.enabled,
        comment=item.comment,
        updated_at=item.updated_at,
    )


def _blacklist_out(item: DestinationBlacklist) -> DestinationBlacklistOut:
    return DestinationBlacklistOut(
        id=_model_id(item),
        pattern=item.pattern,
        kind=item.kind,
        comment=item.comment,
        enabled=item.enabled,
        created_at=item.created_at,
    )


def _subscription_source_out(item: SubscriptionSource) -> SubscriptionSourceOut:
    return SubscriptionSourceOut(
        id=_model_id(item),
        name=item.name,
        url_hint=source_url_hint(item.url),
        fetch_interval_sec=item.fetch_interval_sec,
        max_body_bytes=item.max_body_bytes,
        redirect_limit=item.redirect_limit,
        enabled=item.enabled,
        refreshable=bool(item.url) and item.enabled,
        last_refresh_at=item.last_refresh_at,
        last_refresh_attempt_at=item.last_refresh_attempt_at,
        last_refresh_error=item.last_refresh_error,
        consecutive_failures=item.consecutive_failures,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _subscription_version_out(item: SubscriptionVersion) -> SubscriptionVersionOut:
    return SubscriptionVersionOut(
        id=_model_id(item),
        source_id=item.source_id,
        version=item.version,
        content_hash=item.content_hash,
        size_bytes=item.size_bytes,
        format=item.format,
        fetched_at=item.fetched_at,
        parse_ok=item.parse_ok,
        parse_error=item.parse_error,
        node_count=item.node_count,
        published=item.published,
        created_at=item.created_at,
    )


def _site_subscription_out(item: SiteSubscription) -> SiteSubscriptionOut:
    return SiteSubscriptionOut(
        site_id=item.site_id,
        source_id=item.source_id,
        subscription_version_id=item.subscription_version_id,
        previous_subscription_version_id=item.previous_subscription_version_id,
        updated_at=item.updated_at,
    )


def _ack_out(item: AgentAckDocument) -> AgentAckOut:
    return AgentAckOut(
        node_id=item.node_id,
        release_id=item.release_id,
        desired_version=item.desired_version,
        applied_version=item.applied_version,
        bundle_hash=item.bundle_hash,
        applied_hash=item.applied_hash,
        ok=item.ok,
        singbox_ok=item.singbox_ok,
        nft_ok=item.nft_ok,
        health_ok=item.health_ok,
        rollback_attempted=item.rollback_attempted,
        rollback_ok=item.rollback_ok,
        last_good_version=item.last_good_version,
        stage=item.stage,
        error_code=item.error_code,
        error_message=item.error_message,
        sequence=item.sequence,
        received_at=item.received_at,
    )


def _audit_out(item: AuditEvent) -> AuditEventOut:
    return AuditEventOut(
        event_id=item.event_id,
        actor=item.actor,
        actor_role=item.actor_role,
        request_id=item.request_id,
        source_ip=item.source_ip,
        action=item.action,
        target_type=item.target_type,
        target_id=item.target_id,
        before=item.before,
        after=item.after,
        result=item.result,
        error=item.error,
        immutable_hash=item.immutable_hash,
        previous_hash=item.previous_hash,
        at=item.at,
    )


async def _increment_site_revisions(site_ids: list[str]) -> None:
    for site_id in dict.fromkeys(site_ids):
        site = await Site.get(site_id)
        if site is not None:
            site.config_revision += 1
            await site.save()


async def _increment_all_site_revisions() -> None:
    sites = await Site.find_all().to_list()
    await _increment_site_revisions([_model_id(site) for site in sites])


async def seed_defaults(settings: Settings) -> None:
    if settings.seed_default_sites:
        for slug, name in DEFAULT_SITES:
            if not await Site.find_one(Site.slug == slug):
                await Site(
                    slug=slug, name=name, dns_note="Configure local DNS to this site node"
                ).insert()
    admin_itcode = normalize_itcode(settings.admin_username)
    admin = await AdminUser.find_one(AdminUser.username == settings.admin_username)
    if admin is None:
        await AdminUser(
            username=settings.admin_username,
            itcode=admin_itcode,
            password_hash=hash_password(settings.admin_password),
            auth_source="local",
            password_changed_at=utcnow(),
        ).insert()
    elif not admin.itcode:
        admin.itcode = admin_itcode
        await admin.save()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _settings()
    database = Database(settings)
    await database.connect()
    await seed_defaults(settings)
    app.state.database = database
    worker = SubscriptionWorker(settings)
    worker_task = asyncio.create_task(worker.run())
    app.state.subscription_worker = worker
    try:
        yield
    finally:
        await worker.stop()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await database.close()


app = FastAPI(title="Grouproxy Control Plane", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "grouproxy-backend"}


@app.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None or database.client is None:
        raise HTTPException(status_code=503, detail="database_not_ready")
    try:
        await database.client.admin.command("ping")
    except Exception as exc:  # pragma: no cover - driver-specific exception
        raise HTTPException(status_code=503, detail="database_not_ready") from exc
    return {"status": "ready"}


async def require_management(request: Request) -> str:
    settings = _settings()
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if token and hmac.compare_digest(token, settings.management_token):
        actor = normalize_itcode(settings.admin_username)
        _management_actor.set(actor)
        return actor
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="management_auth_required"
        )
    session = await resolve_session(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="management_auth_required"
        )
    _management_actor.set(session.itcode)
    return session.itcode


async def require_agent(request: Request) -> Node:
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="agent_token_required")
    nodes = await Node.find_all().to_list()
    token_hash = _hash_secret(token)
    for node in nodes:
        if hmac.compare_digest(node.agent_token_hash, token_hash):
            return node
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_agent_token")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", "") or secrets.token_hex(16)


def _request_source_ip(request: Request) -> str:
    return request.client.host if request.client is not None else ""


def _auth_http_error(error: AuthError) -> HTTPException:
    status_code = {
        "invalid_itcode": 422,
        "invalid_password": 422,
        "itcode_already_registered": 409,
        "verification_code_rate_limited": 429,
        "verification_code_attempts_exceeded": 429,
        "gquan_quota_exceeded": 429,
        "gquan_delivery_unavailable": 503,
        "gquan_delivery_rejected": 503,
        "gquan_stub_not_allowed": 503,
        "gquan_test_code_not_configured": 503,
    }.get(error.code, 401)
    return HTTPException(status_code=status_code, detail=error.code)


async def _audit_auth_failure(
    *, request: Request, action: str, itcode: str, error: str
) -> None:
    await append_audit(
        action=action,
        target_type="admin_user",
        target_id=itcode[:64],
        actor=itcode[:64] or "anonymous",
        actor_role="anonymous",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
        result="failed",
        error=error,
    )


def _session_response(token: str, session: ManagementSession) -> LoginResponse:
    return LoginResponse(access_token=token, itcode=session.itcode, expires_at=session.expires_at)


@app.post(
    "/api/v1/auth/verification-codes",
    response_model=VerificationCodeResponse,
    status_code=202,
)
async def request_auth_verification_code(
    payload: VerificationCodeRequest, request: Request
) -> VerificationCodeResponse:
    try:
        itcode = normalize_itcode(payload.itcode)
        existing = await find_user_by_itcode(itcode)
        if payload.purpose == "register" and existing is not None:
            raise AuthError("itcode_already_registered")
        if payload.purpose != "register" and (existing is None or not existing.is_active):
            raise AuthError("account_not_registered")
        challenge = await request_verification_code(
            itcode=itcode,
            purpose=payload.purpose,
            source_ip=_request_source_ip(request),
            settings=_settings(),
        )
    except AuthError as exc:
        await _audit_auth_failure(
            request=request,
            action="auth.verification.request",
            itcode=payload.itcode.strip().casefold(),
            error=exc.code,
        )
        raise _auth_http_error(exc) from exc
    await append_audit(
        action="auth.verification.request",
        target_type="admin_user",
        target_id=itcode,
        actor=itcode,
        actor_role="anonymous",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
        after={"purpose": payload.purpose, "challenge_id": challenge.challenge_id},
    )
    return VerificationCodeResponse(
        challenge_id=challenge.challenge_id,
        expires_at=challenge.expires_at,
        resend_available_at=challenge.resend_available_at,
    )


@app.post("/api/v1/auth/register", response_model=AuthActionResponse, status_code=201)
async def register(payload: RegistrationRequest, request: Request) -> AuthActionResponse:
    try:
        itcode = normalize_itcode(payload.itcode)
        validate_password(payload.password)
        if await find_user_by_itcode(itcode):
            raise AuthError("itcode_already_registered")
        await consume_verification_code(
            challenge_id=payload.challenge_id,
            itcode=itcode,
            purpose="register",
            code=payload.verification_code,
            settings=_settings(),
        )
        await create_registered_user(itcode=itcode, password=payload.password)
    except AuthError as exc:
        await _audit_auth_failure(
            request=request,
            action="auth.register",
            itcode=payload.itcode.strip().casefold(),
            error=exc.code,
        )
        raise _auth_http_error(exc) from exc
    await append_audit(
        action="auth.register",
        target_type="admin_user",
        target_id=itcode,
        actor=itcode,
        actor_role="anonymous",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
        after={"itcode": itcode, "auth_source": "local"},
    )
    return AuthActionResponse()


@app.post("/api/v1/auth/password/change", response_model=AuthActionResponse)
async def change_password(payload: PasswordChangeRequest, request: Request) -> AuthActionResponse:
    try:
        itcode = normalize_itcode(payload.itcode)
        validate_password(payload.password)
        user = await find_user_by_itcode(itcode)
        if user is None or not user.is_active:
            raise AuthError("account_not_registered")
        await consume_verification_code(
            challenge_id=payload.challenge_id,
            itcode=itcode,
            purpose="password_change",
            code=payload.verification_code,
            settings=_settings(),
        )
        user.password_hash = hash_password(payload.password)
        user.password_changed_at = utcnow()
        await user.save()
        await revoke_user_sessions(user)
    except AuthError as exc:
        await _audit_auth_failure(
            request=request,
            action="auth.password.change",
            itcode=payload.itcode.strip().casefold(),
            error=exc.code,
        )
        raise _auth_http_error(exc) from exc
    await append_audit(
        action="auth.password.change",
        target_type="admin_user",
        target_id=itcode,
        actor=itcode,
        actor_role="anonymous",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
    )
    return AuthActionResponse()


@app.post("/api/v1/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request) -> LoginResponse:
    try:
        itcode = normalize_itcode(payload.itcode)
        user = await find_user_by_itcode(itcode)
        valid, needs_upgrade = (
            verify_password(payload.password, user.password_hash) if user else (False, False)
        )
        if user is None or not user.is_active or not valid:
            raise AuthError("invalid_credentials")
        if needs_upgrade:
            user.password_hash = hash_password(payload.password)
            user.password_changed_at = utcnow()
        user.last_login_at = utcnow()
        await user.save()
        token, session = await create_session(user, _settings())
    except AuthError as exc:
        await _audit_auth_failure(
            request=request,
            action="auth.login.password",
            itcode=payload.itcode.strip().casefold(),
            error=exc.code,
        )
        raise _auth_http_error(exc) from exc
    await append_audit(
        action="auth.login.password",
        target_type="admin_user",
        target_id=itcode,
        actor=itcode,
        actor_role="admin",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
    )
    return _session_response(token, session)


@app.post("/api/v1/auth/gquan/login", response_model=LoginResponse)
async def gquan_login(payload: GQuanLoginRequest, request: Request) -> LoginResponse:
    try:
        itcode = normalize_itcode(payload.itcode)
        user = await find_user_by_itcode(itcode)
        if user is None or not user.is_active:
            raise AuthError("account_not_registered")
        await consume_verification_code(
            challenge_id=payload.challenge_id,
            itcode=itcode,
            purpose="gquan_login",
            code=payload.verification_code,
            settings=_settings(),
        )
        user.last_login_at = utcnow()
        await user.save()
        token, session = await create_session(user, _settings())
    except AuthError as exc:
        await _audit_auth_failure(
            request=request,
            action="auth.login.gquan",
            itcode=payload.itcode.strip().casefold(),
            error=exc.code,
        )
        raise _auth_http_error(exc) from exc
    await append_audit(
        action="auth.login.gquan",
        target_type="admin_user",
        target_id=itcode,
        actor=itcode,
        actor_role="admin",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
    )
    return _session_response(token, session)


@app.post("/api/v1/auth/logout", response_model=AuthActionResponse)
async def logout(request: Request, actor: str = Depends(require_management)) -> AuthActionResponse:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    await revoke_session_token(token)
    await append_audit(
        action="auth.logout",
        target_type="admin_user",
        target_id=actor,
        actor=actor,
        actor_role="admin",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
    )
    return AuthActionResponse()


@app.get("/api/v1/sites", response_model=list[SiteOut])
async def list_sites(_: str = Depends(require_management)) -> list[SiteOut]:
    return [_site_out(site) for site in await Site.find_all().sort(+Site.slug).to_list()]


@app.post("/api/v1/sites/{site_id}/shutdown", response_model=SiteOut)
async def set_shutdown(
    site_id: str, request: Request, _: str = Depends(require_management)
) -> SiteOut:
    body = await request.json()
    site = await Site.get(site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    before = {"shutdown": site.shutdown}
    site.shutdown = bool(body.get("shutdown", True))
    site.config_revision += 1
    await site.save()
    await append_audit(
        action="site.shutdown" if site.shutdown else "site.restore",
        target_type="site",
        target_id=site_id,
        actor=_actor(),
        before=before,
        after={"shutdown": site.shutdown},
    )
    return _site_out(site)


@app.get("/api/v1/nodes", response_model=list[NodeOut])
async def list_nodes(_: str = Depends(require_management)) -> list[NodeOut]:
    return [_node_out(node) for node in await Node.find_all().sort(+Node.name).to_list()]


@app.post("/api/v1/nodes", response_model=NodeCreateResponse, status_code=201)
async def create_node(
    payload: NodeCreate, _: str = Depends(require_management)
) -> NodeCreateResponse:
    if await Site.get(payload.site_id) is None:
        raise HTTPException(404, "site_not_found")
    if await Node.find_one(Node.agent_id == payload.agent_id):
        raise HTTPException(409, "agent_id_exists")
    token = secrets.token_urlsafe(32)
    node = Node(
        site_id=payload.site_id,
        name=payload.name,
        agent_id=payload.agent_id,
        agent_token_hash=_hash_secret(token),
        advertise_ip=payload.advertise_ip,
    )
    await node.insert()
    await append_audit(
        action="node.create",
        target_type="node",
        target_id=node.agent_id,
        actor=_actor(),
        after={
            "site_id": node.site_id,
            "name": node.name,
            "agent_id": node.agent_id,
            "token": token,
        },
    )
    return NodeCreateResponse(**_node_out(node).model_dump(), agent_token=token)


@app.get("/api/v1/sites/{site_id}/cidrs", response_model=list[CIDROut])
async def list_cidrs(site_id: str, _: str = Depends(require_management)) -> list[CIDROut]:
    entries = await SiteCIDR.find(SiteCIDR.site_id == site_id).sort(+SiteCIDR.cidr).to_list()
    return [
        CIDROut(
            id=_model_id(item),
            site_id=item.site_id,
            cidr=item.cidr,
            comment=item.comment,
            enabled=item.enabled,
        )
        for item in entries
    ]


@app.post("/api/v1/sites/{site_id}/cidrs", response_model=CIDROut, status_code=201)
async def add_cidr(
    site_id: str, payload: CIDRCreate, _: str = Depends(require_management)
) -> CIDROut:
    site = await Site.get(site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    try:
        cidr = normalize_cidr(payload.cidr)
    except ValueError as exc:
        raise HTTPException(422, "invalid_cidr") from exc
    if await SiteCIDR.find_one(SiteCIDR.site_id == site_id, SiteCIDR.cidr == cidr):
        raise HTTPException(409, "cidr_exists")
    entry = SiteCIDR(
        site_id=site_id,
        cidr=cidr,
        comment=payload.comment,
        enabled=payload.enabled,
        created_by=_actor(),
    )
    await entry.insert()
    site.config_revision += 1
    await site.save()
    await append_audit(
        action="cidr.create",
        target_type="site_cidr",
        target_id=_model_id(entry),
        actor=_actor(),
        after={"site_id": site_id, "cidr": cidr, "comment": payload.comment},
    )
    return CIDROut(
        id=_model_id(entry),
        site_id=entry.site_id,
        cidr=entry.cidr,
        comment=entry.comment,
        enabled=entry.enabled,
    )


@app.delete("/api/v1/sites/{site_id}/cidrs/{cidr_id}", status_code=204)
async def delete_cidr(site_id: str, cidr_id: str, _: str = Depends(require_management)) -> None:
    entry = await SiteCIDR.get(cidr_id)
    if entry is None or entry.site_id != site_id:
        raise HTTPException(404, "cidr_not_found")
    await entry.delete()
    site = await Site.get(site_id)
    if site:
        site.config_revision += 1
        await site.save()
    await append_audit(
        action="cidr.delete",
        target_type="site_cidr",
        target_id=cidr_id,
        actor=_actor(),
        before={"site_id": site_id, "cidr": entry.cidr},
    )


@app.post("/api/v1/cidrs/preview", response_model=CIDRPreviewResponse)
async def preview_cidr(
    payload: CIDRPreviewRequest, _: str = Depends(require_management)
) -> CIDRPreviewResponse:
    try:
        source_ip = normalize_source_ip(payload.source_ip)
    except ValueError as exc:
        raise HTTPException(422, "invalid_source_ip") from exc
    site = await Site.get(payload.site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    cidrs, _ = await effective_cidrs(payload.site_id)
    match = match_source_ip(source_ip, cidrs)
    if site.shutdown:
        reason = "shutdown"
    elif match is None:
        reason = "not_in_allowlist"
    else:
        reason = "allowed"
    return CIDRPreviewResponse(
        allowed=match is not None and not site.shutdown,
        matched_cidr=match,
        requires_auth=site.proxy_auth_required,
        reason=reason,
        effective_cidrs=cidrs,
    )


@app.get("/api/v1/exceptions", response_model=list[TravelExceptionOut])
async def list_exceptions(_: str = Depends(require_management)) -> list[TravelExceptionOut]:
    return [
        _exception_out(item)
        for item in await TravelException.find_all().sort(+TravelException.expires_at).to_list()
    ]


@app.post("/api/v1/exceptions", response_model=TravelExceptionOut, status_code=201)
async def create_exception(
    payload: TravelExceptionCreate, _: str = Depends(require_management)
) -> TravelExceptionOut:
    try:
        cidr = normalize_cidr(payload.cidr)
    except ValueError as exc:
        raise HTTPException(422, "invalid_exception") from exc
    if payload.expires_at <= utcnow():
        raise HTTPException(422, "exception_must_expire_in_future")
    item = TravelException(
        cidr=cidr,
        comment=payload.comment,
        owner=payload.owner,
        expires_at=payload.expires_at,
        enabled=payload.enabled,
        created_by=_actor(),
    )
    await item.insert()
    await _increment_all_site_revisions()
    await append_audit(
        action="exception.create",
        target_type="travel_exception",
        target_id=_model_id(item),
        actor=_actor(),
        after={"cidr": cidr, "expires_at": item.expires_at.isoformat(), "enabled": item.enabled},
    )
    return _exception_out(item)


@app.delete("/api/v1/exceptions/{exception_id}", status_code=204)
async def delete_exception(
    exception_id: str, _: str = Depends(require_management)
) -> None:
    item = await TravelException.get(exception_id)
    if item is None:
        raise HTTPException(404, "exception_not_found")
    await item.delete()
    await _increment_all_site_revisions()
    await append_audit(
        action="exception.delete",
        target_type="travel_exception",
        target_id=exception_id,
        actor=_actor(),
        before={"cidr": item.cidr, "expires_at": item.expires_at.isoformat()},
    )


@app.get("/api/v1/cross-site-allows", response_model=list[CrossSiteAllowOut])
async def list_cross_site_allows(_: str = Depends(require_management)) -> list[CrossSiteAllowOut]:
    return [
        _cross_site_out(item)
        for item in await CrossSiteAllow.find_all().sort(+CrossSiteAllow.updated_at).to_list()
    ]


@app.put("/api/v1/cross-site-allows", response_model=CrossSiteAllowOut)
@app.post("/api/v1/cross-site-allows", response_model=CrossSiteAllowOut, status_code=201)
async def set_cross_site(
    payload: CrossSiteAllowUpdate, _: str = Depends(require_management)
) -> CrossSiteAllowOut:
    from_site_id, to_site_id = payload.from_site_id, payload.to_site_id
    if (
        not from_site_id
        or not to_site_id
        or from_site_id == to_site_id
        or await Site.get(from_site_id) is None
        or await Site.get(to_site_id) is None
    ):
        raise HTTPException(422, "invalid_site_pair")
    relation = await CrossSiteAllow.find_one(
        CrossSiteAllow.from_site_id == from_site_id, CrossSiteAllow.to_site_id == to_site_id
    )
    if relation is None:
        relation = CrossSiteAllow(from_site_id=from_site_id, to_site_id=to_site_id)
    before = {"enabled": relation.enabled, "comment": relation.comment}
    relation.enabled = payload.enabled
    relation.comment = payload.comment
    relation.updated_at = utcnow()
    if relation.id:
        await relation.save()
    else:
        await relation.insert()
    await _increment_site_revisions([to_site_id])
    await append_audit(
        action="cross_site.update",
        target_type="cross_site_allow",
        target_id=_model_id(relation),
        actor=_actor(),
        before=before,
        after={"from_site_id": from_site_id, "to_site_id": to_site_id, "enabled": relation.enabled},
    )
    return _cross_site_out(relation)


@app.get("/api/v1/blacklist", response_model=list[DestinationBlacklistOut])
async def list_blacklist(_: str = Depends(require_management)) -> list[DestinationBlacklistOut]:
    entries = await DestinationBlacklist.find_all().sort(+DestinationBlacklist.pattern).to_list()
    return [
        _blacklist_out(item)
        for item in entries
    ]


@app.post("/api/v1/blacklist", response_model=DestinationBlacklistOut, status_code=201)
async def add_blacklist(
    payload: DestinationBlacklistCreate, _: str = Depends(require_management)
) -> DestinationBlacklistOut:
    pattern = payload.pattern.strip()
    try:
        if payload.kind == "cidr":
            pattern = normalize_cidr(pattern)
        elif payload.kind == "ip":
            pattern = normalize_source_ip(pattern)
        else:
            pattern = pattern.lower().rstrip(".")
    except ValueError as exc:
        raise HTTPException(422, "invalid_blacklist_pattern") from exc
    if not pattern:
        raise HTTPException(422, "invalid_blacklist_pattern")
    if await DestinationBlacklist.find_one(
        DestinationBlacklist.pattern == pattern,
        DestinationBlacklist.kind == payload.kind,
    ):
        raise HTTPException(409, "blacklist_entry_exists")
    item = DestinationBlacklist(
        pattern=pattern,
        kind=payload.kind,
        comment=payload.comment,
        enabled=payload.enabled,
    )
    await item.insert()
    await _increment_all_site_revisions()
    await append_audit(
        action="blacklist.create",
        target_type="destination_blacklist",
        target_id=_model_id(item),
        actor=_actor(),
        after={"pattern": item.pattern, "kind": item.kind, "enabled": item.enabled},
    )
    return _blacklist_out(item)


@app.delete("/api/v1/blacklist/{entry_id}", status_code=204)
async def delete_blacklist(entry_id: str, _: str = Depends(require_management)) -> None:
    item = await DestinationBlacklist.get(entry_id)
    if item is None:
        raise HTTPException(404, "blacklist_entry_not_found")
    await item.delete()
    await _increment_all_site_revisions()
    await append_audit(
        action="blacklist.delete",
        target_type="destination_blacklist",
        target_id=entry_id,
        actor=_actor(),
        before={"pattern": item.pattern, "kind": item.kind},
    )


async def _read_subscription_upload(upload: UploadFile, max_body_bytes: int) -> bytes:
    data = bytearray()
    while chunk := await upload.read(64 * 1024):
        if len(data) + len(chunk) > max_body_bytes:
            raise HTTPException(422, "subscription_response_too_large")
        data.extend(chunk)
    if not data:
        raise HTTPException(422, "subscription_response_empty")
    return bytes(data)


@app.get("/api/v1/subscriptions", response_model=SubscriptionCatalogOut)
async def list_subscriptions(_: str = Depends(require_management)) -> SubscriptionCatalogOut:
    sources = await SubscriptionSource.find_all().sort(+SubscriptionSource.name).to_list()
    versions = await SubscriptionVersion.find_all().sort(
        -SubscriptionVersion.created_at
    ).limit(500).to_list()
    bindings = await SiteSubscription.find_all().sort(+SiteSubscription.site_id).to_list()
    return SubscriptionCatalogOut(
        sources=[_subscription_source_out(item) for item in sources],
        versions=[_subscription_version_out(item) for item in versions],
        site_subscriptions=[_site_subscription_out(item) for item in bindings],
    )


@app.post(
    "/api/v1/subscriptions",
    response_model=SubscriptionRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_subscription_source(
    payload: SubscriptionSourceCreate,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> SubscriptionRefreshResponse:
    try:
        source_url = normalize_source_url(payload.url)
    except SubscriptionError as exc:
        raise HTTPException(422, exc.code) from exc
    if await SubscriptionSource.find_one(SubscriptionSource.name == payload.name.strip()):
        raise HTTPException(409, "subscription_source_name_exists")
    settings = _settings()
    source = SubscriptionSource(
        name=payload.name.strip(),
        url=source_url,
        fetch_interval_sec=payload.fetch_interval_sec,
        max_body_bytes=min(payload.max_body_bytes, settings.subscription_max_body_bytes),
        redirect_limit=payload.redirect_limit,
        created_by=_actor(),
    )
    await source.insert()
    request_id = request.headers.get("x-request-id", "") or secrets.token_hex(16)
    task, merged = await enqueue_refresh_task(
        source=source,
        created_by=_actor(),
        idempotency_key=idempotency_key_header,
        request_id=request_id,
    )
    await append_audit(
        action="subscription_source.create",
        target_type="subscription_source",
        target_id=_model_id(source),
        actor=_actor(),
        request_id=request_id,
        after={
            "name": source.name,
            "url_hint": source_url_hint(source.url),
            "fetch_interval_sec": source.fetch_interval_sec,
        },
    )
    return SubscriptionRefreshResponse(
        source=_subscription_source_out(source), task=_task_out(task), merged=merged
    )


@app.post(
    "/api/v1/subscriptions/upload",
    response_model=SubscriptionUploadResponse,
    status_code=201,
)
async def upload_subscription(
    name: str = Form(..., min_length=1, max_length=120),
    file: UploadFile = File(...),  # noqa: B008 - FastAPI declaration
    _: str = Depends(require_management),
) -> SubscriptionUploadResponse:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(422, "subscription_source_name_required")
    if await SubscriptionSource.find_one(SubscriptionSource.name == clean_name):
        raise HTTPException(409, "subscription_source_name_exists")
    settings = _settings()
    source = SubscriptionSource(
        name=clean_name,
        url="",
        max_body_bytes=settings.subscription_max_body_bytes,
        created_by=_actor(),
    )
    await source.insert()
    content = await _read_subscription_upload(file, source.max_body_bytes)
    try:
        version, _ = await record_uploaded_subscription(source, content, settings)
    except SubscriptionError as exc:
        # The source has no usable version yet, but it remains visible for
        # operators to diagnose or replace. Its raw body is never returned.
        raise HTTPException(422, exc.code) from exc
    await append_audit(
        action="subscription.upload",
        target_type="subscription_version",
        target_id=_model_id(version),
        actor=_actor(),
        after={
            "source_id": _model_id(source),
            "content_hash": version.content_hash,
            "parse_ok": version.parse_ok,
            "format": version.format,
        },
    )
    return SubscriptionUploadResponse(
        source=_subscription_source_out(source), version=_subscription_version_out(version)
    )


@app.post(
    "/api/v1/subscriptions/{source_id}/refresh",
    response_model=SubscriptionRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_subscription(
    source_id: str,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> SubscriptionRefreshResponse:
    source = await SubscriptionSource.get(source_id)
    if source is None:
        raise HTTPException(404, "subscription_source_not_found")
    if not source.url:
        raise HTTPException(409, "subscription_source_not_refreshable")
    request_id = request.headers.get("x-request-id", "") or secrets.token_hex(16)
    task, merged = await enqueue_refresh_task(
        source=source,
        created_by=_actor(),
        idempotency_key=idempotency_key_header,
        request_id=request_id,
    )
    return SubscriptionRefreshResponse(
        source=_subscription_source_out(source), task=_task_out(task), merged=merged
    )


@app.post("/api/v1/config/drafts", response_model=DraftOut, status_code=201)
async def create_draft(payload: DraftCreate, _: str = Depends(require_management)) -> DraftOut:
    site = await Site.get(payload.site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    nodes = await Node.find(Node.site_id == payload.site_id).to_list()
    selected = payload.node_ids or [_model_id(node) for node in nodes]
    if any(node_id not in {_model_id(node) for node in nodes} for node_id in selected):
        raise HTTPException(422, "node_not_in_site")
    cidrs, sources = await effective_cidrs(payload.site_id)
    validation = {"valid": True, "errors": [], "effective_cidrs": cidrs, "acl_sources": sources}
    risk = "high" if payload.diff.get("shutdown") else ("medium" if payload.diff else "low")
    draft = ConfigDraft(
        site_id=payload.site_id,
        node_ids=selected,
        source_revision=site.config_revision,
        diff=payload.diff,
        validation=validation,
        risk_level=risk,
        created_by=_actor(),
        expires_at=utcnow() + timedelta(hours=24),
    )
    await draft.insert()
    await append_audit(
        action="config_draft.create",
        target_type="config_draft",
        target_id=_model_id(draft),
        actor=_actor(),
        after={"site_id": payload.site_id, "risk_level": risk, "diff": payload.diff},
    )
    return _draft_out(draft)


@app.get("/api/v1/config/drafts", response_model=list[DraftOut])
async def list_drafts(_: str = Depends(require_management)) -> list[DraftOut]:
    return [
        _draft_out(item)
        for item in await ConfigDraft.find_all().sort(-ConfigDraft.created_at).to_list()
    ]


@app.get("/api/v1/config/drafts/{draft_id}", response_model=DraftOut)
async def get_draft(draft_id: str, _: str = Depends(require_management)) -> DraftOut:
    draft = await ConfigDraft.get(draft_id)
    if draft is None:
        raise HTTPException(404, "draft_not_found")
    return _draft_out(draft)


async def _create_release_from_draft(
    *,
    draft: ConfigDraft,
    site: Site,
    requested_node_ids: list[str],
    expected_current_version: int | None,
    idempotency_key: str,
    request_id: str,
    actor: str,
) -> tuple[ConfigRelease, bool]:
    existing_task = await Task.find_one(Task.idempotency_key == idempotency_key)
    if existing_task:
        existing_release = await ConfigRelease.find_one(
            ConfigRelease.task_id == existing_task.task_id
        )
        if existing_release:
            return existing_release, True
    if draft.expires_at <= utcnow() or draft.status in {"expired", "released"}:
        raise HTTPException(409, "draft_expired_or_used")
    if str(site.id) != draft.site_id:
        raise HTTPException(409, "site_mismatch")
    nodes = await Node.find(Node.site_id == draft.site_id).to_list()
    selected_document_ids = requested_node_ids or draft.node_ids or [
        _model_id(node) for node in nodes
    ]
    selected = [node for node in nodes if _model_id(node) in set(selected_document_ids)]
    if len(selected) != len(selected_document_ids) or not selected:
        raise HTTPException(409, "invalid_release_nodes")
    # DesiredRelease and agent ACKs use the stable agent identity. Mongo
    # ObjectIds are an implementation detail and must not be used for
    # cross-component reconciliation.
    selected_ids = [node.agent_id for node in selected]
    active_release = await ConfigRelease.find_one(
        {
            "node_ids": {"$in": selected_ids},
            "status": {"$in": ["queued", "applying", "health_check", "rolling_back"]},
        }
    )
    if active_release:
        raise HTTPException(409, "release_in_progress")
    current = await latest_release(draft.site_id)
    current_version = current.desired_version if current else 0
    if expected_current_version is not None and expected_current_version != current_version:
        raise HTTPException(
            status_code=409,
            detail={"code": "version_conflict", "current_version": current_version},
        )
    try:
        release_id, desired_items = await create_desired_release(
            site=site, nodes=selected, settings=_settings(), created_by=actor
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    desired_first = desired_items[0]
    task, _ = await create_task(
        task_type="config.publish",
        target_type="site",
        target_id=draft.site_id,
        payload={"release_id": release_id, "node_ids": selected_ids},
        idempotency_key=idempotency_key,
        created_by=actor,
        request_id=request_id,
    )
    release = ConfigRelease(
        release_id=release_id,
        site_id=draft.site_id,
        node_ids=selected_ids,
        desired_release_id=_model_id(desired_first),
        previous_release_id=desired_first.previous_release_id,
        task_id=task.task_id,
        status="applying",
        stage="applying",
        progress=10,
        created_by=actor,
        started_at=utcnow(),
    )
    await release.insert()
    draft.status = "released"
    await draft.save()
    await append_audit(
        action="config_release.create",
        target_type="config_release",
        target_id=release_id,
        actor=actor,
        request_id=request_id,
        after={
            "site_id": draft.site_id,
            "node_ids": selected_ids,
            "desired_version": desired_first.desired_version,
        },
    )
    return release, False


@app.post("/api/v1/config/releases", response_model=ReleaseOut, status_code=202)
async def create_release(
    payload: ReleaseCreate,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> ReleaseOut:
    draft = await ConfigDraft.get(payload.draft_id)
    if draft is None:
        raise HTTPException(404, "draft_not_found")
    selected_key = ",".join(sorted(payload.node_ids or draft.node_ids))
    idem = (
        payload.idempotency_key
        or idempotency_key_header
        or f"config.publish:{draft.id}:{selected_key}:{draft.source_revision}"
    )
    site = await Site.get(payload.site_id or draft.site_id)
    if site is None:
        raise HTTPException(409, "site_mismatch")
    request_id = request.headers.get("x-request-id", "") or secrets.token_hex(16)
    release, _ = await _create_release_from_draft(
        draft=draft,
        site=site,
        requested_node_ids=payload.node_ids,
        expected_current_version=payload.expected_current_version,
        idempotency_key=idem,
        request_id=request_id,
        actor=_actor(),
    )
    return _release_out(release)


async def _publish_subscription_version(
    *,
    version: SubscriptionVersion,
    sites: list[Site],
    request_id: str,
    actor: str,
    note: str,
    idempotency_key: str | None = None,
    operation: str = "publish",
) -> list[ConfigRelease]:
    if not version.parse_ok:
        raise HTTPException(409, "subscription_version_not_publishable")
    # Check all deployment targets first. This avoids changing site bindings
    # for a later target after an earlier target has already been rejected for
    # a concurrent release.
    nodes_by_site: dict[str, list[Node]] = {}
    task_keys: dict[str, str] = {}
    existing_releases: dict[str, ConfigRelease] = {}
    for site in sites:
        site_id = _model_id(site)
        key_suffix = (
            hashlib.sha256(idempotency_key.strip().encode("utf-8")).hexdigest()
            if idempotency_key and idempotency_key.strip()
            else secrets.token_hex(16)
        )
        task_key = f"subscription.{operation}:{version.id}:{site_id}:{key_suffix}"
        task_keys[site_id] = task_key
        if idempotency_key and idempotency_key.strip():
            existing_task = await Task.find_one(Task.idempotency_key == task_key)
            if existing_task is not None:
                existing_release = await ConfigRelease.find_one(
                    ConfigRelease.task_id == existing_task.task_id
                )
                if existing_release is None:
                    raise HTTPException(409, "subscription_publish_idempotency_incomplete")
                existing_releases[site_id] = existing_release
                continue
        nodes = await Node.find(Node.site_id == site_id).to_list()
        nodes_by_site[site_id] = nodes
        agent_ids = [node.agent_id for node in nodes]
        if not agent_ids:
            continue
        active = await ConfigRelease.find_one(
            {
                "node_ids": {"$in": agent_ids},
                "status": {"$in": ["queued", "applying", "health_check", "rolling_back"]},
            }
        )
        if active:
            raise HTTPException(409, "release_in_progress")

    releases: list[ConfigRelease] = []
    created_releases: list[ConfigRelease] = []
    changed_site_ids: list[str] = []
    for site in sites:
        site_id = _model_id(site)
        if existing_release := existing_releases.get(site_id):
            releases.append(existing_release)
            continue
        binding = await SiteSubscription.find_one(SiteSubscription.site_id == site_id)
        previous_version_id = binding.subscription_version_id if binding else None
        previous_rollback_version_id = binding.previous_subscription_version_id if binding else None
        previous_source_id = binding.source_id if binding else None
        binding_created = binding is None
        binding_changed = False
        if binding is None:
            binding = SiteSubscription(
                site_id=site_id,
                source_id=version.source_id,
                subscription_version_id=_model_id(version),
                previous_subscription_version_id=None,
                updated_by=actor,
            )
            await binding.insert()
            binding_changed = True
        elif (
            previous_version_id != _model_id(version)
            or binding.source_id != version.source_id
        ):
            if previous_version_id != _model_id(version):
                binding.previous_subscription_version_id = previous_version_id
            binding.subscription_version_id = _model_id(version)
            binding.source_id = version.source_id
            binding.updated_by = actor
            binding.updated_at = utcnow()
            await binding.save()
            binding_changed = True
        if binding_changed:
            changed_site_ids.append(site_id)

        nodes = nodes_by_site[site_id]
        if not nodes:
            continue
        cidrs, sources = await effective_cidrs(site_id)
        draft = ConfigDraft(
            site_id=site_id,
            node_ids=[_model_id(node) for node in nodes],
            source_revision=site.config_revision,
            diff={
                "subscription": {
                    "from_version_id": previous_version_id,
                    "to_version_id": _model_id(version),
                    "content_hash": version.content_hash,
                    "format": version.format,
                    "node_count": version.node_count,
                },
                "note": note,
            },
            validation={
                "valid": True,
                "errors": [],
                "effective_cidrs": cidrs,
                "acl_sources": sources,
                "subscription": {
                    "parse_ok": version.parse_ok,
                    "content_hash": version.content_hash,
                    "format": version.format,
                },
            },
            risk_level="medium",
            status="draft",
            created_by=actor,
            expires_at=utcnow() + timedelta(hours=24),
        )
        await draft.insert()
        try:
            release, _ = await _create_release_from_draft(
                draft=draft,
                site=site,
                requested_node_ids=draft.node_ids,
                expected_current_version=None,
                idempotency_key=task_keys[site_id],
                request_id=request_id,
                actor=actor,
            )
        except Exception:
            # The selection was changed before the Desired Bundle was built;
            # restore it when a local release cannot be created.
            if binding_created:
                await binding.delete()
            elif binding_changed:
                binding.subscription_version_id = previous_version_id
                binding.previous_subscription_version_id = previous_rollback_version_id
                binding.source_id = previous_source_id or binding.source_id
                binding.updated_at = utcnow()
                await binding.save()
            raise
        releases.append(release)
        created_releases.append(release)

    if created_releases or changed_site_ids:
        version.published = True
        await version.save()
        await append_audit(
            action=f"subscription.{operation}",
            target_type="subscription_version",
            target_id=_model_id(version),
            actor=actor,
            request_id=request_id,
            after={
                "content_hash": version.content_hash,
                "site_ids": list(
                    dict.fromkeys(
                        [release.site_id for release in created_releases] + changed_site_ids
                    )
                ),
                "release_ids": [release.release_id for release in created_releases],
            },
        )
    return releases


@app.post(
    "/api/v1/subscriptions/{source_id}/versions/{version_id}/publish",
    response_model=SubscriptionPublishOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def publish_subscription_version(
    source_id: str,
    version_id: str,
    payload: SubscriptionPublishRequest,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> SubscriptionPublishOut:
    version = await SubscriptionVersion.get(version_id)
    if version is None or version.source_id != source_id:
        raise HTTPException(404, "subscription_version_not_found")
    requested_ids = list(dict.fromkeys(payload.site_ids))
    if requested_ids:
        sites = [await Site.get(site_id) for site_id in requested_ids]
        if any(site is None for site in sites):
            raise HTTPException(404, "site_not_found")
        target_sites = [site for site in sites if site is not None]
    else:
        target_sites = await Site.find_all().sort(+Site.slug).to_list()
    if not target_sites:
        raise HTTPException(409, "subscription_publish_no_sites")
    request_id = request.headers.get("x-request-id", "") or secrets.token_hex(16)
    releases = await _publish_subscription_version(
        version=version,
        sites=target_sites,
        request_id=request_id,
        actor=_actor(),
        note=payload.note,
        idempotency_key=idempotency_key_header,
    )
    return SubscriptionPublishOut(
        version=_subscription_version_out(version),
        releases=[_release_out(item) for item in releases],
    )


@app.post(
    "/api/v1/subscriptions/sites/{site_id}/rollback",
    response_model=SubscriptionPublishOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rollback_site_subscription(
    site_id: str,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> SubscriptionPublishOut:
    site = await Site.get(site_id)
    binding = await SiteSubscription.find_one(SiteSubscription.site_id == site_id)
    if site is None or binding is None or not binding.previous_subscription_version_id:
        raise HTTPException(409, "subscription_rollback_not_available")
    version = await SubscriptionVersion.get(binding.previous_subscription_version_id)
    if version is None or not version.parse_ok:
        raise HTTPException(409, "subscription_rollback_not_available")
    request_id = request.headers.get("x-request-id", "") or secrets.token_hex(16)
    releases = await _publish_subscription_version(
        version=version,
        sites=[site],
        request_id=request_id,
        actor=_actor(),
        note="Subscription rollback",
        idempotency_key=idempotency_key_header,
        operation="rollback",
    )
    return SubscriptionPublishOut(
        version=_subscription_version_out(version),
        releases=[_release_out(item) for item in releases],
    )


@app.get("/api/v1/config/releases/{release_id}", response_model=ReleaseOut)
async def get_release(release_id: str, _: str = Depends(require_management)) -> ReleaseOut:
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    return _release_out(release)


@app.get("/api/v1/config/releases", response_model=list[ReleaseOut])
async def list_releases(
    site_id: str | None = None, limit: int = 100, _: str = Depends(require_management)
) -> list[ReleaseOut]:
    safe_limit = min(max(limit, 1), 250)
    query: dict[str, Any] = {"site_id": site_id} if site_id else {}
    releases = await (
        ConfigRelease.find(query).sort(-ConfigRelease.created_at).limit(safe_limit).to_list()
    )
    return [_release_out(item) for item in releases]


@app.get("/api/v1/config/releases/{release_id}/acks", response_model=list[AgentAckOut])
async def list_release_acks(
    release_id: str, _: str = Depends(require_management)
) -> list[AgentAckOut]:
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    acks = await AgentAckDocument.find(
        AgentAckDocument.release_id == release_id
    ).sort(+AgentAckDocument.node_id).to_list()
    return [_ack_out(item) for item in acks]


@app.get("/api/v1/tasks", response_model=list[TaskOut])
async def list_tasks(
    limit: int = 100, _: str = Depends(require_management)
) -> list[TaskOut]:
    safe_limit = min(max(limit, 1), 250)
    tasks = await Task.find_all().sort(-Task.created_at).limit(safe_limit).to_list()
    return [_task_out(item) for item in tasks]


@app.get("/api/v1/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, _: str = Depends(require_management)) -> TaskOut:
    task = await Task.find_one(Task.task_id == task_id)
    if task is None:
        raise HTTPException(404, "task_not_found")
    return _task_out(task)


@app.post("/api/v1/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: str, _: str = Depends(require_management)) -> TaskOut:
    task = await Task.find_one(Task.task_id == task_id)
    if task is None:
        raise HTTPException(404, "task_not_found")
    if task.status == "queued":
        task.status = "cancelled"
        task.active = False
        task.stage = "cancelled"
        task.progress_message = "Cancellation acknowledged"
        task.locked_by = ""
        task.lease_expires_at = None
        task.finished_at = utcnow()
    elif task.status == "running":
        task.cancel_requested = True
        task.status = "cancel_requested"
    await task.save()
    await append_audit(
        action="task.cancel",
        target_type="task",
        target_id=task_id,
        actor=_actor(),
        after={"status": task.status},
    )
    return _task_out(task)


@app.get("/agent/v1/desired", response_model=DesiredResponse)
async def agent_desired(  # noqa: B008 - FastAPI dependency declaration
    request: Request,
    node: Node = Depends(require_agent),  # noqa: B008
) -> DesiredResponse:
    try:
        supplied_version = int(request.query_params.get("applied_version", "0"))
    except ValueError:
        supplied_version = 0
    supplied_hash = request.query_params.get("applied_hash", "")
    desired = await latest_release(node.site_id, node.agent_id)
    if desired is None:
        return DesiredResponse(desired_stale=False, bundle=None)
    stale = desired.desired_version > supplied_version or desired.bundle_hash != supplied_hash
    return DesiredResponse(
        desired_stale=stale, release_id=desired.release_id, bundle=desired.bundle if stale else None
    )


@app.get("/agent/v1/blobs/{content_hash}")
async def agent_subscription_blob(
    content_hash: str,
    node: Node = Depends(require_agent),  # noqa: B008 - FastAPI dependency declaration
) -> Response:
    # A node may only retrieve the blob referenced by its own current Desired
    # Bundle. This keeps a valid node token from becoming a subscription dump.
    desired = await latest_release(node.site_id, node.agent_id)
    subscription = desired.bundle.get("subscription") if desired else None
    if not isinstance(subscription, dict) or subscription.get("hash") != content_hash:
        raise HTTPException(404, "subscription_blob_not_assigned")
    version = await SubscriptionVersion.find_one(SubscriptionVersion.content_hash == content_hash)
    if version is None or not version.parse_ok:
        raise HTTPException(404, "subscription_blob_not_found")
    return Response(
        content=version.content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-SHA256": version.content_hash,
        },
    )


@app.post("/agent/v1/heartbeat")
async def agent_heartbeat(
    payload: AgentHeartbeat,
    node: Node = Depends(require_agent),  # noqa: B008 - FastAPI dependency declaration
) -> dict[str, Any]:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")
    previous = await HeartbeatLatest.find_one(HeartbeatLatest.node_id == node.agent_id)
    if previous and payload.sequence <= int(previous.payload.get("sequence", -1)):
        return {"accepted": False, "duplicate": True}
    received = utcnow()
    heartbeat_payload = payload.model_dump(mode="json")
    desired = await latest_release(node.site_id, node.agent_id)
    desired_version = desired.desired_version if desired else 0
    if previous is None:
        await HeartbeatLatest(
            node_id=node.agent_id, payload=heartbeat_payload, received_at=received
        ).insert()
    else:
        previous.payload = heartbeat_payload
        previous.received_at = received
        await previous.save()
    await HeartbeatSample(
        node_id=node.agent_id, payload=heartbeat_payload, received_at=received
    ).insert()
    node.monitor_version = payload.monitor_version
    node.singbox_version = payload.singbox_version
    node.last_seen_at = received
    # The monitor reports facts about its applied state; desired state is
    # authoritative in MongoDB and cannot be advanced by an agent heartbeat.
    node.desired_version = desired_version
    node.applied_version = payload.applied_version
    node.applied_hash = payload.applied_hash
    node.liveness_status = payload.liveness_status or "online"
    node.config_status = payload.config_status
    node.service_status = payload.service_status
    node.subscription_status = payload.subscription_status
    node.last_error = _safe_error(payload.last_error)
    node.last_error_at = received if node.last_error else node.last_error_at
    await node.save()
    return {
        "accepted": True,
        "desired_stale": bool(
            desired
            and (
                desired.desired_version > payload.applied_version
                or desired.bundle_hash != payload.applied_hash
            )
        ),
    }


@app.post("/agent/v1/ack")
async def agent_ack(  # noqa: B008 - FastAPI dependency declaration
    payload: AgentAck,
    node: Node = Depends(require_agent),  # noqa: B008
) -> dict[str, Any]:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")
    previous = await AgentAckDocument.find_one(
        AgentAckDocument.node_id == node.agent_id, sort=[("sequence", -1)]
    )
    if previous and payload.sequence <= previous.sequence:
        return {"accepted": False, "duplicate": True}
    release = await ConfigRelease.find_one(ConfigRelease.release_id == payload.release_id)
    if release is None or node.agent_id not in set(release.node_ids):
        raise HTTPException(409, "release_not_assigned_to_node")
    desired = await DesiredRelease.find_one(
        DesiredRelease.release_id == payload.release_id,
        DesiredRelease.node_id == node.agent_id,
    )
    if desired is None:
        raise HTTPException(409, "desired_release_not_found")
    if (
        payload.desired_version != desired.desired_version
        or payload.bundle_hash != desired.bundle_hash
    ):
        raise HTTPException(409, "ack_bundle_mismatch")
    ack_data = payload.model_dump()
    ack_data["error_message"] = _safe_error(payload.error_message)
    ack = AgentAckDocument(**ack_data)
    await ack.insert()
    node.applied_version = payload.applied_version
    node.applied_hash = payload.applied_hash
    current_desired = await latest_release(node.site_id, node.agent_id)
    node.desired_version = current_desired.desired_version if current_desired else 0
    node.config_status = (
        "in_sync"
        if payload.ok and payload.health_ok
        else (
            "rollback_failed"
            if payload.rollback_attempted and not payload.rollback_ok
            else "failed"
        )
    )
    node.service_status = (
        "healthy"
        if payload.health_ok or (payload.rollback_attempted and payload.rollback_ok)
        else "unhealthy"
    )
    node.last_error = _safe_error(payload.error_message)
    node.last_error_at = utcnow() if node.last_error else node.last_error_at
    node.last_successful_reload_at = (
        utcnow() if payload.ok and payload.health_ok else node.last_successful_reload_at
    )
    await node.save()
    all_acks = await AgentAckDocument.find(
        AgentAckDocument.release_id == payload.release_id
    ).to_list()
    expected = set(release.node_ids)
    received_nodes = {item.node_id for item in all_acks}
    if expected.issubset(received_nodes):
        release.status = (
            "succeeded"
            if all(item.ok and item.health_ok for item in all_acks if item.node_id in expected)
            else "failed"
        )
        release.stage = "succeeded" if release.status == "succeeded" else "failed"
        release.progress = 100
        release.finished_at = utcnow()
        release.error = "" if release.status == "succeeded" else "one_or_more_nodes_failed"
        release.rollback_reason = (
            ""
            if release.status == "succeeded"
            else next(
                (
                    item.error_message
                    for item in all_acks
                    if item.node_id in expected and item.rollback_attempted
                ),
                "",
            )
        )
        await release.save()
        if release.task_id:
            task = await Task.find_one(Task.task_id == release.task_id)
            if task:
                task.status = release.status
                task.stage = release.stage
                task.progress = release.progress
                task.result = {"release_id": release.release_id, "nodes": list(received_nodes)}
                task.finished_at = release.finished_at
                await task.save()
    return {"accepted": True}


@app.get("/api/v1/audit/verify")
async def audit_verify(_: str = Depends(require_management)) -> dict[str, Any]:
    valid, error, count = await verify_audit_chain()
    return {"valid": valid, "error": error, "event_count": count}


@app.get("/api/v1/audit", response_model=list[AuditEventOut])
async def list_audit(
    limit: int = 200, _: str = Depends(require_management)
) -> list[AuditEventOut]:
    safe_limit = min(max(limit, 1), 500)
    events = await AuditEvent.find_all().sort(-AuditEvent.at).limit(safe_limit).to_list()
    return [_audit_out(item) for item in events]


@app.get("/api/v1/overview")
async def overview(_: str = Depends(require_management)) -> dict[str, Any]:
    nodes = await Node.find_all().to_list()
    return {
        "sites": len(await Site.find_all().to_list()),
        "nodes": len(nodes),
        "online_nodes": sum(node.liveness_status == "online" for node in nodes),
        "in_sync_nodes": sum(node.config_status == "in_sync" for node in nodes),
        "drifted_nodes": sum(
            node.config_status in {"drift", "failed", "rollback_failed"} for node in nodes
        ),
        "connections": 0,
        "http_only": True,
    }


@app.get("/api/v1/access/linux-setup.sh", response_class=PlainTextResponse)
async def linux_setup(_: str = Depends(require_management)) -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
PROXY_HOST=proxy.corp.internal
PROXY_PORT=80
export http_proxy=\"http://${PROXY_HOST}:${PROXY_PORT}\"
export https_proxy=\"http://${PROXY_HOST}:${PROXY_PORT}\"
export no_proxy=\"localhost,127.0.0.1,.corp.internal\"
printf 'Proxy configured for %s:%s (HTTP CONNECT only).\\n' \"$PROXY_HOST\" \"$PROXY_PORT\"
printf '%s\\n' 'HTTPS transport is intentionally disabled.'
"""


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host=_settings().host, port=_settings().port, reload=False)
