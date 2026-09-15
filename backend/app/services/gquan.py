"""One Login GQuan APP delivery boundary for authentication codes."""

import asyncio
import random
from urllib.parse import urljoin

import httpx

from ..config import Settings

PURPOSE_LABELS = {
    "register": "账号注册",
    "password_change": "重置密码",
    "gquan_login": "登录",
}


class GQuanDeliveryError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _purpose_label(purpose: str) -> str:
    return PURPOSE_LABELS.get(purpose, purpose.replace("_", " "))


def _delivery_payload(*, itcode: str, code: str, purpose: str) -> dict[str, object]:
    label = _purpose_label(purpose)
    return {
        "to": [itcode],
        "title": "Grouproxy 验证码",
        "desc": f"用于{label}身份验证。",
        "content": f"您的 Grouproxy 验证码是 {code}，10 分钟内有效。",
        "msg_type": "MSG",
    }


def _api_error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("code", "error"):
        value = body.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("code"), str):
            return str(value["code"])
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("code"), str):
        return str(data["code"])
    return ""


def _delivery_rejected(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    data = body.get("data", body)
    if not isinstance(data, dict):
        return False
    status = str(data.get("status", "")).lower()
    if status in {"failed", "error", "rejected"}:
        return True
    results = data.get("results")
    if not isinstance(results, list):
        return False
    for item in results:
        if not isinstance(item, dict):
            continue
        result = str(item.get("data") or item.get("status") or "").lower()
        if result in {"failed", "error", "rejected"}:
            return True
    return False


class GQuanClient:
    """Send a code through the approved APP Bearer token.

    This client intentionally accepts no caller-controlled URL, recipients are
    normalized by the authentication service, and neither response bodies nor
    access tokens are logged or persisted.
    """

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def send_verification_code(self, *, itcode: str, code: str, purpose: str) -> None:
        if self.settings.gquan_delivery_mode == "stub":
            if self.settings.environment != "test":
                raise GQuanDeliveryError("gquan_stub_not_allowed")
            return

        token = self.settings.gquan_app_token
        if token is None or not token.get_secret_value().strip():
            raise GQuanDeliveryError("gquan_delivery_unavailable")

        endpoint = urljoin(
            self.settings.gquan_api_base_url.rstrip("/") + "/",
            "integrations/gquan/app",
        )
        payload = _delivery_payload(itcode=itcode, code=code, purpose=purpose)
        headers = {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(self.settings.gquan_request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            verify=False,
        ) as client:
            for attempt in range(2):
                try:
                    response = await client.post(endpoint, headers=headers, json=payload)
                except httpx.TransportError:
                    if attempt == 0:
                        await asyncio.sleep(0.2 + random.random() * 0.1)
                        continue
                    raise GQuanDeliveryError("gquan_delivery_unavailable") from None
                if response.status_code >= 500:
                    if attempt == 0:
                        await asyncio.sleep(0.2 + random.random() * 0.1)
                        continue
                    raise GQuanDeliveryError("gquan_delivery_unavailable")
                error_code = _api_error_code(response)
                if response.status_code == 429 or error_code == "quota_exceeded":
                    raise GQuanDeliveryError("gquan_quota_exceeded")
                if response.status_code < 200 or response.status_code >= 300:
                    raise GQuanDeliveryError("gquan_delivery_rejected")
                try:
                    body = response.json()
                except ValueError:
                    return
                if _delivery_rejected(body):
                    raise GQuanDeliveryError("gquan_delivery_rejected")
                return
