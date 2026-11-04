#!/usr/bin/env python3
"""Verify durable follow-up history through the live Runtime Edge.

The Backend API runs in-process with a temporary Principal dependency override;
the WebSocket always traverses the configured Nginx address and the live
Runtime. No user login token is created or logged. Temporary Sessions are
deleted through their formal Backend APIs in ``finally``.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import websockets
from websockets.exceptions import InvalidStatus
from sqlalchemy import select


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
_ORIGINAL_WORKING_DIRECTORY = Path.cwd()
os.chdir(BACKEND_ROOT)
try:
    from app.api.v1.application_agent_deps import (  # noqa: E402
        get_application_agent_admin_principal,
    )
    from app.api.v1.user.runtime_workspace import get_principal  # noqa: E402
    from app.config import settings  # noqa: E402
    from app.database import AsyncSessionLocal, engine  # noqa: E402
    from app.main import app  # noqa: E402
    from app.models import (  # noqa: E402
        ApplicationAgent,
        ApplicationAgentRun,
        ApplicationAgentRuntimeBinding,
        CloudHermesProfile,
        HermesRuntimeBinding,
        RuntimeRun,
        RuntimeSession,
        User,
    )
    from app.services.runtime_workspace import Principal  # noqa: E402
finally:
    os.chdir(_ORIGINAL_WORKING_DIRECTORY)


class SmokeError(RuntimeError):
    """An operator-safe failure that must not contain tickets or message text."""


@dataclass(frozen=True, slots=True)
class Target:
    kind: Literal["personal", "application"]
    principal: Principal
    runtime_workspace_id: int
    application_agent_id: int | None
    binding_id: int
    runtime_id: str


@dataclass(frozen=True, slots=True)
class SessionBootstrap:
    target: Target
    runtime_session_id: str
    model_entry_id: int
    runtime_id: str
    websocket_path: str
    websocket_protocol: str
    runtime_access: str
    session_grant: str


async def _session_has_active_runs(
    target: Target,
    runtime_session_id: str,
) -> bool:
    async with AsyncSessionLocal() as db:
        if target.kind == "personal":
            session_db_id = await db.scalar(
                select(RuntimeSession.id).where(
                    RuntimeSession.runtime_workspace_id
                    == target.runtime_workspace_id,
                    RuntimeSession.runtime_session_id == runtime_session_id,
                )
            )
            if session_db_id is None:
                return False
            active_run_id = await db.scalar(
                select(RuntimeRun.id)
                .where(
                    RuntimeRun.session_id == session_db_id,
                    RuntimeRun.status.in_(("accepted", "pending", "running")),
                )
                .limit(1)
            )
        else:
            active_run_id = await db.scalar(
                select(ApplicationAgentRun.id)
                .where(
                    ApplicationAgentRun.application_agent_id
                    == target.application_agent_id,
                    ApplicationAgentRun.runtime_session_id == runtime_session_id,
                    ApplicationAgentRun.status.in_(("queued", "running")),
                )
                .limit(1)
            )
        return active_run_id is not None


async def wait_for_cleanup_safe_session(
    target: Target,
    runtime_session_id: str,
    *,
    timeout_seconds: float,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while await _session_has_active_runs(target, runtime_session_id):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise SmokeError(
                f"cleanup {target.kind} Session still has active Runs"
            )
        await asyncio.sleep(min(0.5, remaining))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create temporary personal and Application Agent Sessions, send "
            "live follow-ups, reconnect, verify Authority history, and clean up."
        )
    )
    parser.add_argument("--user-code", required=True)
    parser.add_argument("--application-agent-id", type=int, required=True)
    parser.add_argument(
        "--target",
        choices=("all", "personal", "application"),
        default="all",
    )
    parser.add_argument(
        "--runtime-edge-base",
        required=True,
        help="Nginx base URL using an IP address, for example http://10.0.0.2",
    )
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    return parser


def _mapping(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SmokeError(f"{name} response is invalid")
    return value


def _string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SmokeError(f"{name} response is invalid")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SmokeError(f"{name} response is invalid")
    return value


def _success(response: httpx.Response, *, endpoint: str) -> dict[str, Any]:
    if response.is_error:
        raise SmokeError(f"{endpoint} returned HTTP {response.status_code}")
    try:
        return _mapping(response.json(), name=endpoint)
    except (TypeError, ValueError) as exc:
        raise SmokeError(f"{endpoint} returned invalid JSON") from exc


def websocket_url(base_url: str, websocket_path: str) -> str:
    base = urlsplit(base_url)
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise SmokeError("Runtime Edge base is invalid")
    if not websocket_path.startswith("/"):
        raise SmokeError("Runtime Edge WebSocket path is invalid")
    return urlunsplit(
        (
            "wss" if base.scheme == "https" else "ws",
            base.netloc,
            websocket_path,
            "",
            "",
        )
    )


@asynccontextmanager
async def connect_runtime_edge(
    bootstrap: SessionBootstrap,
    *,
    runtime_edge_base: str,
    timeout_seconds: float,
    stage: str,
) -> AsyncIterator[websockets.ClientConnection]:
    """Connect with a stage/status receipt but never render ticket material."""

    endpoint = websocket_url(runtime_edge_base, bootstrap.websocket_path)
    try:
        async with websockets.connect(
            endpoint,
            origin=settings.PUBLIC_WEB_BASE.rstrip("/"),
            subprotocols=[
                bootstrap.websocket_protocol,
                f"runtime-access.{bootstrap.runtime_access}",
            ],
            proxy=None,
            open_timeout=min(timeout_seconds, 30.0),
        ) as socket:
            yield socket
    except InvalidStatus as exc:
        status_code = getattr(exc.response, "status_code", "unknown")
        raise SmokeError(
            f"Runtime Edge {stage} handshake returned HTTP {status_code}"
        ) from None


async def load_targets(user_code: str, application_agent_id: int) -> list[Target]:
    async with AsyncSessionLocal() as db:
        users = list(
            (
                await db.scalars(
                    select(User).where(
                        User.user_code == user_code,
                        User.status == "active",
                    )
                )
            ).all()
        )
        if len(users) != 1:
            raise SmokeError("user-code did not resolve one active user")
        user = users[0]
        profile = await db.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.user_id == user.id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
        )
        if profile is None:
            raise SmokeError("personal workspace is unavailable")
        personal_binding = await db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == profile.id,
                HermesRuntimeBinding.user_id == user.id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        agent = await db.scalar(
            select(ApplicationAgent).where(
                ApplicationAgent.id == application_agent_id,
                ApplicationAgent.org_id == user.org_id,
            )
        )
        application_binding = await db.scalar(
            select(ApplicationAgentRuntimeBinding).where(
                ApplicationAgentRuntimeBinding.application_agent_id
                == application_agent_id,
                ApplicationAgentRuntimeBinding.org_id == user.org_id,
                ApplicationAgentRuntimeBinding.status == "ready",
            )
        )
    if personal_binding is None or agent is None or application_binding is None:
        raise SmokeError("personal or Application Runtime target is unavailable")
    if personal_binding.active_run_count >= 10:
        raise SmokeError("personal Runtime has no free Session capacity")
    if application_binding.active_run_count:
        raise SmokeError("Application Runtime has an active Run")
    principal = Principal(user_id=int(user.id), org_id=int(user.org_id))
    return [
        Target(
            kind="personal",
            principal=principal,
            runtime_workspace_id=int(profile.id),
            application_agent_id=None,
            binding_id=int(personal_binding.id),
            runtime_id=str(personal_binding.runtime_id),
        ),
        Target(
            kind="application",
            principal=principal,
            runtime_workspace_id=application_agent_id,
            application_agent_id=application_agent_id,
            binding_id=int(application_binding.id),
            runtime_id=str(application_binding.runtime_id),
        ),
    ]


class BackendSessionApi:
    def __init__(self, target: Target) -> None:
        self.target = target
        self.client: httpx.AsyncClient | None = None
        self.created_session_id: str | None = None

    async def __aenter__(self) -> BackendSessionApi:
        async def principal_override() -> Principal:
            return self.target.principal

        app.dependency_overrides[get_principal] = principal_override
        app.dependency_overrides[
            get_application_agent_admin_principal
        ] = principal_override
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://followup-history-smoke.invalid",
            headers={"Origin": settings.PUBLIC_WEB_BASE.rstrip("/")},
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.client is not None:
            await self.client.aclose()
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(
            get_application_agent_admin_principal,
            None,
        )

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            raise SmokeError("Backend API client is not open")
        return self.client

    def _paths(self, runtime_session_id: str | None = None) -> tuple[str, str, str]:
        if self.target.kind == "personal":
            base = (
                "/api/v1/user/runtime-workspace/"
                f"{self.target.runtime_workspace_id}/sessions"
            )
            prepare = "/api/v1/user/runtime-edge/prepare"
        else:
            agent_id = self.target.application_agent_id
            base = f"/api/v1/user/application-agents/{agent_id}/studio/sessions"
            prepare = (
                f"/api/v1/user/application-agents/{agent_id}"
                "/studio/runtime-edge/prepare"
            )
        delete = f"{base}/{runtime_session_id}" if runtime_session_id else base
        return base, prepare, delete

    async def create_and_prepare(self) -> SessionBootstrap:
        base, prepare_path, _delete = self._paths()
        created = _success(
            await self._client().post(
                base,
                json={
                    "title": (
                        f"followup-history-smoke-{self.target.kind}-"
                        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                    ),
                    "source_type": "web",
                },
            ),
            endpoint=f"create {self.target.kind} Session",
        )
        runtime_session_id = _string(
            created.get("runtime_session_id"),
            name="Runtime Session",
        )
        self.created_session_id = runtime_session_id
        raw_model_entry_id = created.get("session_model_entry_id")
        model_entry_id = (
            _positive_int(raw_model_entry_id, name="Runtime Session model")
            if self.target.kind == "personal"
            else (
                raw_model_entry_id
                if isinstance(raw_model_entry_id, int)
                and not isinstance(raw_model_entry_id, bool)
                and raw_model_entry_id > 0
                else None
            )
        )
        return await self.prepare(runtime_session_id, model_entry_id)

    async def prepare(
        self,
        runtime_session_id: str,
        model_entry_id: int | None,
    ) -> SessionBootstrap:
        _base, prepare_path, _delete = self._paths(runtime_session_id)
        bootstrap = _success(
            await self._client().post(
                prepare_path,
                json={
                    "runtime_workspace_id": self.target.runtime_workspace_id,
                    "runtime_session_id": runtime_session_id,
                    "model_entry_id": model_entry_id,
                    "attachment_file_ids": [],
                },
            ),
            endpoint=f"prepare {self.target.kind} Runtime Edge",
        )
        runtime_id = _string(bootstrap.get("runtime_id"), name="Runtime Edge")
        if runtime_id != self.target.runtime_id:
            raise SmokeError("Runtime Edge target identity changed")
        return SessionBootstrap(
            target=self.target,
            runtime_session_id=runtime_session_id,
            model_entry_id=_positive_int(
                bootstrap.get("model_entry_id"),
                name="Runtime Edge model",
            ),
            runtime_id=runtime_id,
            websocket_path=_string(
                bootstrap.get("websocket_path"),
                name="Runtime Edge",
            ),
            websocket_protocol=_string(
                bootstrap.get("websocket_protocol"),
                name="Runtime Edge",
            ),
            runtime_access=_string(
                bootstrap.get("runtime_access"),
                name="Runtime Edge",
            ),
            session_grant=_string(
                bootstrap.get("session_grant"),
                name="Runtime Edge",
            ),
        )

    async def delete(self, runtime_session_id: str) -> None:
        _base, _prepare, delete_path = self._paths(runtime_session_id)
        response = await self._client().delete(delete_path)
        if response.status_code != 204:
            raise SmokeError(
                f"cleanup {self.target.kind} Session returned HTTP "
                f"{response.status_code}"
            )


def _decode(value: str | bytes) -> dict[str, Any]:
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        return _mapping(json.loads(text), name="Runtime Edge")
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise SmokeError("Runtime Edge sent invalid JSON") from exc


async def _receive(socket: websockets.ClientConnection, deadline: float) -> dict[str, Any]:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise SmokeError("Runtime Edge conversation timed out")
    try:
        raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise SmokeError("Runtime Edge conversation timed out") from exc
    message = _decode(raw)
    if message.get("type") == "error":
        raise SmokeError("Runtime Edge rejected the smoke conversation")
    return message


async def _prepare_socket(
    socket: websockets.ClientConnection,
    bootstrap: SessionBootstrap,
    deadline: float,
) -> None:
    request_id = f"prepare-{uuid4().hex}"
    await socket.send(
        json.dumps(
            {
                "schema_version": 1,
                "type": "session.prepare",
                "request_id": request_id,
                "session_grant": bootstrap.session_grant,
            }
        )
    )
    while True:
        message = await _receive(socket, deadline)
        if message.get("type") == "session.ready" and message.get(
            "request_id"
        ) == request_id:
            return


def _event(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if message.get("type") == "run.event":
        return str(message.get("event_type") or ""), _mapping(
            message.get("data"),
            name="Runtime Edge event",
        )
    return str(message.get("type") or ""), message


async def _send_followups(
    socket: websockets.ClientConnection,
    *,
    parent_run_id: str,
    session_grant: str,
    label: str,
) -> list[tuple[str, str]]:
    followups = [
        (f"followup-{label}-{index}-{uuid4().hex}", f"{label} 追问 {index}")
        for index in range(1, 4)
    ]
    for client_message_id, content in followups:
        await socket.send(
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "followup.submit",
                    "client_message_id": client_message_id,
                    "parent_run_id": parent_run_id,
                    "session_grant": session_grant,
                    "prompt": content,
                    "attachment_file_ids": [],
                }
            )
        )
    return followups


async def run_followup_turn(
    socket: websockets.ClientConnection,
    bootstrap: SessionBootstrap,
    *,
    mode: Literal["redirect", "steer"],
    timeout_seconds: float,
) -> tuple[list[tuple[str, str]], set[str], dict[str, Any]]:
    nonce = uuid4().hex
    parent_client_message_id = f"parent-{mode}-{nonce}"
    if mode == "redirect":
        prompt = (
            "请先连续分析这个问题并写一篇较长答复：如何设计可靠的消息系统。"
            "不要调用任何工具，至少分十点回答。"
        )
    else:
        prompt = (
            "必须调用 terminal 工具执行命令 `sleep 6; echo TOOL_DONE`，"
            "等待工具完成后再简短回复。"
        )
    await socket.send(
        json.dumps(
            {
                "schema_version": 1,
                "type": "run.submit",
                "client_message_id": parent_client_message_id,
                "session_grant": bootstrap.session_grant,
                "prompt": prompt,
                "attachment_file_ids": [],
            }
        )
    )
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    parent_run_id: str | None = None
    followups: list[tuple[str, str]] = []
    dispositions: set[str] = set()
    terminal_dispositions: dict[str, str] = {}
    proof_kinds: dict[str, str] = {}
    member_receipts: dict[str, dict[str, Any]] = {}
    terminal = False
    expected_proof_kind = (
        "correction_user_row" if mode == "redirect" else "steer_oob_marker"
    )
    while not terminal or len(terminal_dispositions) < 3:
        message = await _receive(socket, deadline)
        event_type, data = _event(message)
        if message.get("type") in {"run.accepted", "run.queued"}:
            parent_run_id = _string(message.get("run_id"), name="Runtime Run")
        if message.get("type") == "run.event" and parent_run_id is None:
            parent_run_id = _string(message.get("run_id"), name="Runtime Run")
        should_send = (
            mode == "redirect"
            and event_type
            in {
                "chat.run.started",
                "chat.activity.delta",
                "chat.reasoning.delta",
                "chat.message.delta",
            }
        ) or (
            mode == "steer"
            and event_type == "chat.tool.event"
            and data.get("status") == "running"
        )
        if should_send and not followups:
            if parent_run_id is None:
                raise SmokeError("follow-up trigger has no parent Run")
            followups = await _send_followups(
                socket,
                parent_run_id=parent_run_id,
                session_grant=bootstrap.session_grant,
                label=f"{bootstrap.target.kind}-{mode}",
            )
        if event_type.startswith("followup."):
            dispositions.add(event_type)
        disposition_identity = (
            data if message.get("type") == "run.event" else message
        )
        disposition_client_message_id = disposition_identity.get(
            "client_message_id"
        )
        if (
            event_type in {"followup.applied", "followup.continued", "followup.failed"}
            and isinstance(disposition_client_message_id, str)
        ):
            terminal_dispositions[disposition_client_message_id] = event_type
            proof_kind = disposition_identity.get("proof_kind")
            if isinstance(proof_kind, str):
                proof_kinds[disposition_client_message_id] = proof_kind
            member_receipts[disposition_client_message_id] = {
                "event_type": event_type,
                "event_seq": disposition_identity.get("event_seq"),
                "proof_kind": proof_kind if isinstance(proof_kind, str) else None,
                "continuation_run_id": disposition_identity.get(
                    "continuation_run_id"
                ),
            }
        if event_type == "chat.done":
            terminal = True
    if len(followups) != 3:
        raise SmokeError(f"{mode} trigger was not observed")
    expected_ids = {client_message_id for client_message_id, _content in followups}
    if set(terminal_dispositions) != expected_ids or set(
        terminal_dispositions.values()
    ) != {"followup.applied"}:
        raise SmokeError(f"{mode} follow-up was not canonically consumed")
    if set(proof_kinds) != expected_ids or set(proof_kinds.values()) != {
        expected_proof_kind
    }:
        raise SmokeError(f"{mode} follow-up canonical proof is invalid")
    return (
        followups,
        dispositions,
        {
            "mode": mode,
            "parent_run_id": parent_run_id,
            "member_client_message_ids": sorted(expected_ids),
            "members": member_receipts,
            "first_abnormal_boundary": None,
        },
    )


async def read_reconnected_history(
    bootstrap: SessionBootstrap,
    *,
    runtime_edge_base: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with connect_runtime_edge(
        bootstrap,
        runtime_edge_base=runtime_edge_base,
        timeout_seconds=timeout_seconds,
        stage="history reconnect",
    ) as socket:
        await _prepare_socket(socket, bootstrap, deadline)
        request_id = f"history-{uuid4().hex}"
        await socket.send(
            json.dumps(
                {
                    "schema_version": 1,
                    "type": "session.history",
                    "request_id": request_id,
                    "session_grant": bootstrap.session_grant,
                }
            )
        )
        while True:
            message = await _receive(socket, deadline)
            if message.get("type") != "session.history" or message.get(
                "request_id"
            ) != request_id:
                continue
            raw_messages = message.get("messages")
            if not isinstance(raw_messages, list) or not all(
                isinstance(item, dict) for item in raw_messages
            ):
                raise SmokeError("Runtime Edge history is invalid")
            return list(raw_messages)


def assert_followup_history(
    messages: list[dict[str, Any]],
    followups: list[tuple[str, str]],
) -> None:
    positions: list[int] = []
    for client_message_id, content in followups:
        matches = [
            (index, message)
            for index, message in enumerate(messages)
            if message.get("client_message_id") == client_message_id
        ]
        if len(matches) != 1 or matches[0][1].get("content") != content:
            raise SmokeError("Authority history lost or duplicated a follow-up")
        positions.append(matches[0][0])
    if positions != sorted(positions):
        raise SmokeError("Authority history changed follow-up admission order")
    rendered = json.dumps(messages, ensure_ascii=False)
    if "[Additional user correction]" in rendered or "OUT-OF-BAND USER MESSAGE" in rendered:
        raise SmokeError("Authority history leaked an internal aggregate marker")


async def run_target(
    target: Target,
    *,
    runtime_edge_base: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    bootstrap: SessionBootstrap | None = None
    cleanup_error: Exception | None = None
    conversation_error: Exception | None = None
    async with BackendSessionApi(target) as backend:
        try:
            bootstrap = await backend.create_and_prepare()
            all_followups: list[tuple[str, str]] = []
            disposition_summary: dict[str, list[str]] = {}
            boundary_receipts: list[dict[str, Any]] = []
            async with connect_runtime_edge(
                bootstrap,
                runtime_edge_base=runtime_edge_base,
                timeout_seconds=timeout_seconds,
                stage="initial conversation",
            ) as socket:
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                await _prepare_socket(socket, bootstrap, deadline)
                for mode in ("redirect", "steer"):
                    followups, dispositions, receipt = await run_followup_turn(
                        socket,
                        bootstrap,
                        mode=mode,
                        timeout_seconds=timeout_seconds,
                    )
                    all_followups.extend(followups)
                    disposition_summary[mode] = sorted(dispositions)
                    boundary_receipts.append(receipt)
            history = await read_reconnected_history(
                await backend.prepare(
                    bootstrap.runtime_session_id,
                    bootstrap.model_entry_id,
                ),
                runtime_edge_base=runtime_edge_base,
                timeout_seconds=timeout_seconds,
            )
            assert_followup_history(history, all_followups)
            return {
                "kind": target.kind,
                "binding_id": target.binding_id,
                "runtime_id": target.runtime_id,
                "followup_count": len(all_followups),
                "history_message_count": len(history),
                "dispositions": disposition_summary,
                "boundary_receipts": boundary_receipts,
            }
        except Exception as exc:
            conversation_error = exc
            raise
        finally:
            if backend.created_session_id is not None:
                try:
                    await wait_for_cleanup_safe_session(
                        target,
                        backend.created_session_id,
                        timeout_seconds=timeout_seconds,
                    )
                    await backend.delete(backend.created_session_id)
                except Exception as exc:  # cleanup failure must fail the smoke
                    cleanup_error = exc
            if cleanup_error is not None:
                if conversation_error is not None:
                    raise SmokeError(
                        f"{conversation_error}; temporary Session cleanup also failed"
                    ) from conversation_error
                raise cleanup_error


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout_seconds <= 0:
        raise SmokeError("timeout must be positive")
    targets = await load_targets(args.user_code, args.application_agent_id)
    if args.target != "all":
        targets = [target for target in targets if target.kind == args.target]
    results = []
    for target in targets:
        results.append(
            await run_target(
                target,
                runtime_edge_base=args.runtime_edge_base,
                timeout_seconds=args.timeout_seconds,
            )
        )
    return {
        "success": True,
        "simulation": "principal-override-nginx-live-runtime-edge",
        "results": results,
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
    except SmokeError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, sort_keys=True))
        return 1
    except Exception as exc:  # never render exceptions that may contain a ticket
        print(
            json.dumps(
                {"success": False, "error": f"unexpected {type(exc).__name__}"},
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
