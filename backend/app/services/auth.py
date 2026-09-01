"""Account identity, password, session, and one-time verification primitives."""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from pymongo.errors import DuplicateKeyError

from ..config import Settings
from ..models import AdminUser, AuthVerificationChallenge, ManagementSession, utcnow
from .gquan import GQuanClient, GQuanDeliveryError

ITCODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
PASSWORD_MIN_LENGTH = 12
_password_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)


class AuthError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_itcode(value: str) -> str:
    itcode = value.strip().casefold()
    if not ITCODE_PATTERN.fullmatch(itcode):
        raise AuthError("invalid_itcode")
    return itcode


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > 128:
        raise AuthError("invalid_password")


def hash_password(password: str) -> str:
    validate_password(password)
    return _password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    """Return ``(valid, needs_upgrade)`` including the legacy SHA-256 seed."""

    try:
        if stored_hash.startswith("$argon2"):
            valid = _password_hasher.verify(stored_hash, password)
            return valid, _password_hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, VerifyMismatchError):
        return False, False
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored_hash), True


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_hash(*, challenge_id: str, code: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{challenge_id}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def find_user_by_itcode(itcode: str) -> AdminUser | None:
    user = await AdminUser.find_one(AdminUser.itcode == itcode)
    if user is not None:
        return user
    # Upgrade pre-itcode installations lazily without breaking their seeded
    # administrator account.
    user = await AdminUser.find_one(AdminUser.username == itcode)
    if user is not None and not user.itcode:
        user.itcode = itcode
        await user.save()
    return user


async def create_session(user: AdminUser, settings: Settings) -> tuple[str, ManagementSession]:
    token = secrets.token_urlsafe(48)
    current = utcnow()
    session = ManagementSession(
        session_id=str(uuid4()),
        token_hash=_token_hash(token),
        user_id=str(user.id),
        itcode=user.itcode,
        created_at=current,
        expires_at=current + timedelta(minutes=settings.auth_session_ttl_minutes),
    )
    await session.insert()
    return token, session


async def resolve_session(token: str) -> tuple[ManagementSession, AdminUser] | None:
    session = await ManagementSession.find_one(ManagementSession.token_hash == _token_hash(token))
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        return None
    user = await AdminUser.get(session.user_id)
    if user is None or not user.is_active or user.itcode != session.itcode:
        return None
    session.last_seen_at = utcnow()
    await session.save()
    return session, user


async def revoke_session_token(token: str) -> bool:
    session = await ManagementSession.find_one(ManagementSession.token_hash == _token_hash(token))
    if session is None or session.revoked_at is not None:
        return False
    session.revoked_at = utcnow()
    await session.save()
    return True


async def revoke_user_sessions(user: AdminUser) -> None:
    sessions = await ManagementSession.find(
        {"user_id": str(user.id), "revoked_at": None}
    ).to_list()
    current = utcnow()
    for session in sessions:
        session.revoked_at = current
        await session.save()


async def request_verification_code(
    *,
    itcode: str,
    purpose: str,
    source_ip: str,
    settings: Settings,
    client: GQuanClient | None = None,
) -> AuthVerificationChallenge:
    current = utcnow()
    latest = await AuthVerificationChallenge.find_one(
        AuthVerificationChallenge.itcode == itcode,
        AuthVerificationChallenge.purpose == purpose,
        sort=[("created_at", -1)],
    )
    if latest is not None and latest.resend_available_at > current and latest.status in {
        "pending",
        "delivered",
    }:
        raise AuthError("verification_code_rate_limited")

    configured_code = settings.gquan_test_code
    if settings.gquan_delivery_mode == "stub":
        if configured_code is None or not re.fullmatch(
            r"\d{6}", configured_code.get_secret_value()
        ):
            raise AuthError("gquan_test_code_not_configured")
        code = configured_code.get_secret_value()
    else:
        code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = str(uuid4())
    challenge = AuthVerificationChallenge(
        challenge_id=challenge_id,
        itcode=itcode,
        purpose=purpose,
        code_hash=_code_hash(
            challenge_id=challenge_id,
            code=code,
            secret=settings.management_token,
        ),
        source_ip=source_ip[:64],
        expires_at=current + timedelta(seconds=settings.auth_verification_ttl_seconds),
        resend_available_at=current
        + timedelta(seconds=settings.auth_verification_resend_seconds),
    )
    await challenge.insert()
    try:
        await (client or GQuanClient(settings)).send_verification_code(
            itcode=itcode, code=code, purpose=purpose
        )
    except GQuanDeliveryError as exc:
        challenge.status = "delivery_failed"
        challenge.delivery_error = exc.code
        await challenge.save()
        raise AuthError(exc.code) from exc
    challenge.status = "delivered"
    challenge.delivered_at = utcnow()
    await challenge.save()
    return challenge


async def consume_verification_code(
    *,
    challenge_id: str,
    itcode: str,
    purpose: str,
    code: str,
    settings: Settings,
) -> AuthVerificationChallenge:
    challenge = await AuthVerificationChallenge.find_one(
        AuthVerificationChallenge.challenge_id == challenge_id
    )
    if (
        challenge is None
        or challenge.itcode != itcode
        or challenge.purpose != purpose
        or challenge.status != "delivered"
    ):
        raise AuthError("verification_code_invalid")
    if challenge.expires_at <= utcnow():
        challenge.status = "expired"
        await challenge.save()
        raise AuthError("verification_code_expired")
    if challenge.failed_attempts >= settings.auth_verification_max_attempts:
        challenge.status = "locked"
        await challenge.save()
        raise AuthError("verification_code_attempts_exceeded")
    if not hmac.compare_digest(
        challenge.code_hash,
        _code_hash(
            challenge_id=challenge_id,
            code=code,
            secret=settings.management_token,
        ),
    ):
        challenge.failed_attempts += 1
        if challenge.failed_attempts >= settings.auth_verification_max_attempts:
            challenge.status = "locked"
        await challenge.save()
        if challenge.status == "locked":
            raise AuthError("verification_code_attempts_exceeded")
        raise AuthError("verification_code_invalid")
    challenge.status = "consumed"
    challenge.consumed_at = utcnow()
    await challenge.save()
    return challenge


async def create_registered_user(*, itcode: str, password: str) -> AdminUser:
    try:
        user = AdminUser(
            username=itcode,
            itcode=itcode,
            password_hash=hash_password(password),
            role="employee",
            auth_source="local",
            password_changed_at=utcnow(),
        )
        await user.insert()
    except DuplicateKeyError as exc:
        raise AuthError("itcode_already_registered") from exc
    return user
