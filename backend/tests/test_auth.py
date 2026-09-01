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
from app.services.proxy_credentials import (
    ProxyCredentialError,
    derive_proxy_password,
    proxy_auth_bundle,
)
from main import (
    _session_response,
    list_employee_proxy_credentials,
    list_employees,
    require_management,
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

    monkeypatch.setattr("main.AdminUser.find", lambda *_: Query())

    result = await list_employees("admin")

    assert [item.itcode for item in result] == ["example.user"]
    assert "password_hash" not in result[0].model_dump()


@pytest.mark.asyncio
async def test_employee_credential_metadata_rejects_administrators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def find_admin(_: str):
        return SimpleNamespace(role="admin")

    monkeypatch.setattr("main.find_user_by_itcode", find_admin)

    with pytest.raises(HTTPException) as exc_info:
        await list_employee_proxy_credentials("admin", "admin")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "employee_not_found"


def settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "bundle_hmac_secret": "b" * 32,
        "admin_password": "phase3-admin-password",
        "management_token": "m" * 32,
        "proxy_credential_secret": "p" * 32,
        "gquan_app_token": "sat_test_app_token",
    }
    values.update(updates)
    return Settings(**values)


def test_proxy_credential_is_derived_without_storing_clear_text() -> None:
    configured = settings()
    password = derive_proxy_password(credential_id="credential-a", settings=configured)

    assert len(password) >= 32
    assert password == derive_proxy_password(credential_id="credential-a", settings=configured)
    assert password != derive_proxy_password(credential_id="credential-b", settings=configured)
    assert verify_password(password, hash_password(password))[0] is True
    with pytest.raises(ProxyCredentialError, match="proxy_credential_secret_unavailable"):
        derive_proxy_password(
            credential_id="credential-a", settings=settings(proxy_credential_secret=None)
        )


class _CredentialQuery:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def sort(self, *_: object) -> "_CredentialQuery":
        return self

    async def to_list(self) -> list[object]:
        return self.values


class _CredentialField:
    def __eq__(self, _: object) -> "_CredentialField":  # type: ignore[override]
        return self

    def __pos__(self) -> "_CredentialField":
        return self


class _CredentialModel:
    site_id = _CredentialField()
    active = _CredentialField()
    username = _CredentialField()

    values: list[object] = []

    @classmethod
    def find(cls, *_: object, **__: object) -> _CredentialQuery:
        return _CredentialQuery(cls.values)


@pytest.mark.asyncio
async def test_proxy_auth_bundle_only_derives_verified_active_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings()
    password = derive_proxy_password(credential_id="credential-a", settings=configured)
    credential = SimpleNamespace(
        credential_id="credential-a",
        username="example.user",
        password_hash=hash_password(password),
    )
    _CredentialModel.values = [credential]
    monkeypatch.setattr("app.services.proxy_credentials.ProxyCredential", _CredentialModel)

    disabled = await proxy_auth_bundle(site_id="site-a", required=False, settings=configured)
    assert disabled == {"required": False, "users": []}

    enabled = await proxy_auth_bundle(site_id="site-a", required=True, settings=configured)
    assert enabled == {
        "required": True,
        "users": [{"username": "example.user", "password": password}],
    }

    credential.password_hash = hash_password("different-secret")
    with pytest.raises(ProxyCredentialError, match="proxy_credential_secret_mismatch"):
        await proxy_auth_bundle(site_id="site-a", required=True, settings=configured)


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
