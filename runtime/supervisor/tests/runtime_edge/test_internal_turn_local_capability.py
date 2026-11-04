from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from platform_supervisor.edge import (
    ExternalConnectionGate,
    RuntimeEdgeDependencies,
    attach_runtime_routes,
)
from platform_supervisor.health import (
    RuntimeHealth,
    StartupCheckResult,
    StartupResourceSnapshot,
)
from platform_supervisor.contracts import canonical_payload_digest
from platform_supervisor.security import RuntimeTokenVerifier
from platform_supervisor.security.tokens import encode_runtime_token


SECRET = "runtime-secret-" + "x" * 32
NOW = 1_800_000_000
EMPTY_DIGEST = "sha256:" + "0" * 64


class FixedConfig:
    current_revision = "revision-18"

    def require_revision(self, config_revision: str) -> None:
        if config_revision != self.current_revision:
            raise RuntimeError("CONFIG_REVISION_STALE")


class FakeSessions:
    def __init__(self) -> None:
        self.selected: list[str] = []

    async def prepare(self, identity: object) -> None:
        del identity

    async def select(self, session_id: str) -> None:
        self.selected.append(session_id)

    async def deselect(self, session_id: str) -> None:
        del session_id


class FakeRuns:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.queue_update_sink = None

    def set_queue_update_sink(self, sink) -> None:  # noqa: ANN001
        self.queue_update_sink = sink

    async def submit(self, **kwargs) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(
            run=SimpleNamespace(
                run_id="run-scheduled-0001",
                session_id=self.session_id,
                state="RUNNING",
            ),
            created=True,
        )


class FakeStore:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def persist_command(self, **kwargs) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(created=True, result=None)

    async def complete_command(self, **kwargs) -> None:
        del kwargs

    async def abandon_command(self, **kwargs) -> None:
        del kwargs

    async def get_run(self, run_id: str) -> SimpleNamespace:
        assert run_id == "run-scheduled-0001"
        return SimpleNamespace(
            run_id=run_id,
            session_id=self.session_id,
            state="RUNNING",
            user_id=7,
            org_id=3,
        )


def access_token(user_id: int = 7, org_id: int = 3, nonce: int = 1) -> str:
    return encode_runtime_token(
        {
            "token_type": "runtime_access",
            "schema_version": 1,
            "sub": f"user-{user_id}",
            "user_id": user_id,
            "org_id": org_id,
            "runtime_id": "runtime-1",
            "admission_generation": 11,
            "allowed_origin": "https://agent.example.com",
            "connection_nonce": f"nonce-{nonce:016d}",
            "issued_at": NOW,
            "expires_at": NOW + 300,
        },
        secret=SECRET,
    )


def session_grant(
    session_id: str,
    *,
    user_id: int = 7,
    org_id: int = 3,
) -> str:
    return encode_runtime_token(
        {
            "token_type": "session_grant",
            "schema_version": 1,
            "user_id": user_id,
            "org_id": org_id,
            "runtime_id": "runtime-1",
            "admission_generation": 11,
            "session_id": session_id,
            "profile_id": "default",
            "runtime_agent_profile_id": 0,
            "model_id": "7",
            "upstream_model_id": "qwen3-235b-a22b",
            "context_length": 128000,
            "config_revision": "revision-18",
            "profile_desired_revision": 0,
            "profile_applied_revision": 0,
            "profile_digest": EMPTY_DIGEST,
            "skill_digest": EMPTY_DIGEST,
            "mcp_digest": EMPTY_DIGEST,
            "knowledge_digest": EMPTY_DIGEST,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
            "issued_at": NOW,
            "expires_at": NOW + 1800,
        },
        secret=SECRET,
    )


def build_app(session_id: str) -> tuple[FastAPI, FakeStore]:
    health = RuntimeHealth()
    health.mark_supervisor_ready(
        StartupCheckResult(
            ready=True,
            errors=(),
            warnings=(),
            snapshot=StartupResourceSnapshot(
                nofile_soft=65536,
                pids_limit=4096,
                disk_used_percent=1,
                disk_free_bytes=10 * 1024**3,
                sqlite_ok=True,
            ),
        )
    )
    health.mark_hermes_ready()
    store = FakeStore(session_id)
    dependencies = RuntimeEdgeDependencies(
        runtime_id="runtime-1",
        binding_id="binding-1",
        api_server_key=SECRET,
        token_verifier=RuntimeTokenVerifier(
            runtime_id="runtime-1",
            secret=SECRET,
            clock=lambda: NOW + 10,
        ),
        store=store,  # type: ignore[arg-type]
        sessions=FakeSessions(),  # type: ignore[arg-type]
        runs=FakeRuns(session_id),  # type: ignore[arg-type]
        health=health,
        connections=ExternalConnectionGate(8),
        config=FixedConfig(),  # type: ignore[arg-type]
    )
    app = FastAPI()
    attach_runtime_routes(app, dependencies)
    return app, store


class WebSocketHarness:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def connect(self, token: str | None = None) -> dict:
        if token is None:
            token = access_token()
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "wss",
            "path": "/runtime-edge",
            "raw_path": b"/runtime-edge",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"runtime.example.com"),
                (b"origin", b"https://agent.example.com"),
                (
                    b"sec-websocket-protocol",
                    f"hermes-runtime-v1, runtime-access.{token}".encode(),
                ),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("runtime.example.com", 443),
            "subprotocols": ["hermes-runtime-v1", f"runtime-access.{token}"],
            "state": {},
        }
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, self.outgoing.put))
        await self.incoming.put({"type": "websocket.connect"})
        return await self.outgoing.get()

    async def send_json(self, payload: dict) -> None:
        await self.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(payload),
            }
        )

    async def receive_json(self) -> dict:
        message = await asyncio.wait_for(self.outgoing.get(), timeout=5)
        assert message["type"] == "websocket.send"
        return json.loads(message["text"])

    async def close(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            await self.task


def internal_turn_envelope(session_id: str) -> dict:
    payload = {
        "type": "internal_turn",
        "source": "scheduled_task",
        "run_id": "run-scheduled-0001",
        "client_message_id": "message-scheduled-0001",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": session_id,
            "profile_id": "default",
            "runtime_agent_profile_id": 0,
            "profile_desired_revision": 0,
            "profile_applied_revision": 0,
            "profile_digest": EMPTY_DIGEST,
            "skill_digest": EMPTY_DIGEST,
            "mcp_digest": EMPTY_DIGEST,
            "knowledge_digest": EMPTY_DIGEST,
            "model_id": "7",
            "upstream_model_id": "qwen3-235b-a22b",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": 11,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "prompt": "定时任务生成本地文件",
        "attachment_file_ids": [],
        "scheduled_task_id": 7,
        "scheduled_occurrence_key": "scheduled:7:test",
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-scheduled-0001",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 10, 6, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    return envelope


async def prepare_desktop(
    harness: WebSocketHarness,
    session_id: str,
    *,
    user_id: int = 7,
    org_id: int = 3,
) -> None:
    await harness.send_json(
        {
            "schema_version": 1,
            "type": "session.prepare",
            "request_id": f"prepare-{session_id}",
            "session_grant": session_grant(
                session_id,
                user_id=user_id,
                org_id=org_id,
            ),
            "local_capabilities": True,
        }
    )
    message = await harness.receive_json()
    assert message["type"] == "session.ready"


async def submit_scheduled_turn(
    client: httpx.AsyncClient,
    session_id: str,
) -> None:
    submitted = await client.post(
        "/internal/v1/commands",
        headers={"Authorization": f"Bearer {SECRET}"},
        json=internal_turn_envelope(session_id),
    )
    assert submitted.status_code == 200, submitted.text


async def desktop_status(
    client: httpx.AsyncClient,
    session_id: str,
) -> dict:
    status = await client.get(
        "/internal/v1/desktop-status",
        headers={"Authorization": f"Bearer {SECRET}"},
        params={"session_id": session_id},
    )
    assert status.status_code == 200, status.text
    return status.json()


async def bridge_request(
    client: httpx.AsyncClient,
    session_id: str,
    request_id: str,
) -> httpx.Response:
    return await client.post(
        "/internal/v1/local-capabilities/request",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={
            "request_id": request_id,
            "session_id": session_id,
            "capability": "workspace.list",
            "args": {},
        },
    )


async def bridge_roundtrip(
    client: httpx.AsyncClient,
    harness: WebSocketHarness,
    session_id: str,
) -> tuple[httpx.Response, dict]:
    request_task = asyncio.create_task(
        client.post(
            "/internal/v1/local-capabilities/request",
            headers={"Authorization": f"Bearer {SECRET}"},
            json={
                "request_id": "local-request-0001",
                "session_id": session_id,
                "capability": "workspace.list",
                "args": {},
            },
        )
    )
    receive_task = asyncio.create_task(harness.receive_json())
    request_message: dict | None = None
    try:
        while True:
            done, _ = await asyncio.wait(
                {request_task, receive_task},
                timeout=2,
            )
            assert done, "local capability bridge timed out"
            if request_task in done:
                response = request_task.result()
                assert response.status_code == 200, response.text
                assert request_message is not None
                return response, request_message
            message = receive_task.result()
            if message.get("type") == "local.capability.request":
                request_message = message
                await harness.send_json(
                    {
                        "schema_version": 1,
                        "type": "local.capability.result",
                        "request_id": message["request_id"],
                        "run_id": message["run_id"],
                        "capability": message["capability"],
                        "ok": True,
                        "value": {"entries": []},
                    }
                )
            receive_task = asyncio.create_task(harness.receive_json())
    finally:
        receive_task.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_internal_turn_routes_local_capability_to_selected_desktop() -> None:
    app, _store = build_app("session-desktop-selected")
    harness = WebSocketHarness(app)
    await harness.connect()
    try:
        await prepare_desktop(harness, "session-desktop-selected")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            await submit_scheduled_turn(client, "session-desktop-selected")

            status = await desktop_status(client, "session-desktop-selected")
            assert status == {
                "session_id": "session-desktop-selected",
                "online": True,
            }

            response, request = await bridge_roundtrip(
                client,
                harness,
                "session-desktop-selected",
            )
        assert request["run_id"] == "run-scheduled-0001"
        assert response.json()["ok"] is True
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_internal_turn_routes_to_desktop_parked_on_other_session() -> None:
    # The desktop workspace folder is user-scoped, not session-scoped: a
    # scheduled run must land locally whenever the user's desktop is simply
    # running, even while it is parked on an unrelated conversation.
    app, _store = build_app("session-scheduled-target")
    harness = WebSocketHarness(app)
    await harness.connect()
    try:
        await prepare_desktop(harness, "session-other-selected")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            await submit_scheduled_turn(client, "session-scheduled-target")

            status = await desktop_status(client, "session-scheduled-target")
            assert status["online"] is True

            response, request = await bridge_roundtrip(
                client,
                harness,
                "session-scheduled-target",
            )
        assert request["run_id"] == "run-scheduled-0001"
        assert response.json()["ok"] is True
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_internal_turn_without_any_desktop_stays_offline() -> None:
    # No desktop connected at all: the gate reports offline (cloud-side tools
    # stay unblocked) and the bridge rejects — cloud generation is the
    # designed fallback.
    app, _store = build_app("session-scheduled-offline")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        await submit_scheduled_turn(client, "session-scheduled-offline")

        status = await desktop_status(client, "session-scheduled-offline")
        assert status == {
            "session_id": "session-scheduled-offline",
            "online": False,
        }

        bridged = await bridge_request(
            client,
            "session-scheduled-offline",
            "local-request-offline",
        )
        assert bridged.status_code == 409


@pytest.mark.asyncio
async def test_internal_turn_survives_desktop_reconnect_mid_run() -> None:
    # A desktop drop must not wipe the backend-submitted active-run mapping:
    # after a reconnect (parked anywhere) the same scheduled run still routes
    # locally instead of degrading to 409.
    app, _store = build_app("session-scheduled-reconnect")
    first = WebSocketHarness(app)
    await first.connect()
    try:
        await prepare_desktop(first, "session-scheduled-reconnect")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            await submit_scheduled_turn(client, "session-scheduled-reconnect")
        await first.close()

        second = WebSocketHarness(app)
        # A reconnect presents a fresh access token: the verifier rejects a
        # reused connection_nonce as a replay.
        await second.connect(access_token(nonce=2))
        try:
            await prepare_desktop(second, "session-other-after-reconnect")
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://runtime.local",
            ) as client:
                response, request = await bridge_roundtrip(
                    client,
                    second,
                    "session-scheduled-reconnect",
                )
            assert request["run_id"] == "run-scheduled-0001"
            assert response.json()["ok"] is True
        finally:
            await second.close()
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_internal_turn_rejects_desktop_of_other_user() -> None:
    # Any online desktop flips desktop-status to online (the gate steers the
    # agent toward desktop_* and it recovers to cloud tools after one 409),
    # but the bridge itself must never hand user 7's run to user 8's desktop.
    app, _store = build_app("session-scheduled-owner")
    harness = WebSocketHarness(app)
    await harness.connect(
        access_token(user_id=8, org_id=4, nonce=2),
    )
    try:
        await prepare_desktop(
            harness,
            "session-other-user",
            user_id=8,
            org_id=4,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            await submit_scheduled_turn(client, "session-scheduled-owner")

            status = await desktop_status(client, "session-scheduled-owner")
            assert status["online"] is True

            bridged = await bridge_request(
                client,
                "session-scheduled-owner",
                "local-request-cross-user",
            )
            assert bridged.status_code == 409
    finally:
        await harness.close()
