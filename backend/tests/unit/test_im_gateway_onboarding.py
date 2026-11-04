from __future__ import annotations

import httpx
import pytest

from app.im_gateway import onboarding as onboarding_module
from app.im_gateway.onboarding import (
    WECOM_QR_SOURCE,
    WeComQrClient,
    WeComQrProtocolError,
)


@pytest.mark.asyncio
async def test_qr_adapter_explicitly_ignores_host_socks_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict] = []
    async_client = httpx.AsyncClient

    def build_client(*args, **kwargs):
        constructed.append(kwargs)
        return async_client(*args, **kwargs)

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.setattr(
        onboarding_module.httpx,
        "AsyncClient",
        build_client,
    )
    client = WeComQrClient(
        generate_url="https://work.weixin.qq.com/ai/qc/generate",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": {
                        "scode": "scan-code",
                        "auth_url": "https://work.weixin.qq.com/confirm",
                    }
                },
            )
        ),
    )

    challenge = await client.generate()

    assert challenge.scode == "scan-code"
    assert constructed[0]["trust_env"] is False


@pytest.mark.asyncio
async def test_qr_adapter_uses_wecom_cli_protocol_and_returns_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "scode": "scan-code",
                        "auth_url": "https://work.weixin.qq.com/confirm",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "success",
                    "bot_info": {
                        "botid": "bot-from-qr",
                        "secret": "secret-from-qr",
                    },
                }
            },
        )

    client = WeComQrClient(
        generate_url="https://work.weixin.qq.com/ai/qc/generate",
        query_url="https://work.weixin.qq.com/ai/qc/query_result",
        transport=httpx.MockTransport(handler),
    )

    challenge = await client.generate()
    result = await client.poll(challenge.scode)

    assert challenge.auth_url == "https://work.weixin.qq.com/confirm"
    assert result.status == "credentials_ready"
    assert result.bot_id == "bot-from-qr"
    assert result.secret == "secret-from-qr"
    assert requests[0].url.params["source"] == WECOM_QR_SOURCE
    assert requests[0].url.params["plat"] == "3"
    assert requests[1].url.params["scode"] == "scan-code"
    assert set(requests[1].url.params.keys()) == {"scode"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        ({"data": {"status": "scan"}}, "scanned"),
        ({"data": {"status": "timeout"}}, "expired"),
        ({"data": {"status": "failed"}}, "failed"),
        ({"data": {"status": "unknown"}}, "pending"),
    ],
)
async def test_qr_adapter_maps_all_nonterminal_states(
    upstream: dict, expected: str
) -> None:
    client = WeComQrClient(
        query_url="https://work.weixin.qq.com/ai/qc/query_result",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=upstream)
        ),
    )

    assert (await client.poll("scan-code")).status == expected


@pytest.mark.asyncio
async def test_qr_adapter_rejects_success_without_credentials() -> None:
    client = WeComQrClient(
        query_url="https://work.weixin.qq.com/ai/qc/query_result",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"data": {"status": "success", "bot_info": {}}},
            )
        ),
    )

    with pytest.raises(
        WeComQrProtocolError, match="wecom_qr_missing_botid"
    ):
        await client.poll("scan-code")


@pytest.mark.asyncio
async def test_qr_adapter_rejects_non_wecom_qr_destination() -> None:
    client = WeComQrClient(
        generate_url="https://work.weixin.qq.com/ai/qc/generate",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": {
                        "scode": "scan-code",
                        "auth_url": "https://example.test/phishing",
                    }
                },
            )
        ),
    )

    with pytest.raises(
        WeComQrProtocolError, match="wecom_qr_invalid_auth_url"
    ):
        await client.generate()
