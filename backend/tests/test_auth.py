import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.models import AdminUser
from app.services.auth import AuthError, hash_password, normalize_itcode, verify_password
from app.services.gquan import GQuanClient, GQuanDeliveryError
from app.schemas import RoleUpdate
from main import (
    _session_response,
    list_employees,
    require_authenticated,
    require_management,
    update_user_role,
)


def test_itcode_normalization_and_argon2_passwords() -> None:
    password_hash = hash_password("phase3-valid-password")

    assert normalize_itcode(" Example.User ") == "example.user"
    assert password_hash.startswith("$argon2")
    assert verify_password("phase3-valid-password", password_hash)[0] is True
    assert verify_password("incorrect-password", password_hash)[0] is False
    with pytest.raises(AuthError, match="invalid_itcode"):
        normalize_itcode("not an itcode")


def test_new_accounts_default_to_employee_and_session_reports_the_role() -> None:
    user = SimpleNamespace(itcode="example.user", role="employee")
    session = SimpleNamespace(itcode=user.itcode, expires_at="2099-01-01T00:00:00Z")

    assert AdminUser.model_fields["role"].default == "employee"
    assert _session_response("token", session, user).role == "employee"


@pytest.mark.asyncio
async def test_employee_session_cannot_call_management_api(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(itcode="example.user", role="employee")
    session = SimpleNamespace(itcode=user.itcode)

    async def resolve(_: str):
        return session, user

    monkeypatch.setattr("main._settings", lambda: settings())
    monkeypatch.setattr("main.resolve_session", resolve)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/nodes",
            "headers": [(b"authorization", b"Bearer employee-session")],
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_management(request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "management_admin_required"


@pytest.mark.asyncio
async def test_employee_management_list_exposes_no_password_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = SimpleNamespace(
        itcode="example.user",
        username="example.user",
        role="employee",
        auth_source="local",
        is_active=True,
        created_at="2026-08-29T12:00:00Z",
        password_changed_at="2026-08-29T12:00:00Z",
        last_login_at=None,
        password_hash="must-not-leak",
    )

    class Query:
        def sort(self, *_: object):
            return self

        async def to_list(self):
            return [employee]

    monkeypatch.setattr("main.AdminUser.find_all", lambda *_: Query())

    result = await list_employees("admin")

    assert [item.itcode for item in result] == ["example.user"]
    assert result[0].role == "employee"
    assert "password_hash" not in result[0].model_dump()


def _auth_request(path: str = "/api/v1/nodes") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"authorization", b"Bearer session-token")],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["root", "admin"])
async def test_management_roles_can_call_management_api(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    user = SimpleNamespace(itcode="zhangle" if role == "root" else "alice", role=role)
    session = SimpleNamespace(itcode=user.itcode)

    async def resolve(_: str):
        return session, user

    monkeypatch.setattr("main._settings", lambda: settings())
    monkeypatch.setattr("main.resolve_session", resolve)

    assert await require_management(_auth_request()) == user.itcode


@pytest.mark.asyncio
async def test_root_account_role_cannot_be_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await update_user_role(
            "zhangle",
            RoleUpdate(role="employee"),
            _auth_request("/api/v1/users/zhangle/role"),
            "alice",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "root_role_immutable"


@pytest.mark.asyncio
async def test_administrator_can_grant_and_revoke_peer_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        itcode="alice",
        username="alice",
        role="employee",
        auth_source="local",
        is_active=True,
        created_at="2026-08-29T12:00:00Z",
        password_changed_at="2026-08-29T12:00:00Z",
        last_login_at=None,
    )
    saved: list[str] = []
    revoked: list[str] = []

    async def save() -> None:
        saved.append(user.role)

    user.save = save  # type: ignore[attr-defined]

    async def find_user(itcode: str):
        assert itcode == "alice"
        return user

    async def revoke(target: object) -> None:
        revoked.append(getattr(target, "itcode"))

    async def audit(**_: object) -> None:
        return None

    monkeypatch.setattr("main.find_user_by_itcode", find_user)
    monkeypatch.setattr("main.revoke_user_sessions", revoke)
    monkeypatch.setattr("main.append_audit", audit)
    monkeypatch.setattr("main._request_id", lambda _: "request-1")
    monkeypatch.setattr("main._request_source_ip", lambda _: "127.0.0.1")

    granted = await update_user_role(
        "alice",
        RoleUpdate(role="admin"),
        _auth_request("/api/v1/users/alice/role"),
        "zhangle",
    )
    assert granted.role == "admin"
    assert saved == ["admin"]
    assert revoked == []

    revoked_result = await update_user_role(
        "alice",
        RoleUpdate(role="employee"),
        _auth_request("/api/v1/users/alice/role"),
        "zhangle",
    )
    assert revoked_result.role == "employee"
    assert saved == ["admin", "employee"]
    assert revoked == ["alice"]


def settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "bundle_hmac_secret": "b" * 32,
        "admin_password": "phase3-admin-password",
        "management_token": "m" * 32,
        "gquan_app_token": "sat_test_app_token",
        "gquan_delivery_mode": "app",
    }
    values.update(updates)
    return Settings(**values)


def test_management_sessions_default_to_thirty_days() -> None:
    assert settings().auth_session_ttl_minutes == 43_200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "management_session_expired",
        "management_session_revoked",
        "management_session_invalid",
        "management_account_inactive",
    ],
)
async def test_session_failure_reason_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    async def resolve(_: str):
        raise AuthError(error_code)

    monkeypatch.setattr("main._settings", lambda: settings())
    monkeypatch.setattr("main.resolve_session", resolve)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sites",
            "headers": [(b"authorization", b"Bearer browser-session")],
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_authenticated(request)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == error_code


@pytest.mark.asyncio
async def test_gquan_app_delivery_uses_app_bearer_and_content() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"data": {"status": "captured"}})

    client = GQuanClient(settings(), transport=httpx.MockTransport(handler))
    await client.send_verification_code(
        itcode="example.user",
        code="123456",
        purpose="register",
    )

    assert captured["url"] == "https://one.1oa.com.cn/springboard/api/v1/integrations/gquan/app"
    assert captured["authorization"] == "Bearer sat_test_app_token"
    assert captured["body"] == {
        "to": ["example.user"],
        "title": "Grouproxy verification code",
        "desc": "Verification for register.",
        "content": "Your Grouproxy verification code is 123456. It expires in 10 minutes.",
        "msg_type": "MSG",
    }


@pytest.mark.asyncio
async def test_gquan_stub_is_only_available_for_the_test_environment() -> None:
    test_client = GQuanClient(settings(environment="test", gquan_delivery_mode="stub"))
    await test_client.send_verification_code(
        itcode="example.user",
        code="123456",
        purpose="gquan_login",
    )

    production_client = GQuanClient(settings(gquan_delivery_mode="stub"))
    with pytest.raises(GQuanDeliveryError, match="gquan_stub_not_allowed"):
        await production_client.send_verification_code(
            itcode="example.user",
            code="123456",
            purpose="gquan_login",
        )
