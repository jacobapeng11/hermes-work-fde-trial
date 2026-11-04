"""One-time default MCP Desired binding for a new personal Runtime."""

from __future__ import annotations

import copy
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CloudHermesProfile, Mcp, RuntimeAgentProfile
from app.schemas.profile_desired import DefaultProfileAssetMutation
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_workspace import RuntimeWorkspaceRecord


logger = logging.getLogger(__name__)

DEFAULT_MCP_BOOTSTRAP_KEY = "default_mcp_bootstrap"
PENDING = "pending"
INSTALLING = "installing"
SUCCEEDED = "succeeded"
FAILED = "failed"
_STATUSES = frozenset({PENDING, INSTALLING, SUCCEEDED, FAILED})


def default_mcp_bootstrap_status(profile_config: dict[str, Any] | None) -> str | None:
    if not isinstance(profile_config, dict):
        return None
    state = profile_config.get(DEFAULT_MCP_BOOTSTRAP_KEY)
    if not isinstance(state, dict):
        return None
    status = state.get("status")
    return str(status) if status in _STATUSES else None


def with_default_mcp_bootstrap_status(
    profile_config: dict[str, Any] | None,
    *,
    status: str,
    slug: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in _STATUSES:
        raise ValueError("unsupported default MCP bootstrap status")
    config = copy.deepcopy(profile_config) if isinstance(profile_config, dict) else {}
    state: dict[str, Any] = {"status": status}
    if slug:
        state["slug"] = slug
    if error:
        state["error"] = " ".join(error.split())[:500]
    config[DEFAULT_MCP_BOOTSTRAP_KEY] = state
    return config


class DefaultMcpBootstrapService:
    """Bind the configured MCP through the stable default-Profile Desired path."""

    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    async def ensure_for_runtime(self, record: RuntimeWorkspaceRecord) -> None:
        slug = settings.PERSONAL_DEFAULT_MCP_SLUG.strip().lower()
        profile = await self._profile_for_update(record)
        if default_mcp_bootstrap_status(profile.profile_config) != PENDING:
            await self.session.rollback()
            return
        if not slug:
            profile.profile_config = with_default_mcp_bootstrap_status(
                profile.profile_config,
                status=SUCCEEDED,
            )
            await self.session.commit()
            return
        profile.profile_config = with_default_mcp_bootstrap_status(
            profile.profile_config,
            status=INSTALLING,
            slug=slug,
        )
        try:
            mcp = await self.session.scalar(
                select(Mcp).where(
                    Mcp.org_id == record.org_id,
                    Mcp.slug == slug,
                    Mcp.visibility != "private",
                    Mcp.status == "published",
                    Mcp.revoked_at.is_(None),
                )
            )
            default_profile = await self.session.scalar(
                select(RuntimeAgentProfile).where(
                    RuntimeAgentProfile.org_id == record.org_id,
                    RuntimeAgentProfile.user_id == record.user_id,
                    RuntimeAgentProfile.runtime_workspace_id
                    == record.runtime_workspace_id,
                    RuntimeAgentProfile.profile_kind == "default",
                    RuntimeAgentProfile.status != "deleted",
                )
            )
            if mcp is None or default_profile is None:
                raise RuntimeError("configured stable MCP/default Profile is unavailable")
            await ProfileDesiredMutationService(self.session).mutate_default_asset(
                DefaultProfileAssetMutation(
                    org_id=record.org_id,
                    user_id=record.user_id,
                    runtime_workspace_id=record.runtime_workspace_id,
                    runtime_agent_profile_id=int(default_profile.id),
                    asset_type="mcp",
                    action="install",
                    asset_id=int(mcp.id),
                    operation_id=(
                        f"default-mcp-bootstrap-{record.runtime_workspace_id}-{mcp.id}"
                    ),
                )
            )
            profile.profile_config = with_default_mcp_bootstrap_status(
                profile.profile_config,
                status=SUCCEEDED,
                slug=slug,
            )
            await self.session.commit()
        except Exception as exc:
            await self.session.rollback()
            profile = await self._profile_for_update(record)
            profile.profile_config = with_default_mcp_bootstrap_status(
                profile.profile_config,
                status=FAILED,
                slug=slug,
                error=str(exc),
            )
            await self.session.commit()
            logger.exception(
                "default MCP Desired bootstrap failed org_id=%s workspace_id=%s",
                record.org_id,
                record.runtime_workspace_id,
            )

    async def _profile_for_update(self, record: RuntimeWorkspaceRecord) -> CloudHermesProfile:
        profile = await self.session.scalar(
            select(CloudHermesProfile)
            .where(
                CloudHermesProfile.id == record.cloud_profile_id,
                CloudHermesProfile.org_id == record.org_id,
                CloudHermesProfile.user_id == record.user_id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
            .with_for_update()
        )
        if profile is None:
            raise RuntimeError("default Runtime profile not found")
        return profile
