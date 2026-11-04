from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


WECOM_QR_SOURCE = "wecom_cli_external"


class WeComQrProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class WeComQrChallenge:
    scode: str
    auth_url: str


@dataclass(frozen=True)
class WeComQrPollResult:
    status: str
    bot_id: str | None = None
    secret: str | None = None


class WeComQrClient:
    """Minimal audited adapter for WecomTeam/wecom-cli's QR protocol."""

    def __init__(
        self,
        *,
        generate_url: str | None = None,
        query_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.generate_url = (
            generate_url or settings.WECOM_QR_GENERATE_URL
        )
        self.query_url = query_url or settings.WECOM_QR_QUERY_URL
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def generate(self) -> WeComQrChallenge:
        payload = await self._get_json(
            self.generate_url,
            params={"source": WECOM_QR_SOURCE, "plat": "3"},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WeComQrProtocolError("wecom_qr_invalid_generate_response")
        scode = self._required_text(data, "scode")
        auth_url = self._required_auth_url(data)
        return WeComQrChallenge(scode=scode, auth_url=auth_url)

    async def poll(self, scode: str) -> WeComQrPollResult:
        payload = await self._get_json(
            self.query_url,
            # Match WecomTeam/wecom-cli exactly. `source` and `plat` belong
            # only to the generate request; query_result accepts the scode.
            params={"scode": scode},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WeComQrProtocolError("wecom_qr_invalid_query_response")
        status = str(data.get("status") or "pending").strip().lower()
        if status == "success":
            bot_info = data.get("bot_info")
            if not isinstance(bot_info, dict):
                raise WeComQrProtocolError("wecom_qr_missing_bot_info")
            return WeComQrPollResult(
                status="credentials_ready",
                bot_id=self._required_text(bot_info, "botid"),
                secret=self._required_text(bot_info, "secret"),
            )
        if status in {"scan", "scanned", "authorized", "confirming"}:
            return WeComQrPollResult(status="scanned")
        if status in {"expired", "timeout"}:
            return WeComQrPollResult(status="expired")
        if status in {"failed", "error", "cancelled", "canceled"}:
            return WeComQrPollResult(status="failed")
        return WeComQrPollResult(status="pending")

    async def _get_json(
        self, url: str, *, params: dict[str, str]
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, ImportError) as exc:
            raise WeComQrProtocolError("wecom_qr_upstream_unavailable") from exc
        if not isinstance(payload, dict):
            raise WeComQrProtocolError("wecom_qr_invalid_response")
        return payload

    @staticmethod
    def _required_text(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WeComQrProtocolError(
                f"wecom_qr_missing_{key}"
            )
        return value.strip()

    @classmethod
    def _required_auth_url(cls, data: dict[str, Any]) -> str:
        value = cls._required_text(data, "auth_url")
        try:
            parsed = httpx.URL(value)
        except Exception as exc:
            raise WeComQrProtocolError(
                "wecom_qr_invalid_auth_url"
            ) from exc
        host = str(parsed.host or "").lower()
        if parsed.scheme != "https" or not (
            host == "work.weixin.qq.com"
            or host.endswith(".work.weixin.qq.com")
        ):
            raise WeComQrProtocolError(
                "wecom_qr_invalid_auth_url"
            )
        return str(parsed)
