from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from uuid import uuid4

import pytest

from platform_supervisor.hermes.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
)
from platform_supervisor.hermes.schemas import RuntimeEndpoint


class ImageAttachWebSocket:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        self.sent.append(request)
        method = request["method"]
        if method == "session.resume":
            result = {
                "session_id": "rs-101-a",
                "info": {"model": "6"},
                "messages_omitted": True,
                "messages": [],
                "message_count": 0,
            }
        elif method == "shell.exec":
            result = {"code": 0, "stdout": ""}
        elif method == "image.attach":
            result = {
                "attached": True,
                "path": request["params"]["path"],
                "count": 1,
            }
        elif method == "prompt.submit":
            result = {"status": "streaming"}
        else:
            result = {}
        await self.frames.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": result,
                }
            )
        )
        if method == "prompt.submit":
            await self.frames.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "message.complete",
                            "session_id": "rs-101-a",
                            "payload": {
                                "text": "ok",
                                "status": "complete",
                                "usage": {},
                            },
                        },
                    }
                )
            )

    async def recv(self) -> str:
        return await self.frames.get()


@pytest.mark.asyncio
async def test_image_attachment_is_attached_before_prompt_submit() -> None:
    websocket = ImageAttachWebSocket()

    @asynccontextmanager
    async def connector(_endpoint):
        yield websocket

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace/sessions",
    )
    endpoint = RuntimeEndpoint(
        runtime_workspace_id=101,
        runtime_id=f"runtime-{uuid4().hex[:12]}",
        endpoint_base_url="http://127.0.0.1:9119",
        endpoint_host_header="127.0.0.1:9119",
        api_key="runtime-secret",
    )
    events = []
    async for event in adapter.stream_session_chat(
        endpoint,
        runtime_session_id="rs-101-a",
        runtime_run_id="rr-image",
        message="请识别这张图片",
        attachments=[
            {
                "file_id": "12",
                "display_name": "image.png",
                "relative_path": "workspace/uploads/image.png",
                "mime_type": "image/png",
            }
        ],
    ):
        events.append(event)

    methods = [request["method"] for request in websocket.sent]
    assert methods.index("image.attach") < methods.index("prompt.submit")
    image_request = next(
        request for request in websocket.sent
        if request["method"] == "image.attach"
    )
    assert image_request["params"] == {
        "session_id": "rs-101-a",
        "path": "/data/workspace/sessions/uploads/image.png",
    }
    assert events
