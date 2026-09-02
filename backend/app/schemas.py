from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginRequest(BaseModel):
    itcode: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    itcode: str
    role: Literal["admin", "employee"]
    expires_at: datetime


class VerificationCodeRequest(BaseModel):
    itcode: str = Field(min_length=2, max_length=64)
    purpose: Literal["register", "password_change", "gquan_login"]


class VerificationCodeResponse(BaseModel):
    challenge_id: str
    expires_at: datetime
    resend_available_at: datetime


class RegistrationRequest(BaseModel):
    itcode: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=128)
    challenge_id: str = Field(min_length=16, max_length=128)
    verification_code: str = Field(pattern=r"^\d{6}$")


class PasswordChangeRequest(BaseModel):
    itcode: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=128)
    challenge_id: str = Field(min_length=16, max_length=128)
    verification_code: str = Field(pattern=r"^\d{6}$")


class GQuanLoginRequest(BaseModel):
    itcode: str = Field(min_length=2, max_length=64)
    challenge_id: str = Field(min_length=16, max_length=128)
    verification_code: str = Field(pattern=r"^\d{6}$")


class AuthActionResponse(BaseModel):
    status: Literal["ok"] = "ok"


class SiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    name: str
    dns_note: str
    proxy_auth_required: bool
    http_port: int
    shutdown: bool
    config_revision: int


class SiteProxyAuthUpdate(BaseModel):
    required: bool


class SiteNameUpdate(BaseModel):
    """Update only the operator-facing site label.

    The slug, network policy, listener and release revision are intentionally
    outside this payload.  Renaming a site is presentation metadata and does
    not require a node rollout.
    """

    name: str = Field(min_length=1, max_length=128)


class ProxyCredentialOut(BaseModel):
    site_id: str
    username: str
    active: bool
    rotated_at: datetime


class ProxyCredentialReveal(ProxyCredentialOut):
    password: str
    release_id: str | None = None


class EmployeeOut(BaseModel):
    """Management-safe view of a local employee account."""

    itcode: str
    auth_source: str
    is_active: bool
    created_at: datetime
    password_changed_at: datetime | None
    last_login_at: datetime | None


class EmployeeAccessSiteOut(BaseModel):
    id: str
    slug: str
    name: str
    proxy_auth_required: bool
    credential_configured: bool
    username: str | None = None


class EmployeeProxyAccessOut(BaseModel):
    itcode: str
    sites: list[EmployeeAccessSiteOut]


class NodeCreate(BaseModel):
    site_id: str
    name: str
    agent_id: str
    advertise_ip: str = ""


class NodeNameUpdate(BaseModel):
    """Only the operator-facing label is mutable; node identity is not."""

    name: str = Field(min_length=1, max_length=128)


class NodeOut(BaseModel):
    id: str
    site_id: str
    name: str
    agent_id: str
    advertise_ip: str
    monitor_version: str
    singbox_version: str
    last_seen_at: datetime | None
    desired_version: int
    applied_version: int
    applied_hash: str
    liveness_status: str
    config_status: str
    service_status: str
    subscription_status: str
    probe_status: str
    last_error: str


class NodeCreateResponse(NodeOut):
    agent_token: str


class CIDRCreate(BaseModel):
    cidr: str
    comment: str = ""
    enabled: bool = True


class CIDROut(BaseModel):
    id: str
    site_id: str
    cidr: str
    comment: str
    enabled: bool


class CIDRPreviewRequest(BaseModel):
    site_id: str
    source_ip: str


class CIDRPreviewResponse(BaseModel):
    allowed: bool
    matched_cidr: str | None = None
    requires_auth: bool
    reason: str
    effective_cidrs: list[str]


class TravelExceptionCreate(BaseModel):
    cidr: str
    comment: str = ""
    owner: str = ""
    expires_at: datetime
    enabled: bool = True


class TravelExceptionOut(BaseModel):
    id: str
    cidr: str
    comment: str
    owner: str
    expires_at: datetime
    enabled: bool
    created_at: datetime


class CrossSiteAllowUpdate(BaseModel):
    from_site_id: str
    to_site_id: str
    enabled: bool = False
    comment: str = ""


class CrossSiteAllowOut(BaseModel):
    id: str
    from_site_id: str
    to_site_id: str
    enabled: bool
    comment: str
    updated_at: datetime


class DestinationBlacklistCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=255)
    kind: Literal["domain", "ip", "cidr"] = "domain"
    comment: str = ""
    enabled: bool = True


class DestinationBlacklistOut(BaseModel):
    id: str
    pattern: str
    kind: Literal["domain", "ip", "cidr"]
    comment: str
    enabled: bool
    created_at: datetime


class SubscriptionSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    fetch_interval_sec: int = Field(default=21_600, ge=300, le=604_800)
    max_body_bytes: int = Field(default=2_000_000, ge=1_024, le=10_000_000)
    redirect_limit: int = Field(default=3, ge=0, le=5)


class SubscriptionSourceOut(BaseModel):
    id: str
    name: str
    url_hint: str
    fetch_interval_sec: int
    max_body_bytes: int
    redirect_limit: int
    enabled: bool
    refreshable: bool
    last_refresh_at: datetime | None
    last_refresh_attempt_at: datetime | None
    last_refresh_error: str
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime


class SubscriptionVersionOut(BaseModel):
    id: str
    source_id: str
    version: int
    content_hash: str
    size_bytes: int
    format: str
    fetched_at: datetime
    parse_ok: bool
    parse_error: str
    node_count: int
    published: bool
    created_at: datetime


class SiteSubscriptionOut(BaseModel):
    site_id: str
    source_id: str
    subscription_version_id: str
    previous_subscription_version_id: str | None
    updated_at: datetime


class SubscriptionCatalogOut(BaseModel):
    sources: list[SubscriptionSourceOut]
    versions: list[SubscriptionVersionOut]
    site_subscriptions: list[SiteSubscriptionOut]


class SubscriptionPublishRequest(BaseModel):
    site_ids: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=500)


class DraftCreate(BaseModel):
    site_id: str
    node_ids: list[str] = Field(default_factory=list)
    diff: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class ProxySelectionRequest(BaseModel):
    """Select an already reported outbound for one node's selector."""

    group: str = Field(default="subscription", min_length=1, max_length=255)
    outbound: str = Field(min_length=1, max_length=255)
    expected_current_version: int | None = None
    note: str = Field(default="", max_length=500)


class DraftOut(BaseModel):
    id: str
    site_id: str
    node_ids: list[str]
    source_revision: int
    diff: dict[str, Any]
    validation: dict[str, Any]
    risk_level: str
    status: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ReleaseCreate(BaseModel):
    draft_id: str
    site_id: str | None = None
    node_ids: list[str] = Field(default_factory=list)
    expected_current_version: int | None = None
    idempotency_key: str | None = None
    note: str = ""


class ReleaseOut(BaseModel):
    release_id: str
    site_id: str
    node_ids: list[str]
    desired_release_id: str
    previous_release_id: str | None
    task_id: str | None
    status: str
    stage: str
    progress: int
    error: str
    rollback_reason: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class TaskOut(BaseModel):
    task_id: str
    task_type: str
    target_type: str
    target_id: str
    status: str
    progress: int
    stage: str
    progress_message: str
    retry_count: int
    max_retries: int
    error: str
    result: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None
    next_run_at: datetime
    locked_by: str
    lease_expires_at: datetime | None


class SubscriptionRefreshResponse(BaseModel):
    source: SubscriptionSourceOut
    task: TaskOut
    merged: bool = False


class SubscriptionUploadResponse(BaseModel):
    source: SubscriptionSourceOut
    version: SubscriptionVersionOut


class SubscriptionPublishOut(BaseModel):
    version: SubscriptionVersionOut
    releases: list[ReleaseOut]


class AgentAckOut(BaseModel):
    node_id: str
    release_id: str
    desired_version: int
    applied_version: int
    bundle_hash: str
    applied_hash: str
    ok: bool
    singbox_ok: bool
    nft_ok: bool
    health_ok: bool
    rollback_attempted: bool
    rollback_ok: bool
    last_good_version: int
    stage: str
    error_code: str
    error_message: str
    sequence: int
    received_at: datetime


class AuditEventOut(BaseModel):
    event_id: str
    actor: str
    actor_role: str
    request_id: str
    source_ip: str
    action: str
    target_type: str
    target_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    result: str
    error: str
    immutable_hash: str
    previous_hash: str
    at: datetime


class BackupRecordOut(BaseModel):
    backup_id: str
    scope: str
    origin: Literal["manual", "scheduled"]
    artifact_paths: list[str]
    format: str
    checksum: str
    encrypted: bool
    storage_ref: str
    status: str
    created_by: str
    created_at: datetime
    verified_at: datetime | None
    last_rehearsed_at: datetime | None
    restore_task_id: str | None
    error: str
    size_bytes: int = 0
    manifest: dict[str, Any] = Field(default_factory=dict)


class BackupCreateRequest(BaseModel):
    scope: Literal["control_plane"] = "control_plane"


class BackupRestoreRequest(BaseModel):
    # Verification rehearsal is the safe default. A destructive write-back is
    # only accepted when the operator explicitly confirms it.
    confirm: bool = False


class BackupCreateResponse(BaseModel):
    backup: BackupRecordOut
    task: TaskOut


class BackupRestoreResponse(BaseModel):
    backup: BackupRecordOut
    task: TaskOut


class AccessLogIn(BaseModel):
    ts: datetime
    policy_version: int = Field(default=0, ge=0)
    src_ip: str = Field(default="", max_length=64)
    src_cidr_match: str = Field(default="", max_length=64)
    username: str = Field(default="", max_length=128)
    cert_fp: str = Field(default="", max_length=256)
    dst_host: str = Field(default="", max_length=255)
    dst_port: int = Field(default=0, ge=0, le=65535)
    action: Literal["allow", "deny"]
    deny_reason: str = Field(default="", max_length=64)
    bytes_up: int = Field(default=0, ge=0)
    bytes_down: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class AgentLogBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    entries: list[AccessLogIn] = Field(default_factory=list, max_length=500)


class ConnectionTopItem(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    connections: int = Field(default=0, ge=0)
    bytes_up: int = Field(default=0, ge=0)
    bytes_down: int = Field(default=0, ge=0)


class ConnectionSnapshotIn(BaseModel):
    sampled_at: datetime
    active_connections: int = Field(default=0, ge=0)
    bytes_up: int = Field(default=0, ge=0)
    bytes_down: int = Field(default=0, ge=0)
    top_sources: list[ConnectionTopItem] = Field(default_factory=list, max_length=20)
    top_destinations: list[ConnectionTopItem] = Field(default_factory=list, max_length=20)
    top_users: list[ConnectionTopItem] = Field(default_factory=list, max_length=20)
    api_available: bool = True


class AgentConnectionBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    snapshots: list[ConnectionSnapshotIn] = Field(default_factory=list, max_length=16)


class ProxyHistoryPoint(BaseModel):
    at: datetime | None = None
    delay_ms: int | None = Field(default=None, ge=0, le=300_000)


class ProxyEndpointSnapshot(BaseModel):
    """Safe metadata for one outbound endpoint; no server or credential data."""

    name: str = Field(min_length=1, max_length=255)
    type: str = Field(default="unknown", max_length=64)
    udp: bool = False
    alive: bool | None = None
    delay_ms: int | None = Field(default=None, ge=0, le=300_000)
    history: list[ProxyHistoryPoint] = Field(default_factory=list, max_length=20)


class ProxyGroupSnapshot(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(default="unknown", max_length=64)
    now: str = Field(default="", max_length=255)
    all: list[str] = Field(default_factory=list, max_length=500)
    nodes: list[ProxyEndpointSnapshot] = Field(default_factory=list, max_length=500)
    udp: bool = False
    delay_ms: int | None = Field(default=None, ge=0, le=300_000)
    history: list[ProxyHistoryPoint] = Field(default_factory=list, max_length=20)


class AgentProxyConfigBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    sampled_at: datetime
    api_available: bool = False
    groups: list[ProxyGroupSnapshot] = Field(default_factory=list, max_length=100)
    error: str = Field(default="", max_length=256)

    @field_validator("groups", mode="before")
    @classmethod
    def normalize_legacy_null_groups(cls, value: Any) -> Any:
        # Older monitors encoded an unavailable local API as ``groups: null``.
        # Keep the wire contract array-shaped internally while allowing their
        # persisted telemetry spool to be replayed after an upgrade.
        return [] if value is None else value


class ProbeResultIn(BaseModel):
    outbound_tag: str = Field(min_length=1, max_length=128)
    target_url: str = Field(min_length=1, max_length=2048)
    success: bool
    latency_ms: int = Field(default=0, ge=0, le=300_000)
    error_class: str = Field(default="", max_length=64)
    sampled_at: datetime


class AgentProbeBatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=8, max_length=128)
    sequence: int = Field(ge=1)
    results: list[ProbeResultIn] = Field(default_factory=list, max_length=100)
    task_id: str | None = Field(default=None, max_length=128)


class TelemetryBatchResponse(BaseModel):
    accepted: bool
    duplicate: bool = False


class ProbeRequestForAgent(BaseModel):
    task_id: str
    target_url: str
    outbound_tags: list[str] = Field(default_factory=list)


class AgentHeartbeatResponse(BaseModel):
    accepted: bool
    duplicate: bool = False
    desired_stale: bool = False
    probe_requests: list[ProbeRequestForAgent] = Field(default_factory=list)


class AccessLogOut(BaseModel):
    id: str
    ts: datetime
    site_id: str
    node_id: str
    policy_version: int
    src_ip: str
    src_cidr_match: str
    username: str
    cert_fp: str
    dst_host: str
    dst_port: int
    action: str
    deny_reason: str
    bytes_up: int
    bytes_down: int
    duration_ms: int


class ConnectionSnapshotOut(BaseModel):
    id: str
    node_id: str
    site_id: str
    sampled_at: datetime
    active_connections: int
    bytes_up: int
    bytes_down: int
    top_sources: list[ConnectionTopItem]
    top_destinations: list[ConnectionTopItem]
    top_users: list[ConnectionTopItem]
    api_available: bool
    received_at: datetime


class ProxyConfigSnapshotOut(BaseModel):
    id: str
    node_id: str
    site_id: str
    sampled_at: datetime
    api_available: bool
    groups: list[ProxyGroupSnapshot]
    error: str
    received_at: datetime


class ProbeHistoryOut(BaseModel):
    id: str
    node_id: str
    site_id: str
    outbound_tag: str
    target_url: str
    success: bool
    latency_ms: int
    error_class: str
    sampled_at: datetime


class ProbeCircuitOut(BaseModel):
    node_id: str
    site_id: str
    outbound_tag: str
    state: str
    consecutive_failures: int
    consecutive_successes: int
    opened_at: datetime | None
    half_open_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_latency_ms: int
    last_error_class: str
    reason: str
    updated_at: datetime


class AlertOut(BaseModel):
    id: str
    fingerprint: str
    category: str
    severity: str
    site_id: str
    node_id: str
    title: str
    detail: str
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None


class ProbeTaskRequest(BaseModel):
    target_url: str = Field(default="https://www.google.com/ncr", min_length=1, max_length=2048)
    outbound_tags: list[str] = Field(default_factory=list, max_length=100)


class AccessConfigOut(BaseModel):
    fqdn: str
    port: int
    protocol: Literal["http-connect"] = "http-connect"
    https_proxy_enabled: bool = False


class AgentHeartbeat(BaseModel):
    node_id: str
    monitor_version: str = "unknown"
    singbox_version: str = "unknown"
    desired_version: int = 0
    applied_version: int = 0
    applied_hash: str = ""
    bundle_hash: str = ""
    liveness_status: str = "online"
    config_status: str = "unknown"
    service_status: str = "unknown"
    subscription_status: str = "not_configured"
    process_ok: bool = False
    port_ok: bool = False
    api_ok: bool = False
    cpu_percent: float = 0
    memory_bytes: int = 0
    spool_bytes: int = 0
    connections: int = 0
    bytes_last_minute: int = 0
    last_error: str = ""
    request_id: str = ""
    sequence: int = Field(ge=1)


class AgentAck(BaseModel):
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
    sequence: int = Field(ge=1)


class DesiredResponse(BaseModel):
    desired_stale: bool = False
    release_id: str | None = None
    bundle: dict[str, Any] | None = None
