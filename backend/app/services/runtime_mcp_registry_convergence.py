from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    HermesRuntimeBinding,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryRevision,
    RuntimeMcpRegistryState,
)
from app.schemas.asset_projection import (
    RuntimeMcpRegistryApplyRequest,
    RuntimeMcpRegistryApplyResponse,
    runtime_mcp_registry_servers_are_valid,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


logger = logging.getLogger(__name__)


class RuntimeMcpRegistryClient(Protocol):
    async def apply_runtime_mcp_registry(
        self,
        endpoint: RuntimeEndpoint,
        request: RuntimeMcpRegistryApplyRequest,
    ) -> RuntimeMcpRegistryApplyResponse: ...


@dataclass(frozen=True, slots=True)
class ClaimedRuntimeMcpRegistry:
    job_id: int
    lease_epoch: int
    request: RuntimeMcpRegistryApplyRequest
    endpoint: RuntimeEndpoint


class RuntimeMcpRegistryConvergenceWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        worker_id: str,
        client: RuntimeMcpRegistryClient | None = None,
        lease_seconds: int = 180,
        concurrency: int = 2,
        batch_size: int = 8,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.client = client or HermesDashboardProtocolAdapter()
        self.lease_seconds = lease_seconds
        self.concurrency = concurrency
        self.batch_size = batch_size

    async def reconcile_once(self) -> int:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            ids = list(
                await db.scalars(
                    select(RuntimeMcpRegistryJob.id)
                    .where(
                        RuntimeMcpRegistryJob.available_at <= now,
                        or_(
                            RuntimeMcpRegistryJob.status == "waiting",
                            (
                                (RuntimeMcpRegistryJob.status == "updating")
                                & (
                                    RuntimeMcpRegistryJob.lease_expires_at.is_(None)
                                    | (RuntimeMcpRegistryJob.lease_expires_at <= now)
                                )
                            ),
                        ),
                    )
                    .order_by(RuntimeMcpRegistryJob.available_at, RuntimeMcpRegistryJob.id)
                    .limit(self.batch_size)
                )
            )
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(job_id: int) -> bool:
            async with semaphore:
                return await self._process(job_id)

        outcomes = await asyncio.gather(
            *(process(int(job_id)) for job_id in ids),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning(
                    "runtime_mcp_registry_worker_error",
                    extra={"error_type": type(outcome).__name__},
                )
        return sum(outcome is True for outcome in outcomes)

    async def _process(self, job_id: int) -> bool:
        claimed = await self._claim(job_id)
        if claimed is None:
            return False
        try:
            response = await self.client.apply_runtime_mcp_registry(
                claimed.endpoint,
                claimed.request,
            )
            if response.status != "ready":
                raise RuntimeError(response.error_code or "MCP_REGISTRY_APPLY_FAILED")
            if (
                response.operation_id != claimed.request.operation_id
                or response.applied_revision != claimed.request.desired_revision
                or response.registry_digest != claimed.request.registry_digest
            ):
                raise RuntimeError("MCP_REGISTRY_RECEIPT_MISMATCH")
            await self._complete(claimed, response)
        except Exception as exc:
            await self._fail(claimed, str(exc)[:128])
            return True
        return True

    async def _claim(self, job_id: int) -> ClaimedRuntimeMcpRegistry | None:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            job = await db.scalar(
                select(RuntimeMcpRegistryJob)
                .where(RuntimeMcpRegistryJob.id == job_id)
                .with_for_update()
            )
            if (
                job is None
                or job.available_at > now
                or job.status not in {"waiting", "updating"}
                or (
                    job.status == "updating"
                    and job.lease_expires_at is not None
                    and job.lease_expires_at > now
                )
            ):
                return None
            state = await db.scalar(
                select(RuntimeMcpRegistryState)
                .where(RuntimeMcpRegistryState.id == job.runtime_mcp_registry_state_id)
                .with_for_update()
            )
            if state is None:
                job.status = "failed"
                job.last_error_code = "MCP_REGISTRY_STATE_NOT_FOUND"
                await db.commit()
                return None
            target_revision = int(state.desired_revision or 0)
            if target_revision <= 0:
                job.status = "failed"
                job.last_error_code = "MCP_REGISTRY_REVISION_INVALID"
                await db.commit()
                return None
            if (
                int(state.applied_revision or 0) == target_revision
                and state.status == "ready"
            ):
                job.status = "completed"
                job.stage = "ready"
                await db.commit()
                return None
            revision = await db.scalar(
                select(RuntimeMcpRegistryRevision).where(
                    RuntimeMcpRegistryRevision.runtime_mcp_registry_state_id
                    == state.id,
                    RuntimeMcpRegistryRevision.revision == target_revision,
                )
            )
            binding = await db.scalar(
                select(HermesRuntimeBinding).where(
                    HermesRuntimeBinding.id == state.runtime_binding_id,
                    HermesRuntimeBinding.status == "ready",
                )
            )
            if (
                revision is None
                or binding is None
                or not binding.endpoint_base_url
                or not binding.endpoint_host_header
            ):
                await self._defer(job, state, "MCP_RUNTIME_NOT_READY", now)
                await db.commit()
                return None
            endpoint = RuntimeEndpoint(
                runtime_workspace_id=int(state.runtime_workspace_id),
                runtime_id=str(state.runtime_id),
                endpoint_base_url=str(binding.endpoint_base_url),
                endpoint_host_header=str(binding.endpoint_host_header),
                api_key=RuntimeApiKeyCodec().decrypt(str(binding.api_key_encrypted)),
                timeout_seconds=120,
            )
            raw_servers = dict(revision.normalized_servers or {}).get("servers") or []
            if not runtime_mcp_registry_servers_are_valid(raw_servers):
                job.status = "failed"
                job.stage = "resolve_registry"
                job.available_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error_code = "MCP_REGISTRY_CONFIG_INVALID"
                job.last_error_message = "MCP_REGISTRY_CONFIG_INVALID"
                state.status = "failed"
                state.last_error_code = "MCP_REGISTRY_CONFIG_INVALID"
                state.last_error_message = "MCP_REGISTRY_CONFIG_INVALID"
                await db.commit()
                return None
            request = RuntimeMcpRegistryApplyRequest(
                operation_id=(
                    f"registry-{int(state.id)}-revision-{target_revision}-"
                    f"{str(revision.registry_digest).removeprefix('sha256:')[:16]}"
                ),
                runtime_id=str(state.runtime_id),
                binding_id=int(binding.id),
                runtime_workspace_id=int(state.runtime_workspace_id),
                desired_revision=target_revision,
                expected_applied_revision=int(state.applied_revision or 0),
                registry_digest=str(revision.registry_digest),
                servers=raw_servers,
            )
            job.target_revision = target_revision
            job.status = "updating"
            job.stage = "reload_mcp"
            job.lease_owner = self.worker_id
            job.lease_epoch = int(job.lease_epoch or 0) + 1
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.last_error_code = None
            job.last_error_message = None
            state.status = "installing"
            await db.commit()
            return ClaimedRuntimeMcpRegistry(
                job_id=int(job.id),
                lease_epoch=int(job.lease_epoch),
                request=request,
                endpoint=endpoint,
            )

    async def _complete(
        self,
        claimed: ClaimedRuntimeMcpRegistry,
        response: RuntimeMcpRegistryApplyResponse,
    ) -> None:
        async with self.session_factory() as db:
            job, state = await self._locked(db, claimed)
            if job is None or state is None:
                return
            if int(state.desired_revision) != claimed.request.desired_revision:
                job.status = "waiting"
                job.stage = "resolve_registry"
                job.available_at = datetime.utcnow()
                job.lease_owner = None
                job.lease_expires_at = None
                await db.commit()
                return
            evidence = {item.server_slug: item for item in response.servers}
            entries = list(
                await db.scalars(
                    select(RuntimeMcpRegistryEntry).where(
                        RuntimeMcpRegistryEntry.runtime_mcp_registry_state_id
                        == state.id
                    )
                )
            )
            now = datetime.utcnow()
            for entry in entries:
                item = evidence.get(str(entry.server_slug))
                if item is None or item.status != "ready":
                    raise RuntimeError("MCP_REGISTRY_EVIDENCE_MISSING")
                entry.status = "ready"
                entry.tools_schema_digest = item.tools_schema_digest
                entry.tools_schema = [{"name": name} for name in item.tool_names]
                entry.initialized_at = now
                entry.tools_listed_at = now
                entry.last_error_code = None
                entry.last_error_message = None
            state.applied_revision = claimed.request.desired_revision
            state.applied_digest = claimed.request.registry_digest
            state.status = "ready"
            state.last_error_code = None
            state.last_error_message = None
            state.last_reload_at = now
            state.last_reload_affected_run_ids = response.affected_active_run_ids
            job.status = "completed"
            job.stage = "ready"
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            job.last_error_message = None
            await db.commit()

    async def _fail(
        self,
        claimed: ClaimedRuntimeMcpRegistry,
        code: str,
    ) -> None:
        async with self.session_factory() as db:
            job, state = await self._locked(db, claimed)
            if job is None or state is None:
                return
            now = datetime.utcnow()
            job.status = "waiting"
            job.stage = "reload_mcp"
            job.available_at = now + timedelta(
                seconds=min(60, 2 ** min(int(job.attempt_count or 1), 5))
            )
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = code
            job.last_error_message = code
            state.status = "failed"
            state.last_error_code = code
            state.last_error_message = code
            await db.commit()

    async def _locked(self, db: AsyncSession, claimed: ClaimedRuntimeMcpRegistry):
        job = await db.scalar(
            select(RuntimeMcpRegistryJob)
            .where(
                RuntimeMcpRegistryJob.id == claimed.job_id,
                RuntimeMcpRegistryJob.lease_epoch == claimed.lease_epoch,
                RuntimeMcpRegistryJob.lease_owner == self.worker_id,
            )
            .with_for_update()
        )
        if job is None:
            return None, None
        state = await db.scalar(
            select(RuntimeMcpRegistryState)
            .where(RuntimeMcpRegistryState.id == job.runtime_mcp_registry_state_id)
            .with_for_update()
        )
        return job, state

    @staticmethod
    async def _defer(job, state, code: str, now: datetime) -> None:
        job.status = "waiting"
        job.stage = "resolve_runtime"
        job.available_at = now + timedelta(seconds=2)
        job.last_error_code = code
        job.last_error_message = code
        state.status = "installing"


__all__ = ["RuntimeMcpRegistryConvergenceWorker"]
