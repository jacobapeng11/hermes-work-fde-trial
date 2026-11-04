from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    HermesRuntimeBinding,
    Mcp,
    McpVersion,
    ProfileMcpBinding,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryRevision,
    RuntimeMcpRegistryState,
)
from app.services.mcp_identity import runtime_mcp_server_key
from app.schemas.mcp_connection import validate_mcp_connection_target


class RuntimeMcpRegistryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeMcpRegistryDesiredResult:
    state_id: int | None
    desired_revision: int
    created_revision: bool


class RuntimeMcpRegistryDesiredService:
    """Build one complete Runtime Registry Desired from all Profile bindings."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def reconcile_workspace(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        source_type: str,
        source_id: str | None,
    ) -> RuntimeMcpRegistryDesiredResult:
        bindings = list(
            (
                await self.db.execute(
                    select(ProfileMcpBinding, Mcp, McpVersion)
                    .join(Mcp, Mcp.id == ProfileMcpBinding.mcp_id)
                    .join(McpVersion, McpVersion.id == Mcp.current_version_id)
                    .where(
                        ProfileMcpBinding.org_id == org_id,
                        ProfileMcpBinding.runtime_workspace_id
                        == runtime_workspace_id,
                        ProfileMcpBinding.status == "enabled",
                        Mcp.org_id == org_id,
                        Mcp.status.notin_(("deleted", "revoked")),
                        McpVersion.status != "deleted",
                    )
                )
            ).all()
        )
        servers_by_mcp: dict[int, dict[str, object]] = {}
        key_owners: dict[str, int] = {}
        for _binding, mcp, version in bindings:
            mcp_id = int(mcp.id)
            server_key = (
                runtime_mcp_server_key(mcp_id)
                if settings.mcp_runtime_identity_enabled(user_id)
                else str(mcp.slug)
            )
            owner = key_owners.setdefault(server_key, mcp_id)
            if owner != mcp_id:
                raise RuntimeMcpRegistryError("MCP_REGISTRY_KEY_COLLISION")
            try:
                validate_mcp_connection_target(
                    str(version.transport),
                    version.connection_config,
                )
            except ValueError as exc:
                raise RuntimeMcpRegistryError("MCP_REGISTRY_CONFIG_INVALID") from exc
            servers_by_mcp[mcp_id] = {
                "mcp_id": mcp_id,
                "mcp_version_id": int(version.id),
                "server_slug": server_key,
                "transport": str(version.transport),
                "connection_config": dict(version.connection_config or {}),
                "credential_refs": dict(version.secret_refs or {}),
                "config_digest": str(version.config_digest),
            }
        servers = sorted(
            servers_by_mcp.values(),
            key=lambda item: (str(item["server_slug"]), int(item["mcp_id"])),
        )
        state = await self.db.scalar(
            select(RuntimeMcpRegistryState)
            .where(
                RuntimeMcpRegistryState.runtime_workspace_id
                == runtime_workspace_id,
                RuntimeMcpRegistryState.org_id == org_id,
                RuntimeMcpRegistryState.user_id == user_id,
            )
            .with_for_update()
        )
        if not servers and state is None:
            return RuntimeMcpRegistryDesiredResult(None, 0, False)
        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id
                == runtime_workspace_id,
                HermesRuntimeBinding.org_id == org_id,
                HermesRuntimeBinding.user_id == user_id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        if binding is None:
            raise RuntimeMcpRegistryError("MCP_RUNTIME_NOT_READY")
        binding_changed = state is not None and (
            int(state.runtime_binding_id) != int(binding.id)
            or str(state.runtime_id) != str(binding.runtime_id)
        )
        if state is None:
            state = RuntimeMcpRegistryState(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_binding_id=int(binding.id),
                runtime_id=str(binding.runtime_id),
                desired_revision=0,
                applied_revision=0,
                status="ready",
            )
            self.db.add(state)
            await self.db.flush()
        registry_digest = self._digest(servers)
        if (
            str(state.desired_digest or "") == registry_digest
            and int(state.desired_revision or 0) > 0
            and not binding_changed
        ):
            if int(state.applied_revision or 0) < int(state.desired_revision):
                await self._ensure_job(state)
            return RuntimeMcpRegistryDesiredResult(
                int(state.id),
                int(state.desired_revision),
                False,
            )
        next_revision = int(state.desired_revision or 0) + 1
        self.db.add(
            RuntimeMcpRegistryRevision(
                runtime_mcp_registry_state_id=int(state.id),
                revision=next_revision,
                registry_digest=registry_digest,
                normalized_servers={"servers": servers},
                source_type=source_type,
                source_id=source_id,
            )
        )
        await self.db.execute(
            delete(RuntimeMcpRegistryEntry).where(
                RuntimeMcpRegistryEntry.runtime_mcp_registry_state_id == state.id
            )
        )
        for server in servers:
            self.db.add(
                RuntimeMcpRegistryEntry(
                    runtime_mcp_registry_state_id=int(state.id),
                    mcp_id=int(server["mcp_id"]),
                    mcp_version_id=int(server["mcp_version_id"]),
                    server_slug=str(server["server_slug"]),
                    config_digest=str(server["config_digest"]),
                    status="installing",
                )
            )
        state.runtime_binding_id = int(binding.id)
        state.runtime_id = str(binding.runtime_id)
        state.desired_revision = next_revision
        state.desired_digest = registry_digest
        state.status = "installing"
        state.last_error_code = None
        state.last_error_message = None
        await self._ensure_job(state)
        await self.db.flush()
        return RuntimeMcpRegistryDesiredResult(int(state.id), next_revision, True)

    async def _ensure_job(
        self,
        state: RuntimeMcpRegistryState,
    ) -> RuntimeMcpRegistryJob:
        job = await self.db.scalar(
            select(RuntimeMcpRegistryJob)
            .where(RuntimeMcpRegistryJob.runtime_mcp_registry_state_id == state.id)
            .with_for_update()
        )
        now = datetime.utcnow()
        if job is None:
            job = RuntimeMcpRegistryJob(
                runtime_mcp_registry_state_id=int(state.id),
                target_revision=int(state.desired_revision),
                status="waiting",
                stage="resolve_registry",
                available_at=now,
            )
            self.db.add(job)
            return job
        job.target_revision = int(state.desired_revision)
        job.status = "waiting"
        job.stage = "resolve_registry"
        job.available_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error_code = None
        job.last_error_message = None
        return job

    @staticmethod
    def _digest(value: object) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


__all__ = [
    "RuntimeMcpRegistryDesiredResult",
    "RuntimeMcpRegistryDesiredService",
    "RuntimeMcpRegistryError",
]
