from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


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
