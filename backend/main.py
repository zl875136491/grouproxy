"""Grouproxy control plane entry point.

The first implementation keeps the HTTP API deliberately small, but the
contracts are explicit: browser management endpoints live under ``/api/v1``
and node endpoints live under ``/agent/v1``.  The monitor owns all host-side
changes; this service only computes and records desired state.
"""

import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.db import Database
from app.models import (
    AdminUser,
    ConfigDraft,
    ConfigRelease,
    CrossSiteAllow,
    DesiredRelease,
    HeartbeatLatest,
    HeartbeatSample,
    Node,
    Site,
    SiteCIDR,
    Task,
    TravelException,
    utcnow,
)
from app.models import (
    AgentAck as AgentAckDocument,
)
from app.schemas import (
    AgentAck,
    AgentHeartbeat,
    CIDRCreate,
    CIDROut,
    CIDRPreviewRequest,
    CIDRPreviewResponse,
    DesiredResponse,
    DraftCreate,
    DraftOut,
    LoginRequest,
    LoginResponse,
    NodeCreate,
    NodeCreateResponse,
    NodeOut,
    ReleaseCreate,
    ReleaseOut,
    SiteOut,
    TaskOut,
)
from app.services.audit import append_audit, verify_audit_chain
from app.services.bundles import create_desired_release, latest_release
from app.services.cidr import effective_cidrs, match_source_ip, normalize_cidr, normalize_source_ip
from app.services.tasks import create_task

DEFAULT_SITES = [
    ("north", "North Region"),
    ("east", "East Region"),
    ("south", "South Region"),
    ("west", "West Region"),
    ("central", "Central Region"),
]


def _settings() -> Settings:
    return get_settings()


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
    )


async def seed_defaults(settings: Settings) -> None:
    if settings.seed_default_sites:
        for slug, name in DEFAULT_SITES:
            if not await Site.find_one(Site.slug == slug):
                await Site(
                    slug=slug, name=name, dns_note="Configure local DNS to this site node"
                ).insert()
    if not await AdminUser.find_one(AdminUser.username == settings.admin_username):
        await AdminUser(
            username=settings.admin_username,
            password_hash=_hash_secret(settings.admin_password),
            auth_source="local",
        ).insert()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _settings()
    database = Database(settings)
    await database.connect()
    await seed_defaults(settings)
    app.state.database = database
    yield
    await database.close()


app = FastAPI(title="Grouproxy Control Plane", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
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
    if not token or not hmac.compare_digest(token, settings.management_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="management_auth_required"
        )
    return settings.admin_username


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


@app.post("/api/v1/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    settings = _settings()
    if not hmac.compare_digest(
        payload.username, settings.admin_username
    ) or not hmac.compare_digest(
        _hash_secret(payload.password), _hash_secret(settings.admin_password)
    ):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    return LoginResponse(access_token=settings.management_token)


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
        actor=_settings().admin_username,
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
        actor=_settings().admin_username,
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
    entry = SiteCIDR(
        site_id=site_id,
        cidr=cidr,
        comment=payload.comment,
        enabled=payload.enabled,
        created_by=_settings().admin_username,
    )
    await entry.insert()
    site.config_revision += 1
    await site.save()
    await append_audit(
        action="cidr.create",
        target_type="site_cidr",
        target_id=_model_id(entry),
        actor=_settings().admin_username,
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
        actor=_settings().admin_username,
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


@app.post("/api/v1/exceptions", status_code=201)
async def create_exception(
    request: Request, _: str = Depends(require_management)
) -> dict[str, Any]:
    body = await request.json()
    try:
        cidr = normalize_cidr(str(body["cidr"]))
        expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "invalid_exception") from exc
    if expires_at <= utcnow():
        raise HTTPException(422, "exception_must_expire_in_future")
    item = TravelException(
        cidr=cidr,
        comment=str(body.get("comment", "")),
        owner=str(body.get("owner", "")),
        expires_at=expires_at,
        created_by=_settings().admin_username,
    )
    await item.insert()
    await append_audit(
        action="exception.create",
        target_type="travel_exception",
        target_id=_model_id(item),
        actor=_settings().admin_username,
        after={"cidr": cidr, "expires_at": expires_at.isoformat()},
    )
    return {
        "id": _model_id(item),
        "cidr": item.cidr,
        "expires_at": item.expires_at,
        "enabled": item.enabled,
    }


@app.post("/api/v1/cross-site-allows", status_code=201)
async def set_cross_site(request: Request, _: str = Depends(require_management)) -> dict[str, Any]:
    body = await request.json()
    from_site_id, to_site_id = str(body.get("from_site_id", "")), str(body.get("to_site_id", ""))
    if (
        not from_site_id
        or not to_site_id
        or await Site.get(from_site_id) is None
        or await Site.get(to_site_id) is None
    ):
        raise HTTPException(422, "invalid_site_pair")
    relation = await CrossSiteAllow.find_one(
        CrossSiteAllow.from_site_id == from_site_id, CrossSiteAllow.to_site_id == to_site_id
    )
    if relation is None:
        relation = CrossSiteAllow(from_site_id=from_site_id, to_site_id=to_site_id)
    relation.enabled = bool(body.get("enabled", False))
    relation.comment = str(body.get("comment", ""))
    relation.updated_at = utcnow()
    if relation.id:
        await relation.save()
    else:
        await relation.insert()
    await append_audit(
        action="cross_site.update",
        target_type="cross_site_allow",
        target_id=_model_id(relation),
        actor=_settings().admin_username,
        after={"from_site_id": from_site_id, "to_site_id": to_site_id, "enabled": relation.enabled},
    )
    return {
        "id": _model_id(relation),
        "from_site_id": from_site_id,
        "to_site_id": to_site_id,
        "enabled": relation.enabled,
    }


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
        created_by=_settings().admin_username,
        expires_at=utcnow() + timedelta(hours=24),
    )
    await draft.insert()
    await append_audit(
        action="config_draft.create",
        target_type="config_draft",
        target_id=_model_id(draft),
        actor=_settings().admin_username,
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
    existing_task = await Task.find_one(Task.idempotency_key == idem)
    if existing_task:
        existing_release = await ConfigRelease.find_one(
            ConfigRelease.task_id == existing_task.task_id
        )
        if existing_release:
            return _release_out(existing_release)
    if draft.expires_at <= utcnow() or draft.status in {"expired", "released"}:
        raise HTTPException(409, "draft_expired_or_used")
    site = await Site.get(payload.site_id or draft.site_id)
    if site is None or str(site.id) != draft.site_id:
        raise HTTPException(409, "site_mismatch")
    nodes = await Node.find(Node.site_id == draft.site_id).to_list()
    selected_document_ids = (
        payload.node_ids or draft.node_ids or [_model_id(node) for node in nodes]
    )
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
    if (
        payload.expected_current_version is not None
        and payload.expected_current_version != current_version
    ):
        raise HTTPException(
            status_code=409, detail={"code": "version_conflict", "current_version": current_version}
        )
    request_id = request.headers.get("x-request-id", "") or secrets.token_hex(16)
    release_id, desired_items = await create_desired_release(
        site=site, nodes=selected, settings=_settings(), created_by=_settings().admin_username
    )
    desired_first = desired_items[0]
    task, _ = await create_task(
        task_type="config.publish",
        target_type="site",
        target_id=draft.site_id,
        payload={"release_id": release_id, "node_ids": selected_ids},
        idempotency_key=idem,
        created_by=_settings().admin_username,
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
        created_by=_settings().admin_username,
        started_at=utcnow(),
    )
    await release.insert()
    draft.status = "released"
    await draft.save()
    await append_audit(
        action="config_release.create",
        target_type="config_release",
        target_id=release_id,
        actor=_settings().admin_username,
        request_id=request_id,
        after={
            "site_id": draft.site_id,
            "node_ids": selected_ids,
            "desired_version": desired_first.desired_version,
        },
    )
    return _release_out(release)


@app.get("/api/v1/config/releases/{release_id}", response_model=ReleaseOut)
async def get_release(release_id: str, _: str = Depends(require_management)) -> ReleaseOut:
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    return _release_out(release)


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
        task.finished_at = utcnow()
    elif task.status == "running":
        task.cancel_requested = True
        task.status = "cancel_requested"
    await task.save()
    await append_audit(
        action="task.cancel",
        target_type="task",
        target_id=task_id,
        actor=_settings().admin_username,
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
