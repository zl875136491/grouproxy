"""Proxy-only credentials derived from a control-plane secret at bundle time."""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from ..config import Settings
from ..models import ProxyCredential, utcnow
from .auth import hash_password, verify_password


class ProxyCredentialError(ValueError):
    pass


@dataclass(frozen=True)
class ProxyCredentialSnapshot:
    """The durable fields needed to undo a failed credential deployment."""

    credential_id: str
    username: str
    password_hash: str
    active: bool
    rotated_at: datetime


@dataclass(frozen=True)
class ProxyCredentialRotation:
    credential: ProxyCredential
    password: str
    previous: ProxyCredentialSnapshot | None

    @property
    def created(self) -> bool:
        return self.previous is None


def credential_secret(settings: Settings) -> str:
    value = settings.proxy_credential_secret
    if value is None:
        raise ProxyCredentialError("proxy_credential_secret_unavailable")
    secret = value.get_secret_value()
    if len(secret) < 32:
        raise ProxyCredentialError("proxy_credential_secret_invalid")
    return secret


def derive_proxy_password(*, credential_id: str, settings: Settings) -> str:
    """Return a stable, high-entropy HTTP Basic secret for one credential."""

    digest = hmac.new(
        credential_secret(settings).encode("utf-8"),
        f"grouproxy-proxy-credential:{credential_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def rotate_proxy_credential(
    *, site_id: str, itcode: str, settings: Settings
) -> ProxyCredentialRotation:
    """Create or rotate one employee credential without persisting its password.

    A random credential identifier is the rotation boundary. The configured
    backend-only secret derives the one-time reveal and the node bundle value;
    MongoDB retains only an Argon2 verifier for consistency checks.
    """

    credential_id = str(uuid4())
    password = derive_proxy_password(credential_id=credential_id, settings=settings)
    current = utcnow()
    existing = await ProxyCredential.find_one(
        ProxyCredential.site_id == site_id,
        ProxyCredential.itcode == itcode,
    )
    if existing is None:
        credential = ProxyCredential(
            credential_id=credential_id,
            site_id=site_id,
            itcode=itcode,
            username=itcode,
            password_hash=hash_password(password),
            active=True,
            rotated_at=current,
        )
        await credential.insert()
        return ProxyCredentialRotation(
            credential=credential,
            password=password,
            previous=None,
        )

    previous = ProxyCredentialSnapshot(
        credential_id=existing.credential_id,
        username=existing.username,
        password_hash=existing.password_hash,
        active=existing.active,
        rotated_at=existing.rotated_at,
    )
    existing.credential_id = credential_id
    existing.username = itcode
    existing.password_hash = hash_password(password)
    existing.active = True
    existing.rotated_at = current
    await existing.save()
    return ProxyCredentialRotation(
        credential=existing,
        password=password,
        previous=previous,
    )


async def restore_proxy_credential(rotation: ProxyCredentialRotation) -> None:
    """Restore the prior state when its automatic deployment cannot start."""

    if rotation.created:
        await rotation.credential.delete()
        return
    previous = rotation.previous
    if previous is None:  # pragma: no cover - guarded by ``created`` above
        return
    credential = rotation.credential
    credential.credential_id = previous.credential_id
    credential.username = previous.username
    credential.password_hash = previous.password_hash
    credential.active = previous.active
    credential.rotated_at = previous.rotated_at
    await credential.save()


async def active_proxy_credential_count(site_id: str) -> int:
    return await ProxyCredential.find(
        ProxyCredential.site_id == site_id,
        ProxyCredential.active == True,  # noqa: E712 - Beanie expression
    ).count()


async def proxy_auth_bundle(
    *, site_id: str, required: bool, settings: Settings
) -> dict[str, object]:
    if not required:
        return {"required": False, "users": []}

    credentials = (
        await ProxyCredential.find(
            ProxyCredential.site_id == site_id,
            ProxyCredential.active == True,  # noqa: E712 - Beanie expression
        )
        .sort(+ProxyCredential.username)
        .to_list()
    )
    if not credentials:
        raise ProxyCredentialError("proxy_auth_requires_credential")

    users: list[dict[str, str]] = []
    for credential in credentials:
        password = derive_proxy_password(credential_id=credential.credential_id, settings=settings)
        valid, _ = verify_password(password, credential.password_hash)
        if not valid:
            raise ProxyCredentialError("proxy_credential_secret_mismatch")
        users.append({"username": credential.username, "password": password})
    return {"required": True, "users": users}
