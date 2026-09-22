"""Grouproxy control plane entry point.

The first implementation keeps the HTTP API deliberately small, but the
contracts are explicit: browser management endpoints live under ``/api/v1``
and node endpoints live under ``/agent/v1``.  The monitor owns all host-side
changes; this service only computes and records desired state.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from beanie.exceptions import CollectionWasNotInitialized
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import PROXY_LISTEN_PORT, ROOT_ITCODE, Settings, get_settings, is_management_role
from app.db import Database
from app.models import (
    AccessLog,
    AdminUser,
    Alert,
    AuditEvent,
    BackupRecord,
    ConfigDraft,
    ConfigRelease,
    ConnectionSnapshot,
    DesiredRelease,
    HeartbeatLatest,
    HeartbeatSample,
    ManagementSession,
    Node,
    ProbeCircuit,
    ProbeHistory,
    ProxyConfigSnapshot,
    ProxyQualitySample,
    ServiceQualityDefinition,
    Site,
    SiteSubscription,
    SourceBlacklist,
    SubscriptionSource,
    SubscriptionVersion,
    SystemSettings,
    Task,
    TelemetryBatch,
    TelemetryCursor,
    utcnow,
)
from app.models import (
    AgentAck as AgentAckDocument,
)
from app.schemas import (
    AccessConfigOut,
    AccessLogOut,
    AgentAck,
    AgentAckOut,
    AgentConnectionBatch,
    AgentHeartbeat,
    AgentHeartbeatResponse,
    AgentLogBatch,
    AgentProbeBatch,
    AgentProxyConfigBatch,
    AlertOut,
    AuditEventOut,
    AuthActionResponse,
    BackupCreateRequest,
    BackupCreateResponse,
    BackupRecordOut,
    BackupRestoreRequest,
    BackupRestoreResponse,
    ConnectionHistoryResponse,
    ConnectionSnapshotOut,
    DesiredResponse,
    DraftCreate,
    DraftOut,
    EmployeeOut,
    GQuanLoginRequest,
    LoginRequest,
    LoginResponse,
    NodeCreate,
    NodeCreateResponse,
    NodeNameUpdate,
    NodeOut,
    PasswordChangeRequest,
    ProbeCircuitOut,
    ProbeHistoryOut,
    ProbeRequestForAgent,
    ProbeTaskRequest,
    ProxyConfigSnapshotOut,
    ProxyEndpointSnapshot,
    ProxyGroupSnapshot,
    ProxySelectionRequest,
    RegistrationRequest,
    ReleaseCreate,
    ReleaseDetailOut,
    ReleaseEventOut,
    ReleaseOut,
    RoleUpdate,
    ServiceQualityResponse,
    SiteNameUpdate,
    SiteOut,
    SiteSubscriptionOut,
    SourceBlacklistCreate,
    SourceBlacklistDistributionOut,
    SourceBlacklistMutationOut,
    SourceBlacklistOut,
    SourceBlacklistPreviewRequest,
    SourceBlacklistPreviewResponse,
    SubscriptionCatalogOut,
    SubscriptionPublishOut,
    SubscriptionPublishRequest,
    SubscriptionRefreshResponse,
    SubscriptionSingleNodeCreate,
    SubscriptionSourceCreate,
    SubscriptionSourceOut,
    SubscriptionUploadResponse,
    SubscriptionVersionContentOut,
    SubscriptionVersionOut,
    SystemSettingsOut,
    SystemSettingsUpdate,
    TaskOut,
    TelemetryBatchResponse,
    VerificationCodeRequest,
    VerificationCodeResponse,
)
from app.services.access import access_profile, load_linux_setup_script, load_windows_setup_script
from app.services.alerts import refresh_deny_spike_alerts, refresh_liveness, sync_node_alerts
from app.services.audit import append_audit, redact, verify_audit_chain
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
from app.services.backup_worker import BackupWorker
from app.services.bundles import (
    create_desired_release,
    latest_release,
    repair_stale_proxy_selection,
    strip_retired_policy_fields,
)
from app.services.cidr import (
    effective_blacklist,
    normalize_source_ip,
)
from app.services.cidr import (
    preview_source_blacklist as preview_blacklist_helper,
)
from app.services.probes import record_probe_result
from app.services.subscription_worker import SubscriptionWorker, enqueue_refresh_task
from app.services.subscriptions import (
    SubscriptionError,
    normalize_single_node,
    normalize_source_url,
    record_uploaded_subscription,
    single_node_source_name,
    source_url_hint,
)
from app.services.tasks import (
    claim_probe_task_for_node,
    complete_task,
    create_task,
    fail_task,
    reclaim_expired_tasks,
)

SERVICE_QUALITY_RETENTION = timedelta(days=35)


# Configure logging with sensitive-field redaction
class SensitiveFieldFilter(logging.Filter):
    """Redact sensitive fields from log messages using the same logic as audit logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact message string if it contains potential sensitive patterns
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            # Replace common patterns for secrets in logs
            msg = record.msg
            # Redact Bearer tokens
            msg = re.sub(r'Bearer\s+[A-Za-z0-9_\-\.]+', 'Bearer [REDACTED]', msg)
            # Redact password= patterns
            msg = re.sub(r'password["\']?\s*[:=]\s*["\']?[^"\'}\s,]+', 'password=[REDACTED]', msg, flags=re.IGNORECASE)
            # Redact token= patterns
            msg = re.sub(r'token["\']?\s*[:=]\s*["\']?[^"\'}\s,]+', 'token=[REDACTED]', msg, flags=re.IGNORECASE)
            # Redact secret= patterns
            msg = re.sub(r'secret["\']?\s*[:=]\s*["\']?[^"\'}\s,]+', 'secret=[REDACTED]', msg, flags=re.IGNORECASE)
            record.msg = msg
        
        # Redact args if they contain sensitive data structures
        # Keep the original type (dict/list stay dict/list after redaction)
        if hasattr(record, "args") and record.args:
            try:
                # logging normalizes a single mapping argument to a mapping
                # instead of a one-item tuple. Preserve that shape so the
                # formatter can still resolve named placeholders. Positional
                # placeholders still need the original one-item tuple shape.
                if isinstance(record.args, dict):
                    redacted = redact(record.args)
                    record.args = redacted if "%(" in record.msg else (redacted,)
                else:
                    record.args = tuple(
                        redact(arg) if isinstance(arg, (dict, list)) else arg
                        for arg in record.args
                    )
            except Exception:
                # If redaction fails, pass through to avoid breaking logging
                pass
        return True


# Install log filter on root logger and uvicorn loggers
_log_filter = SensitiveFieldFilter()
logging.getLogger().addFilter(_log_filter)
logging.getLogger("uvicorn").addFilter(_log_filter)
logging.getLogger("uvicorn.access").addFilter(_log_filter)
logging.getLogger("uvicorn.error").addFilter(_log_filter)


# Simple in-memory rate limiter
class RateLimiter:
    """In-memory rate limiter using sliding window per key (IP/user/node).
    
    Designed to be lightweight and avoid external dependencies. For production
    with multiple backend instances, consider Redis-backed rate limiting.
    """

    def __init__(self):
        self._windows: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self, key: str, limit: int, window_seconds: int
    ) -> tuple[bool, int]:
        """Check if request exceeds rate limit.
        
        Returns (allowed, remaining_requests). Cleans up old entries.
        """
        async with self._lock:
            now = datetime.now().timestamp()
            cutoff = now - window_seconds
            
            # Get or create window for this key
            if key not in self._windows:
                self._windows[key] = []
            
            # Remove expired timestamps
            self._windows[key] = [ts for ts in self._windows[key] if ts > cutoff]
            
            current_count = len(self._windows[key])
            
            if current_count >= limit:
                return False, 0
            
            # Record this request
            self._windows[key].append(now)
            return True, limit - current_count - 1

    async def cleanup_old_windows(self):
        """Periodic cleanup of stale rate limit windows."""
        async with self._lock:
            now = datetime.now().timestamp()
            # Remove windows that haven't been accessed in 1 hour
            stale_cutoff = now - 3600
            keys_to_remove = [
                key
                for key, timestamps in self._windows.items()
                if not timestamps or max(timestamps) < stale_cutoff
            ]
            for key in keys_to_remove:
                del self._windows[key]


_rate_limiter = RateLimiter()


# Rate limit middleware
class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply rate limits to API endpoints based on endpoint type and caller."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"
        
        # Define rate limits: (requests, window_seconds, key_type)
        # key_type: "ip" uses client IP, "user" uses auth token, "none" skips
        rate_config = None
        
        # Auth endpoints (most strict)
        if path.startswith("/api/v1/auth/"):
            if path in {
                "/api/v1/auth/login",
                "/api/v1/auth/register",
                "/api/v1/auth/gquan/login",
            }:
                rate_config = (10, 60, "ip")  # 10 req/min per IP
            elif path == "/api/v1/auth/verification-codes":
                rate_config = (5, 60, "ip")  # 5 req/min per IP (already has internal limit)
            else:
                rate_config = (30, 60, "ip")  # Other auth: 30 req/min per IP
        
        # Management API (moderate)
        elif path.startswith("/api/v1/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            # State-changing management operations
            auth_header = request.headers.get("authorization", "")
            if auth_header:
                # Use token hash as key for authenticated requests
                token = auth_header.removeprefix("Bearer ").strip()
                token_key = hashlib.sha256(token.encode()).hexdigest()[:16]
                rate_config = (100, 60, f"user:{token_key}")
            else:
                rate_config = (20, 60, "ip")  # Unauthenticated: stricter limit
        
        # Agent API (higher limits)
        elif path.startswith("/agent/v1/"):
            auth_header = request.headers.get("authorization", "")
            if auth_header:
                token = auth_header.removeprefix("Bearer ").strip()
                token_key = hashlib.sha256(token.encode()).hexdigest()[:16]
                rate_config = (1000, 60, f"agent:{token_key}")
            else:
                rate_config = (50, 60, "ip")
        
        # Apply rate limit if configured
        if rate_config:
            limit, window, key_type = rate_config
            if key_type == "ip":
                rate_key = f"ip:{client_ip}"
            elif key_type == "none":
                rate_key = None
            else:
                rate_key = key_type  # Already includes prefix
            
            if rate_key:
                allowed, remaining = await _rate_limiter.check_rate_limit(
                    rate_key, limit, window
                )
                
                if not allowed:
                    logging.warning(
                        f"Rate limit exceeded: {rate_key} on {path}, "
                        f"limit={limit}/{window}s"
                    )
                    return Response(
                        content=json.dumps({"detail": "rate_limit_exceeded"}),
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        media_type="application/json",
                        headers={
                            "X-RateLimit-Limit": str(limit),
                            "X-RateLimit-Window": str(window),
                            "Retry-After": str(window),
                        }
                    )
        
        return await call_next(request)

DEFAULT_SITES = [
    ("north", "North Region"),
    ("east", "East Region"),
    ("south", "South Region"),
    ("west", "West Region"),
    ("central", "Central Region"),
]

_management_actor: ContextVar[str] = ContextVar("management_actor", default="")
_management_role: ContextVar[str] = ContextVar("management_role", default="")


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    itcode: str
    role: str


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
        shutdown=site.shutdown,
        config_revision=site.config_revision,
    )

def _employee_out(user: AdminUser) -> EmployeeOut:
    role = getattr(user, "role", "employee") or "employee"
    if role not in {"root", "admin", "employee"}:
        role = "employee"
    return EmployeeOut(
        itcode=user.itcode or user.username,
        role=role,
        auth_source=user.auth_source,
        is_active=user.is_active,
        created_at=user.created_at,
        password_changed_at=user.password_changed_at,
        last_login_at=user.last_login_at,
    )


def _system_settings_out(settings: SystemSettings) -> SystemSettingsOut:
    return SystemSettingsOut(
        settings_id=settings.settings_id,
        service_quality=settings.service_quality.model_dump(),
        updated_at=settings.updated_at,
        updated_by=settings.updated_by,
    )


async def _global_system_settings() -> SystemSettings:
    settings = await SystemSettings.find_one(SystemSettings.settings_id == "global")
    if settings is not None:
        return settings

    settings = SystemSettings()
    try:
        await settings.insert()
    except DuplicateKeyError:
        # Another request may initialize the singleton concurrently.
        existing = await SystemSettings.find_one(SystemSettings.settings_id == "global")
        if existing is not None:
            return existing
        raise
    return settings


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
        probe_status=node.probe_status,
        active_connections=node.active_connections,
        bytes_up=node.bytes_up,
        bytes_down=node.bytes_down,
        rx_bps=node.rx_bps,
        tx_bps=node.tx_bps,
        last_error=node.last_error,
    )


def _draft_out(draft: ConfigDraft) -> DraftOut:
    return DraftOut(
        id=_model_id(draft),
        site_id=draft.site_id,
        node_ids=draft.node_ids,
        source_revision=draft.source_revision,
        diff=strip_retired_policy_fields(draft.diff),
        validation=strip_retired_policy_fields(draft.validation),
        risk_level=draft.risk_level,
        status=draft.status,
        expires_at=draft.expires_at,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def _subscription_version_id_from_bundle(bundle: Any) -> str:
    if not isinstance(bundle, dict):
        return ""
    subscription = bundle.get("subscription")
    if not isinstance(subscription, dict):
        return ""
    return str(subscription.get("version_id") or "").strip()


async def _documents_by_id(model: Any, ids: list[str]) -> dict[str, Any]:
    unique = [item for item in dict.fromkeys(ids) if item]
    if not unique:
        return {}
    loaded = await asyncio.gather(*[model.get(item) for item in unique])
    documents: dict[str, Any] = {}
    for item_id, document in zip(unique, loaded, strict=True):
        if document is None:
            continue
        documents[item_id] = document
        documents[_model_id(document)] = document
    return documents


async def _subscription_labels_for_releases(
    releases: list[ConfigRelease],
) -> dict[str, tuple[str, str]]:
    """Resolve the operator-facing subscription name for each release."""

    if not releases:
        return {}
    try:
        desired_docs = await DesiredRelease.find(
            {"release_id": {"$in": [item.release_id for item in releases]}}
        ).to_list()
    except CollectionWasNotInitialized:
        return {}
    version_id_by_release: dict[str, str] = {}
    for desired in desired_docs:
        if desired.release_id in version_id_by_release:
            continue
        version_id = _subscription_version_id_from_bundle(desired.bundle)
        if version_id:
            version_id_by_release[desired.release_id] = version_id
    versions = await _documents_by_id(
        SubscriptionVersion, list(version_id_by_release.values())
    )
    sources = await _documents_by_id(
        SubscriptionSource, [item.source_id for item in versions.values()]
    )
    labels: dict[str, tuple[str, str]] = {}
    for release_id, version_id in version_id_by_release.items():
        version = versions.get(version_id)
        if version is None:
            continue
        source = sources.get(version.source_id)
        if source is None:
            continue
        labels[release_id] = (source.name, _model_id(source))
    return labels


def _release_out(
    release: ConfigRelease,
    *,
    subscription_name: str = "",
    subscription_source_id: str = "",
) -> ReleaseOut:
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
        subscription_name=subscription_name,
        subscription_source_id=subscription_source_id,
    )


async def _release_outs(releases: list[ConfigRelease]) -> list[ReleaseOut]:
    labels = await _subscription_labels_for_releases(releases)
    return [
        _release_out(
            item,
            subscription_name=labels.get(item.release_id, ("", ""))[0],
            subscription_source_id=labels.get(item.release_id, ("", ""))[1],
        )
        for item in releases
    ]


async def _release_out_enriched(release: ConfigRelease) -> ReleaseOut:
    return (await _release_outs([release]))[0]


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


def _source_blacklist_out(item: SourceBlacklist) -> SourceBlacklistOut:
    kind = getattr(item, "kind", "domain") or "domain"
    if kind == "network":
        kind = "cidr"
    direction = getattr(item, "direction", "source") or "source"
    return SourceBlacklistOut(
        id=_model_id(item),
        node_id=getattr(item, "node_id", "") or "",
        direction=direction,
        kind=kind,
        pattern=item.pattern,
        comment=item.comment,
        enabled=item.enabled,
        created_by=item.created_by,
        created_at=item.created_at,
    )


def _subscription_source_out(item: SubscriptionSource) -> SubscriptionSourceOut:
    return SubscriptionSourceOut(
        id=_model_id(item),
        name=item.name,
        source_type=item.source_type,
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


def _subscription_version_content_out(item: SubscriptionVersion) -> SubscriptionVersionContentOut:
    """Render a text subscription payload for an authenticated inspector."""

    try:
        content = item.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Subscription parsers accept text documents. Keep a clear API error
        # rather than silently replacing bytes and showing a misleading file.
        raise HTTPException(422, "subscription_content_not_text") from exc
    return SubscriptionVersionContentOut(
        **_subscription_version_out(item).model_dump(),
        content=content,
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


def _latest_ack_per_node(items: list[AgentAckDocument]) -> list[AgentAckDocument]:
    """Keep only the newest ACK for each node in a release.

    A monitor retries a rejected bundle and therefore legitimately emits more
    than one ACK for the same release.  Release state must represent the
    current attempt, not remain failed forever because an earlier attempt was
    rejected.
    """

    latest: dict[str, AgentAckDocument] = {}
    for item in items:
        previous = latest.get(item.node_id)
        if previous is None or (item.received_at, item.sequence) > (
            previous.received_at,
            previous.sequence,
        ):
            latest[item.node_id] = item
    return sorted(latest.values(), key=lambda item: item.node_id)


def _audit_out(item: AuditEvent) -> AuditEventOut:
    # Historical audit events remain immutable so their hash chain can still
    # verify. Filter retired policy fields only from management responses.
    return AuditEventOut(
        event_id=item.event_id,
        actor=item.actor,
        actor_role=item.actor_role,
        request_id=item.request_id,
        source_ip=item.source_ip,
        action=item.action,
        target_type=item.target_type,
        target_id=item.target_id,
        before=strip_retired_policy_fields(item.before),
        after=strip_retired_policy_fields(item.after),
        result=item.result,
        error=item.error,
        immutable_hash=item.immutable_hash,
        previous_hash=item.previous_hash,
        at=item.at,
    )


def _backup_out(item: BackupRecord) -> BackupRecordOut:
    return BackupRecordOut(
        backup_id=item.backup_id,
        scope=item.scope,
        origin=item.origin,
        artifact_paths=item.artifact_paths,
        format=item.format,
        checksum=item.checksum,
        encrypted=item.encrypted,
        storage_ref=item.storage_ref,
        status=item.status,
        created_by=item.created_by,
        created_at=item.created_at,
        verified_at=item.verified_at,
        last_rehearsed_at=item.last_rehearsed_at,
        restore_task_id=item.restore_task_id,
        error=item.error,
        size_bytes=item.size_bytes,
        manifest=item.manifest,
    )


def _access_log_out(item: AccessLog) -> AccessLogOut:
    return AccessLogOut(
        id=_model_id(item),
        ts=item.ts,
        site_id=item.site_id,
        node_id=item.node_id,
        policy_version=item.policy_version,
        src_ip=item.src_ip,
        src_cidr_match=item.src_cidr_match,
        username=item.username,
        cert_fp=item.cert_fp,
        dst_host=item.dst_host,
        dst_port=item.dst_port,
        action=item.action,
        deny_reason=item.deny_reason,
        bytes_up=item.bytes_up,
        bytes_down=item.bytes_down,
        duration_ms=item.duration_ms,
    )


def _connection_out(item: ConnectionSnapshot) -> ConnectionSnapshotOut:
    return ConnectionSnapshotOut(
        id=_model_id(item),
        node_id=item.node_id,
        site_id=item.site_id,
        sampled_at=item.sampled_at,
        active_connections=item.active_connections,
        bytes_up=item.bytes_up,
        bytes_down=item.bytes_down,
        rx_bps=getattr(item, "rx_bps", 0),
        tx_bps=getattr(item, "tx_bps", 0),
        top_sources=item.top_sources,
        top_destinations=item.top_destinations,
        top_users=item.top_users,
        connections=getattr(item, "connections", []) or [],
        api_available=item.api_available,
        received_at=item.received_at,
    )


async def _connection_scope_query(site_id: str | None, node_id: str | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if site_id:
        query["site_id"] = site_id
    if node_id:
        node = await _find_node_reference(node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        query["node_id"] = node.agent_id
    return query


async def _connection_history_query(
    *,
    site_id: str | None,
    node_id: str | None,
    since: datetime | None,
    until: datetime | None,
    source_ip: str | None,
    destination: str | None,
    network: str | None,
    outbound: str | None,
    search: str | None,
) -> dict[str, Any]:
    query = await _connection_scope_query(site_id, node_id)
    if since is not None or until is not None:
        query["sampled_at"] = {
            **({"$gte": since} if since is not None else {}),
            **({"$lte": until} if until is not None else {}),
        }

    connection_match: dict[str, Any] = {}
    if source_ip and source_ip.strip():
        connection_match["src_ip"] = {
            "$regex": re.escape(source_ip.strip()[:128]),
            "$options": "i",
        }
    if destination and destination.strip():
        pattern = {"$regex": re.escape(destination.strip()[:128]), "$options": "i"}
        connection_match["$or"] = [{"dst_host": pattern}, {"dst_ip": pattern}]
    if network and network.strip():
        connection_match["network"] = {
            "$regex": f"^{re.escape(network.strip()[:32])}$",
            "$options": "i",
        }
    if outbound and outbound.strip():
        connection_match["outbound_chain"] = {
            "$regex": re.escape(outbound.strip()[:128]),
            "$options": "i",
        }
    if connection_match:
        query["connections"] = {"$elemMatch": connection_match}

    if search and search.strip():
        pattern = {"$regex": re.escape(search.strip()[:128]), "$options": "i"}
        query["$or"] = [
            {"node_id": pattern},
            {"top_sources.label": pattern},
            {"top_destinations.label": pattern},
            {"top_users.label": pattern},
            {"connections.src_ip": pattern},
            {"connections.dst_host": pattern},
            {"connections.dst_ip": pattern},
            {"connections.network": pattern},
            {"connections.inbound": pattern},
            {"connections.outbound_chain": pattern},
            {"connections.rule": pattern},
        ]
    return query


def _safe_proxy_label(value: Any, limit: int = 255) -> str:
    """Normalize operator-visible labels without retaining arbitrary payloads."""

    return " ".join(str(value or "").split())[:limit]


def _proxy_selection_group(value: str) -> str:
    """Allow manual selection only for the selector rendered by Grouproxy.

    Proxy telemetry can contain arbitrary Clash groups such as ``GLOBAL``.
    They are useful for observation, but the generated sing-box runtime has one
    controllable selector: ``subscription``. Accepting another group would
    create a release that cannot be applied as requested.
    """

    if _safe_proxy_label(value).casefold() != "subscription":
        raise HTTPException(409, "proxy_group_not_selectable")
    return "subscription"


def _sanitize_proxy_group(group: ProxyGroupSnapshot) -> ProxyGroupSnapshot | None:
    """Keep only the bounded, non-sensitive projection sent by a monitor."""

    group_name = _safe_proxy_label(group.name)
    if not group_name:
        return None
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in group.all:
        name = _safe_proxy_label(raw_name)
        if name and name not in seen:
            names.append(name)
            seen.add(name)
        if len(names) >= 500:
            break

    endpoints: dict[str, ProxyEndpointSnapshot] = {}
    for endpoint in group.nodes:
        name = _safe_proxy_label(endpoint.name)
        if not name or name in endpoints:
            continue
        history = endpoint.history[:20]
        endpoints[name] = ProxyEndpointSnapshot(
            name=name,
            type=_safe_proxy_label(endpoint.type, 64) or "unknown",
            udp=endpoint.udp,
            alive=endpoint.alive,
            delay_ms=endpoint.delay_ms,
            history=history,
        )
        if name not in seen and len(names) < 500:
            names.append(name)
            seen.add(name)

    # A selector can list a node before its individual metadata is available.
    # Preserve that name so the UI still shows the complete selection pool.
    for name in names:
        endpoints.setdefault(name, ProxyEndpointSnapshot(name=name))

    return ProxyGroupSnapshot(
        name=group_name,
        type=_safe_proxy_label(group.type, 64) or "unknown",
        now=_safe_proxy_label(group.now),
        all=names,
        nodes=list(endpoints.values())[:500],
        udp=group.udp,
        delay_ms=group.delay_ms,
        history=group.history[:20],
    )


def _proxy_config_out(item: ProxyConfigSnapshot) -> ProxyConfigSnapshotOut:
    groups: list[ProxyGroupSnapshot] = []
    for raw_group in item.groups[:100]:
        try:
            parsed = ProxyGroupSnapshot.model_validate(raw_group)
        except Exception:
            continue
        sanitized = _sanitize_proxy_group(parsed)
        if sanitized is not None:
            groups.append(sanitized)
    return ProxyConfigSnapshotOut(
        id=_model_id(item),
        node_id=item.node_id,
        site_id=item.site_id,
        sampled_at=item.sampled_at,
        api_available=item.api_available,
        groups=groups,
        error=_safe_error(item.error, 256),
        received_at=item.received_at,
    )


def _proxy_quality_samples(
    *,
    node: Node,
    groups: list[ProxyGroupSnapshot],
    sampled_at: datetime,
    received_at: datetime,
) -> list[dict[str, Any]]:
    """Project one current sample per subscription outbound.

    Monitor snapshots repeat a bounded local history. Persisting only the most
    recent history point keeps the ingest path small while still preserving a
    minute-level series; the first snapshot after a deployment also backfills
    the latest point already known by the monitor.
    """

    subscription = next(
        (group for group in groups if group.name.strip().casefold() == "subscription"),
        None,
    )
    if subscription is None:
        return []

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, datetime]] = set()
    expires_at = received_at + SERVICE_QUALITY_RETENTION
    for endpoint in subscription.nodes[:500]:
        outbound_tag = _safe_log_text(endpoint.name, 255)
        if not outbound_tag:
            continue
        valid_history = [
            point
            for point in endpoint.history
            if point.delay_ms is not None and point.at is not None
        ]
        latest_point = max(valid_history, key=lambda point: point.at or sampled_at, default=None)
        if latest_point is not None:
            point_at = latest_point.at or sampled_at
            key = (outbound_tag, point_at)
            if key not in seen:
                result.append(
                    {
                        "node_id": node.agent_id,
                        "site_id": node.site_id,
                        "service": "subscription",
                        "outbound_tag": outbound_tag,
                        "delay_ms": latest_point.delay_ms,
                        "success": True,
                        "sampled_at": point_at,
                        "received_at": received_at,
                        "expires_at": expires_at,
                    }
                )
                seen.add(key)
        elif endpoint.delay_ms is not None and endpoint.alive is not False:
            key = (outbound_tag, sampled_at)
            if key not in seen:
                result.append(
                    {
                        "node_id": node.agent_id,
                        "site_id": node.site_id,
                        "service": "subscription",
                        "outbound_tag": outbound_tag,
                        "delay_ms": endpoint.delay_ms,
                        "success": True,
                        "sampled_at": sampled_at,
                        "received_at": received_at,
                        "expires_at": expires_at,
                    }
                )
                seen.add(key)

        # A failed /delay call does not append a history point. Keep the
        # availability dimension visible with a failure-only sample.
        if endpoint.alive is False:
            key = (outbound_tag, sampled_at)
            if key not in seen:
                result.append(
                    {
                        "node_id": node.agent_id,
                        "site_id": node.site_id,
                        "service": "subscription",
                        "outbound_tag": outbound_tag,
                        "delay_ms": None,
                        "success": False,
                        "sampled_at": sampled_at,
                        "received_at": received_at,
                        "expires_at": expires_at,
                    }
                )
                seen.add(key)
    return result


async def _persist_proxy_quality_samples(samples: list[dict[str, Any]]) -> None:
    if not samples:
        return
    operations = [
        UpdateOne(
            {
                "node_id": sample["node_id"],
                "service": sample["service"],
                "outbound_tag": sample["outbound_tag"],
                "sampled_at": sample["sampled_at"],
            },
            {"$setOnInsert": sample},
            upsert=True,
        )
        for sample in samples
    ]
    await ProxyQualitySample.get_motor_collection().bulk_write(operations, ordered=False)


def _probe_history_out(item: ProbeHistory) -> ProbeHistoryOut:
    return ProbeHistoryOut(
        id=_model_id(item),
        node_id=item.node_id,
        site_id=item.site_id,
        outbound_tag=item.outbound_tag,
        target_url=item.target_url,
        success=item.success,
        latency_ms=item.latency_ms,
        error_class=item.error_class,
        sampled_at=item.sampled_at,
    )


def _probe_circuit_out(item: ProbeCircuit) -> ProbeCircuitOut:
    return ProbeCircuitOut(
        node_id=item.node_id,
        site_id=item.site_id,
        outbound_tag=item.outbound_tag,
        state=item.state,
        consecutive_failures=item.consecutive_failures,
        consecutive_successes=item.consecutive_successes,
        opened_at=item.opened_at,
        half_open_at=item.half_open_at,
        last_success_at=item.last_success_at,
        last_failure_at=item.last_failure_at,
        last_latency_ms=item.last_latency_ms,
        last_error_class=item.last_error_class,
        reason=item.reason,
        updated_at=item.updated_at,
    )


def _alert_out(item: Alert) -> AlertOut:
    return AlertOut(
        id=_model_id(item),
        fingerprint=item.fingerprint,
        category=item.category,
        severity=item.severity,
        site_id=item.site_id,
        node_id=item.node_id,
        title=item.title,
        detail=item.detail,
        status=item.status,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
        resolved_at=item.resolved_at,
    )


def _safe_log_text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _safe_probe_target(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(422, "invalid_probe_target")
    if parsed.query or parsed.fragment:
        raise HTTPException(422, "probe_target_query_not_allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "invalid_probe_target") from exc
    hostname = parsed.hostname.rstrip(".").casefold()
    blocked_names = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if hostname in blocked_names or hostname.endswith((".local", ".internal")):
        raise HTTPException(422, "probe_target_private_network")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise HTTPException(422, "probe_target_unresolvable") from exc
    if not addresses:
        raise HTTPException(422, "probe_target_unresolvable")
    for address in addresses:
        try:
            parsed_ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            not parsed_ip.is_global
            or parsed_ip.is_private
            or parsed_ip.is_loopback
            or parsed_ip.is_link_local
            or parsed_ip.is_reserved
            or parsed_ip.is_multicast
        ):
            raise HTTPException(422, "probe_target_private_network")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


async def _purge_telemetry_batches(*, node_id: str, kind: str) -> None:
    """Drop prior-process sequence markers so a monitor restart can ingest again."""

    await TelemetryBatch.get_motor_collection().delete_many(
        {"node_id": node_id, "kind": kind}
    )


def _new_telemetry_batch(
    *, node_id: str, kind: str, batch_id: str, sequence: int, item_count: int
) -> TelemetryBatch:
    return TelemetryBatch(
        node_id=node_id,
        kind=kind,
        batch_id=batch_id,
        sequence=sequence,
        item_count=item_count,
    )


async def _accept_telemetry_batch(
    *, node: Node, kind: str, batch_id: str, sequence: int, item_count: int
) -> bool:
    """Reserve and persist one monotonic telemetry batch.

    A read-then-insert check is racy when two monitor retries arrive together.
    The cursor update below is conditional in MongoDB, so only the request with
    the highest sequence can advance it; the unique batch/sequence indexes then
    make replayed payloads idempotent.

    Monitor sequence counters are per process. After a node is re-imaged or the
    state file is reset, the same (node_id, kind, sequence) values can reappear
    with a new batch_id. Those collisions must purge the old markers instead of
    silently dropping live connection and traffic snapshots.
    """

    existing = await TelemetryBatch.find_one(
        TelemetryBatch.node_id == node.agent_id,
        TelemetryBatch.kind == kind,
        TelemetryBatch.batch_id == batch_id,
    )
    if existing is not None:
        return False

    cursor_collection = TelemetryCursor.get_motor_collection()
    current = utcnow()
    cursor_filter = {"node_id": node.agent_id, "kind": kind}
    sequence_restarted = False
    advanced = await cursor_collection.update_one(
        {**cursor_filter, "last_sequence": {"$lt": sequence}},
        {
            "$set": {
                "last_sequence": sequence,
                "last_batch_id": batch_id,
                "updated_at": current,
            }
        },
    )
    if advanced.matched_count == 0:
        restarted = await cursor_collection.update_one(
            {**cursor_filter, "last_sequence": {"$gt": sequence}},
            {
                "$set": {
                    "last_sequence": sequence,
                    "last_batch_id": batch_id,
                    "updated_at": current,
                }
            },
        )
        if restarted.matched_count > 0:
            sequence_restarted = True
        else:
            try:
                await TelemetryCursor(
                    node_id=node.agent_id,
                    kind=kind,
                    last_sequence=sequence,
                    last_batch_id=batch_id,
                    updated_at=current,
                ).insert()
            except DuplicateKeyError:
                # Another request created the cursor between the conditional update
                # and insert. Retry the same atomic comparison once.
                advanced = await cursor_collection.update_one(
                    {**cursor_filter, "last_sequence": {"$lt": sequence}},
                    {
                        "$set": {
                            "last_sequence": sequence,
                            "last_batch_id": batch_id,
                            "updated_at": current,
                        }
                    },
                )
                if advanced.matched_count == 0:
                    return False

    if sequence_restarted:
        await _purge_telemetry_batches(node_id=node.agent_id, kind=kind)

    batch = _new_telemetry_batch(
        node_id=node.agent_id,
        kind=kind,
        batch_id=batch_id,
        sequence=sequence,
        item_count=item_count,
    )
    try:
        await batch.insert()
    except DuplicateKeyError:
        collision = await TelemetryBatch.find_one(
            TelemetryBatch.node_id == node.agent_id,
            TelemetryBatch.kind == kind,
            TelemetryBatch.sequence == sequence,
        )
        if collision is None or collision.batch_id == batch_id:
            return False
        await _purge_telemetry_batches(node_id=node.agent_id, kind=kind)
        try:
            await _new_telemetry_batch(
                node_id=node.agent_id,
                kind=kind,
                batch_id=batch_id,
                sequence=sequence,
                item_count=item_count,
            ).insert()
        except DuplicateKeyError:
            return False
    return True


async def _claim_probe_requests(node: Node) -> list[ProbeRequestForAgent]:
    await reclaim_expired_tasks(task_type="node.probe")
    settings = _settings()
    if settings.probe_auto_enabled:
        await _schedule_automatic_probe(node, settings)
    task = await claim_probe_task_for_node(
        node_id=node.agent_id,
        worker_id=f"monitor:{node.agent_id}",
    )
    if task is None:
        return []
    try:
        target_url = _safe_probe_target(
            str(task.payload.get("target_url", settings.probe_target_url))
        )
    except HTTPException as exc:
        # A task payload is persisted before it reaches a monitor. Do not leave
        # an invalid or stale task leased forever if policy changes later.
        await fail_task(task, error=str(exc.detail), retryable=False)
        return []
    tags = [
        _safe_log_text(str(tag), 128)
        for tag in task.payload.get("outbound_tags", [])
        if _safe_log_text(str(tag), 128)
    ]
    tags = tags[: settings.probe_max_outbounds]
    return [ProbeRequestForAgent(task_id=task.task_id, target_url=target_url, outbound_tags=tags)]


async def _schedule_automatic_probe(node: Node, settings: Settings) -> None:
    """Create at most one low-volume probe task per configured time slot."""

    try:
        target_url = _safe_probe_target(settings.probe_target_url)
    except HTTPException:
        # A bad operator setting must not enqueue a task that can never be
        # delivered to a monitor. The configuration error remains visible in
        # deployment logs and can be corrected without draining a queue.
        return
    active = await Task.find_one(
        {
            "task_type": "node.probe",
            "target_id": node.agent_id,
            "active": True,
        }
    )
    if active is not None:
        return
    interval = settings.probe_interval_seconds
    slot = int(utcnow().timestamp() // interval)
    key = f"node.probe:auto:{node.agent_id}:{slot}"
    try:
        await create_task(
            task_type="node.probe",
            target_type="node",
            target_id=node.agent_id,
            payload={"target_url": target_url, "outbound_tags": []},
            idempotency_key=key,
            created_by="scheduler",
            request_id=f"probe-scheduler:{slot}",
        )
    except DuplicateKeyError:
        # A concurrent heartbeat may have created the same active probe. The
        # unique partial index is the final arbiter; no request should fail.
        return


async def _find_node_reference(node_id: str) -> Node | None:
    try:
        node = await Node.get(node_id)
    except Exception:
        # Agent IDs are stable strings; only document IDs are ObjectIds.
        node = None
    if node is not None:
        return node
    return await Node.find_one(Node.agent_id == node_id)


async def _increment_site_revisions(site_ids: list[str]) -> None:
    for site_id in dict.fromkeys(site_ids):
        site = await Site.get(site_id)
        if site is not None:
            site.config_revision += 1
            await site.save()


async def _increment_all_site_revisions() -> None:
    sites = await Site.find_all().to_list()
    await _increment_site_revisions([_model_id(site) for site in sites])


_ACTIVE_CONFIG_RELEASE_STATUSES = frozenset(
    {"queued", "applying", "health_check", "rolling_back"}
)


@dataclass(frozen=True)
class _SourceBlacklistReleasePlan:
    """One affected site and the nodes that must receive its policy change."""

    site: Site
    nodes: list[Node]


@dataclass(frozen=True)
class _SourceBlacklistDistribution:
    """Recorded release work for one source-blacklist mutation."""

    releases: list[ConfigRelease]
    draft_ids: list[str]
    no_node_site_ids: list[str]
    targets: list[SourceBlacklistDistributionOut]


async def _blacklist_nodes(node_ids: list[str]) -> list[Node]:
    nodes: list[Node] = []
    seen: set[str] = set()
    for raw in node_ids:
        node_id = raw.strip()
        if not node_id or node_id in seen:
            continue
        node = await _find_node_reference(node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        seen.add(node.agent_id)
        nodes.append(node)
    if not nodes:
        raise HTTPException(422, "blacklist_node_required")
    return nodes


async def _source_blacklist_no_effect_distribution(
    *, node_ids: list[str]
) -> list[SourceBlacklistDistributionOut]:
    nodes = await _blacklist_nodes(node_ids)
    grouped: dict[str, list[str]] = {}
    for node in nodes:
        grouped.setdefault(node.site_id, []).append(node.agent_id)
    return [
        SourceBlacklistDistributionOut(
            site_id=site_id,
            node_ids=agent_ids,
            state="no_effect",
            release=None,
        )
        for site_id, agent_ids in grouped.items()
    ]


async def _source_blacklist_release_plans(
    *, node_ids: list[str]
) -> list[_SourceBlacklistReleasePlan]:
    nodes = await _blacklist_nodes(node_ids)
    grouped: dict[str, list[Node]] = {}
    for node in nodes:
        grouped.setdefault(node.site_id, []).append(node)
    plans: list[_SourceBlacklistReleasePlan] = []
    for site_id, site_nodes in grouped.items():
        site = await Site.get(site_id)
        if site is None:
            raise HTTPException(404, "site_not_found")
        active = await _active_config_release_for_nodes(site_nodes)
        if active is not None:
            raise HTTPException(
                409,
                {
                    "code": "source_blacklist_release_in_progress",
                    "site_id": site_id,
                    "release_id": active.release_id,
                },
            )
        plans.append(_SourceBlacklistReleasePlan(site=site, nodes=site_nodes))
    return plans


async def _blacklist_validation_for_nodes(nodes: list[Node]) -> dict[str, Any]:
    """Informational draft validation; each node's signed bundle is independent."""

    rules: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for node in nodes:
        for rule in await effective_blacklist(node.agent_id):
            key = (rule["direction"], rule["kind"], rule["pattern"])
            if key in seen:
                continue
            seen.add(key)
            rules.append(rule)
    return {"valid": True, "errors": [], "blacklist": rules}


async def seed_defaults(settings: Settings) -> None:
    if settings.seed_default_sites:
        for slug, name in DEFAULT_SITES:
            if not await Site.find_one(Site.slug == slug):
                await Site(
                    slug=slug, name=name, dns_note="Configure local DNS to this site node"
                ).insert()
    await _ensure_operator_account(
        itcode=ROOT_ITCODE,
        role="root",
        password=settings.admin_password,
        sync_password=settings.environment in {"test", "development"},
    )
    configured = normalize_itcode(settings.admin_username)
    if configured != ROOT_ITCODE:
        await _ensure_operator_account(
            itcode=configured,
            role="admin",
            password=settings.admin_password,
            sync_password=settings.environment in {"test", "development"},
        )


async def _ensure_operator_account(
    *, itcode: str, role: str, password: str, sync_password: bool = False
) -> None:
    user = await find_user_by_itcode(itcode)
    if user is None:
        user = await AdminUser.find_one(AdminUser.username == itcode)
    if user is None:
        await AdminUser(
            username=itcode,
            itcode=itcode,
            password_hash=hash_password(password),
            role=role,
            auth_source="local",
            password_changed_at=utcnow(),
        ).insert()
        return
    changed = False
    if not user.itcode:
        user.itcode = itcode
        changed = True
    if sync_password and user.auth_source == "local":
        user.password_hash = hash_password(password)
        changed = True
    if itcode == ROOT_ITCODE:
        if user.role != "root":
            user.role = "root"
            changed = True
        if not user.is_active:
            user.is_active = True
            changed = True
    elif user.role == "employee":
        # Bootstrap the configured operator without promoting later
        # self-registered accounts that happen to share a historical username.
        if user.username == itcode and user.auth_source == "local" and not user.last_login_at:
            user.role = role
            changed = True
    if changed:
        await user.save()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _settings()
    database = Database(settings)
    await database.connect()
    await seed_defaults(settings)
    app.state.database = database
    worker = SubscriptionWorker(settings)
    worker_task = asyncio.create_task(worker.run())
    backup_worker = BackupWorker(settings)
    backup_worker_task = asyncio.create_task(backup_worker.run())

    async def observe() -> None:
        """Background observability loop: refresh liveness and alerts.
        
        Exceptions are logged but never crash the control plane. Transient
        failures (network, DB timeout) are expected; persistent errors should
        be investigated from logs.
        """
        while True:
            try:
                await refresh_liveness()
                await refresh_deny_spike_alerts()
            except Exception as exc:
                # Log errors for investigation but never crash the API
                logging.error(
                    f"Observability loop error (continuing): {exc.__class__.__name__}: {exc}",
                    exc_info=False,  # Don't spam with full traceback for expected transients
                )
            await asyncio.sleep(15)

    async def rate_limit_cleanup() -> None:
        """Periodic cleanup of stale rate limit windows."""
        while True:
            try:
                await _rate_limiter.cleanup_old_windows()
            except Exception:
                pass
            await asyncio.sleep(300)  # Every 5 minutes

    observe_task = asyncio.create_task(observe())
    cleanup_task = asyncio.create_task(rate_limit_cleanup())
    app.state.subscription_worker = worker
    app.state.backup_worker = backup_worker
    try:
        yield
    finally:
        await worker.stop()
        await backup_worker.stop()
        worker_task.cancel()
        backup_worker_task.cancel()
        observe_task.cancel()
        cleanup_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        try:
            await backup_worker_task
        except asyncio.CancelledError:
            pass
        try:
            await observe_task
        except asyncio.CancelledError:
            pass
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await database.close()


app = FastAPI(title="Grouproxy Control Plane", version="0.6.0", lifespan=lifespan)

# CORS: strict origin allowlist, no wildcard ports. Production same-origin
# deploys (dashboard and backend both served from the same domain) should set
# GROUPROXY_CORS_ALLOWED_ORIGINS="" to disable browser CORS entirely.
_settings_for_cors = get_settings()
_cors_origins = [
    origin.strip()
    for origin in _settings_for_cors.cors_allowed_origins.split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# CSRF protection: verify Origin/Referer for state-changing browser requests.
# Agent endpoints (/agent/v1/*) and health checks are excluded (machine-to-machine).
class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """Protect browser management APIs from CSRF by validating Origin/Referer.
    
    Bearer token auth reduces CSRF risk, but we still validate that state-changing
    requests come from allowed origins. Agent endpoints use machine-to-machine
    Bearer tokens and are excluded from this check.
    """

    def __init__(self, app, allowed_origins: list[str]):
        super().__init__(app)
        self.allowed_origins = set(allowed_origins)

    async def dispatch(self, request: Request, call_next):
        # Only check state-changing methods on browser management endpoints
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            path = request.url.path
            # Exclude agent APIs (machine-to-machine), health checks, and public assets
            if not (
                path.startswith("/agent/")
                or path in {"/healthz", "/readyz"}
                or path.startswith("/api/v1/access/")  # public workstation assets
            ):
                # Check Origin or Referer header
                origin = request.headers.get("origin", "")
                referer = request.headers.get("referer", "")
                
                # Extract origin from referer if origin header is missing
                if not origin and referer:
                    try:
                        parsed = urlsplit(referer)
                        origin = f"{parsed.scheme}://{parsed.netloc}"
                    except Exception:
                        origin = ""
                
                # Verify origin is in allowed list (if CORS is enabled)
                if self.allowed_origins:
                    if not origin or origin not in self.allowed_origins:
                        logging.warning(
                            f"CSRF check failed: origin={origin!r} not in allowed origins, "
                            f"path={path}, method={request.method}"
                        )
                        return JSONResponse(
                            status_code=status.HTTP_403_FORBIDDEN,
                            content={"detail": "csrf_origin_mismatch"},
                        )
        
        return await call_next(request)


if _cors_origins:
    app.add_middleware(CSRFProtectionMiddleware, allowed_origins=_cors_origins)

# Rate limiting for all endpoints
app.add_middleware(RateLimitMiddleware)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    # Keep the deployed API version visible without requiring management
    # authentication.  This makes stale dashboard/backend processes obvious
    # when a newly added route is reported as 404.
    return {"status": "ok", "service": "grouproxy-backend", "version": "0.6.0"}


@app.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None or database.client is None:
        raise HTTPException(status_code=503, detail="database_not_ready")
    try:
        # Add timeout to prevent readyz from hanging on dead/slow MongoDB
        await asyncio.wait_for(
            database.client.admin.command("ping"),
            timeout=2.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="database_ping_timeout")
    except Exception as exc:  # pragma: no cover - driver-specific exception
        raise HTTPException(status_code=503, detail="database_not_ready") from exc
    return {"status": "ready"}


async def require_authenticated(request: Request) -> AuthenticatedPrincipal:
    settings = _settings()
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if token and hmac.compare_digest(token, settings.management_token):
        return AuthenticatedPrincipal(
            itcode=normalize_itcode(settings.admin_username),
            role="root" if normalize_itcode(settings.admin_username) == ROOT_ITCODE else "admin",
        )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="management_auth_required"
        )
    try:
        session, user = await resolve_session(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.code
        ) from exc
    return AuthenticatedPrincipal(itcode=session.itcode, role=user.role)


async def require_management(request: Request) -> str:
    principal = await require_authenticated(request)
    if not is_management_role(principal.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="management_admin_required"
        )
    _management_actor.set(principal.itcode)
    _management_role.set(principal.role)
    return principal.itcode


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


async def _audit_auth_failure(*, request: Request, action: str, itcode: str, error: str) -> None:
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


def _session_response(token: str, session: ManagementSession, user: AdminUser) -> LoginResponse:
    return LoginResponse(
        access_token=token,
        itcode=session.itcode,
        role=user.role,
        expires_at=session.expires_at,
    )


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
    return _session_response(token, session, user)


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
    return _session_response(token, session, user)


@app.post("/api/v1/auth/logout", response_model=AuthActionResponse)
async def logout(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(  # noqa: B008 - FastAPI dependency declaration
        require_authenticated
    ),
) -> AuthActionResponse:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    await revoke_session_token(token)
    await append_audit(
        action="auth.logout",
        target_type="admin_user",
        target_id=principal.itcode,
        actor=principal.itcode,
        actor_role=principal.role,
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
    )
    return AuthActionResponse()


@app.get("/api/v1/employees", response_model=list[EmployeeOut])
@app.get("/api/v1/users", response_model=list[EmployeeOut])
async def list_employees(_: str = Depends(require_management)) -> list[EmployeeOut]:
    """List accounts without exposing password or session material."""

    employees = await AdminUser.find_all().sort("+itcode").to_list()
    return [_employee_out(employee) for employee in employees]


@app.patch("/api/v1/users/{itcode}/role", response_model=EmployeeOut)
async def update_user_role(
    itcode: str,
    payload: RoleUpdate,
    request: Request,
    actor: str = Depends(require_management),
) -> EmployeeOut:
    target_itcode = normalize_itcode(itcode)
    if target_itcode == ROOT_ITCODE:
        raise HTTPException(409, "root_role_immutable")
    user = await find_user_by_itcode(target_itcode)
    if user is None:
        raise HTTPException(404, "account_not_found")
    if user.role == "root":
        raise HTTPException(409, "root_role_immutable")
    before = {"itcode": user.itcode, "role": user.role}
    if user.role != payload.role:
        user.role = payload.role
        await user.save()
        if payload.role == "employee":
            await revoke_user_sessions(user)
        await append_audit(
            action="user.role.update",
            target_type="admin_user",
            target_id=user.itcode,
            actor=actor,
            actor_role="admin",
            request_id=_request_id(request),
            source_ip=_request_source_ip(request),
            before=before,
            after={"itcode": user.itcode, "role": user.role},
        )
    return _employee_out(user)


@app.get("/api/v1/system-settings", response_model=SystemSettingsOut)
async def get_system_settings(_: str = Depends(require_management)) -> SystemSettingsOut:
    return _system_settings_out(await _global_system_settings())


@app.patch("/api/v1/system-settings", response_model=SystemSettingsOut)
async def update_system_settings(
    payload: SystemSettingsUpdate,
    request: Request,
    actor: str = Depends(require_management),
) -> SystemSettingsOut:
    settings = await _global_system_settings()
    before = {
        "service_quality": settings.service_quality.model_dump(mode="json"),
        "updated_at": settings.updated_at.isoformat(),
        "updated_by": settings.updated_by,
    }
    settings.service_quality = ServiceQualityDefinition(
        **payload.service_quality.model_dump()
    )
    settings.updated_at = utcnow()
    settings.updated_by = actor
    await settings.save()
    after = {
        "service_quality": settings.service_quality.model_dump(mode="json"),
        "updated_at": settings.updated_at.isoformat(),
        "updated_by": settings.updated_by,
    }
    await append_audit(
        action="system_settings.update",
        target_type="system_settings",
        target_id=settings.settings_id,
        actor=actor,
        actor_role=_management_role.get() or "admin",
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
        before=before,
        after=after,
    )
    return _system_settings_out(settings)


@app.get("/api/v1/sites", response_model=list[SiteOut])
async def list_sites(_: str = Depends(require_management)) -> list[SiteOut]:
    return [_site_out(site) for site in await Site.find_all().sort(+Site.slug).to_list()]


@app.patch("/api/v1/sites/{site_id}", response_model=SiteOut)
async def update_site_name(
    site_id: str,
    payload: SiteNameUpdate,
    request: Request,
    _: str = Depends(require_management),
) -> SiteOut:
    """Change the display name of a site without changing its identity.

    Site names are operator-facing metadata.  They must not alter the slug,
    policy revision, node binding or desired release, so monitors do not need
    to reload merely because a label was corrected in the console.
    """

    site = await Site.get(site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(422, "site_name_required")
    if len(name) > 128:
        raise HTTPException(422, "site_name_too_long")
    before = {"name": site.name}
    if site.name != name:
        site.name = name
        await site.save()
        await append_audit(
            action="site.rename",
            target_type="site",
            target_id=site_id,
            actor=_actor(),
            actor_role="admin",
            request_id=_request_id(request),
            source_ip=_request_source_ip(request),
            before=before,
            after={"name": site.name},
        )
    return _site_out(site)


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


@app.patch("/api/v1/nodes/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: str,
    payload: NodeNameUpdate,
    request: Request,
    _: str = Depends(require_management),
) -> NodeOut:
    """Update only a node's display label; agent identity and site stay fixed."""

    node = await _find_node_reference(node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(422, "node_name_required")
    if len(name) > 128:
        raise HTTPException(422, "node_name_too_long")
    before = {"name": node.name}
    if node.name != name:
        node.name = name
        await node.save()
        await append_audit(
            action="node.rename",
            target_type="node",
            target_id=node.agent_id,
            actor=_actor(),
            actor_role="admin",
            request_id=_request_id(request),
            source_ip=_request_source_ip(request),
            before=before,
            after={"name": node.name},
        )
    return _node_out(node)


@app.get("/api/v1/blacklist", response_model=list[SourceBlacklistOut])
@app.get("/api/v1/source-blacklist", response_model=list[SourceBlacklistOut])
async def list_source_blacklist(_: str = Depends(require_management)) -> list[SourceBlacklistOut]:
    entries = await SourceBlacklist.find_all().sort(+SourceBlacklist.created_at).to_list()
    return [_source_blacklist_out(item) for item in entries]


@app.post(
    "/api/v1/blacklist/preview",
    response_model=SourceBlacklistPreviewResponse,
)
@app.post(
    "/api/v1/source-blacklist/preview",
    response_model=SourceBlacklistPreviewResponse,
)
async def preview_source_blacklist(
    payload: SourceBlacklistPreviewRequest,
    _: str = Depends(require_management),
) -> SourceBlacklistPreviewResponse:
    """Preview source or destination access against one node's blacklist."""

    source_ip = payload.source_ip.strip()
    dest_host = payload.dest_host.strip()
    if not source_ip and not dest_host:
        raise HTTPException(422, "preview_target_required")
    if source_ip:
        try:
            source_ip = normalize_source_ip(source_ip)
        except ValueError as exc:
            raise HTTPException(422, "invalid_source_ip") from exc
    node = await _find_node_reference(payload.node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    site = await Site.get(node.site_id)
    rules = await effective_blacklist(node.agent_id)
    preview = preview_blacklist_helper(
        source_ip,
        rules,
        site_shutdown=bool(site and site.shutdown),
        dest_host=dest_host or None,
    )
    return SourceBlacklistPreviewResponse(
        allowed=preview["allowed"],
        matched_pattern=preview["matched_pattern"],
        reason=str(preview["reason"]),
        blacklist=rules,
        source_blacklist=rules,
        outcome=preview["outcome"],  # type: ignore[arg-type]
        unresolved_domain_patterns=list(preview["unresolved_domain_patterns"]),
    )


@app.post(
    "/api/v1/blacklist",
    response_model=SourceBlacklistMutationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
@app.post(
    "/api/v1/source-blacklist",
    response_model=SourceBlacklistMutationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def add_source_blacklist(
    payload: SourceBlacklistCreate,
    request: Request,
    _: str = Depends(require_management),
) -> SourceBlacklistMutationOut:
    pattern = payload.pattern
    node_ids = list(payload.node_ids)
    for node_id in node_ids:
        existing = await SourceBlacklist.find_one(
            SourceBlacklist.node_id == node_id,
            SourceBlacklist.direction == payload.direction,
            SourceBlacklist.kind == payload.kind,
            SourceBlacklist.pattern == pattern,
        )
        if existing is not None:
            raise HTTPException(409, "source_blacklist_entry_exists")

    plans = (
        await _source_blacklist_release_plans(node_ids=node_ids) if payload.enabled else []
    )
    created: list[SourceBlacklist] = []
    for node_id in node_ids:
        item = SourceBlacklist(
            node_id=node_id,
            direction=payload.direction,
            kind=payload.kind,
            pattern=pattern,
            comment=payload.comment.strip(),
            enabled=payload.enabled,
            created_by=_actor(),
        )
        await item.insert()
        created.append(item)
    request_id = _request_id(request)
    representative = created[0]
    if payload.enabled:
        distribution = await _distribute_source_blacklist_change(
            rule=representative,
            operation="created",
            plans=plans,
            actor=_actor(),
            request_id=request_id,
        )
        targets = distribution.targets
    else:
        targets = await _source_blacklist_no_effect_distribution(node_ids=node_ids)
    await append_audit(
        action="source_blacklist.create",
        target_type="source_blacklist",
        target_id=_model_id(representative),
        actor=_actor(),
        request_id=request_id,
        source_ip=_request_source_ip(request),
        after={
            "node_ids": node_ids,
            "direction": payload.direction,
            "kind": payload.kind,
            "pattern": pattern,
            "enabled": payload.enabled,
            "rule_ids": [_model_id(item) for item in created],
            "release_ids": [
                target.release.release_id for target in targets if target.release is not None
            ],
        },
    )
    outs = [_source_blacklist_out(item) for item in created]
    return SourceBlacklistMutationOut(
        rule=outs[0],
        rules=outs,
        operation="created",
        distribution=targets,
    )


@app.delete(
    "/api/v1/blacklist/{entry_id}",
    response_model=SourceBlacklistMutationOut,
)
@app.delete(
    "/api/v1/source-blacklist/{entry_id}",
    response_model=SourceBlacklistMutationOut,
)
async def delete_source_blacklist(
    entry_id: str,
    request: Request,
    _: str = Depends(require_management),
) -> SourceBlacklistMutationOut:
    item = await SourceBlacklist.get(entry_id)
    if item is None:
        raise HTTPException(404, "source_blacklist_entry_not_found")

    plans = (
        await _source_blacklist_release_plans(node_ids=[item.node_id]) if item.enabled else []
    )
    rule = _source_blacklist_out(item)
    await item.delete()
    request_id = _request_id(request)
    if item.enabled:
        distribution = await _distribute_source_blacklist_change(
            rule=item,
            operation="deleted",
            plans=plans,
            actor=_actor(),
            request_id=request_id,
        )
        targets = distribution.targets
    else:
        targets = await _source_blacklist_no_effect_distribution(node_ids=[item.node_id])
    await append_audit(
        action="source_blacklist.delete",
        target_type="source_blacklist",
        target_id=entry_id,
        actor=_actor(),
        request_id=request_id,
        source_ip=_request_source_ip(request),
        before={
            "node_id": item.node_id,
            "direction": item.direction,
            "kind": item.kind,
            "pattern": item.pattern,
        },
        after={
            "release_ids": [
                target.release.release_id for target in targets if target.release is not None
            ],
        },
    )
    return SourceBlacklistMutationOut(
        rule=rule,
        rules=[rule],
        operation="deleted",
        distribution=targets,
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
    sources = await (
        SubscriptionSource.find_all().sort(-SubscriptionSource.created_at).to_list()
    )
    versions = (
        await SubscriptionVersion.find_all()
        .sort(-SubscriptionVersion.created_at)
        .limit(500)
        .to_list()
    )
    bindings = await SiteSubscription.find_all().sort(+SiteSubscription.site_id).to_list()
    return SubscriptionCatalogOut(
        sources=[_subscription_source_out(item) for item in sources],
        versions=[_subscription_version_out(item) for item in versions],
        site_subscriptions=[_site_subscription_out(item) for item in bindings],
    )


@app.get(
    "/api/v1/subscriptions/{source_id}/versions/{version_id}/content",
    response_model=SubscriptionVersionContentOut,
)
async def get_subscription_version_content(
    source_id: str,
    version_id: str,
    _: str = Depends(require_management),
) -> SubscriptionVersionContentOut:
    """Return one immutable subscription document for the detail viewer."""

    source = await SubscriptionSource.get(source_id)
    if source is None:
        raise HTTPException(404, "subscription_source_not_found")
    version = await SubscriptionVersion.get(version_id)
    if version is None or version.source_id != source_id:
        raise HTTPException(404, "subscription_version_not_found")
    return _subscription_version_content_out(version)


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
        source_url = normalize_source_url(payload.url, scheme=payload.scheme)
    except SubscriptionError as exc:
        raise HTTPException(422, exc.code) from exc
    if await SubscriptionSource.find_one(SubscriptionSource.name == payload.name.strip()):
        raise HTTPException(409, "subscription_source_name_exists")
    settings = _settings()
    source = SubscriptionSource(
        name=payload.name.strip(),
        source_type="http",
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
        source_type="upload",
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
    "/api/v1/subscriptions/single-node",
    response_model=SubscriptionUploadResponse,
    status_code=201,
)
async def create_single_node_subscription(
    payload: SubscriptionSingleNodeCreate,
    _: str = Depends(require_management),
) -> SubscriptionUploadResponse:
    try:
        content = normalize_single_node(payload.uri)
    except SubscriptionError as exc:
        raise HTTPException(422, exc.code) from exc
    clean_name = payload.name.strip() or single_node_source_name(payload.uri)
    if await SubscriptionSource.find_one(SubscriptionSource.name == clean_name):
        raise HTTPException(409, "subscription_source_name_exists")
    settings = _settings()
    source = SubscriptionSource(
        name=clean_name,
        source_type="single_node",
        url="",
        max_body_bytes=settings.subscription_max_body_bytes,
        created_by=_actor(),
    )
    await source.insert()
    try:
        version, _ = await record_uploaded_subscription(source, content, settings)
    except SubscriptionError as exc:  # pragma: no cover - normalized above
        raise HTTPException(422, exc.code) from exc
    await append_audit(
        action="subscription.single_node.create",
        target_type="subscription_version",
        target_id=_model_id(version),
        actor=_actor(),
        after={
            "source_id": _model_id(source),
            "content_hash": version.content_hash,
            "parse_ok": version.parse_ok,
            "format": version.format,
            "node_count": version.node_count,
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
    selected_nodes = [node for node in nodes if _model_id(node) in set(selected)]
    validation = await _blacklist_validation_for_nodes(selected_nodes)
    clean_diff = strip_retired_policy_fields(payload.diff)
    validation["valid"] = True
    validation["errors"] = []
    risk = "high" if clean_diff.get("shutdown") else ("medium" if clean_diff else "low")
    draft = ConfigDraft(
        site_id=payload.site_id,
        node_ids=selected,
        source_revision=site.config_revision,
        diff=clean_diff,
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
        after={"site_id": payload.site_id, "risk_level": risk, "diff": clean_diff},
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


async def _active_config_release_for_nodes(nodes: list[Node]) -> ConfigRelease | None:
    """Return a release which would be superseded by a new desired bundle."""

    agent_ids = [node.agent_id for node in nodes]
    if not agent_ids:
        return None
    return await ConfigRelease.find_one(
        {
            "node_ids": {"$in": agent_ids},
            "status": {"$in": list(_ACTIVE_CONFIG_RELEASE_STATUSES)},
        }
    )


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
    selected_document_ids = (
        requested_node_ids or draft.node_ids or [_model_id(node) for node in nodes]
    )
    selected = [node for node in nodes if _model_id(node) in set(selected_document_ids)]
    if len(selected) != len(selected_document_ids) or not selected:
        raise HTTPException(409, "invalid_release_nodes")
    # DesiredRelease and agent ACKs use the stable agent identity. Mongo
    # ObjectIds are an implementation detail and must not be used for
    # cross-component reconciliation.
    selected_ids = [node.agent_id for node in selected]
    active_release = await _active_config_release_for_nodes(selected)
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
            site=site,
            nodes=selected,
            settings=_settings(),
            created_by=actor,
            proxy_selection=(
                draft.diff.get("proxy_selection") if isinstance(draft.diff, dict) else None
            ),
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


async def _distribute_source_blacklist_change(
    *,
    rule: SourceBlacklist,
    operation: str,
    plans: list[_SourceBlacklistReleasePlan],
    actor: str,
    request_id: str,
) -> _SourceBlacklistDistribution:
    """Create normal per-site releases for an already persisted rule change.

    Bundles are signed and released through the same path as operator-created
    drafts. A site without a node has nothing to acknowledge, so its revision
    advances once without inventing an unfinishable release.
    """

    releases: list[ConfigRelease] = []
    draft_ids: list[str] = []
    no_node_site_ids: list[str] = []
    targets: list[SourceBlacklistDistributionOut] = []
    rule_id = _model_id(rule)
    rule_change = {
        "operation": operation,
        "rule_id": rule_id,
        "node_id": getattr(rule, "node_id", ""),
        "direction": getattr(rule, "direction", "source"),
        "kind": rule.kind,
        "pattern": rule.pattern,
        "enabled": rule.enabled,
    }
    for plan in plans:
        site = plan.site
        site_id = _model_id(site)
        if not plan.nodes:
            site.config_revision += 1
            await site.save()
            no_node_site_ids.append(site_id)
            targets.append(
                SourceBlacklistDistributionOut(
                    site_id=site_id,
                    node_ids=[],
                    state="no_nodes",
                    release=None,
                )
            )
            continue
        validation = await _blacklist_validation_for_nodes(plan.nodes)
        draft = ConfigDraft(
            site_id=site_id,
            node_ids=[_model_id(node) for node in plan.nodes],
            source_revision=site.config_revision,
            diff={"blacklist": rule_change},
            validation=validation,
            risk_level="medium",
            created_by=actor,
            expires_at=utcnow() + timedelta(hours=24),
        )
        await draft.insert()
        draft_ids.append(_model_id(draft))
        try:
            release, reused = await _create_release_from_draft(
                draft=draft,
                site=site,
                requested_node_ids=draft.node_ids,
                expected_current_version=None,
                idempotency_key=f"source-blacklist:{operation}:{rule_id}:{site_id}",
                request_id=request_id,
                actor=actor,
            )
        except Exception:
            # This temporary draft is not operator-authored. Keep it out of
            # the normal release queue when a race or bundle validation error
            # prevents the release from being created.
            if draft.status == "draft":
                draft.status = "expired"
                draft.updated_at = utcnow()
                await draft.save()
            raise
        if reused:
            # The deterministic key can only be reused after a retry. Retire
            # the duplicate temporary draft and surface the original release.
            draft.status = "expired"
            draft.updated_at = utcnow()
            await draft.save()
        releases.append(release)
        targets.append(
            SourceBlacklistDistributionOut(
                site_id=site_id,
                node_ids=[node.agent_id for node in plan.nodes],
                state="released",
                release=await _release_out_enriched(release),
            )
        )
    return _SourceBlacklistDistribution(
        releases=releases,
        draft_ids=draft_ids,
        no_node_site_ids=no_node_site_ids,
        targets=targets,
    )


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
    return await _release_out_enriched(release)


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
        elif previous_version_id != _model_id(version) or binding.source_id != version.source_id:
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
        validation = await _blacklist_validation_for_nodes(nodes)
        validation["subscription"] = {
            "parse_ok": version.parse_ok,
            "content_hash": version.content_hash,
            "format": version.format,
        }
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
            validation=validation,
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
        releases=await _release_outs(releases),
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
        releases=await _release_outs(releases),
    )


@app.get("/api/v1/config/releases/{release_id}/detail", response_model=ReleaseDetailOut)
async def get_release_detail(
    release_id: str, _: str = Depends(require_management)
) -> ReleaseDetailOut:
    """Return the stable operator view used by the release workbench."""
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    task = await Task.find_one(Task.task_id == release.task_id) if release.task_id else None
    acks = _latest_ack_per_node(
        await AgentAckDocument.find(AgentAckDocument.release_id == release_id).to_list()
    )
    events: list[ReleaseEventOut] = [
        ReleaseEventOut(
            timestamp=release.created_at,
            source="orchestrator",
            message=f"Release {release.release_id} created for {len(release.node_ids)} node(s).",
        )
    ]
    if task:
        events.append(
            ReleaseEventOut(
                timestamp=task.created_at,
                source="task",
                message=f"Task {task.task_id} entered {task.status} state.",
                level="success" if task.status == "succeeded" else "info",
            )
        )
    for ack in acks:
        events.append(
            ReleaseEventOut(
                timestamp=ack.received_at,
                source=f"agent:{ack.node_id}",
                message=(
                    f"Node ACK received: {ack.stage}; "
                    f"sing-box={'pass' if ack.singbox_ok else 'fail'}, "
                    f"nftables={'pass' if ack.nft_ok else 'fail'}, "
                    f"health={'pass' if ack.health_ok else 'fail'}."
                ),
                level="success" if ack.ok and ack.health_ok else "error",
            )
        )
    if release.finished_at:
        events.append(
            ReleaseEventOut(
                timestamp=release.finished_at,
                source="coordinator",
                message=f"Release completed with status {release.status}.",
                level="success" if release.status == "succeeded" else "error",
            )
        )
    events.sort(key=lambda item: item.timestamp)
    return ReleaseDetailOut(
        release=await _release_out_enriched(release),
        task=_task_out(task) if task else None,
        acknowledgements=[_ack_out(item) for item in acks],
        events=events,
    )


@app.get("/api/v1/config/releases", response_model=list[ReleaseOut])
async def list_releases(
    site_id: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    _: str = Depends(require_management),
) -> list[ReleaseOut]:
    safe_limit = min(max(limit, 1), 250)
    query: dict[str, Any] = {"site_id": site_id} if site_id else {}
    if status:
        query["status"] = status
    if since or until:
        query["created_at"] = {
            **({"$gte": since} if since else {}),
            **({"$lte": until} if until else {}),
        }
    releases = await (
        ConfigRelease.find(query).sort(-ConfigRelease.created_at).limit(safe_limit).to_list()
    )
    return await _release_outs(releases)


@app.get("/api/v1/config/releases/{release_id}/acks", response_model=list[AgentAckOut])
async def list_release_acks(
    release_id: str, _: str = Depends(require_management)
) -> list[AgentAckOut]:
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    acks = (
        await AgentAckDocument.find(AgentAckDocument.release_id == release_id)
        .to_list()
    )
    return [_ack_out(item) for item in _latest_ack_per_node(acks)]


@app.get("/api/v1/config/releases/{release_id}", response_model=ReleaseOut)
async def get_release(release_id: str, _: str = Depends(require_management)) -> ReleaseOut:
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    return await _release_out_enriched(release)


@app.get("/api/v1/tasks", response_model=list[TaskOut])
async def list_tasks(
    status: str | None = None,
    task_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    _: str = Depends(require_management),
) -> list[TaskOut]:
    safe_limit = min(max(limit, 1), 250)
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if task_type:
        query["task_type"] = task_type
    if since or until:
        query["created_at"] = {
            **({"$gte": since} if since else {}),
            **({"$lte": until} if until else {}),
        }
    tasks = await Task.find(query).sort(-Task.created_at).limit(safe_limit).to_list()
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


@app.get("/api/v1/logs", response_model=list[AccessLogOut])
async def list_logs(
    site_id: str | None = None,
    node_id: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str | None = None,
    limit: int = 200,
    _: str = Depends(require_management),
) -> list[AccessLogOut]:
    safe_limit = min(max(limit, 1), 500)
    query: dict[str, Any] = {}
    if site_id:
        query["site_id"] = site_id
    if node_id:
        node = await _find_node_reference(node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        query["node_id"] = node.agent_id
    if action in {"allow", "deny"}:
        query["action"] = action
    if since is not None or until is not None:
        query["ts"] = {
            **({"$gte": since} if since is not None else {}),
            **({"$lte": until} if until is not None else {}),
        }
    if search and search.strip():
        # Search is a literal operator query. Escaping keeps names such as
        # ``*.example`` from becoming an unbounded MongoDB regular expression.
        pattern = re.escape(search.strip()[:128])
        query["$or"] = [
            {"dst_host": {"$regex": pattern, "$options": "i"}},
            {"src_ip": {"$regex": pattern, "$options": "i"}},
            {"username": {"$regex": pattern, "$options": "i"}},
            {"deny_reason": {"$regex": pattern, "$options": "i"}},
        ]
    entries = await AccessLog.find(query).sort(-AccessLog.ts).limit(safe_limit).to_list()
    return [_access_log_out(item) for item in entries]


@app.get("/api/v1/connections", response_model=list[ConnectionSnapshotOut])
async def list_connections(
    site_id: str | None = None,
    node_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    _: str = Depends(require_management),
) -> list[ConnectionSnapshotOut]:
    safe_limit = min(max(limit, 1), 250)
    query: dict[str, Any] = {}
    if site_id:
        query["site_id"] = site_id
    if node_id:
        node = await _find_node_reference(node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        query["node_id"] = node.agent_id
    if since is not None or until is not None:
        query["sampled_at"] = {
            **({"$gte": since} if since is not None else {}),
            **({"$lte": until} if until is not None else {}),
        }
    entries = await (
        ConnectionSnapshot.find(query)
        .sort(-ConnectionSnapshot.sampled_at)
        .limit(safe_limit)
        .to_list()
    )
    return [_connection_out(item) for item in entries]


@app.get("/api/v1/connections/live", response_model=list[ConnectionSnapshotOut])
async def list_live_connections(
    site_id: str | None = None,
    node_id: str | None = None,
    _: str = Depends(require_management),
) -> list[ConnectionSnapshotOut]:
    """Return the latest connection snapshot for each selected node."""

    if node_id:
        node = await _find_node_reference(node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        selected_nodes = [node]
    else:
        selected_nodes = await Node.find({"site_id": site_id} if site_id else {}).to_list()
    entries: list[ConnectionSnapshot] = []
    for node in selected_nodes:
        snapshot = (
            await ConnectionSnapshot.find(ConnectionSnapshot.node_id == node.agent_id)
            .sort(-ConnectionSnapshot.sampled_at)
            .first_or_none()
        )
        if snapshot is not None:
            entries.append(snapshot)
    entries.sort(key=lambda item: item.sampled_at, reverse=True)
    return [_connection_out(entry) for entry in entries]


@app.get("/api/v1/connections/history", response_model=ConnectionHistoryResponse)
async def list_connection_history(
    site_id: str | None = None,
    node_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source_ip: str | None = None,
    destination: str | None = None,
    network: str | None = None,
    outbound: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _: str = Depends(require_management),
) -> ConnectionHistoryResponse:
    """Return retained connection snapshots with audit-oriented filters."""

    safe_limit = min(max(limit, 1), 250)
    safe_offset = min(max(offset, 0), 100_000)
    query = await _connection_history_query(
        site_id=site_id,
        node_id=node_id,
        since=since,
        until=until,
        source_ip=source_ip,
        destination=destination,
        network=network,
        outbound=outbound,
        search=search,
    )
    total = await ConnectionSnapshot.find(query).count()
    entries = (
        await ConnectionSnapshot.find(query)
        .sort(-ConnectionSnapshot.sampled_at)
        .skip(safe_offset)
        .limit(safe_limit)
        .to_list()
    )
    return ConnectionHistoryResponse(
        items=[_connection_out(item) for item in entries],
        total=total,
        limit=safe_limit,
        offset=safe_offset,
        has_more=safe_offset + len(entries) < total,
    )


@app.get("/api/v1/nodes/{node_id}/connections", response_model=ConnectionSnapshotOut)
async def get_node_live_connections(
    node_id: str, _: str = Depends(require_management)
) -> ConnectionSnapshotOut:
    node = await _find_node_reference(node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    snapshot = (
        await ConnectionSnapshot.find(ConnectionSnapshot.node_id == node.agent_id)
        .sort(-ConnectionSnapshot.sampled_at)
        .first_or_none()
    )
    if snapshot is None:
        raise HTTPException(404, "connection_snapshot_not_found")
    return _connection_out(snapshot)


@app.get("/api/v1/service-quality", response_model=ServiceQualityResponse)
async def list_service_quality(
    window: Literal["1h", "24h", "7d", "30d"] = "24h",
    site_id: str | None = None,
    node_id: str | None = None,
    service: Literal["subscription"] = "subscription",
    _: str = Depends(require_management),
) -> ServiceQualityResponse:
    """Aggregate the measured subscription outbound quality for a time window."""

    window_delta = {
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }[window]
    until = utcnow()
    from_at = until - window_delta
    nodes = await Node.find_all().to_list()
    selected_node: Node | None = None
    if node_id:
        selected_node = await _find_node_reference(node_id)
        if selected_node is None:
            raise HTTPException(404, "node_not_found")
    selected_nodes = [
        node
        for node in nodes
        if (not site_id or node.site_id == site_id)
        and (selected_node is None or node.agent_id == selected_node.agent_id)
    ]
    node_ids = [node.agent_id for node in selected_nodes]
    node_by_id = {node.agent_id: node for node in selected_nodes}
    sites = await Site.find_all().to_list()
    site_names = {str(site.id): site.name for site in sites}

    match: dict[str, Any] = {
        "service": service,
        "sampled_at": {"$gte": from_at, "$lte": until},
    }
    if site_id:
        match["site_id"] = site_id
    if node_id:
        match["node_id"] = selected_node.agent_id if selected_node else ""
    pipeline = [
        {"$match": match},
        {
            "$sort": {
                "node_id": 1,
                "site_id": 1,
                "service": 1,
                "outbound_tag": 1,
                "sampled_at": 1,
            }
        },
        {
            "$group": {
                "_id": {
                    "node_id": "$node_id",
                    "site_id": "$site_id",
                    "service": "$service",
                    "outbound_tag": "$outbound_tag",
                },
                "sample_count": {"$sum": 1},
                "successful_samples": {
                    "$sum": {"$cond": ["$success", 1, 0]}
                },
                "average_latency_ms": {"$avg": "$delay_ms"},
                "min_latency_ms": {"$min": "$delay_ms"},
                "max_latency_ms": {"$max": "$delay_ms"},
                "last_latency_ms": {"$last": "$delay_ms"},
                "last_success": {"$last": "$success"},
                "last_sampled_at": {"$last": "$sampled_at"},
            }
        },
    ]
    aggregated: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    async for row in ProxyQualitySample.get_motor_collection().aggregate(pipeline):
        identity = row.get("_id") or {}
        row_node_id = str(identity.get("node_id") or "")
        row_site_id = str(identity.get("site_id") or "")
        row_service = str(identity.get("service") or service)
        outbound_tag = str(identity.get("outbound_tag") or "")
        if not outbound_tag:
            continue
        sample_count = int(row.get("sample_count") or 0)
        successful_samples = int(row.get("successful_samples") or 0)
        aggregated[(row_node_id, row_site_id, row_service, outbound_tag)] = {
            "node_id": row_node_id,
            "node_name": (
                node_by_id.get(row_node_id).name
                if row_node_id in node_by_id
                else row_node_id
            ),
            "site_id": row_site_id,
            "site_name": site_names.get(row_site_id, row_site_id),
            "service": row_service,
            "outbound_tag": outbound_tag,
            "average_latency_ms": (
                float(row["average_latency_ms"])
                if row.get("average_latency_ms") is not None
                else None
            ),
            "min_latency_ms": (
                int(row["min_latency_ms"]) if row.get("min_latency_ms") is not None else None
            ),
            "max_latency_ms": (
                int(row["max_latency_ms"]) if row.get("max_latency_ms") is not None else None
            ),
            "success_rate": successful_samples / sample_count * 100 if sample_count else None,
            "sample_count": sample_count,
            "successful_samples": successful_samples,
            "failed_samples": max(sample_count - successful_samples, 0),
            "last_latency_ms": (
                int(row["last_latency_ms"])
                if row.get("last_latency_ms") is not None
                else None
            ),
            "last_success": row.get("last_success"),
            "last_sampled_at": row.get("last_sampled_at"),
        }

    # Include currently reported subscription endpoints even before their first
    # quality sample arrives, so a new node does not disappear from the page.
    if node_ids:
        snapshots = await ProxyConfigSnapshot.find({"node_id": {"$in": node_ids}}).to_list()
        for snapshot in snapshots:
            node = node_by_id.get(snapshot.node_id)
            if node is None:
                continue
            for raw_group in snapshot.groups:
                try:
                    group = ProxyGroupSnapshot.model_validate(raw_group)
                except Exception:
                    continue
                if group.name.strip().casefold() != service:
                    continue
                endpoint_names = list(
                    dict.fromkeys(group.all + [endpoint.name for endpoint in group.nodes])
                )
                for outbound_tag in endpoint_names:
                    clean_tag = _safe_log_text(outbound_tag, 255)
                    if not clean_tag:
                        continue
                    key = (node.agent_id, node.site_id, service, clean_tag)
                    aggregated.setdefault(
                        key,
                        {
                            "node_id": node.agent_id,
                            "node_name": node.name,
                            "site_id": node.site_id,
                            "site_name": site_names.get(node.site_id, node.site_id),
                            "service": service,
                            "outbound_tag": clean_tag,
                            "average_latency_ms": None,
                            "min_latency_ms": None,
                            "max_latency_ms": None,
                            "success_rate": None,
                            "sample_count": 0,
                            "successful_samples": 0,
                            "failed_samples": 0,
                            "last_latency_ms": None,
                            "last_success": None,
                            "last_sampled_at": None,
                        },
                    )

    entries = list(aggregated.values())
    entries.sort(
        key=lambda item: (
            item["node_name"].casefold(),
            item["average_latency_ms"] is None,
            item["average_latency_ms"] if item["average_latency_ms"] is not None else float("inf"),
            item["outbound_tag"].casefold(),
        )
    )
    return ServiceQualityResponse(
        window=window,
        from_at=from_at,
        until=until,
        service=service,
        samples=sum(item["sample_count"] for item in entries),
        entries=entries,
    )


@app.get("/api/v1/proxy-configs", response_model=list[ProxyConfigSnapshotOut])
@app.get("/api/v1/proxies", response_model=list[ProxyConfigSnapshotOut])
async def list_proxy_configs(
    site_id: str | None = None,
    node_id: str | None = None,
    limit: int = 100,
    _: str = Depends(require_management),
) -> list[ProxyConfigSnapshotOut]:
    """Return one recent, safe proxy projection per enrolled node."""

    safe_limit = min(max(limit, 1), 100)
    query: dict[str, Any] = {}
    if site_id:
        query["site_id"] = site_id
    if node_id:
        node = await _find_node_reference(node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        query["node_id"] = node.agent_id
    # A bounded recent window keeps this read cheap even when a node has been
    # reporting for months. The final map guarantees one snapshot per node.
    entries = await (
        ProxyConfigSnapshot.find(query)
        .sort(-ProxyConfigSnapshot.sampled_at)
        .limit(min(safe_limit * 20, 2_000))
        .to_list()
    )
    latest: dict[str, ProxyConfigSnapshot] = {}
    for entry in entries:
        latest.setdefault(entry.node_id, entry)
    selected = list(latest.values())[:safe_limit]
    selected.sort(key=lambda item: (item.site_id, item.node_id))
    return [_proxy_config_out(item) for item in selected]


@app.get("/api/v1/nodes/{node_id}/proxy-config", response_model=ProxyConfigSnapshotOut)
async def get_node_proxy_config(
    node_id: str, _: str = Depends(require_management)
) -> ProxyConfigSnapshotOut:
    node = await _find_node_reference(node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    snapshot = await _latest_proxy_config(node.agent_id)
    if snapshot is None:
        raise HTTPException(404, "proxy_config_not_found")
    return _proxy_config_out(snapshot)


@app.post("/api/v1/nodes/{node_id}/proxy-selection", response_model=ReleaseOut, status_code=202)
async def select_node_proxy(
    node_id: str,
    payload: ProxySelectionRequest,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> ReleaseOut:
    """Create a release that changes the selected outbound for one node.

    The control plane only accepts names that the monitor recently reported in
    its safe proxy projection. It records the choice in a normal draft/release
    and never calls a node's loopback API itself.
    """

    node = await _find_node_reference(node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    group_name = _proxy_selection_group(payload.group)
    outbound_name = " ".join(payload.outbound.split())
    request_id = _request_id(request)
    expected_version_key = (
        str(payload.expected_current_version)
        if payload.expected_current_version is not None
        else "latest"
    )
    idempotency_key = (idempotency_key_header or "").strip() or (
        "proxy-selection:"
        f"{node.agent_id}:{group_name}:{outbound_name}:"
        f"{expected_version_key}"
    )
    # Idempotency is checked before creating a draft. A browser retry should
    # return the existing release and must not leave a second draft behind.
    existing_task = await Task.find_one(Task.idempotency_key == idempotency_key)
    if existing_task is not None:
        existing_release = await ConfigRelease.find_one(
            ConfigRelease.task_id == existing_task.task_id
        )
        if existing_release is not None:
            return await _release_out_enriched(existing_release)
        raise HTTPException(409, "release_idempotency_incomplete")
    snapshot = await _latest_proxy_config(node.agent_id)
    if snapshot is None:
        raise HTTPException(409, "proxy_snapshot_not_found")
    selected_group: ProxyGroupSnapshot | None = None
    for raw_group in snapshot.groups:
        try:
            group = ProxyGroupSnapshot.model_validate(raw_group)
        except Exception:
            continue
        if group.name == group_name:
            selected_group = group
            break
    if selected_group is None:
        raise HTTPException(409, "proxy_group_not_found")
    if outbound_name not in selected_group.all:
        raise HTTPException(409, "proxy_outbound_not_found")
    if not selected_group.all:
        raise HTTPException(409, "proxy_group_not_selectable")
    site = await Site.get(node.site_id)
    if site is None:
        raise HTTPException(409, "site_not_found")
    source_rules = await effective_blacklist(node.agent_id)
    draft = ConfigDraft(
        site_id=node.site_id,
        node_ids=[_model_id(node)],
        source_revision=site.config_revision,
        diff={
            "proxy_selection": {
                "node_id": node.agent_id,
                "group": group_name,
                "outbound": outbound_name,
                "from": selected_group.now or None,
            },
            "note": payload.note,
        },
        validation={
            "valid": True,
            "errors": [],
            "blacklist": source_rules,
            "proxy_selection": {
                "group": group_name,
                "outbound": outbound_name,
                "snapshot_at": snapshot.sampled_at.isoformat(),
            },
        },
        risk_level="medium",
        created_by=_actor(),
        expires_at=utcnow() + timedelta(hours=24),
    )
    await draft.insert()
    try:
        release, reused = await _create_release_from_draft(
            draft=draft,
            site=site,
            requested_node_ids=[_model_id(node)],
            expected_current_version=payload.expected_current_version,
            idempotency_key=idempotency_key,
            request_id=request_id,
            actor=_actor(),
        )
    except Exception:
        draft.status = "expired"
        draft.updated_at = utcnow()
        await draft.save()
        raise
    if reused:
        # A concurrent request won the idempotency race after the pre-check.
        # Retire this temporary draft so the operator only sees the real one.
        draft.status = "expired"
        draft.updated_at = utcnow()
        await draft.save()
        return await _release_out_enriched(release)
    await append_audit(
        action="proxy_selection.update",
        target_type="node",
        target_id=node.agent_id,
        actor=_actor(),
        actor_role="admin",
        request_id=request_id,
        source_ip=_request_source_ip(request),
        after={
            "site_id": node.site_id,
            "group": group_name,
            "outbound": outbound_name,
            "release_id": release.release_id,
        },
    )
    return await _release_out_enriched(release)


async def _latest_proxy_config(node_id: str) -> ProxyConfigSnapshot | None:
    """Read the most recent safe proxy snapshot for a monitor identity."""

    return await (
        ProxyConfigSnapshot.find(ProxyConfigSnapshot.node_id == node_id)
        .sort(-ProxyConfigSnapshot.sampled_at)
        .first_or_none()
    )


@app.get("/api/v1/nodes/{node_id}/probes")
async def list_node_probes(
    node_id: str,
    limit: int = 100,
    _: str = Depends(require_management),
) -> dict[str, Any]:
    node = await _find_node_reference(node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    safe_limit = min(max(limit, 1), 250)
    history = await (
        ProbeHistory.find(ProbeHistory.node_id == node.agent_id)
        .sort(-ProbeHistory.sampled_at)
        .limit(safe_limit)
        .to_list()
    )
    circuits = await (
        ProbeCircuit.find(ProbeCircuit.node_id == node.agent_id)
        .sort(+ProbeCircuit.outbound_tag)
        .to_list()
    )
    return {
        "node_id": node.agent_id,
        "history": [_probe_history_out(item) for item in history],
        "circuits": [_probe_circuit_out(item) for item in circuits],
    }


@app.post("/api/v1/nodes/{node_id}/probes", response_model=TaskOut, status_code=202)
async def create_node_probe(
    node_id: str,
    payload: ProbeTaskRequest,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(require_management),
) -> TaskOut:
    node = await _find_node_reference(node_id)
    if node is None:
        raise HTTPException(404, "node_not_found")
    target_url = _safe_probe_target(payload.target_url)
    tags = [_safe_log_text(tag, 128) for tag in payload.outbound_tags if _safe_log_text(tag, 128)]
    settings = _settings()
    if len(tags) > settings.probe_max_outbounds:
        raise HTTPException(422, "probe_outbound_limit_exceeded")
    request_id = _request_id(request)
    key = idempotency_key_header or (
        f"node.probe:{node.agent_id}:{target_url}:{','.join(sorted(tags))}"
    )
    existing_active = await Task.find_one(
        {
            "task_type": "node.probe",
            "target_id": node.agent_id,
            "active": True,
        }
    )
    if existing_active is not None:
        task = existing_active
    else:
        try:
            task, _ = await create_task(
                task_type="node.probe",
                target_type="node",
                target_id=node.agent_id,
                payload={"target_url": target_url, "outbound_tags": tags},
                idempotency_key=key,
                created_by=_actor(),
                request_id=request_id,
            )
        except DuplicateKeyError:
            # Another request won the active-probe race. Return that task so a
            # burst of clicks cannot turn into a long probe backlog.
            task = await Task.find_one(
                {
                    "task_type": "node.probe",
                    "target_id": node.agent_id,
                    "active": True,
                }
            )
            if task is None:
                raise
    await append_audit(
        action="node.probe.create",
        target_type="node",
        target_id=node.agent_id,
        actor=_actor(),
        request_id=request_id,
        after={"task_id": task.task_id, "target_url": target_url, "outbound_count": len(tags)},
    )
    return _task_out(task)


@app.get("/api/v1/alerts", response_model=list[AlertOut])
async def list_alerts(
    status_filter: str | None = None,
    limit: int = 200,
    _: str = Depends(require_management),
) -> list[AlertOut]:
    safe_limit = min(max(limit, 1), 500)
    query = {"status": status_filter} if status_filter in {"open", "resolved"} else {}
    alerts = await Alert.find(query).sort(-Alert.last_seen_at).limit(safe_limit).to_list()
    return [_alert_out(item) for item in alerts]


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
    # Repair bundles created before stale selector filtering was introduced.
    # This is intentionally done at the agent boundary so an already queued
    # release can recover without requiring an operator to recreate it.
    await repair_stale_proxy_selection(desired, _settings())
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


@app.post("/agent/v1/heartbeat", response_model=AgentHeartbeatResponse)
async def agent_heartbeat(
    payload: AgentHeartbeat,
    node: Node = Depends(require_agent),  # noqa: B008 - FastAPI dependency declaration
) -> dict[str, Any]:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")
    previous = await HeartbeatLatest.find_one(HeartbeatLatest.node_id == node.agent_id)
    if previous and payload.sequence == int(previous.payload.get("sequence", -1)):
        return AgentHeartbeatResponse(accepted=False, duplicate=True)
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
        node_id=node.agent_id,
        payload=heartbeat_payload,
        received_at=received,
        expires_at=received + timedelta(days=7),
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
    node.active_connections = payload.connections
    node.bytes_up = payload.bytes_up
    node.bytes_down = payload.bytes_down
    node.rx_bps = payload.rx_bps
    node.tx_bps = payload.tx_bps
    node.last_error = _safe_error(payload.last_error)
    node.last_error_at = received if node.last_error else node.last_error_at
    await node.save()
    await sync_node_alerts(node)
    probe_requests = await _claim_probe_requests(node)
    return {
        "accepted": True,
        "desired_stale": bool(
            desired
            and (
                desired.desired_version > payload.applied_version
                or desired.bundle_hash != payload.applied_hash
            )
        ),
        "probe_requests": probe_requests,
    }


@app.post("/agent/v1/logs", response_model=TelemetryBatchResponse)
async def agent_logs(
    payload: AgentLogBatch,
    node: Node = Depends(require_agent),  # noqa: B008
) -> TelemetryBatchResponse:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")
    accepted = await _accept_telemetry_batch(
        node=node,
        kind="access_log",
        batch_id=payload.batch_id,
        sequence=payload.sequence,
        item_count=len(payload.entries),
    )
    if not accepted:
        return TelemetryBatchResponse(accepted=False, duplicate=True)
    desired = await latest_release(node.site_id, node.agent_id)
    policy_version = desired.desired_version if desired else 0
    documents = []
    for entry in payload.entries:
        # Keep only the fields needed for operations; never persist URL query,
        # cookies, or authorization material from a monitor log line.
        expires = entry.ts + (timedelta(days=90) if entry.action == "deny" else timedelta(days=14))
        documents.append(
            AccessLog(
                ts=entry.ts,
                site_id=node.site_id,
                node_id=node.agent_id,
                batch_id=payload.batch_id,
                policy_version=entry.policy_version or policy_version,
                src_ip=_safe_log_text(entry.src_ip, 64),
                src_cidr_match=_safe_log_text(entry.src_cidr_match, 64),
                username=_safe_log_text(entry.username, 128),
                cert_fp=_safe_log_text(entry.cert_fp, 256),
                dst_host=_safe_log_text(entry.dst_host, 255),
                dst_port=entry.dst_port,
                action=entry.action,
                deny_reason=_safe_log_text(entry.deny_reason, 64),
                bytes_up=entry.bytes_up,
                bytes_down=entry.bytes_down,
                duration_ms=entry.duration_ms,
                expires_at=expires,
            )
        )
    if documents:
        await AccessLog.insert_many(documents)
    await append_audit(
        action="agent.logs.ingest",
        target_type="node",
        target_id=node.agent_id,
        actor="agent",
        actor_role="agent",
        after={"batch_id": payload.batch_id, "item_count": len(documents)},
    )
    return TelemetryBatchResponse(accepted=True)


@app.post("/agent/v1/connections", response_model=TelemetryBatchResponse)
async def agent_connections(
    payload: AgentConnectionBatch,
    node: Node = Depends(require_agent),  # noqa: B008
) -> TelemetryBatchResponse:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")
    accepted = await _accept_telemetry_batch(
        node=node,
        kind="connection_snapshot",
        batch_id=payload.batch_id,
        sequence=payload.sequence,
        item_count=len(payload.snapshots),
    )
    if not accepted:
        return TelemetryBatchResponse(accepted=False, duplicate=True)
    documents = [
        ConnectionSnapshot(
            node_id=node.agent_id,
            site_id=node.site_id,
            batch_id=payload.batch_id,
            sampled_at=snapshot.sampled_at,
            active_connections=snapshot.active_connections,
            bytes_up=snapshot.bytes_up,
            bytes_down=snapshot.bytes_down,
            rx_bps=snapshot.rx_bps,
            tx_bps=snapshot.tx_bps,
            top_sources=[item.model_dump() for item in snapshot.top_sources],
            top_destinations=[item.model_dump() for item in snapshot.top_destinations],
            top_users=[item.model_dump() for item in snapshot.top_users],
            connections=[item.model_dump() for item in snapshot.connections],
            api_available=snapshot.api_available,
            expires_at=snapshot.sampled_at
            + timedelta(days=_settings().connection_history_retention_days),
        )
        for snapshot in payload.snapshots
    ]
    if documents:
        await ConnectionSnapshot.insert_many(documents)
        latest = max(documents, key=lambda item: item.sampled_at)
        node.active_connections = latest.active_connections
        node.bytes_up = latest.bytes_up
        node.bytes_down = latest.bytes_down
        node.rx_bps = latest.rx_bps
        node.tx_bps = latest.tx_bps
        await node.save()
    return TelemetryBatchResponse(accepted=True)


@app.post("/agent/v1/proxy-config", response_model=TelemetryBatchResponse)
@app.post("/agent/v1/proxy-configs", response_model=TelemetryBatchResponse)
async def agent_proxy_config(
    payload: AgentProxyConfigBatch,
    node: Node = Depends(require_agent),  # noqa: B008
) -> TelemetryBatchResponse:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")
    accepted = await _accept_telemetry_batch(
        node=node,
        kind="proxy_config",
        batch_id=payload.batch_id,
        sequence=payload.sequence,
        item_count=len(payload.groups),
    )
    if not accepted:
        return TelemetryBatchResponse(accepted=False, duplicate=True)
    groups: list[dict[str, Any]] = []
    for group in payload.groups:
        sanitized = _sanitize_proxy_group(group)
        if sanitized is not None:
            groups.append(sanitized.model_dump(mode="json"))
    received = utcnow()
    quality_samples = (
        _proxy_quality_samples(
            node=node,
            groups=payload.groups,
            sampled_at=payload.sampled_at,
            received_at=received,
        )
        if payload.api_available
        else []
    )
    snapshot = await ProxyConfigSnapshot.find_one(ProxyConfigSnapshot.node_id == node.agent_id)
    is_new_snapshot = snapshot is None
    if snapshot is None:
        snapshot = ProxyConfigSnapshot(node_id=node.agent_id, site_id=node.site_id)
    snapshot.site_id = node.site_id
    snapshot.batch_id = payload.batch_id
    snapshot.sampled_at = payload.sampled_at
    snapshot.api_available = payload.api_available
    # Keep the last known selectable groups when the loopback API is
    # temporarily unavailable.  The availability/error fields describe the
    # latest attempt, while the retained projection lets operators continue
    # comparing the node's configuration during a short outage.
    if payload.api_available:
        snapshot.groups = groups
    snapshot.error = _safe_error(payload.error, 256)
    snapshot.received_at = received
    snapshot.expires_at = payload.sampled_at + timedelta(days=7)
    if is_new_snapshot:
        await snapshot.insert()
    else:
        await snapshot.save()
    await _persist_proxy_quality_samples(quality_samples)
    return TelemetryBatchResponse(accepted=True)


@app.post("/agent/v1/probes", response_model=TelemetryBatchResponse)
async def agent_probes(
    payload: AgentProbeBatch,
    node: Node = Depends(require_agent),  # noqa: B008
) -> TelemetryBatchResponse:
    if payload.node_id != node.agent_id:
        raise HTTPException(409, "node_id_mismatch")

    # Validate every result before reserving the batch sequence. Otherwise a
    # malformed item late in the list would consume the sequence and make the
    # monitor drop the whole batch on retry.
    settings = _settings()
    sanitized_results: list[tuple[str, str, bool, int, str, datetime]] = []
    for result in payload.results:
        target_url = _safe_probe_target(result.target_url)
        outbound_tag = _safe_log_text(result.outbound_tag, 128)
        if not outbound_tag:
            raise HTTPException(422, "invalid_probe_outbound_tag")
        if len(sanitized_results) >= settings.probe_max_outbounds:
            raise HTTPException(422, "probe_outbound_limit_exceeded")
        sanitized_results.append(
            (
                outbound_tag,
                target_url,
                result.success,
                result.latency_ms,
                _safe_log_text(result.error_class, 64),
                result.sampled_at,
            )
        )
    accepted = await _accept_telemetry_batch(
        node=node,
        kind="probe_result",
        batch_id=payload.batch_id,
        sequence=payload.sequence,
        item_count=len(payload.results),
    )
    if not accepted:
        return TelemetryBatchResponse(accepted=False, duplicate=True)
    for (
        outbound_tag,
        target_url,
        success,
        latency_ms,
        error_class,
        sampled_at,
    ) in sanitized_results:
        await record_probe_result(
            node=node,
            batch_id=payload.batch_id,
            outbound_tag=outbound_tag,
            target_url=target_url,
            success=success,
            latency_ms=latency_ms,
            error_class=error_class,
            sampled_at=sampled_at,
        )
    if payload.task_id:
        task = await Task.find_one(Task.task_id == payload.task_id)
        if task is not None and task.target_id == node.agent_id and task.task_type == "node.probe":
            await complete_task(
                task,
                result={"result_count": len(payload.results)},
                message="Probe results received",
            )
    return TelemetryBatchResponse(accepted=True)


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
    if previous and payload.sequence == previous.sequence:
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
    await sync_node_alerts(node)
    all_acks = _latest_ack_per_node(
        await AgentAckDocument.find(AgentAckDocument.release_id == payload.release_id).to_list()
    )
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
        if release.status == "succeeded":
            release.error = ""
        else:
            # Preserve an actionable monitor error in the release record. The
            # old aggregate value hid the ACK code (for example a stale
            # outbound selection) and made the dashboard failure impossible
            # to diagnose without opening the node logs.
            failed_ack = next(
                (
                    item
                    for item in all_acks
                    if item.node_id in expected and not (item.ok and item.health_ok)
                ),
                None,
            )
            release.error = _safe_error(
                (failed_ack.error_code if failed_ack else "")
                or (failed_ack.error_message if failed_ack else "")
                or "one_or_more_nodes_failed"
            )
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
                task.result = {
                    "release_id": release.release_id,
                    "nodes": list(received_nodes),
                    "errors": [
                        {
                            "node_id": item.node_id,
                            "error_code": item.error_code,
                            "error_message": item.error_message,
                        }
                        for item in all_acks
                        if item.node_id in expected and not (item.ok and item.health_ok)
                    ],
                }
                task.finished_at = release.finished_at
                await task.save()
    return {"accepted": True}


@app.get("/api/v1/audit/verify")
async def audit_verify(_: str = Depends(require_management)) -> dict[str, Any]:
    valid, error, count = await verify_audit_chain()
    return {"valid": valid, "error": error, "event_count": count}


@app.get("/api/v1/audit", response_model=list[AuditEventOut])
async def list_audit(
    action: str | None = None,
    actor: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    _: str = Depends(require_management),
) -> list[AuditEventOut]:
    safe_limit = min(max(limit, 1), 500)
    query: dict[str, Any] = {}
    if action:
        query["action"] = action[:128]
    if actor:
        query["actor"] = actor[:128]
    if since is not None or until is not None:
        query["at"] = {
            **({"$gte": since} if since is not None else {}),
            **({"$lte": until} if until is not None else {}),
        }
    events = await AuditEvent.find(query).sort(-AuditEvent.at).limit(safe_limit).to_list()
    return [_audit_out(item) for item in events]


@app.get("/api/v1/audit/export")
async def export_audit(
    request: Request,
    export_format: str = "json",
    limit: int = 5_000,
    actor: str = Depends(require_management),
) -> Response:
    """Download a bounded, recursively redacted audit export.

    The query parameter is named ``export_format`` so it cannot collide with
    Python's built-in formatter names in generated OpenAPI clients.
    """

    if export_format not in {"json", "ndjson"}:
        raise HTTPException(422, "audit_export_format_invalid")
    safe_limit = min(max(limit, 1), 5_000)
    events = await AuditEvent.find_all().sort(+AuditEvent.at).limit(safe_limit).to_list()
    rows = [redact(_audit_out(item).model_dump(mode="json")) for item in events]
    request_id = _request_id(request)
    await append_audit(
        action="audit.export",
        target_type="audit",
        target_id="audit_event",
        actor=actor,
        actor_role="admin",
        request_id=request_id,
        source_ip=_request_source_ip(request),
        after={"format": export_format, "event_count": len(rows)},
    )
    if export_format == "ndjson":
        body = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )
        media_type = "application/x-ndjson"
        extension = "ndjson"
    else:
        body = json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        media_type = "application/json"
        extension = "json"
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="grouproxy-audit.{extension}"',
        },
    )


@app.get("/api/v1/backups", response_model=list[BackupRecordOut])
async def list_backups(
    limit: int = 100, _: str = Depends(require_management)
) -> list[BackupRecordOut]:
    safe_limit = min(max(limit, 1), 250)
    records = (
        await BackupRecord.find_all().sort(-BackupRecord.created_at).limit(safe_limit).to_list()
    )
    return [_backup_out(item) for item in records]


@app.post(
    "/api/v1/backups",
    response_model=BackupCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_backup(
    payload: BackupCreateRequest,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str = Depends(require_management),
) -> BackupCreateResponse:
    request_id = _request_id(request)
    idempotency_key = idempotency_key_header or (
        f"backup.create:{payload.scope}:{secrets.token_hex(16)}"
    )
    existing_task = await Task.find_one(Task.idempotency_key == idempotency_key)
    if existing_task is not None:
        existing_record = await BackupRecord.find_one(
            BackupRecord.backup_id == str(existing_task.payload.get("backup_id", ""))
        )
        if existing_record is not None:
            return BackupCreateResponse(
                backup=_backup_out(existing_record), task=_task_out(existing_task)
            )
    # Derive the record id from the idempotency key so two simultaneous callers
    # cannot leave an orphaned backup record when only one task wins the unique
    # key race.
    idempotency_digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    backup_id = f"bkp_{idempotency_digest[:32]}"
    record = await BackupRecord.find_one(BackupRecord.backup_id == backup_id)
    if record is not None:
        task = await Task.find_one(Task.idempotency_key == idempotency_key)
        if task is not None:
            return BackupCreateResponse(backup=_backup_out(record), task=_task_out(task))
    if record is None:
        record = BackupRecord(
            backup_id=backup_id,
            scope=payload.scope,
            origin="manual",
            status="queued",
            created_by=actor,
        )
        try:
            await record.insert()
        except DuplicateKeyError:
            record = await BackupRecord.find_one(BackupRecord.backup_id == backup_id)
            if record is None:
                raise
    try:
        task, created = await create_task(
            task_type="backup.create",
            target_type="backup",
            target_id=record.backup_id,
            payload={"backup_id": record.backup_id, "scope": record.scope},
            idempotency_key=idempotency_key,
            created_by=actor,
            request_id=request_id,
        )
    except Exception:
        if record.status == "queued" and not record.storage_ref:
            await record.delete()
        raise
    if not created:
        return BackupCreateResponse(backup=_backup_out(record), task=_task_out(task))
    await append_audit(
        action="backup.create.request",
        target_type="backup",
        target_id=record.backup_id,
        actor=actor,
        actor_role="admin",
        request_id=request_id,
        source_ip=_request_source_ip(request),
        after={"scope": record.scope, "task_id": task.task_id},
    )
    return BackupCreateResponse(backup=_backup_out(record), task=_task_out(task))


@app.post(
    "/api/v1/backups/{backup_id}/restore",
    response_model=BackupRestoreResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restore_backup_task(
    backup_id: str,
    payload: BackupRestoreRequest,
    request: Request,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str = Depends(require_management),
) -> BackupRestoreResponse:
    record = await BackupRecord.find_one(BackupRecord.backup_id == backup_id)
    if record is None:
        raise HTTPException(404, "backup_not_found")
    if record.status in {"planned", "queued", "running", "failed"} or not record.storage_ref:
        raise HTTPException(409, "backup_not_verified")
    active_restore = await Task.find_one(
        {
            "task_type": "backup.restore",
            "target_id": backup_id,
            "active": True,
        }
    )
    if active_restore is not None:
        return BackupRestoreResponse(backup=_backup_out(record), task=_task_out(active_restore))
    request_id = _request_id(request)
    idempotency_key = idempotency_key_header or (
        "backup.restore:"
        f"{backup_id}:{'apply' if payload.confirm else 'rehearsal'}:"
        f"{secrets.token_hex(12)}"
    )
    existing_task = await Task.find_one(Task.idempotency_key == idempotency_key)
    if existing_task is not None:
        return BackupRestoreResponse(backup=_backup_out(record), task=_task_out(existing_task))
    try:
        task, created = await create_task(
            task_type="backup.restore",
            target_type="backup",
            target_id=backup_id,
            payload={"backup_id": backup_id, "confirm": payload.confirm},
            idempotency_key=idempotency_key,
            created_by=actor,
            request_id=request_id,
        )
    except DuplicateKeyError:
        task = await Task.find_one(
            {
                "task_type": "backup.restore",
                "target_id": backup_id,
                "active": True,
            }
        )
        if task is None:
            raise
        created = False
    if not created:
        return BackupRestoreResponse(backup=_backup_out(record), task=_task_out(task))
    record.restore_task_id = task.task_id
    record.status = "restore_queued"
    await record.save()
    await append_audit(
        action="backup.restore.request",
        target_type="backup",
        target_id=backup_id,
        actor=actor,
        actor_role="admin",
        request_id=request_id,
        source_ip=_request_source_ip(request),
        after={"task_id": task.task_id, "confirmed": payload.confirm},
    )
    return BackupRestoreResponse(backup=_backup_out(record), task=_task_out(task))


@app.get("/api/v1/overview")
async def overview(_: str = Depends(require_management)) -> dict[str, Any]:
    nodes = await Node.find_all().to_list()
    node_traffic = [
        {
            "node_id": node.agent_id,
            "name": node.name,
            "site_id": node.site_id,
            "liveness_status": node.liveness_status,
            "active_connections": node.active_connections,
            "bytes_up": node.bytes_up,
            "bytes_down": node.bytes_down,
            "rx_bps": node.rx_bps,
            "tx_bps": node.tx_bps,
        }
        for node in nodes
    ]
    open_circuits = await ProbeCircuit.find(ProbeCircuit.state == "open").count()
    open_alerts = await Alert.find(Alert.status == "open").count()
    return {
        "sites": len(await Site.find_all().to_list()),
        "nodes": len(nodes),
        "online_nodes": sum(node.liveness_status == "online" for node in nodes),
        "in_sync_nodes": sum(node.config_status == "in_sync" for node in nodes),
        "drifted_nodes": sum(
            node.config_status in {"drift", "failed", "rollback_failed"} for node in nodes
        ),
        "connections": sum(node.active_connections for node in nodes),
        "bytes_up": sum(node.bytes_up for node in nodes),
        "bytes_down": sum(node.bytes_down for node in nodes),
        "rx_bps": sum(node.rx_bps for node in nodes),
        "tx_bps": sum(node.tx_bps for node in nodes),
        "node_traffic": node_traffic,
        "open_circuits": open_circuits,
        "open_alerts": open_alerts,
        "http_only": True,
    }


@app.get("/api/v1/access/linux-setup.sh", response_class=PlainTextResponse)
async def linux_setup() -> PlainTextResponse:
    return PlainTextResponse(
        load_linux_setup_script(_settings()),
        headers={"Content-Disposition": 'attachment; filename="grouproxy-linux-setup.sh"'},
        media_type="text/x-shellscript",
    )


@app.get("/api/v1/access/windows-setup.ps1", response_class=PlainTextResponse)
async def windows_setup() -> PlainTextResponse:
    return PlainTextResponse(
        load_windows_setup_script(_settings()),
        headers={"Content-Disposition": 'attachment; filename="grouproxy-windows-setup.ps1"'},
        media_type="text/plain",
    )


@app.get("/api/v1/access/config", response_model=AccessConfigOut)
async def access_config() -> AccessConfigOut:
    profile = access_profile(_settings())
    return AccessConfigOut(
        environment=profile.environment,
        fqdn=profile.fqdn,
        port=PROXY_LISTEN_PORT,
        macos_shortcut_url=profile.macos_shortcut_url,
    )


@app.get("/api/v1/access/proxy.pac", response_class=PlainTextResponse)
async def proxy_pac() -> PlainTextResponse:
    profile = access_profile(_settings())
    # PAC only chooses the single HTTP listener. It is not an authorization
    # layer and never embeds regional IP addresses.
    content = f"""function FindProxyForURL(url, host) {{
  if (isPlainHostName(host) ||
      shExpMatch(host, \"localhost\") ||
      isInNet(host, \"10.0.0.0\", \"255.0.0.0\") ||
      isInNet(host, \"172.16.0.0\", \"255.240.0.0\") ||
      isInNet(host, \"192.168.0.0\", \"255.255.0.0\")) return \"DIRECT\";
  return \"PROXY {profile.fqdn}:{PROXY_LISTEN_PORT}\";
}}
"""
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": 'attachment; filename="grouproxy-proxy.pac"'},
        media_type="application/x-ns-proxy-autoconfig",
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host=_settings().host, port=_settings().port, reload=False)
