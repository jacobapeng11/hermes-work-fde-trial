from __future__ import annotations

import pytest

import app.hermes_runtime.protocol as protocol


class FakeSharedClient:
    def __init__(self) -> None:
        self.is_closed = False

    async def aclose(self) -> None:
        self.is_closed = True


@pytest.mark.asyncio
async def test_shared_runtime_http_client_is_singleton_and_closes(
    monkeypatch,
) -> None:
    fake = FakeSharedClient()
    created: list[FakeSharedClient] = []
    monkeypatch.setattr(protocol, "_runtime_http_client", None)
    monkeypatch.setattr(
        protocol,
        "_new_runtime_http_client",
        lambda: created.append(fake) or fake,
    )

    assert await protocol.shared_runtime_http_client() is fake
    assert await protocol.shared_runtime_http_client() is fake
    assert created == [fake]

    await protocol.close_runtime_http_client()
    assert fake.is_closed is True
    assert protocol._runtime_http_client is None
