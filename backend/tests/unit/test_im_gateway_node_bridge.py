from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from app.im_gateway.node_bridge import WeComBridgeError, WeComNodeBridge


@pytest.mark.asyncio
async def test_official_sdk_bridge_reports_the_pinned_full_contract() -> None:
    bridge = WeComNodeBridge()
    try:
        ping = await bridge.start()

        assert bridge.is_running is True
        assert ping["sdk"] == "@wecom/aibot-node-sdk"
        assert ping["sdk_version"] == "1.0.7"
        assert ping["capabilities"]["inbound_messages"] == [
            "text",
            "image",
            "mixed",
            "voice",
            "file",
            "video",
        ]
        assert ping["capabilities"]["quote_messages"] == [
            "text",
            "image",
            "mixed",
            "voice",
            "file",
        ]
        assert set(ping["capabilities"]["outbound"]) >= {
            "generic_reply",
            "stream",
            "stream_with_template_card",
            "template_card",
            "update_template_card",
            "active_markdown",
            "active_template_card",
            "file",
            "image",
            "voice",
            "video",
        }
    finally:
        temp_root = bridge.temp_root
        await bridge.stop()

    assert bridge.is_running is False
    assert temp_root is not None
    assert temp_root.exists() is False


@pytest.mark.asyncio
async def test_bridge_restarts_cleanly_after_an_unexpected_exit() -> None:
    bridge = WeComNodeBridge()
    try:
        await bridge.start()
        old_root = bridge.temp_root
        process = bridge._process
        assert process is not None

        process.terminate()
        assert await bridge._wait_for_exit(process, timeout=5)
        if bridge._reader_task is not None:
            await asyncio.wait_for(bridge._reader_task, timeout=5)
        assert bridge.is_running is False

        ping = await bridge.start()
        assert ping["pong"] is True
        assert bridge.is_running is True
        assert bridge.temp_root != old_root
        assert old_root is not None
        assert old_root.exists() is False
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_stream_state_submission_uses_sdk_fifo_without_waiting_for_ack(
) -> None:
    connections: asyncio.Queue[ServerConnection] = asyncio.Queue()
    received: asyncio.Queue[
        tuple[ServerConnection, dict[str, Any]]
    ] = asyncio.Queue()
    disconnected = asyncio.Event()

    async def on_bridge_event(event: dict[str, Any]) -> None:
        if event.get("type") == "connection.disconnected":
            disconnected.set()

    async def handler(connection: ServerConnection) -> None:
        try:
            auth = json.loads(await connection.recv())
            await connection.send(
                json.dumps(
                    {
                        "headers": {
                            "req_id": auth["headers"]["req_id"],
                        },
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )
            )
            await connections.put(connection)
            async for raw in connection:
                await received.put((connection, json.loads(raw)))
        except ConnectionClosed:
            pass

    bridge = WeComNodeBridge(event_handler=on_bridge_event)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            await bridge.start()
            await bridge.connect(
                channel_id=1,
                generation=2,
                lease_epoch=3,
                bot_id="bot-1",
                secret="secret-1",
                ws_url=f"ws://127.0.0.1:{port}",
            )
            connection = await asyncio.wait_for(
                connections.get(), timeout=2
            )

            await asyncio.wait_for(
                bridge.submit_stream_state(
                    channel_id=1,
                    generation=2,
                    lease_epoch=3,
                    provider_request_id="request-1",
                    stream_id="stream-1",
                    content="已收到，正在排队…",
                ),
                timeout=1,
            )
            first_connection, first = await asyncio.wait_for(
                received.get(), timeout=1
            )
            assert first_connection is connection
            assert first["headers"]["req_id"] == "request-1"
            assert first["body"]["stream"] == {
                "id": "stream-1",
                "finish": False,
                "content": "已收到，正在排队…",
            }

            await asyncio.wait_for(
                bridge.submit_stream_state(
                    channel_id=1,
                    generation=2,
                    lease_epoch=3,
                    provider_request_id="request-1",
                    stream_id="stream-1",
                    content="<think></think>",
                ),
                timeout=1,
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(received.get(), timeout=0.1)

            terminal = asyncio.create_task(
                bridge.reply_stream(
                    channel_id=1,
                    generation=2,
                    lease_epoch=3,
                    provider_request_id="request-1",
                    stream_id="stream-1",
                    content="最终回答",
                    finish=True,
                    non_blocking=False,
                )
            )
            await connection.send(
                json.dumps(
                    {
                        "headers": {"req_id": "request-1"},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )
            )
            second_connection, second = await asyncio.wait_for(
                received.get(), timeout=1
            )
            assert second_connection is connection
            assert second["body"]["stream"] == {
                "id": "stream-1",
                "finish": False,
                "content": "<think></think>",
            }
            assert terminal.done() is False

            await connection.send(
                json.dumps(
                    {
                        "headers": {"req_id": "request-1"},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )
            )
            terminal_connection, terminal_frame = (
                await asyncio.wait_for(received.get(), timeout=1)
            )
            assert terminal_connection is connection
            assert terminal_frame["body"]["stream"] == {
                "id": "stream-1",
                "finish": True,
                "content": "最终回答",
            }
            assert terminal.done() is False

            await connection.send(
                json.dumps(
                    {
                        "headers": {"req_id": "request-1"},
                        "errcode": 0,
                        "errmsg": "ok",
                    }
                )
            )
            assert await asyncio.wait_for(terminal, timeout=1) is True

            await bridge.submit_stream_state(
                channel_id=1,
                generation=2,
                lease_epoch=3,
                provider_request_id="request-disconnect",
                stream_id="stream-disconnect",
                content="<think></think>",
            )
            await asyncio.wait_for(received.get(), timeout=1)
            await connection.close()
            await asyncio.wait_for(disconnected.wait(), timeout=1)
            await asyncio.sleep(0.05)

            assert (await bridge.request("ping"))["pong"] is True
            with pytest.raises(WeComBridgeError):
                await bridge.submit_stream_state(
                    channel_id=1,
                    generation=2,
                    lease_epoch=3,
                    provider_request_id="request-local-error",
                    stream_id="stream-local-error",
                    content="<think></think>",
                )
        finally:
            await bridge.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"submitted": False}])
async def test_stream_state_submission_rejects_invalid_bridge_response(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    bridge = WeComNodeBridge()

    async def request(
        operation: str, **_payload: object
    ) -> dict[str, object]:
        assert operation == "submit_stream_state"
        assert _payload["timeout"] == 5
        return response

    monkeypatch.setattr(bridge, "request", request)

    with pytest.raises(WeComBridgeError) as exc_info:
        await bridge.submit_stream_state(
            channel_id=1,
            generation=2,
            lease_epoch=3,
            provider_request_id="request-1",
            stream_id="stream-1",
            content="<think></think>",
        )

    assert exc_info.value.code == "BRIDGE_INVALID_RESPONSE"
