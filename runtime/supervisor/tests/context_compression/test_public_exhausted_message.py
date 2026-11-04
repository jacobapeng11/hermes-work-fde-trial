from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from platform_supervisor.hermes.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
)
from platform_supervisor.hermes.schemas import RuntimeEndpoint


def _endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        endpoint_base_url="http://127.0.0.1:31080",
        endpoint_host_header="hermes-runtime-101.hermes-runtimes.hermes.local",
        api_key="runtime-api-key",
    )


def _event(event_type: str, session_id: str, payload: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": event_type, "session_id": session_id, "payload": payload},
    }


class FakeWebSocket:
    def __init__(self, frames: list[dict]) -> None:
        self.frames = [json.dumps(frame) for frame in frames]
        self.sent: list[dict] = []
        self._response_count = 0

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if not self.frames:
            raise AssertionError("dashboard adapter read past terminal frame")
        frame = json.loads(self.frames.pop(0))
        if not frame.get("$response"):
            return json.dumps(frame)
        while len(self.sent) <= self._response_count:
            await asyncio.sleep(0)
        request = self.sent[self._response_count]
        self._response_count += 1
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": frame["$response"],
            }
        )


async def _stream_events(ws: FakeWebSocket):
    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    return [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-error", message="hello"
        )
    ]


@pytest.mark.asyncio
async def test_compression_exhausted_public_message_is_chinese() -> None:
    upstream_message = "Context length exceeded: max compression attempts (10) reached."
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-error",
                    "resumed": "rs-error",
                    "messages_omitted": True,
                    "messages": [],
                    "message_count": 0,
                }
            },
            {"$response": {"status": "streaming"}},
            _event(
                "message.complete",
                "live-error",
                {
                    "text": upstream_message,
                    "rendered": upstream_message,
                    "error": upstream_message,
                    "status": "error",
                    "usage": {"active_subagents": 0},
                },
            ),
        ]
    )

    events = await _stream_events(ws)

    completed = next(
        event for event in events if event.event == "chat.message.completed"
    )
    error = next(event for event in events if event.event == "chat.error")
    expected = "当前上下文压缩已经达到10次，你可以选择是否要继续"
    assert completed.data["text"] == expected
    assert completed.data["rendered"] == expected
    assert completed.data["error"] == expected
    assert error.data["message"] == expected


@pytest.mark.asyncio
async def test_ordinary_terminal_error_message_is_unchanged() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-error",
                    "resumed": "rs-error",
                    "messages_omitted": True,
                    "messages": [],
                    "message_count": 0,
                }
            },
            {"$response": {"status": "streaming"}},
            _event(
                "message.complete",
                "live-error",
                {
                    "text": "Error: upstream failed",
                    "status": "error",
                    "usage": {"active_subagents": 0},
                },
            ),
        ]
    )

    events = await _stream_events(ws)
    error = next(event for event in events if event.event == "chat.error")

    assert error.data["message"] == "Error: upstream failed"
