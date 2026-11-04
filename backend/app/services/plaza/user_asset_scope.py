"""Trusted default-Profile scope for user asset management.

The user Plaza is intentionally distinct from expert management.  A Runtime
Workspace can contain several Profiles, but requests made through the user
asset APIs may only inspect or mutate the caller's default Profile.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HermesRuntimeBinding, RuntimeAgentProfile
from app.services.runtime_agent_profiles import DEFAULT_HERMES_PROFILE_NAME


class UserAssetScopeError(RuntimeError):
    """A caller has no usable default-Profile asset scope."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DefaultUserAssetScope:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_agent_profile_id: int
    binding: HermesRuntimeBinding


async def resolve_default_user_asset_scope(
    session: AsyncSession,
    *,
    org_id: int,
    user_id: int,
) -> DefaultUserAssetScope:
    """Resolve the only Profile visible to the user asset-management domain.

    The Profile id is always derived server-side from the authenticated owner
    and its Runtime binding.  It is never accepted from the client, and this
    function deliberately has no "any profile" variant for expert callers.
    """

    binding = await session.scalar(
        select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.org_id == org_id,
            HermesRuntimeBinding.user_id == user_id,
        )
    )
    if binding is None:
        raise UserAssetScopeError("runtime_not_bound")

    workspace_id = int(binding.runtime_workspace_id)
    # Asset management writes the next Desired head, so it must remain
    # available while a previous revision is converging.  Execution callers
    # deliberately keep using the stricter ready/applied resolver.
    profile_id = await session.scalar(
        select(RuntimeAgentProfile.id)
        .where(
            RuntimeAgentProfile.runtime_workspace_id == workspace_id,
            RuntimeAgentProfile.org_id == org_id,
            RuntimeAgentProfile.user_id == user_id,
            RuntimeAgentProfile.profile_kind == "default",
            RuntimeAgentProfile.hermes_profile_name == DEFAULT_HERMES_PROFILE_NAME,
            RuntimeAgentProfile.status != "deleted",
        )
        .limit(1)
    )
    if profile_id is None:
        raise UserAssetScopeError("default_profile_not_found")

    return DefaultUserAssetScope(
        org_id=org_id,
        user_id=user_id,
        runtime_workspace_id=workspace_id,
        runtime_agent_profile_id=int(profile_id),
        binding=binding,
    )
