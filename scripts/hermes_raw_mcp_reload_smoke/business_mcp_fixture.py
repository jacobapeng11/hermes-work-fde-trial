"""Stable MCP catalog and Profile Desired fixture for the raw reload smoke."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models import (
    Mcp,
    ProfileMcpBinding,
    RuntimeAgentProfile,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryState,
)
from app.schemas.asset_catalog import CreateMcpAssetCommand, McpVersionMaterial
from app.services.asset_catalog import AssetCatalogService

from expert_profile_resume_smoke.contracts import SmokeError
from expert_profile_resume_smoke.mcp_fixture import McpProfileFixture


class BusinessMcpProfileFixture(McpProfileFixture):
    """Drive MCP visibility through the current Backend asset control plane."""

    async def bind(self, profile: str, connection: str) -> dict[str, Any]:
        expert = self.experts[profile]
        profile_id = int(expert["runtime_agent_profile_id"])
        identity = self.identity(connection)
        asset = await self._create_asset(connection)
        self.store.record_control(
            "profile.capabilities.replace",
            profile=str(expert["hermes_profile_name"]),
            connection=connection,
            mcp_asset_id=asset["asset_id"],
        )
        await self._request(
            "PUT",
            f"/user/experts/{profile_id}/capabilities",
            params={"runtime_workspace_id": self.target.workspace_id},
            json={
                "capabilities": {
                    "mcps": [
                        {
                            "source": "public",
                            "asset_id": asset["asset_id"],
                        }
                    ]
                },
                "change_summary": "Active MCP reload acceptance fixture",
            },
        )
        observed = await self._wait_applied(profile_id, asset)
        self.experts[profile] = observed
        servers = await self.dashboard.list_profile_mcp_servers(
            self.target.endpoint,
            profile_name=str(observed["hermes_profile_name"]),
        )
        names = {str(item.get("name") or "") for item in servers}
        if names != {identity.server_name}:
            raise SmokeError("business MCP Profile projection did not round-trip")
        return {
            "profile": str(observed["hermes_profile_name"]),
            "runtime_agent_profile_id": profile_id,
            "connection": connection,
            "server_name": identity.server_name,
            "mcp_asset_id": asset["asset_id"],
            "mcp_version_id": asset["version_id"],
            "desired_revision": int(observed["desired_revision"]),
            "applied_revision": int(observed["applied_revision"]),
            "registry_status": "ready",
        }

    async def _create_asset(self, connection: str) -> dict[str, Any]:
        identity = self.identity(connection)
        material = McpVersionMaterial(
            transport="streamable_http",
            connection_config={"url": self.marker.url("registry", connection)},
            tools_schema=[
                {"name": identity.marker_tool},
                *(
                    [{"name": identity.hold_tool}]
                    if identity.hold_tool is not None
                    else []
                ),
            ],
        )
        async with AsyncSessionLocal() as db:
            result = await AssetCatalogService(db).create_mcp(
                CreateMcpAssetCommand(
                    org_id=self.target.principal.org_id,
                    owner_user_id=self.target.principal.user_id,
                    slug=identity.server_name,
                    name=f"Active reload smoke {connection.upper()}",
                    description="Temporary deterministic MCP reload acceptance asset",
                    visibility="organization",
                    material=material,
                )
            )
            await db.commit()
        asset = {
            "asset_id": int(result.asset_id),
            "version_id": int(result.version_id),
            "slug": identity.server_name,
        }
        self.store.checkpoint.mcp_assets.append(asset)
        self.store.save()
        return asset

    async def _wait_applied(
        self,
        profile_id: int,
        asset: dict[str, Any],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 180
        last_profile: dict[str, Any] = {}
        last_registry: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_profile = await self._request(
                "GET",
                f"/user/experts/{profile_id}",
                params={"runtime_workspace_id": self.target.workspace_id},
            )
            last_registry = await self._registry_evidence(asset)
            if (
                last_profile.get("status") == "ready"
                and last_profile.get("convergence_status") == "applied"
                and int(last_profile.get("desired_revision") or 0)
                == int(last_profile.get("applied_revision") or -1)
                and last_registry.get("state_status") == "ready"
                and last_registry.get("entry_status") == "ready"
            ):
                return last_profile
            await asyncio.sleep(1)
        raise SmokeError(
            "business MCP did not converge: "
            f"profile={last_profile.get('convergence_status')},"
            f"registry={last_registry.get('state_status')},"
            f"entry={last_registry.get('entry_status')}"
        )

    async def _registry_evidence(self, asset: dict[str, Any]) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            state = await db.scalar(
                select(RuntimeMcpRegistryState).where(
                    RuntimeMcpRegistryState.runtime_workspace_id
                    == self.target.workspace_id
                )
            )
            entry = None
            if state is not None:
                entry = await db.scalar(
                    select(RuntimeMcpRegistryEntry).where(
                        RuntimeMcpRegistryEntry.runtime_mcp_registry_state_id
                        == state.id,
                        RuntimeMcpRegistryEntry.mcp_id == asset["asset_id"],
                        RuntimeMcpRegistryEntry.mcp_version_id
                        == asset["version_id"],
                    )
                )
        return {
            "state_status": str(state.status) if state is not None else None,
            "entry_status": str(entry.status) if entry is not None else None,
        }

    async def cleanup(self, errors: list[str]) -> None:
        await super().cleanup(errors)
        await self._wait_disabled_and_unregistered(errors)
        for asset in reversed(self.store.checkpoint.mcp_assets):
            try:
                async with AsyncSessionLocal() as db:
                    mcp = await db.get(Mcp, int(asset["asset_id"]))
                    if (
                        mcp is None
                        or int(mcp.org_id) != self.target.principal.org_id
                        or str(mcp.slug) != str(asset["slug"])
                    ):
                        raise SmokeError("temporary MCP asset identity mismatch")
                    await AssetCatalogService(db).revoke_mcp(
                        org_id=self.target.principal.org_id,
                        mcp_id=int(mcp.id),
                    )
                    await db.commit()
            except Exception as exc:
                errors.append(f"mcp:{asset['asset_id']}:{type(exc).__name__}")

    async def _wait_disabled_and_unregistered(self, errors: list[str]) -> None:
        deadline = time.monotonic() + 180
        profile_ids = self.store.checkpoint.experts
        asset_ids = [
            int(item["asset_id"]) for item in self.store.checkpoint.mcp_assets
        ]
        while time.monotonic() < deadline:
            async with AsyncSessionLocal() as db:
                enabled_profiles = int(
                    await db.scalar(
                        select(func.count())
                        .select_from(RuntimeAgentProfile)
                        .where(
                            RuntimeAgentProfile.id.in_(profile_ids),
                            RuntimeAgentProfile.status.notin_(
                                ("deleted", "disabled", "revoked")
                            ),
                        )
                    )
                    or 0
                )
                registry_entries = int(
                    await db.scalar(
                        select(func.count())
                        .select_from(RuntimeMcpRegistryEntry)
                        .where(RuntimeMcpRegistryEntry.mcp_id.in_(asset_ids))
                    )
                    or 0
                )
            if enabled_profiles == 0 and registry_entries == 0:
                return
            await asyncio.sleep(1)
        errors.append("mcp_registry_cleanup:Timeout")

    async def verify_cleanup(self, errors: list[str]) -> None:
        await super().verify_cleanup(errors)
        asset_ids = [
            int(item["asset_id"]) for item in self.store.checkpoint.mcp_assets
        ]
        async with AsyncSessionLocal() as db:
            active_assets = int(
                await db.scalar(
                    select(func.count())
                    .select_from(Mcp)
                    .where(Mcp.id.in_(asset_ids), Mcp.status != "revoked")
                )
                or 0
            )
            bindings = int(
                await db.scalar(
                    select(func.count())
                    .select_from(ProfileMcpBinding)
                    .where(ProfileMcpBinding.mcp_id.in_(asset_ids))
                )
                or 0
            )
            registry_entries = int(
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeMcpRegistryEntry)
                    .where(RuntimeMcpRegistryEntry.mcp_id.in_(asset_ids))
                )
                or 0
            )
        self.store.checkpoint.evidence["mcp_asset_cleanup"] = {
            "active_assets": active_assets,
            "profile_bindings": bindings,
            "registry_entries": registry_entries,
        }
        if active_assets or bindings or registry_entries:
            errors.append(
                "mcp_verification:"
                f"assets={active_assets},bindings={bindings},"
                f"registry={registry_entries}"
            )


__all__ = ["BusinessMcpProfileFixture"]
