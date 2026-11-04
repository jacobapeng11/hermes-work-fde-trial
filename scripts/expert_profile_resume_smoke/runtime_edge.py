"""Runtime Edge Session/Run protocol driver and exact Session cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import websockets

from app.config import settings
from app.services.runtime_commands import RuntimeCommandClient, RuntimeCommandError

from .contracts import CheckpointStore, RunEvidence, SmokeError, Target


def websocket_url(base: str, path: str) -> str:
    parsed = urlsplit(base)
    return urlunsplit(
        ("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, "", "")
    )


class RuntimeEdge:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        target: Target,
        store: CheckpointStore,
        runtime_edge_base: str,
        timeout_seconds: float,
    ) -> None:
        self.client = client
        self.target = target
        self.store = store
        self.runtime_edge_base = runtime_edge_base
        self.timeout_seconds = timeout_seconds
        self.command_client = RuntimeCommandClient()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.running_tasks: set[asyncio.Task[RunEvidence]] = set()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise SmokeError(
                f"{method} {path} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json() if response.content else None

    async def create_session(
        self,
        profile_id: int | None,
        label: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": f"resume-smoke-{label}-{self.store.checkpoint.suffix}",
            "source_type": "web",
        }
        if profile_id is not None:
            payload["runtime_agent_profile_id"] = profile_id
        session = await self.request(
            "POST",
            f"/user/runtime-workspace/{self.target.workspace_id}/sessions",
            json=payload,
        )
        session_id = str(session["runtime_session_id"])
        self.store.checkpoint.sessions.append(session_id)
        self.sessions[label] = session
        self.store.save()
        return session

    async def prepare(self, session: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/user/runtime-edge/prepare",
            json={
                "runtime_workspace_id": self.target.workspace_id,
                "runtime_session_id": session["runtime_session_id"],
                "model_entry_id": int(session["session_model_entry_id"]),
                "attachment_file_ids": [],
            },
        )

    async def prepare_probe(
        self,
        session: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        session_id = str(session["runtime_session_id"])
        self.store.record_control(
            "session.prepare",
            label=label,
            session_id=session_id,
        )
        bootstrap = await self.prepare(session)
        endpoint = websocket_url(
            self.runtime_edge_base,
            str(bootstrap["websocket_path"]),
        )
        started = time.monotonic()
        async with websockets.connect(
            endpoint,
            origin=settings.PUBLIC_WEB_BASE.rstrip("/"),
            subprotocols=[
                str(bootstrap["websocket_protocol"]),
                f"runtime-access.{bootstrap['runtime_access']}",
            ],
            proxy=None,
            open_timeout=30,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "schema_version": 1,
                        "type": "session.prepare",
                        "request_id": f"probe-{uuid4().hex}",
                        "session_grant": bootstrap["session_grant"],
                    }
                )
            )
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
                if message.get("type") == "error":
                    return {
                        "status": "error",
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "error": {
                            key: message.get(key)
                            for key in ("code", "phase", "message")
                        },
                    }
                if message.get("type") == "session.ready":
                    return {
                        "status": "ready",
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }

    async def invalidate(
        self,
        session: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        session_id = str(session["runtime_session_id"])
        profile_id = int(session.get("runtime_agent_profile_id") or 0)
        self.store.record_control(
            "session.invalidate",
            label=label,
            session_id=session_id,
        )
        started = time.monotonic()
        try:
            ack = await self.command_client.mutate_session(
                target=self.target.command_target,
                operation="invalidate",
                session_id=session_id,
                profile_id=f"expert-{profile_id}" if profile_id else "default",
            )
            return {
                "status": "completed",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "ack": ack.model_dump(mode="json"),
            }
        except RuntimeCommandError as exc:
            return {
                "status": "error",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error": str(exc),
            }

    def launch_run(
        self,
        session: dict[str, Any],
        prompt: str,
        label: str,
        *,
        accepted: asyncio.Event | None = None,
    ) -> asyncio.Task[RunEvidence]:
        task = asyncio.create_task(
            self.run(session, prompt, label, accepted=accepted)
        )
        self.running_tasks.add(task)
        task.add_done_callback(self.running_tasks.discard)
        return task

    async def run(
        self,
        session: dict[str, Any],
        prompt: str,
        label: str,
        *,
        accepted: asyncio.Event | None = None,
    ) -> RunEvidence:
        evidence = RunEvidence(
            label=label,
            session_id=str(session["runtime_session_id"]),
        )
        bootstrap = await self.prepare(session)
        endpoint = websocket_url(
            self.runtime_edge_base,
            str(bootstrap["websocket_path"]),
        )
        nonce = uuid4().hex
        started = time.monotonic()

        def persist(stage: str) -> None:
            self.store.checkpoint.evidence.setdefault("runs", {})[label] = {
                **asdict(evidence),
                "stage": stage,
            }
            self.store.save()

        try:
            async with websockets.connect(
                endpoint,
                origin=settings.PUBLIC_WEB_BASE.rstrip("/"),
                subprotocols=[
                    str(bootstrap["websocket_protocol"]),
                    f"runtime-access.{bootstrap['runtime_access']}",
                ],
                proxy=None,
                open_timeout=30,
            ) as ws:
                await self._prepare_websocket(ws, bootstrap, evidence, nonce)
                await ws.send(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "type": "run.submit",
                            "client_message_id": f"message-{nonce}",
                            "session_grant": bootstrap["session_grant"],
                            "prompt": prompt,
                            "attachment_file_ids": [],
                        },
                        ensure_ascii=False,
                    )
                )
                await self._collect_run_events(
                    ws,
                    evidence,
                    accepted=accepted,
                    started=started,
                    persist=persist,
                )
        except asyncio.TimeoutError:
            evidence.error = "timeout"
        except Exception as exc:
            evidence.error = f"{type(exc).__name__}: {exc}"
        persist("terminal" if evidence.error is None else "error")
        return evidence

    async def _prepare_websocket(
        self,
        ws: Any,
        bootstrap: dict[str, Any],
        evidence: RunEvidence,
        nonce: str,
    ) -> None:
        self.store.record_control(
            "session.prepare",
            label=evidence.label,
            session_id=evidence.session_id,
        )
        for attempt in range(3):
            await ws.send(
                json.dumps(
                    {
                        "schema_version": 1,
                        "type": "session.prepare",
                        "request_id": f"prepare-{nonce}-{attempt}",
                        "session_grant": bootstrap["session_grant"],
                    }
                )
            )
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
                if message.get("type") == "error":
                    code = str(message.get("code") or "")
                    if code == "RUNTIME_SESSION_PREPARE_FAILED" and attempt < 2:
                        await asyncio.sleep(2)
                        break
                    raise SmokeError(f"session.prepare failed: {code}")
                if message.get("type") == "session.ready":
                    return

    async def _collect_run_events(
        self,
        ws: Any,
        evidence: RunEvidence,
        *,
        accepted: asyncio.Event | None,
        started: float,
        persist: Any,
    ) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            message_type = message.get("type")
            if message_type in {"run.queued", "run.accepted"}:
                evidence.run_id = str(message.get("run_id") or evidence.run_id)
                if evidence.run_id and evidence.run_id not in self.store.checkpoint.run_ids:
                    self.store.checkpoint.run_ids.append(evidence.run_id)
                if accepted is not None:
                    accepted.set()
                persist(str(message_type))
                continue
            if message_type in {
                "run.redirect_pending",
                "run.redirect_accepted",
                "run.redirect_consumed",
                "run.redirect_promoted",
                "run.redirect_canceled",
                "run.steer_submission_uncertain",
            }:
                evidence.events.append(
                    {
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "event": str(message_type),
                        "data": {
                            key: message.get(key)
                            for key in (
                                "redirect_id",
                                "parent_run_id",
                                "promoted_run_id",
                                "status",
                            )
                            if key in message
                        },
                    }
                )
                promoted_run_id = str(message.get("promoted_run_id") or "")
                if message_type == "run.redirect_promoted" and promoted_run_id:
                    evidence.run_id = promoted_run_id
                    if promoted_run_id not in self.store.checkpoint.run_ids:
                        self.store.checkpoint.run_ids.append(promoted_run_id)
                    await ws.send(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "type": "run.resume",
                                "run_id": promoted_run_id,
                                "after_seq": 0,
                            }
                        )
                    )
                    if accepted is not None:
                        accepted.set()
                persist(str(message_type))
                continue
            if message_type == "error":
                evidence.error = f"edge:{message.get('code') or 'error'}"
                return
            if message_type != "run.event":
                continue
            if self._record_run_event(message, evidence, started, persist):
                return

    @staticmethod
    def _record_run_event(
        message: dict[str, Any],
        evidence: RunEvidence,
        started: float,
        persist: Any,
    ) -> bool:
        evidence.run_id = str(message.get("run_id") or evidence.run_id)
        event_type = str(message.get("event_type") or "")
        data = dict(message.get("data") or {})
        if event_type == "chat.reasoning.delta":
            if isinstance(data.get("delta"), str):
                evidence.reasoning = (evidence.reasoning + data["delta"])[-6000:]
            return False
        if event_type == "chat.message.delta":
            return False
        bounded = {
            key: data.get(key)
            for key in (
                "seq",
                "name",
                "status",
                "delta",
                "text",
                "preview",
                "result",
                "run_id",
                "session_id",
            )
            if key in data
        }
        evidence.events.append(
            {
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "event": event_type,
                "data": bounded,
            }
        )
        provider_id = str(data.get("run_id") or "")
        if provider_id and provider_id not in evidence.provider_live_ids:
            evidence.provider_live_ids.append(provider_id)
        if event_type == "chat.tool.event":
            evidence.tools.append(bounded)
        if event_type == "chat.message.completed" and isinstance(data.get("text"), str):
            evidence.final_text = data["text"]
        if event_type == "chat.error":
            evidence.error = json.dumps(data, ensure_ascii=False)[:500]
        persist(event_type)
        return event_type == "chat.done"

    async def cleanup(self, errors: list[str]) -> None:
        if self.running_tasks:
            _, pending = await asyncio.wait(self.running_tasks, timeout=15)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                errors.append(f"cancel_test_runs:{len(pending)}_tasks_canceled")
        for session_id in reversed(self.store.checkpoint.sessions):
            for attempt in range(31):
                try:
                    await self.request(
                        "DELETE",
                        f"/user/runtime-workspace/{self.target.workspace_id}"
                        f"/sessions/{session_id}",
                    )
                    break
                except Exception as exc:
                    if attempt == 30:
                        errors.append(f"session:{session_id}:{type(exc).__name__}")
                    else:
                        await asyncio.sleep(1)

    async def close(self) -> None:
        await self.command_client.close()
