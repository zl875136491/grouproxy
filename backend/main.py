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
import re
import secrets
import socket
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from pymongo.errors import DuplicateKeyError

from app.config import Settings, get_settings
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
    CrossSiteAllow,
    DesiredRelease,
    DestinationBlacklist,
    HeartbeatLatest,
    HeartbeatSample,
    ManagementSession,
    Node,
    ProbeCircuit,
    ProbeHistory,
    ProxyConfigSnapshot,
    ProxyCredential,
    Site,
    SiteCIDR,
    SiteSubscription,
    SubscriptionSource,
    SubscriptionVersion,
    Task,
    TelemetryBatch,
    TelemetryCursor,
    TravelException,
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
    CIDRCreate,
    CIDROut,
    CIDRPreviewRequest,
    CIDRPreviewResponse,
    ConnectionSnapshotOut,
    CrossSiteAllowOut,
    CrossSiteAllowUpdate,
    DesiredResponse,
    DestinationBlacklistCreate,
    DestinationBlacklistOut,
    DraftCreate,
    DraftOut,
    EmployeeAccessSiteOut,
    EmployeeOut,
    EmployeeProxyAccessOut,
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
    ProxySelectionRequest,
    ProxyCredentialOut,
    ProxyCredentialReveal,
    ProxyEndpointSnapshot,
    ProxyGroupSnapshot,
    RegistrationRequest,
    ReleaseCreate,
    ReleaseOut,
    SiteNameUpdate,
    SiteOut,
    SiteProxyAuthUpdate,
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
    TelemetryBatchResponse,
    TravelExceptionCreate,
    TravelExceptionOut,
    VerificationCodeRequest,
    VerificationCodeResponse,
)
from app.services.access import render_linux_setup_script
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
from app.services.bundles import create_desired_release, latest_release
from app.services.cidr import effective_cidrs, match_source_ip, normalize_cidr, normalize_source_ip
from app.services.probes import record_probe_result
from app.services.proxy_credentials import (
    ProxyCredentialError,
    ProxyCredentialRotation,
    active_proxy_credential_count,
    credential_secret,
    proxy_auth_bundle,
    restore_proxy_credential,
    rotate_proxy_credential,
)
from app.services.subscription_worker import SubscriptionWorker, enqueue_refresh_task
from app.services.subscriptions import (
    SubscriptionError,
    normalize_source_url,
    record_uploaded_subscription,
    source_url_hint,
)
from app.services.tasks import (
    claim_probe_task_for_node,
    complete_task,
    create_task,
    fail_task,
    reclaim_expired_tasks,
)

DEFAULT_SITES = [
    ("north", "North Region"),
    ("east", "East Region"),
    ("south", "South Region"),
    ("west", "West Region"),
    ("central", "Central Region"),
]

_management_actor: ContextVar[str] = ContextVar("management_actor", default="")


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
        proxy_auth_required=site.proxy_auth_required,
        http_port=site.http_port,
        shutdown=site.shutdown,
        config_revision=site.config_revision,
    )


def _proxy_credential_out(item: ProxyCredential) -> ProxyCredentialOut:
    return ProxyCredentialOut(
        site_id=item.site_id,
        username=item.username,
        active=item.active,
        rotated_at=item.rotated_at,
    )


def _employee_out(user: AdminUser) -> EmployeeOut:
    return EmployeeOut(
        itcode=user.itcode or user.username,
        auth_source=user.auth_source,
        is_active=user.is_active,
        created_at=user.created_at,
        password_changed_at=user.password_changed_at,
        last_login_at=user.last_login_at,
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
        probe_status=node.probe_status,
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
        top_sources=item.top_sources,
        top_destinations=item.top_destinations,
        top_users=item.top_users,
        api_available=item.api_available,
        received_at=item.received_at,
    )


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


async def _accept_telemetry_batch(
    *, node: Node, kind: str, batch_id: str, sequence: int, item_count: int
) -> bool:
    """Reserve and persist one monotonic telemetry batch.

    A read-then-insert check is racy when two monitor retries arrive together.
    The cursor update below is conditional in MongoDB, so only the request with
    the highest sequence can advance it; the unique batch/sequence indexes then
    make replayed payloads idempotent.
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

    try:
        await TelemetryBatch(
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
            role="admin",
            auth_source="local",
            password_changed_at=utcnow(),
        ).insert()
    else:
        changed = False
        if not admin.itcode:
            admin.itcode = admin_itcode
            changed = True
        # Existing installations predate roles. Only the configured bootstrap
        # account is migrated to admin; self-registered accounts remain least
        # privileged even if they held an older browser session.
        if admin.role != "admin":
            admin.role = "admin"
            changed = True
        if changed:
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
    backup_worker = BackupWorker(settings)
    backup_worker_task = asyncio.create_task(backup_worker.run())

    async def observe() -> None:
        while True:
            try:
                await refresh_liveness()
                await refresh_deny_spike_alerts()
            except Exception:
                # Observability must never take down the control plane.
                pass
            await asyncio.sleep(15)

    observe_task = asyncio.create_task(observe())
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


async def require_authenticated(request: Request) -> AuthenticatedPrincipal:
    settings = _settings()
    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    if token and hmac.compare_digest(token, settings.management_token):
        return AuthenticatedPrincipal(
            itcode=normalize_itcode(settings.admin_username), role="admin"
        )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="management_auth_required"
        )
    resolved = await resolve_session(token)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="management_auth_required"
        )
    session, user = resolved
    return AuthenticatedPrincipal(itcode=session.itcode, role=user.role)


async def require_management(request: Request) -> str:
    principal = await require_authenticated(request)
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="management_admin_required"
        )
    _management_actor.set(principal.itcode)
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
async def list_employees(_: str = Depends(require_management)) -> list[EmployeeOut]:
    """List employee identities without exposing password or session material."""

    employees = (
        await AdminUser.find({"role": "employee"})
        .sort("+itcode")
        .to_list()
    )
    return [_employee_out(employee) for employee in employees]


@app.get(
    "/api/v1/employees/{itcode}/proxy-credentials",
    response_model=list[ProxyCredentialOut],
)
async def list_employee_proxy_credentials(
    itcode: str, _: str = Depends(require_management)
) -> list[ProxyCredentialOut]:
    """Return credential metadata for the selected employee, never a secret."""

    try:
        subject_itcode = normalize_itcode(itcode)
    except AuthError as exc:
        raise _auth_http_error(exc) from exc
    employee = await find_user_by_itcode(subject_itcode)
    if employee is None or employee.role != "employee":
        raise HTTPException(404, "employee_not_found")
    credentials = (
        await ProxyCredential.find(ProxyCredential.itcode == subject_itcode)
        .sort(+ProxyCredential.site_id)
        .to_list()
    )
    return [_proxy_credential_out(credential) for credential in credentials]


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


@app.put("/api/v1/sites/{site_id}/proxy-auth", response_model=SiteOut)
async def set_site_proxy_auth(
    site_id: str,
    payload: SiteProxyAuthUpdate,
    request: Request,
    _: str = Depends(require_management),
) -> SiteOut:
    """Enable or disable HTTP Basic at one site for the next normal release."""

    site = await Site.get(site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    if site.proxy_auth_required == payload.required:
        return _site_out(site)
    if payload.required:
        try:
            # This validates both the backend-only derivation secret and every
            # current credential before a policy can require authentication.
            credential_secret(_settings())
            if await active_proxy_credential_count(site_id) < 1:
                raise ProxyCredentialError("proxy_auth_requires_credential")
            await proxy_auth_bundle(site_id=site_id, required=True, settings=_settings())
        except ProxyCredentialError as exc:
            raise HTTPException(409, str(exc)) from exc

    before = {"proxy_auth_required": site.proxy_auth_required}
    site.proxy_auth_required = payload.required
    site.config_revision += 1
    await site.save()
    await append_audit(
        action="site.proxy_auth.update",
        target_type="site",
        target_id=site_id,
        actor=_actor(),
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
        before=before,
        after={"proxy_auth_required": site.proxy_auth_required},
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
async def delete_exception(exception_id: str, _: str = Depends(require_management)) -> None:
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
    return [_blacklist_out(item) for item in entries]


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
            site=site,
            nodes=selected,
            settings=_settings(),
            created_by=actor,
            proxy_selection=(draft.diff.get("proxy_selection") if isinstance(draft.diff, dict) else None),
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


async def _publish_proxy_credential_change(
    *,
    site: Site,
    rotation: ProxyCredentialRotation,
    actor: str,
    request_id: str,
) -> ConfigRelease | None:
    """Create the traceable release that delivers a rotated active password."""

    nodes = await Node.find(Node.site_id == _model_id(site)).to_list()
    if not nodes:
        # A credential can be prepared before a site receives its first node.
        # The normal first release will include it later.
        return None
    agent_ids = [node.agent_id for node in nodes]
    active_release = await ConfigRelease.find_one(
        {
            "node_ids": {"$in": agent_ids},
            "status": {"$in": ["queued", "applying", "health_check", "rolling_back"]},
        }
    )
    if active_release is not None:
        raise HTTPException(409, "release_in_progress")
    cidrs, sources = await effective_cidrs(_model_id(site))
    draft = ConfigDraft(
        site_id=_model_id(site),
        node_ids=[_model_id(node) for node in nodes],
        source_revision=site.config_revision,
        diff={
            "proxy_auth": {
                "required": True,
                "credential_change": {
                    "itcode": rotation.credential.itcode,
                    "username": rotation.credential.username,
                    "action": "created" if rotation.created else "rotated",
                },
            }
        },
        validation={
            "valid": True,
            "errors": [],
            "effective_cidrs": cidrs,
            "acl_sources": sources,
            "proxy_auth": {
                "required": True,
                "credential_count": await active_proxy_credential_count(_model_id(site)),
            },
        },
        risk_level="medium",
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
            idempotency_key=(
                f"proxy_credential.rotate:{_model_id(site)}:{rotation.credential.credential_id}"
            ),
            request_id=request_id,
            actor=actor,
        )
    except Exception:
        # Do not leave a user-facing draft behind when its automatic deployment
        # was rejected before any Desired Bundle was created.
        draft.status = "expired"
        draft.updated_at = utcnow()
        await draft.save()
        raise
    return release


async def _rotate_proxy_credential_for_subject(
    *,
    site: Site,
    itcode: str,
    actor: str,
    actor_role: str,
    request: Request,
) -> ProxyCredentialReveal:
    """Rotate a credential and start delivery when its site requires auth."""

    settings = _settings()
    try:
        rotation = await rotate_proxy_credential(
            site_id=_model_id(site), itcode=itcode, settings=settings
        )
    except ProxyCredentialError as exc:
        raise HTTPException(409, str(exc)) from exc

    original_revision = site.config_revision
    release: ConfigRelease | None = None
    try:
        site.config_revision += 1
        await site.save()
        if site.proxy_auth_required:
            release = await _publish_proxy_credential_change(
                site=site,
                rotation=rotation,
                actor=actor,
                request_id=_request_id(request),
            )
    except Exception:
        await restore_proxy_credential(rotation)
        # A rejected automatic release must not make an unrelated policy
        # revision appear pending. Preserve a later revision if one exists.
        current_site = await Site.get(_model_id(site))
        if current_site is not None and current_site.config_revision == original_revision + 1:
            current_site.config_revision = original_revision
            await current_site.save()
        raise

    await append_audit(
        action="proxy_credential.rotate",
        target_type="proxy_credential",
        target_id=rotation.credential.credential_id,
        actor=actor,
        actor_role=actor_role,
        request_id=_request_id(request),
        source_ip=_request_source_ip(request),
        before={
            "site_id": _model_id(site),
            "itcode": itcode,
            "credential_configured": rotation.previous is not None,
            "active": rotation.previous.active if rotation.previous is not None else False,
        },
        after={
            "site_id": _model_id(site),
            "itcode": itcode,
            "username": rotation.credential.username,
            "active": rotation.credential.active,
            "release_id": release.release_id if release is not None else None,
        },
    )
    return ProxyCredentialReveal(
        **_proxy_credential_out(rotation.credential).model_dump(),
        password=rotation.password,
        release_id=release.release_id if release is not None else None,
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
    return [_release_out(item) for item in releases]


@app.get("/api/v1/config/releases/{release_id}/acks", response_model=list[AgentAckOut])
async def list_release_acks(
    release_id: str, _: str = Depends(require_management)
) -> list[AgentAckOut]:
    release = await ConfigRelease.find_one(ConfigRelease.release_id == release_id)
    if release is None:
        raise HTTPException(404, "release_not_found")
    acks = (
        await AgentAckDocument.find(AgentAckDocument.release_id == release_id)
        .sort(+AgentAckDocument.node_id)
        .to_list()
    )
    return [_ack_out(item) for item in acks]


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
    idempotency_key = (idempotency_key_header or "").strip() or (
        "proxy-selection:"
        f"{node.agent_id}:{group_name}:{outbound_name}:"
        f"{payload.expected_current_version if payload.expected_current_version is not None else 'latest'}"
    )
    # Idempotency is checked before creating a draft. A browser retry should
    # return the existing release and must not leave a second draft behind.
    existing_task = await Task.find_one(Task.idempotency_key == idempotency_key)
    if existing_task is not None:
        existing_release = await ConfigRelease.find_one(
            ConfigRelease.task_id == existing_task.task_id
        )
        if existing_release is not None:
            return _release_out(existing_release)
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
    cidrs, sources = await effective_cidrs(node.site_id)
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
            "effective_cidrs": cidrs,
            "acl_sources": sources,
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
        return _release_out(release)
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
    return _release_out(release)


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
    if previous and payload.sequence <= int(previous.payload.get("sequence", -1)):
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
            top_sources=[item.model_dump() for item in snapshot.top_sources],
            top_destinations=[item.model_dump() for item in snapshot.top_destinations],
            top_users=[item.model_dump() for item in snapshot.top_users],
            api_available=snapshot.api_available,
            expires_at=snapshot.sampled_at + timedelta(days=7),
        )
        for snapshot in payload.snapshots
    ]
    if documents:
        await ConnectionSnapshot.insert_many(documents)
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
    await sync_node_alerts(node)
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
    latest_connections = await (
        ConnectionSnapshot.find_all().sort(-ConnectionSnapshot.sampled_at).limit(500).to_list()
    )
    seen_nodes: set[str] = set()
    connection_count = 0
    for snapshot in latest_connections:
        if snapshot.node_id in seen_nodes:
            continue
        seen_nodes.add(snapshot.node_id)
        connection_count += snapshot.active_connections
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
        "connections": connection_count,
        "open_circuits": open_circuits,
        "open_alerts": open_alerts,
        "http_only": True,
    }


@app.get("/api/v1/access/linux-setup.sh", response_class=PlainTextResponse)
async def linux_setup(
    _: AuthenticatedPrincipal = Depends(require_authenticated),  # noqa: B008 - FastAPI dependency declaration
) -> str:
    return render_linux_setup_script(_settings())


@app.get("/api/v1/access/config", response_model=AccessConfigOut)
async def access_config(
    _: AuthenticatedPrincipal = Depends(require_authenticated),  # noqa: B008 - FastAPI dependency declaration
) -> AccessConfigOut:
    settings = _settings()
    return AccessConfigOut(fqdn=settings.proxy_access_fqdn, port=settings.proxy_access_port)


@app.get("/api/v1/access/proxy-credentials", response_model=EmployeeProxyAccessOut)
async def employee_proxy_access(
    principal: AuthenticatedPrincipal = Depends(  # noqa: B008 - FastAPI dependency declaration
        require_authenticated
    ),
) -> EmployeeProxyAccessOut:
    credentials = await ProxyCredential.find(ProxyCredential.itcode == principal.itcode).to_list()
    credentials_by_site = {item.site_id: item for item in credentials}
    sites = await Site.find_all().sort(+Site.slug).to_list()
    return EmployeeProxyAccessOut(
        itcode=principal.itcode,
        sites=[
            EmployeeAccessSiteOut(
                id=_model_id(site),
                slug=site.slug,
                name=site.name,
                proxy_auth_required=site.proxy_auth_required,
                credential_configured=bool(
                    credentials_by_site.get(_model_id(site))
                    and credentials_by_site[_model_id(site)].active
                ),
                username=(
                    credentials_by_site[_model_id(site)].username
                    if credentials_by_site.get(_model_id(site))
                    and credentials_by_site[_model_id(site)].active
                    else None
                ),
            )
            for site in sites
        ],
    )


@app.post(
    "/api/v1/access/proxy-credentials/{site_id}/rotate",
    response_model=ProxyCredentialReveal,
)
async def rotate_own_proxy_credential(
    site_id: str,
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(  # noqa: B008 - FastAPI dependency declaration
        require_authenticated
    ),
) -> ProxyCredentialReveal:
    site = await Site.get(site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return await _rotate_proxy_credential_for_subject(
        site=site,
        itcode=principal.itcode,
        actor=principal.itcode,
        actor_role=principal.role,
        request=request,
    )


@app.post(
    "/api/v1/sites/{site_id}/proxy-credentials/{itcode}/rotate",
    response_model=ProxyCredentialReveal,
)
async def admin_rotate_proxy_credential(
    site_id: str,
    itcode: str,
    request: Request,
    response: Response,
    _: str = Depends(require_management),
) -> ProxyCredentialReveal:
    site = await Site.get(site_id)
    if site is None:
        raise HTTPException(404, "site_not_found")
    try:
        subject_itcode = normalize_itcode(itcode)
    except AuthError as exc:
        raise _auth_http_error(exc) from exc
    subject = await find_user_by_itcode(subject_itcode)
    if subject is None or not subject.is_active:
        raise HTTPException(404, "employee_not_found")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return await _rotate_proxy_credential_for_subject(
        site=site,
        itcode=subject_itcode,
        actor=_actor(),
        actor_role="admin",
        request=request,
    )


@app.get("/api/v1/access/proxy.pac", response_class=PlainTextResponse)
async def proxy_pac(
    _: AuthenticatedPrincipal = Depends(require_authenticated),  # noqa: B008 - FastAPI dependency declaration
) -> str:
    settings = _settings()
    # PAC only chooses the single HTTP listener. It is not an authorization
    # layer and never embeds regional IP addresses.
    return f"""function FindProxyForURL(url, host) {{
  if (isPlainHostName(host) ||
      shExpMatch(host, \"localhost\") ||
      isInNet(host, \"10.0.0.0\", \"255.0.0.0\") ||
      isInNet(host, \"172.16.0.0\", \"255.240.0.0\") ||
      isInNet(host, \"192.168.0.0\", \"255.255.0.0\")) return \"DIRECT\";
  return \"PROXY {settings.proxy_access_fqdn}:{settings.proxy_access_port}\";
}}
"""


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host=_settings().host, port=_settings().port, reload=False)
