from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    itcode: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    itcode: str
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


class NodeCreate(BaseModel):
    site_id: str
    name: str
    agent_id: str
    advertise_ip: str = ""


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
