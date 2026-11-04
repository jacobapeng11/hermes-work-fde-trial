#!/usr/bin/env python3
"""Execute one real Runtime Edge conversation without browser/EnterpriseIdp login.

The Backend routes run in-process so a temporary, verified Principal can be
provided through FastAPI's dependency override.  The Runtime Edge WebSocket is
always the live public route: RuntimeAccess and SessionGrant are issued by the
same Backend route implementations that the User Web calls, and validated by
the live Supervisor.  No platform login JWT is generated, persisted or logged.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import websockets
from sqlalchemy import select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.api.v1.user.runtime_workspace import get_principal  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import CloudHermesProfile, HermesRuntimeBinding, User  # noqa: E402
from app.services.runtime_workspace import Principal  # noqa: E402


class RuntimeEdgeSmokeError(RuntimeError):
    """An operator-safe failure: never include ticket, prompt, or reply data."""


@dataclass(frozen=True)
class SmokeTarget:
    principal: Principal
    runtime_id: str
    binding_id: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create one Runtime Session and exercise the live Runtime Edge "
            "WebSocket without browser or EnterpriseIdp login."
        )
    )
    parser.add_argument("--runtime-workspace-id", type=int, required=True)
    parser.add_argument("--expected-user-id", type=int, required=True)
    parser.add_argument(
        "--runtime-edge-base",
        default=None,
        help=(
            "Optional direct Nginx Runtime Edge base for an interface smoke. "
            "It bypasses browser SSO but still traverses Nginx, Kourier, and "
            "the live Supervisor."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser


def websocket_url(*, public_base: str, websocket_path: str) -> str:
    base = urlsplit(public_base)
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise RuntimeEdgeSmokeError("configured public web base is invalid")
    if not websocket_path.startswith("/"):
        raise RuntimeEdgeSmokeError("Runtime Edge returned an invalid WebSocket path")
    return urlunsplit(
        (
            "wss" if base.scheme == "https" else "ws",
            base.netloc,
            websocket_path,
            "",
            "",
        )
    )


def _require_mapping(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeEdgeSmokeError(f"{name} response is invalid")
    return value


def _require_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeEdgeSmokeError(f"{name} response is invalid")
    return value


def _require_int(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeEdgeSmokeError(f"{name} response is invalid")
    return value


def _assert_success(response: httpx.Response, *, endpoint: str) -> dict[str, Any]:
    if response.is_error:
        raise RuntimeEdgeSmokeError(
            f"{endpoint} returned HTTP {response.status_code}"
        )
    try:
        return _require_mapping(response.json(), name=endpoint)
    except (TypeError, ValueError) as exc:
        raise RuntimeEdgeSmokeError(f"{endpoint} returned invalid JSON") from exc


async def load_target(
    *,
    runtime_workspace_id: int,
    expected_user_id: int,
) -> SmokeTarget:
    async with AsyncSessionLocal() as db:
        profile = await db.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
        )
        if profile is None or int(profile.user_id) != expected_user_id:
            raise RuntimeEdgeSmokeError("runtime workspace ownership does not match")
        user = await db.scalar(
            select(User).where(
                User.id == expected_user_id,
                User.org_id == profile.org_id,
                User.status == "active",
            )
        )
        binding = await db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                HermesRuntimeBinding.org_id == profile.org_id,
                HermesRuntimeBinding.user_id == expected_user_id,
                HermesRuntimeBinding.status == "ready",
            )
        )
    if user is None or binding is None:
        raise RuntimeEdgeSmokeError("active personal Runtime Binding is unavailable")
    return SmokeTarget(
        principal=Principal(user_id=expected_user_id, org_id=int(profile.org_id)),
        runtime_id=str(binding.runtime_id),
        binding_id=int(binding.id),
    )


async def create_bootstrap(
    *,
    target: SmokeTarget,
    runtime_workspace_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    async def principal_override() -> Principal:
        return target.principal

    app.dependency_overrides[get_principal] = principal_override
    origin = settings.PUBLIC_WEB_BASE.rstrip("/")
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime-edge-interface-smoke.invalid",
            headers={"Origin": origin},
        ) as client:
            created = _assert_success(
                await client.post(
                    f"/api/v1/user/runtime-workspace/{runtime_workspace_id}/sessions",
                    json={
                        "title": (
                            "runtime-edge-interface-smoke-"
                            f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                        ),
                        "source_type": "web",
                    },
                ),
                endpoint="create Runtime Session",
            )
            session_id = _require_string(
                created.get("runtime_session_id"), name="Runtime Session"
            )
            model_entry_id = _require_int(
                created.get("session_model_entry_id"), name="Runtime Session"
            )
            bootstrap = _assert_success(
                await client.post(
                    "/api/v1/user/runtime-edge/prepare",
                    json={
                        "runtime_workspace_id": runtime_workspace_id,
                        "runtime_session_id": session_id,
                        "model_entry_id": model_entry_id,
                        "attachment_file_ids": [],
                    },
                ),
                endpoint="prepare Runtime Edge",
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
    return created, bootstrap


async def run_websocket_conversation(
    *,
    bootstrap: dict[str, Any],
    runtime_edge_base: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    runtime_id = _require_string(bootstrap.get("runtime_id"), name="Runtime Edge")
    websocket_path = _require_string(
        bootstrap.get("websocket_path"), name="Runtime Edge"
    )
    protocol = _require_string(
        bootstrap.get("websocket_protocol"), name="Runtime Edge"
    )
    runtime_access = _require_string(
        bootstrap.get("runtime_access"), name="Runtime Edge"
    )
    session_grant = _require_string(
        bootstrap.get("session_grant"), name="Runtime Edge"
    )
    endpoint = websocket_url(
        public_base=runtime_edge_base,
        websocket_path=websocket_path,
    )
    request_nonce = uuid4().hex
    expected_reply = f"RUNTIME_EDGE_ACCEPTANCE_{request_nonce}"
    request_id = f"prepare-{request_nonce}"
    message_id = f"message-{request_nonce}"
    completed_reply = False
    terminal = False
    event_count = 0
    run_id: str | None = None
    async with websockets.connect(
        endpoint,
        origin=settings.PUBLIC_WEB_BASE.rstrip("/"),
        subprotocols=[protocol, f"runtime-access.{runtime_access}"],
        proxy=None,
        open_timeout=min(timeout_seconds, 30.0),
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "session.prepare",
                    "request_id": request_id,
                    "session_grant": session_grant,
                }
            )
        )
        await _wait_for_prepare(
            socket=socket,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )
        await socket.send(
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "run.submit",
                    "client_message_id": message_id,
                    "session_grant": session_grant,
                    "prompt": (
                        f'请仅回复“{expected_reply}”，不要调用工具，也不要解释。'
                    ),
                    "attachment_file_ids": [],
                }
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while not terminal:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeEdgeSmokeError("Runtime Edge conversation timed out")
            message = _decode_edge_message(
                await asyncio.wait_for(socket.recv(), timeout=remaining)
            )
            message_type = message.get("type")
            if message_type == "error":
                raise RuntimeEdgeSmokeError("Runtime Edge rejected the conversation")
            if message_type in {"run.queued", "run.accepted"}:
                run_id = _require_string(message.get("run_id"), name="Runtime Edge")
                continue
            if message_type != "run.event":
                continue
            event_count += 1
            run_id = _require_string(message.get("run_id"), name="Runtime Edge")
            event_type = _require_string(message.get("event_type"), name="Runtime Edge")
            data = _require_mapping(message.get("data"), name="Runtime Edge")
            if event_type == "chat.message.completed":
                text = data.get("text")
                if isinstance(text, str) and expected_reply in text:
                    completed_reply = True
            if event_type == "chat.done":
                terminal = True
    if run_id is None or not completed_reply:
        raise RuntimeEdgeSmokeError("Runtime Edge completed without the expected reply")
    return {
        "runtime_id": runtime_id,
        "runtime_run_id": run_id,
        "event_count": event_count,
        "message_completed": completed_reply,
        "chat_done": terminal,
    }


async def _wait_for_prepare(
    *,
    socket: websockets.ClientConnection,
    request_id: str,
    timeout_seconds: float,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RuntimeEdgeSmokeError("Runtime Edge session preparation timed out")
        message = _decode_edge_message(
            await asyncio.wait_for(socket.recv(), timeout=remaining)
        )
        message_type = message.get("type")
        if message_type == "error":
            raise RuntimeEdgeSmokeError("Runtime Edge rejected session preparation")
        if message_type == "session.ready" and message.get("request_id") == request_id:
            return


def _decode_edge_message(value: str | bytes) -> dict[str, Any]:
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        return _require_mapping(json.loads(text), name="Runtime Edge")
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeEdgeSmokeError("Runtime Edge sent an invalid message") from exc


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout_seconds <= 0:
        raise RuntimeEdgeSmokeError("timeout must be positive")
    target = await load_target(
        runtime_workspace_id=args.runtime_workspace_id,
        expected_user_id=args.expected_user_id,
    )
    created, bootstrap = await create_bootstrap(
        target=target,
        runtime_workspace_id=args.runtime_workspace_id,
    )
    evidence = await run_websocket_conversation(
        bootstrap=bootstrap,
        runtime_edge_base=args.runtime_edge_base or settings.PUBLIC_WEB_BASE,
        timeout_seconds=args.timeout_seconds,
    )
    return {
        "success": True,
        "simulation": "api-principal-override-live-runtime-edge",
        "runtime_workspace_id": args.runtime_workspace_id,
        "runtime_session_id": _require_string(
            created.get("runtime_session_id"), name="Runtime Session"
        ),
        "binding_id": target.binding_id,
        **evidence,
    }


async def _run_and_dispose(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return await run_smoke(args)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = asyncio.run(_run_and_dispose(args))
    except RuntimeEdgeSmokeError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        return 1
    except Exception as exc:  # Defensive: do not render exceptions containing tickets.
        print(
            json.dumps(
                {"success": False, "error": f"unexpected {type(exc).__name__}"},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
