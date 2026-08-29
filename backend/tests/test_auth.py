import json

import httpx
import pytest

from app.config import Settings
from app.services.auth import AuthError, hash_password, normalize_itcode, verify_password
from app.services.gquan import GQuanClient, GQuanDeliveryError


def test_itcode_normalization_and_argon2_passwords() -> None:
    password_hash = hash_password("phase3-valid-password")

    assert normalize_itcode(" Example.User ") == "example.user"
    assert password_hash.startswith("$argon2")
    assert verify_password("phase3-valid-password", password_hash)[0] is True
    assert verify_password("incorrect-password", password_hash)[0] is False
    with pytest.raises(AuthError, match="invalid_itcode"):
        normalize_itcode("not an itcode")


def settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "bundle_hmac_secret": "b" * 32,
        "admin_password": "phase3-admin-password",
        "management_token": "m" * 32,
        "gquan_app_token": "sat_test_app_token",
    }
    values.update(updates)
    return Settings(**values)


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
