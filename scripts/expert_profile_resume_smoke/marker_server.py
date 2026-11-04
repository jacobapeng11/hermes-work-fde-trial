"""Deterministic temporary MCP servers used as external capability evidence."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .contracts import SmokeError


class MarkerMcp:
    def __init__(self, *, suffix: str, host: str, port: int) -> None:
        self.suffix = suffix
        self.host = host
        self.port = port
        self.sessions: dict[str, dict[str, str]] = {}
        self.history: list[dict[str, Any]] = []
        self.hold_entered = asyncio.Event()
        self.hold_release = asyncio.Event()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.app = FastAPI()
        self.app.add_api_route(
            f"/{suffix}/mcp/{{group}}/{{version}}",
            self.handle,
            methods=["POST"],
        )

    def url(self, group: str, version: str) -> str:
        return f"http://{self.host}:{self.port}/{self.suffix}/mcp/{group}/{version}"

    @property
    def safe_suffix(self) -> str:
        return self.suffix.replace("-", "_")

    def isolation_tool_name(self, connection: str, kind: str = "marker") -> str:
        if connection not in {"a", "b"} or kind not in {"marker", "hold"}:
            raise ValueError("invalid isolation MCP tool identity")
        if connection == "b" and kind == "hold":
            raise ValueError("mcp-b does not expose a hold tool")
        return f"{connection}_{kind}_{self.safe_suffix}"

    async def start(self) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="0.0.0.0",
                port=self.port,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self._task = asyncio.create_task(self._server.serve())
        deadline = time.monotonic() + 10
        while not self._server.started:
            if self._task.done():
                raise SmokeError("temporary MCP marker failed to start")
            if time.monotonic() >= deadline:
                raise SmokeError("temporary MCP marker start timed out")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    def reset_hold(self) -> None:
        self.hold_entered.clear()
        self.hold_release.clear()

    def release_all_holds(self) -> None:
        self.hold_release.set()

    async def handle(self, group: str, version: str, request: Request) -> Response:
        allowed = {
            ("alpha", "control"),
            ("alpha", "v1"),
            ("alpha", "v2"),
            ("beta", "static"),
            ("registry", "a"),
            ("registry", "b"),
        }
        if (group, version) not in allowed:
            return Response(status_code=404)
        payload = await request.json()
        request_id = payload.get("id") if isinstance(payload, dict) else None
        method = str(payload.get("method") or "") if isinstance(payload, dict) else ""
        if method == "initialize":
            return self._initialize(group, version, payload, request_id)
        if method.startswith("notifications/"):
            return Response(status_code=202)
        current = self.sessions.get(request.headers.get("mcp-session-id", ""))
        if current is None:
            return self._rpc_error(request_id, -32001, "unknown session", 404)
        if method == "ping":
            result: dict[str, Any] = {}
        elif method == "tools/list":
            result = {"tools": self._tool_definitions(group, version)}
        elif method == "tools/call":
            called = await self._call_tool(group, version, current, payload, request_id)
            if isinstance(called, JSONResponse):
                return called
            result = called
        else:
            return self._rpc_error(request_id, -32601, "method not found")
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _initialize(
        self,
        group: str,
        version: str,
        payload: dict[str, Any],
        request_id: Any,
    ) -> JSONResponse:
        session_id = uuid4().hex
        boot_id = f"{group}-{version}-{uuid4().hex[:10]}"
        self.sessions[session_id] = {
            "group": group,
            "version": version,
            "boot_id": boot_id,
        }
        self._event("initialize", group, version, boot_id=boot_id)
        protocol = str(
            ((payload.get("params") or {}).get("protocolVersion")) or "2025-03-26"
        )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": f"resume-smoke-{group}-{version}",
                        "version": "1.0.0",
                    },
                },
            },
            headers={"Mcp-Session-Id": session_id},
        )

    def _tool_definitions(self, group: str, version: str) -> list[dict[str, Any]]:
        if group == "registry":
            tools = [self.isolation_tool_name(version)]
            if version == "a":
                tools.append(self.isolation_tool_name("a", "hold"))
        else:
            tools = [
                "hold_marker"
                if version == "control"
                else f"get_{group}_{version}_marker"
            ]
        return [
            {
                "name": tool,
                "description": (
                    f"Return deterministic {group} {version} resume smoke evidence."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
            for tool in tools
        ]

    async def _call_tool(
        self,
        group: str,
        version: str,
        current: dict[str, str],
        payload: dict[str, Any],
        request_id: Any,
    ) -> dict[str, Any] | JSONResponse:
        tool = str((payload.get("params") or {}).get("name") or "")
        expected = {item["name"] for item in self._tool_definitions(group, version)}
        if tool not in expected:
            return self._rpc_error(request_id, -32602, "unknown tool")
        hold_state: str | None = None
        is_hold = version == "control" or (
            group == "registry"
            and tool == self.isolation_tool_name("a", "hold")
        )
        if is_hold:
            self._event("hold_entered", group, version, boot_id=current["boot_id"])
            self.hold_entered.set()
            try:
                await asyncio.wait_for(
                    self.hold_release.wait(),
                    timeout=90 if group == "registry" else 45,
                )
                hold_state = "released"
            except asyncio.TimeoutError:
                hold_state = "auto_timeout"
        marker = (
            f"MCP_{version.upper()}_{self.suffix}"
            if group == "registry"
            else f"{group.upper()}_{version.upper()}_{self.suffix}"
        )
        self._event(
            "tool_call",
            group,
            version,
            boot_id=current["boot_id"],
            tool=tool,
            hold=hold_state,
        )
        content = json.dumps(
            {
                "marker": marker,
                "boot_id": current["boot_id"],
                "hold": hold_state,
            },
            sort_keys=True,
        )
        return {"content": [{"type": "text", "text": content}], "isError": False}

    def _event(self, event: str, group: str, version: str, **extra: Any) -> None:
        self.history.append(
            {"at": time.time(), "event": event, "group": group, "version": version, **extra}
        )

    @staticmethod
    def _rpc_error(
        request_id: Any,
        code: int,
        message: str,
        status_code: int = 200,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            status_code=status_code,
        )
