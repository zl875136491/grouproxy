from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from beanie import Document, Indexed
from pydantic import Field
from pymongo import ASCENDING, IndexModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminUser(Document):
    """Management account keyed by the employee's immutable IT code.

    ``username`` remains as a compatibility mirror for the original control
    plane collection. New code must use ``itcode`` as the account identity.
    """

    username: Indexed(str, unique=True)
    itcode: str = ""
    password_hash: str
    is_active: bool = True
    auth_source: str = "local"
    password_changed_at: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        indexes = [
            IndexModel(
                [("itcode", ASCENDING)],
                name="unique_admin_itcode",
                unique=True,
                sparse=True,
            )
        ]


class AuthVerificationChallenge(Document):
    """Short-lived, single-use GQuan verification challenge.

    The code is deliberately stored only as a salted digest. The associated
    app-token delivery result does not include raw One Login payloads.
    """

    challenge_id: Indexed(str, unique=True)
    itcode: str
    purpose: str
    code_hash: str
    status: str = "pending"
    failed_attempts: int = 0
    source_ip: str = ""
    delivery_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    resend_available_at: datetime
    delivered_at: datetime | None = None
    consumed_at: datetime | None = None

    class Settings:
        indexes = [
            IndexModel(
                [("itcode", ASCENDING), ("purpose", ASCENDING), ("created_at", -1)],
                name="verification_challenge_lookup",
            ),
            IndexModel(
                [("expires_at", ASCENDING)],
                name="verification_challenge_ttl",
                expireAfterSeconds=0,
            ),
        ]


class ManagementSession(Document):
    """Opaque browser session; only a SHA-256 token digest is persisted."""

    session_id: Indexed(str, unique=True)
    token_hash: Indexed(str, unique=True)
    user_id: str
    itcode: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None

    class Settings:
        indexes = [
            IndexModel(
                [("expires_at", ASCENDING)],
                name="management_session_ttl",
                expireAfterSeconds=0,
            )
        ]


class Site(Document):
    slug: Indexed(str, unique=True)
    name: str
    dns_note: str = ""
    proxy_auth_required: bool = False
    http_port: int = 80
    shutdown: bool = False
    config_revision: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class Node(Document):
    site_id: str
    name: str
    agent_id: Indexed(str, unique=True)
    agent_token_hash: str
    advertise_ip: str = ""
    monitor_version: str = "unknown"
    singbox_version: str = "unknown"
    last_seen_at: datetime | None = None
    desired_version: int = 0
    applied_version: int = 0
    applied_hash: str = ""
    liveness_status: str = "offline"
    config_status: str = "unknown"
    service_status: str = "unknown"
    subscription_status: str = "not_configured"
    last_error: str = ""
    last_error_at: datetime | None = None
    last_successful_reload_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SiteCIDR(Document):
    site_id: str
    cidr: str
    comment: str = ""
    enabled: bool = True
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)


class TravelException(Document):
    cidr: str
    comment: str = ""
    owner: str = ""
    expires_at: datetime
    enabled: bool = True
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)


class CrossSiteAllow(Document):
    from_site_id: str
    to_site_id: str
    enabled: bool = False
    comment: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class DestinationBlacklist(Document):
    pattern: str
    kind: str = "domain"
    comment: str = ""
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class SubscriptionSource(Document):
    """A control-plane-owned upstream subscription definition.

    ``url`` can contain an upstream credential, so API response models must
    never expose it. Nodes only ever receive an immutable content version.
    """

    name: Indexed(str, unique=True)
    url: str = ""
    secret_ref: str = ""
    fetch_interval_sec: int = 21_600
    max_body_bytes: int = 2_000_000
    redirect_limit: int = 3
    enabled: bool = True
    last_refresh_at: datetime | None = None
    last_refresh_attempt_at: datetime | None = None
    last_refresh_error: str = ""
    consecutive_failures: int = 0
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SubscriptionVersion(Document):
    """An immutable fetched or uploaded subscription payload."""

    source_id: str
    version: int
    content_hash: Indexed(str)
    size_bytes: int
    format: str
    content: bytes
    fetched_at: datetime = Field(default_factory=utcnow)
    parse_ok: bool = False
    parse_error: str = ""
    node_count: int = 0
    published: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class SiteSubscription(Document):
    """The currently selected subscription version for one site."""

    site_id: Indexed(str, unique=True)
    subscription_version_id: str
    previous_subscription_version_id: str | None = None
    source_id: str
    updated_by: str = "system"
    updated_at: datetime = Field(default_factory=utcnow)


class DesiredRelease(Document):
    release_id: str
    node_id: str
    site_id: str
    desired_version: int
    source_revision: int = 0
    bundle_hash: str
    bundle: dict[str, Any]
    previous_release_id: str | None = None
    status: str = "generated"
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    created_by: str = "system"


class AgentAck(Document):
    node_id: str
    release_id: str
    desired_version: int
    applied_version: int
    bundle_hash: str
    applied_hash: str
    ok: bool
    singbox_ok: bool = False
    nft_ok: bool = False
    health_ok: bool = False
    rollback_attempted: bool = False
    rollback_ok: bool = False
    last_good_version: int = 0
    stage: str = "unknown"
    error_code: str = ""
    error_message: str = ""
    sequence: int = 0
    received_at: datetime = Field(default_factory=utcnow)


class ConfigDraft(Document):
    site_id: str
    node_ids: list[str] = Field(default_factory=list)
    source_revision: int
    diff: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "low"
    status: str = "draft"
    created_by: str = "system"
    expires_at: datetime
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ConfigRelease(Document):
    release_id: Indexed(str, unique=True)
    site_id: str
    node_ids: list[str] = Field(default_factory=list)
    desired_release_id: str
    previous_release_id: str | None = None
    task_id: str | None = None
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    error: str = ""
    rollback_reason: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)


class Task(Document):
    task_id: Indexed(str, unique=True)
    task_type: str
    target_type: str
    target_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Indexed(str, unique=True)
    status: str = "queued"
    active: bool = True
    progress: int = 0
    stage: str = "queued"
    progress_message: str = ""
    retry_count: int = 0
    max_retries: int = 3
    next_run_at: datetime = Field(default_factory=utcnow)
    locked_by: str = ""
    locked_at: datetime | None = None
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False
    error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    class Settings:
        indexes = [
            IndexModel(
                [("target_id", ASCENDING)],
                name="unique_active_subscription_refresh",
                unique=True,
                partialFilterExpression={
                    "task_type": "subscription.refresh",
                    "active": True,
                },
            )
        ]


class HeartbeatLatest(Document):
    node_id: Indexed(str, unique=True)
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=utcnow)


class HeartbeatSample(Document):
    node_id: str
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=utcnow)


class AuditEvent(Document):
    event_id: Indexed(str, unique=True) = Field(default_factory=lambda: str(uuid4()))
    actor: str = "system"
    actor_role: str = "system"
    request_id: str = ""
    source_ip: str = ""
    action: str
    target_type: str
    target_id: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    result: str = "success"
    error: str = ""
    previous_hash: str = ""
    immutable_hash: Indexed(str, unique=True)
    at: datetime = Field(default_factory=utcnow)


class AccessLog(Document):
    ts: Indexed(datetime)
    site_id: str
    node_id: str
    policy_version: int
    src_ip: str
    src_cidr_match: str = ""
    username: str = ""
    cert_fp: str = ""
    dst_host: str = ""
    dst_port: int = 0
    action: str
    deny_reason: str = ""
    bytes_up: int = 0
    bytes_down: int = 0
    duration_ms: int = 0


class ConnectionSnapshot(Document):
    node_id: str
    site_id: str
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=utcnow)


class BackupRecord(Document):
    backup_id: Indexed(str, unique=True)
    scope: str
    artifact_paths: list[str] = Field(default_factory=list)
    format: str = "tar.zst"
    checksum: str = ""
    encrypted: bool = False
    storage_ref: str = ""
    status: str = "planned"
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utcnow)
    verified_at: datetime | None = None
    restore_task_id: str | None = None
    error: str = ""


DOCUMENT_MODELS: list[type[Document]] = [
    AdminUser,
    AuthVerificationChallenge,
    ManagementSession,
    Site,
    Node,
    SiteCIDR,
    TravelException,
    CrossSiteAllow,
    DestinationBlacklist,
    SubscriptionSource,
    SubscriptionVersion,
    SiteSubscription,
    DesiredRelease,
    AgentAck,
    ConfigDraft,
    ConfigRelease,
    Task,
    HeartbeatLatest,
    HeartbeatSample,
    AuditEvent,
    AccessLog,
    ConnectionSnapshot,
    BackupRecord,
]
