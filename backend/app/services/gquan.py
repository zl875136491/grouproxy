"""One Login GQuan APP delivery boundary for authentication codes."""

import asyncio
import random
from urllib.parse import urljoin

import httpx

from ..config import Settings


class GQuanDeliveryError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
        payload = {
            "to": [itcode],
            "title": "Grouproxy verification code",
            "desc": f"Verification for {purpose.replace('_', ' ')}.",
            "content": f"Your Grouproxy verification code is {code}. It expires in 10 minutes.",
            "msg_type": "MSG",
        }
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
                if response.status_code == 429:
                    raise GQuanDeliveryError("gquan_quota_exceeded")
                if response.status_code < 200 or response.status_code >= 300:
                    raise GQuanDeliveryError("gquan_delivery_rejected")
                try:
                    body = response.json()
                except ValueError:
                    return
                data = body.get("data", body) if isinstance(body, dict) else {}
                delivery_status = (
                    str(data.get("status", "")).lower() if isinstance(data, dict) else ""
                )
                if delivery_status in {"failed", "error", "rejected"}:
                    raise GQuanDeliveryError("gquan_delivery_rejected")
                return
