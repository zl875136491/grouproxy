import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..models import AuditEvent
from .crypto import canonical_json

_audit_lock = asyncio.Lock()
_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "agent_token",
    "agent_token_hash",
    "secret",
    "secret_ref",
    "authorization",
    "private_key",
    "client_ca_pem",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


async def append_audit(
    *,
    action: str,
    target_type: str,
    target_id: str,
    actor: str = "system",
    actor_role: str = "system",
    request_id: str = "",
    source_ip: str = "",
    before: Any = None,
    after: Any = None,
    result: str = "success",
    error: str = "",
) -> AuditEvent:
    async with _audit_lock:
        previous = await AuditEvent.find().sort(-AuditEvent.at).first_or_none()
        previous_hash = previous.immutable_hash if previous else ""
        # BSON datetimes have millisecond precision. Truncate before hashing
        # so the value reconstructed from MongoDB verifies byte-for-byte.
        current = datetime.now(timezone.utc)
        recorded_at = current.replace(microsecond=(current.microsecond // 1000) * 1000)
        # MongoDB stores datetimes at millisecond precision. Keep the chain
        # order unambiguous even when two events arrive in the same tick.
        if previous is not None:
            previous_at = previous.at.astimezone(timezone.utc)
            if recorded_at <= previous_at:
                recorded_at = previous_at + timedelta(milliseconds=1)
        payload = {
            "event_id": str(uuid4()),
            "actor": actor,
            "actor_role": actor_role,
            "request_id": request_id,
            "source_ip": source_ip,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "before": redact(before or {}),
            "after": redact(after or {}),
            "result": result,
            "error": error[:512],
            "previous_hash": previous_hash,
            "at": recorded_at.isoformat(),
        }
        immutable_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
        event = AuditEvent(
            event_id=payload["event_id"],
            actor=payload["actor"],
            actor_role=payload["actor_role"],
            request_id=payload["request_id"],
            source_ip=payload["source_ip"],
            action=payload["action"],
            target_type=payload["target_type"],
            target_id=payload["target_id"],
            before=payload["before"],
            after=payload["after"],
            result=payload["result"],
            error=payload["error"],
            previous_hash=previous_hash,
            immutable_hash=immutable_hash,
            at=recorded_at,
        )
        await event.insert()
        return event


async def verify_audit_chain() -> tuple[bool, str, int]:
    events = await AuditEvent.find().sort(+AuditEvent.at).to_list()
    # TTL retention may remove the historical predecessor of the first event
    # still present. Treat that predecessor hash as the retained chain anchor;
    # all links inside the retained window remain strictly verified.
    previous_hash = events[0].previous_hash if events else ""
    for index, event in enumerate(events):
        payload = {
            "event_id": event.event_id,
            "actor": event.actor,
            "actor_role": event.actor_role,
            "request_id": event.request_id,
            "source_ip": event.source_ip,
            "action": event.action,
            "target_type": event.target_type,
            "target_id": event.target_id,
            "before": event.before,
            "after": event.after,
            "result": event.result,
            "error": event.error,
            "previous_hash": event.previous_hash,
            "at": event.at.isoformat(),
        }
        if index > 0 and event.previous_hash != previous_hash:
            return False, f"previous_hash_mismatch:{index}", len(events)
        if hashlib.sha256(canonical_json(payload)).hexdigest() != event.immutable_hash:
            return False, f"immutable_hash_mismatch:{index}", len(events)
        previous_hash = event.immutable_hash
    return True, "", len(events)
