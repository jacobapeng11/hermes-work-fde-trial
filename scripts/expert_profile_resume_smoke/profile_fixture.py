"""Create, project, verify, and delete temporary expert Profile fixtures."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.models import RuntimeAgentProfile, RuntimeRun, RuntimeSession

from .contracts import CheckpointStore, SmokeError, Target
from .marker_server import MarkerMcp


def skill_files(*, name: str, marker: str, instructions: str) -> dict[str, bytes]:
    body = f"""---
name: {name}
description: Deterministic Profile resume smoke marker {marker}.
version: 1.0.0
---

# Profile resume smoke

The exact Skill marker is `{marker}`.
{instructions}
Never guess the marker and never use terminal or network tools.
"""
    return {f"{name}/SKILL.md": body.encode("utf-8")}


class ProfileFixture:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        target: Target,
        store: CheckpointStore,
        marker: MarkerMcp,
    ) -> None:
        self.client = client
        self.target = target
        self.store = store
        self.marker = marker
        self.dashboard = HermesDashboardProtocolAdapter()
        self.profiles = HermesProfileAdapter(self.dashboard)
        self.experts: dict[str, dict[str, Any]] = {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise SmokeError(
                f"{method} {path} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json() if response.content else None

    async def create_expert(self, group: str) -> dict[str, Any]:
        created = await self._request(
            "POST",
            "/user/experts",
            json={
                "runtime_workspace_id": self.target.workspace_id,
                "name": f"resume-smoke-{group}-{self.store.checkpoint.suffix}",
                "description": "Temporary Profile resume acceptance expert",
                "soul_content": (
                    f"You are the deterministic {group} Profile resume smoke expert. "
                    "Use only explicitly requested smoke tools and report exact results."
                ),
                "capabilities": {"skills": [], "mcps": []},
                "share_scope": {},
            },
        )
        profile_id = int(created["runtime_agent_profile_id"])
        self.store.checkpoint.experts.append(profile_id)
        self.store.save()
        deadline = time.monotonic() + 120
        # Profile ``status`` is the lifecycle field (``active`` for a usable
        # self-created expert).  Projection convergence is exposed separately
        # as ``effective_status``/``convergence_status``; waiting on the old
        # status=ready contract would time out forever after the B12 split.
        while not (
            str(created.get("effective_status")) in {"applied", "ready"}
            and str(created.get("convergence_status")) == "applied"
        ):
            if time.monotonic() >= deadline:
                raise SmokeError(f"expert {group} did not become ready")
            await asyncio.sleep(1)
            created = await self._request(
                "GET",
                f"/user/experts/{profile_id}",
                params={"runtime_workspace_id": self.target.workspace_id},
            )
        self.experts[group] = created
        return created

    async def project_alpha(
        self,
        *,
        skill_version: str,
        mcp_version: str,
    ) -> dict[str, Any]:
        expert = self.experts["alpha"]
        profile_id = int(expert["runtime_agent_profile_id"])
        profile_name = str(expert["hermes_profile_name"])
        suffix = self.store.checkpoint.suffix
        self.store.record_control(
            "profile.skills.replace",
            profile=profile_name,
            version=skill_version,
        )
        await self.profiles.replace_profile_skills(
            self.target.endpoint,
            name=profile_name,
            runtime_agent_profile_id=profile_id,
            files=skill_files(
                name=f"resume-alpha-{suffix}",
                marker=f"ALPHA_SKILL_{skill_version.upper()}_{suffix}",
                instructions=(
                    "When requested, call the exact MCP tool named by the prompt "
                    "after reading this Skill."
                ),
            ),
        )
        self.store.record_control(
            "profile.mcp.replace",
            profile=profile_name,
            version=mcp_version,
        )
        await self.profiles.replace_mcp_servers(
            self.target.endpoint,
            name=profile_name,
            servers={
                f"resume-alpha-control-{suffix}": {
                    "transport": "streamable_http",
                    "enabled": True,
                    "url": self.marker.url("alpha", "control"),
                },
                f"resume-alpha-{mcp_version}-{suffix}": {
                    "transport": "streamable_http",
                    "enabled": True,
                    "url": self.marker.url("alpha", mcp_version),
                },
            },
        )
        return await self.verify_projection("alpha", skill_version, mcp_version)

    async def project_beta(self) -> dict[str, Any]:
        expert = self.experts["beta"]
        profile_id = int(expert["runtime_agent_profile_id"])
        profile_name = str(expert["hermes_profile_name"])
        suffix = self.store.checkpoint.suffix
        self.store.record_control(
            "profile.skills.replace",
            profile=profile_name,
            version="static",
        )
        await self.profiles.replace_profile_skills(
            self.target.endpoint,
            name=profile_name,
            runtime_agent_profile_id=profile_id,
            files=skill_files(
                name=f"resume-beta-{suffix}",
                marker=f"BETA_SKILL_STATIC_{suffix}",
                instructions="Call the beta static marker tool when requested.",
            ),
        )
        self.store.record_control(
            "profile.mcp.replace",
            profile=profile_name,
            version="static",
        )
        await self.profiles.replace_mcp_servers(
            self.target.endpoint,
            name=profile_name,
            servers={
                f"resume-beta-static-{suffix}": {
                    "transport": "streamable_http",
                    "enabled": True,
                    "url": self.marker.url("beta", "static"),
                }
            },
        )
        return await self.verify_projection("beta", "static", "static")

    async def project_registry_keeper(self) -> dict[str, Any]:
        """Make both Alpha MCP versions globally discoverable before local switches."""

        expert = self.experts["registry-keeper"]
        profile_name = str(expert["hermes_profile_name"])
        suffix = self.store.checkpoint.suffix
        servers = {
            f"resume-alpha-control-{suffix}": {
                "transport": "streamable_http",
                "enabled": True,
                "url": self.marker.url("alpha", "control"),
            },
            f"resume-alpha-v1-{suffix}": {
                "transport": "streamable_http",
                "enabled": True,
                "url": self.marker.url("alpha", "v1"),
            },
            f"resume-alpha-v2-{suffix}": {
                "transport": "streamable_http",
                "enabled": True,
                "url": self.marker.url("alpha", "v2"),
            },
        }
        self.store.record_control(
            "profile.mcp.replace",
            profile=profile_name,
            version="registry-keeper",
        )
        await self.profiles.replace_mcp_servers(
            self.target.endpoint,
            name=profile_name,
            servers=servers,
        )
        observed = await self.dashboard.list_profile_mcp_servers(
            self.target.endpoint,
            profile_name=profile_name,
        )
        observed_names = {str(item.get("name") or "") for item in observed}
        if observed_names != set(servers):
            raise SmokeError("registry keeper MCP projection did not round-trip exactly")
        return {
            "profile": profile_name,
            "servers": sorted(observed_names),
        }

    async def verify_projection(
        self,
        group: str,
        skill_version: str,
        mcp_version: str,
    ) -> dict[str, Any]:
        expert = self.experts[group]
        profile_id = int(expert["runtime_agent_profile_id"])
        profile_name = str(expert["hermes_profile_name"])
        config = await self.dashboard.dashboard_json_request(
            self.target.endpoint,
            "GET",
            f"/api/config?profile={profile_name}",
        )
        servers = await self.dashboard.list_profile_mcp_servers(
            self.target.endpoint,
            profile_name=profile_name,
        )
        listing = await self.dashboard.dashboard_json_request(
            self.target.endpoint,
            "GET",
            f"/api/files?path=/data/workspace/experts/{profile_id}/skills",
        )
        if (
            self.store.checkpoint.suffix not in json.dumps(servers, ensure_ascii=False)
            or not listing.get("entries")
        ):
            raise SmokeError(f"{group} Profile projection did not round-trip")
        return {
            "profile": profile_name,
            "skill_version": skill_version,
            "mcp_version": mcp_version,
            "skills": config.get("skills"),
            "servers": servers,
            "entries": listing.get("entries"),
        }

    async def cleanup(self, errors: list[str]) -> None:
        for profile_id in reversed(self.store.checkpoint.experts):
            try:
                await self._request(
                    "DELETE",
                    f"/user/experts/{profile_id}",
                    params={"runtime_workspace_id": self.target.workspace_id},
                )
            except Exception as exc:
                errors.append(f"expert:{profile_id}:{type(exc).__name__}")

    async def verify_cleanup(self, errors: list[str]) -> None:
        checkpoint = self.store.checkpoint
        async with AsyncSessionLocal() as db:
            active_runs = int(
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeRun)
                    .where(
                        RuntimeRun.runtime_workspace_id == self.target.workspace_id,
                        RuntimeRun.status.in_(("accepted", "pending", "running")),
                    )
                )
                or 0
            )
            active_sessions = await self._count_active_sessions(db)
            profiles = await self._count_remaining_profiles(db)
        checkpoint.evidence["cleanup_verification"] = {
            "active_runs": active_runs,
            "active_test_sessions": active_sessions,
            "remaining_enabled_test_profiles": profiles,
        }
        if active_runs or active_sessions or profiles:
            errors.append(
                f"verification:runs={active_runs},sessions={active_sessions},"
                f"profiles={profiles}"
            )

    async def _count_active_sessions(self, db: Any) -> int:
        sessions = self.store.checkpoint.sessions
        if not sessions:
            return 0
        return int(
            await db.scalar(
                select(func.count())
                .select_from(RuntimeSession)
                .where(
                    RuntimeSession.runtime_session_id.in_(sessions),
                    RuntimeSession.status == "active",
                )
            )
            or 0
        )

    async def _count_remaining_profiles(self, db: Any) -> int:
        experts = self.store.checkpoint.experts
        if not experts:
            return 0
        return int(
            await db.scalar(
                select(func.count())
                .select_from(RuntimeAgentProfile)
                .where(
                    RuntimeAgentProfile.id.in_(experts),
                    RuntimeAgentProfile.status.notin_(
                        ("deleted", "disabled", "revoked")
                    ),
                )
            )
            or 0
        )
