from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ApplicationAgentRuntimeBinding, HermesRuntimeBinding
from app.schemas.runtime_internal import (
    CancelCommandPayload,
    CancelScheduledCandidateCommandPayload,
    CancelActiveRunsCommandPayload,
    ApplyConfigCommandPayload,
    ConfigBundleReference,
    RuntimeCommandAck,
    RuntimeCommandEnvelope,
    RuntimeCommandPayload,
    SessionMutationCommandPayload,
    canonical_payload_digest,
)
from app.services.runtime_config_bundle import config_bundle_signature
from app.services.runtime_maintenance import RuntimeMaintenanceService
from app.services.runtime_workspace import RuntimeApiKeyCodec


class RuntimeCommandError(RuntimeError):
    pass


def digest_managed_runtime_config(home: Path) -> str:
    roots = (
        home / "config.yaml",
        home / "SOUL.md",
        home / "platform",
        home / "skills",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_symlink():
            raise RuntimeCommandError("Runtime config contains a forbidden symlink")
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise RuntimeCommandError(
                        "Runtime config contains a forbidden symlink"
                    )
                if path.is_file():
                    files.append(path)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(home).as_posix()):
        relative = path.relative_to(home).as_posix().encode("utf-8")
        content = _runtime_config_digest_content(path)
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _runtime_config_digest_content(path: Path) -> bytes:
    if path.name != "config.yaml":
        return path.read_bytes()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeCommandError("Runtime config YAML is invalid") from exc
    if isinstance(document, dict):
        providers = document.get("providers")
        if isinstance(providers, dict):
            document["providers"] = {
                key: value
                for key, value in providers.items()
                if not str(key).startswith("runtime-")
            }
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class RuntimeCommandTarget:
    runtime_id: str
    binding_id: str
    endpoint_host_header: str
    api_server_key: str
    admission_generation: int


async def load_runtime_command_admission_generation(
    db: AsyncSession,
    *,
    require_open: bool = True,
) -> int:
    """Read command generation; new work is gated, revocation remains available."""

    control = RuntimeMaintenanceService(db)
    snapshot = (
        await control.require_admission_open()
        if require_open
        else await control.snapshot()
    )
    generation = int(snapshot.admission_generation)
    if generation <= 0:
        raise RuntimeCommandError("Runtime admission generation is unavailable")
    return generation


async def load_personal_runtime_command_target(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
    runtime_workspace_id: int,
) -> RuntimeCommandTarget:
    """Load the single ready personal Runtime target from control-plane state."""

    binding = await db.scalar(
        select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.org_id == org_id,
            HermesRuntimeBinding.user_id == user_id,
            HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
            HermesRuntimeBinding.status == "ready",
        )
    )
    if (
        binding is None
        or not binding.endpoint_host_header
        or not binding.api_key_encrypted
    ):
        raise RuntimeCommandError("Runtime command target is unavailable")
    return RuntimeCommandTarget(
        runtime_id=str(binding.runtime_id),
        binding_id=str(binding.id),
        endpoint_host_header=str(binding.endpoint_host_header),
        api_server_key=RuntimeApiKeyCodec().decrypt(
            str(binding.api_key_encrypted)
        ),
        admission_generation=await load_runtime_command_admission_generation(db),
    )


async def load_runtime_reload_command_target(
    db: AsyncSession,
    *,
    binding_type: str,
    binding_id: int,
) -> RuntimeCommandTarget:
    """Load an exact Binding target for maintenance-time Run cancellation.

    Admission may already be frozen, therefore this deliberately reads the
    current generation with ``require_open=False``.  It never persists any
    restart-specific state.
    """

    model = (
        HermesRuntimeBinding
        if binding_type == "personal_runtime"
        else ApplicationAgentRuntimeBinding
        if binding_type == "application_runtime"
        else None
    )
    if model is None:
        raise RuntimeCommandError("unsupported Runtime Binding type")
    row = await db.scalar(select(model).where(model.id == int(binding_id)))
    if row is None or not row.endpoint_host_header or not row.api_key_encrypted:
        raise RuntimeCommandError("Runtime command target is unavailable")
    return RuntimeCommandTarget(
        runtime_id=str(row.runtime_id),
        binding_id=str(row.id),
        endpoint_host_header=str(row.endpoint_host_header),
        api_server_key=RuntimeApiKeyCodec().decrypt(str(row.api_key_encrypted)),
        admission_generation=await load_runtime_command_admission_generation(
            db,
            require_open=False,
        ),
    )


def deterministic_command_id(*parts: object) -> str:
    digest = hashlib.sha256(
        "\n".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"cmd_{digest[:48]}"


def build_runtime_command(
    *,
    target: RuntimeCommandTarget,
    command_id: str,
    payload: RuntimeCommandPayload,
    issued_at: datetime | None = None,
    deadline_seconds: int | None = None,
) -> RuntimeCommandEnvelope:
    now = issued_at or datetime.now(timezone.utc)
    deadline = (
        now + timedelta(seconds=deadline_seconds)
        if deadline_seconds is not None
        else None
    )
    return RuntimeCommandEnvelope(
        command_id=command_id,
        runtime_id=target.runtime_id,
        binding_id=target.binding_id,
        admission_generation=target.admission_generation,
        payload_digest=canonical_payload_digest(
            payload.model_dump(mode="json", exclude_none=True)
        ),
        issued_at=now,
        deadline_at=deadline,
        payload=payload,
    )


class RuntimeCommandClient:
    def __init__(
        self,
        *,
        ingress_base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.ingress_base_url = (
            ingress_base_url or settings.HERMES_RUNTIME_INGRESS_BASE_URL
        ).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self.max_attempts = max(1, max_attempts)

    async def send(
        self,
        *,
        target: RuntimeCommandTarget,
        envelope: RuntimeCommandEnvelope,
    ) -> RuntimeCommandAck:
        if (
            envelope.runtime_id != target.runtime_id
            or envelope.binding_id != target.binding_id
        ):
            raise RuntimeCommandError("Runtime command target does not match envelope")
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self._shared_client().post(
                    f"{self.ingress_base_url}/internal/v1/commands",
                    content=envelope.model_dump_json(exclude_none=True),
                    headers=self._headers(target),
                    timeout=self._command_timeout(envelope),
                )
                if response.status_code >= 500 and attempt < self.max_attempts:
                    await asyncio.sleep(0.05 * attempt)
                    continue
                if response.status_code >= 400:
                    try:
                        ack = RuntimeCommandAck.model_validate(response.json())
                    except Exception:
                        ack = None
                    if (
                        ack is not None
                        and ack.command_id == envelope.command_id
                        and ack.state == "rejected"
                    ):
                        return ack
                    detail = response.text.strip().replace("\n", " ")[:240]
                    raise RuntimeCommandError(
                        f"Runtime command rejected with HTTP {response.status_code}"
                        + (f": {detail}" if detail else "")
                    )
                ack = RuntimeCommandAck.model_validate(response.json())
                if ack.command_id != envelope.command_id:
                    raise RuntimeCommandError("Runtime command ACK does not match")
                return ack
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    break
                await asyncio.sleep(0.05 * attempt)
        raise RuntimeCommandError("Runtime command delivery failed") from last_error

    async def cancel_scheduled_candidate(
        self,
        *,
        target: RuntimeCommandTarget,
        session_id: str,
        scheduled_task_id: int,
    ) -> str:
        payload = CancelScheduledCandidateCommandPayload(
            session_id=session_id,
            scheduled_task_id=scheduled_task_id,
        )
        envelope = build_runtime_command(
            target=target,
            command_id=f"cmd_{uuid4().hex}",
            payload=payload,
            deadline_seconds=30,
        )
        ack = await self.send(target=target, envelope=envelope)
        result = dict(ack.result or {})
        status = str(result.get("status") or "")
        if ack.state != "completed" or status not in {
            "canceled",
            "already_running",
            "not_found",
        }:
            raise RuntimeCommandError(
                "scheduled candidate cancellation was not confirmed"
            )
        return status

    async def cancel_active_runs(
        self,
        *,
        target: RuntimeCommandTarget,
    ) -> dict[str, Any]:
        """Interrupt all Runs currently held by a Runtime process."""

        payload = CancelActiveRunsCommandPayload()
        envelope = build_runtime_command(
            target=target,
            command_id=f"cmd_{uuid4().hex}",
            payload=payload,
            deadline_seconds=60,
        )
        ack = await self.send(target=target, envelope=envelope)
        result = dict(ack.result or {})
        if ack.state != "completed" or not isinstance(result.get("runs"), list):
            raise RuntimeCommandError(
                ack.error_code or "Runtime active Run cancellation was not completed"
            )
        return result

    async def cancel_run(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
    ) -> RuntimeCommandAck:
        """Cancel one Run for compatibility with pre-batch-cancel Supervisors."""

        envelope = build_runtime_command(
            target=target,
            command_id=f"cmd_{uuid4().hex}",
            payload=CancelCommandPayload(run_id=run_id),
            deadline_seconds=30,
        )
        return await self.send(target=target, envelope=envelope)

    async def apply_config(
        self,
        *,
        target: RuntimeCommandTarget,
        config_revision: str,
        config_digest: str,
        download_url: str,
    ) -> RuntimeCommandAck:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=5)
        unsigned_bundle = ConfigBundleReference(
            runtime_id=target.runtime_id,
            binding_id=target.binding_id,
            config_revision=config_revision,
            config_digest=config_digest,
            download_url=download_url,
            expires_at=expires_at,
            signature="0" * 64,
        )
        bundle = unsigned_bundle.model_copy(
            update={
                "signature": config_bundle_signature(
                    secret=target.api_server_key,
                    runtime_id=target.runtime_id,
                    binding_id=target.binding_id,
                    config_revision=config_revision,
                    config_digest=config_digest,
                    download_url=download_url,
                    minimum_protocol_version=(
                        unsigned_bundle.minimum_protocol_version
                    ),
                    expires_at=expires_at,
                )
            }
        )
        payload = ApplyConfigCommandPayload(bundle=bundle)
        envelope = build_runtime_command(
            target=target,
            # A rejected apply must be retryable after its underlying cause is
            # repaired.  The signed bundle contains a fresh expiry timestamp,
            # so reusing a deterministic command id would bind that id to a
            # different payload in Supervisor.  Transport retries still reuse
            # this exact envelope inside ``send``.
            command_id=f"cmd_{uuid4().hex}",
            payload=payload,
            issued_at=now,
            deadline_seconds=300,
        )
        ack = await self.send(target=target, envelope=envelope)
        result = ack.result or {}
        if (
            ack.state != "completed"
            or result.get("config_revision") != config_revision
            or result.get("config_digest") != config_digest
        ):
            raise RuntimeCommandError(
                ack.error_code or "Runtime config apply was not completed"
            )
        return ack

    async def mutate_session(
        self,
        *,
        target: RuntimeCommandTarget,
        operation: str,
        session_id: str,
        profile_id: str,
        title: str | None = None,
    ) -> RuntimeCommandAck:
        payload = SessionMutationCommandPayload(
            operation=operation,
            session_id=session_id,
            profile_id=profile_id,
            title=title,
        )
        envelope = build_runtime_command(
            target=target,
            command_id=(
                # Invalidation is a repeatable state transition: the same
                # Session may be rebuilt after every Profile asset revision.
                # A deterministic id would replay the first durable ACK and
                # silently skip every later invalidation. Retries of this one
                # delivery remain idempotent because send() reuses `envelope`.
                f"cmd_{uuid4().hex}"
                if operation == "invalidate"
                else deterministic_command_id(
                    "session_mutation",
                    target.runtime_id,
                    operation,
                    session_id,
                    profile_id,
                    title or "",
                )
            ),
            payload=payload,
            deadline_seconds=120,
        )
        ack = await self.send(target=target, envelope=envelope)
        if ack.state != "completed":
            raise RuntimeCommandError(
                ack.error_code or "Runtime session mutation was not completed"
            )
        return ack

    async def get_run(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
    ) -> dict[str, Any] | None:
        try:
            response = await self._shared_client().get(
                f"{self.ingress_base_url}/internal/v1/runs/{run_id}",
                headers=self._headers(target),
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RuntimeCommandError("Runtime Run query failed") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeCommandError(
                f"Runtime Run query rejected with HTTP {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeCommandError("Runtime Run query returned an invalid payload")
        return payload

    async def get_run_metadata(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Read status/Session metadata without transferring event bodies."""
        try:
            response = await self._shared_client().get(
                f"{self.ingress_base_url}/internal/v1/runs/{run_id}/metadata",
                headers=self._headers(target),
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RuntimeCommandError("Runtime Run metadata query failed") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeCommandError(
                f"Runtime Run metadata query rejected with HTTP {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeCommandError("Runtime Run metadata returned an invalid payload")
        return payload

    async def wait_run(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
        event_sink: Any | None = None,
        timeout_seconds: float = 600,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        seen_seqs: set[int] = set()
        saw_done = False
        dispatch_lock = asyncio.Lock()

        async def dispatch(raw: dict[str, Any]) -> None:
            nonlocal saw_done
            seq = int(raw.get("seq") or 0)
            async with dispatch_lock:
                if seq > 0 and seq in seen_seqs:
                    return
                if seq > 0:
                    seen_seqs.add(seq)
                event_type = str(raw.get("event_type") or "")
                data = dict(raw.get("data") or {})
                data.setdefault("runtime_run_id", run_id)
                if seq > 0:
                    data.setdefault("seq", seq)
                if event_sink is not None:
                    await event_sink(event_type, data)
                saw_done = saw_done or event_type == "chat.done"

        async def consume_realtime() -> None:
            after_seq = 0
            while time.monotonic() < deadline and not saw_done:
                try:
                    async for raw in self.stream_run_events(
                        target=target,
                        run_id=run_id,
                        after_seq=after_seq,
                        timeout_seconds=max(
                            1.0,
                            deadline - time.monotonic(),
                        ),
                    ):
                        seq = int(raw.get("seq") or 0)
                        await dispatch(raw)
                        after_seq = max(after_seq, seq)
                    if saw_done:
                        return
                except RuntimeCommandError:
                    pass
                await asyncio.sleep(max(0.01, poll_interval_seconds))

        stream_task = (
            asyncio.create_task(
                consume_realtime(),
                name=f"runtime-events:{run_id}",
            )
            if event_sink is not None
            else None
        )
        try:
            while True:
                snapshot = await self.get_run(target=target, run_id=run_id)
                if snapshot is not None:
                    state = str(snapshot.get("status") or "").upper()
                    if state in {
                        "COMPLETED",
                        "FAILED",
                        "CANCELED",
                        "FAILED_SUBMISSION_UNCERTAIN",
                        "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
                        "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
                        "RUN_INTERRUPTED_BY_ADMIN_RESTART",
                    }:
                        if stream_task is not None:
                            if not stream_task.done():
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(stream_task),
                                        timeout=2,
                                    )
                                except (
                                    asyncio.TimeoutError,
                                    TimeoutError,
                                    RuntimeCommandError,
                                ):
                                    pass
                            if stream_task.done() and not stream_task.cancelled():
                                stream_error = stream_task.exception()
                                if stream_error is not None:
                                    raise stream_error
                        events = snapshot.get("events")
                        for raw in events if isinstance(events, list) else []:
                            if isinstance(raw, dict):
                                await dispatch(raw)
                        if event_sink is not None and not saw_done:
                            await event_sink(
                                "chat.done",
                                {
                                    "runtime_run_id": run_id,
                                    "status": (
                                        "completed"
                                        if state == "COMPLETED"
                                        else "canceled"
                                        if state == "CANCELED"
                                        else "failed"
                                    ),
                                },
                            )
                        return snapshot
                if time.monotonic() >= deadline:
                    raise RuntimeCommandError(
                        "Runtime Run did not finish before deadline"
                    )
                await asyncio.sleep(poll_interval_seconds)
        finally:
            if stream_task is not None and not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)

    async def stream_run_events(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
        after_seq: int = 0,
        timeout_seconds: float = 600,
    ):
        encoded_run_id = quote(run_id, safe="")
        headers = {
            **self._headers(target),
            "Accept": "application/x-ndjson",
        }
        try:
            async with self._shared_client().stream(
                "GET",
                (
                    f"{self.ingress_base_url}/internal/v1/runs/"
                    f"{encoded_run_id}/events"
                ),
                params={"after_seq": max(0, int(after_seq))},
                headers=headers,
                timeout=httpx.Timeout(
                    connect=5,
                    pool=1,
                    write=10,
                    read=max(1.0, timeout_seconds),
                ),
            ) as response:
                if response.status_code >= 400:
                    raise RuntimeCommandError(
                        "Runtime event stream rejected with HTTP "
                        f"{response.status_code}"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeCommandError(
                            "Runtime event stream returned invalid JSON"
                        ) from exc
                    if (
                        not isinstance(payload, dict)
                        or payload.get("type") != "run.event"
                        or payload.get("run_id") != run_id
                    ):
                        raise RuntimeCommandError(
                            "Runtime event stream returned an invalid event"
                        )
                    yield payload
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RuntimeCommandError("Runtime event stream failed") from exc

    async def close(self) -> None:
        if (
            self._owns_client
            and self._client is not None
            and not self._client.is_closed
        ):
            await self._client.aclose()

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(connect=5, pool=1, write=10, read=30),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=32,
                    keepalive_expiry=600,
                ),
            )
        return self._client

    @staticmethod
    def _command_timeout(envelope: RuntimeCommandEnvelope) -> httpx.Timeout:
        read_seconds = 30.0
        if envelope.deadline_at is not None:
            remaining = (
                envelope.deadline_at - datetime.now(timezone.utc)
            ).total_seconds()
            read_seconds = max(read_seconds, remaining + 5)
        return httpx.Timeout(connect=5, pool=1, write=10, read=read_seconds)

    @staticmethod
    def _headers(target: RuntimeCommandTarget) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {target.api_server_key}",
            "Host": target.endpoint_host_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
