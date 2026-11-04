from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage, RuntimeAgentProfile, RuntimeAgentProfileRevision


EMPTY_ASSET_DIGEST = "sha256:" + "0" * 64


class ProfileExecutionIdentityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileExecutionIdentity:
    runtime_agent_profile_id: int
    desired_revision: int
    applied_revision: int
    profile_digest: str
    skill_digest: str
    mcp_digest: str
    knowledge_digest: str


def unmanaged_profile_execution_identity() -> ProfileExecutionIdentity:
    return ProfileExecutionIdentity(
        runtime_agent_profile_id=0,
        desired_revision=0,
        applied_revision=0,
        profile_digest=EMPTY_ASSET_DIGEST,
        skill_digest=EMPTY_ASSET_DIGEST,
        mcp_digest=EMPTY_ASSET_DIGEST,
        knowledge_digest=EMPTY_ASSET_DIGEST,
    )


async def resolve_profile_execution_identity(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
    runtime_workspace_id: int,
    runtime_agent_profile_id: int | None,
    hermes_profile_name: str,
) -> ProfileExecutionIdentity:
    if runtime_agent_profile_id is None:
        raise ProfileExecutionIdentityError("PROFILE_NOT_FOUND")
    profile = await db.scalar(
        select(RuntimeAgentProfile).where(
            RuntimeAgentProfile.id == runtime_agent_profile_id,
            RuntimeAgentProfile.org_id == org_id,
            RuntimeAgentProfile.user_id == user_id,
            RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
            RuntimeAgentProfile.hermes_profile_name == hermes_profile_name,
            RuntimeAgentProfile.status == "active",
        )
    )
    if profile is None:
        raise ProfileExecutionIdentityError("PROFILE_NOT_FOUND")
    expert_package_id = getattr(profile, "expert_package_id", None)
    if expert_package_id is not None:
        execution_status = await db.scalar(
            select(ExpertPackage.execution_status).where(
                ExpertPackage.id == expert_package_id,
                ExpertPackage.org_id == org_id,
            )
        )
        if execution_status == "force_disabled":
            raise ProfileExecutionIdentityError("PROFILE_EXECUTION_REVOKED")
    desired_revision = int(profile.desired_revision or 0)
    applied_revision = int(profile.applied_revision or 0)
    if applied_revision < 0 or desired_revision < applied_revision:
        raise ProfileExecutionIdentityError("PROFILE_REVISION_INVALID")
    if applied_revision == 0:
        if desired_revision > 0:
            raise ProfileExecutionIdentityError("PROFILE_ASSET_UPDATING")
        return ProfileExecutionIdentity(
            runtime_agent_profile_id=int(profile.id),
            desired_revision=desired_revision,
            applied_revision=0,
            profile_digest=EMPTY_ASSET_DIGEST,
            skill_digest=EMPTY_ASSET_DIGEST,
            mcp_digest=EMPTY_ASSET_DIGEST,
            knowledge_digest=EMPTY_ASSET_DIGEST,
        )
    revision = await db.scalar(
        select(RuntimeAgentProfileRevision).where(
            RuntimeAgentProfileRevision.runtime_agent_profile_id == profile.id,
            RuntimeAgentProfileRevision.revision == applied_revision,
        )
    )
    if revision is None:
        raise ProfileExecutionIdentityError("PROFILE_REVISION_NOT_FOUND")
    return ProfileExecutionIdentity(
        runtime_agent_profile_id=int(profile.id),
        desired_revision=applied_revision,
        applied_revision=applied_revision,
        profile_digest=str(revision.profile_digest),
        skill_digest=str(revision.skill_digest),
        mcp_digest=str(revision.mcp_digest),
        knowledge_digest=str(revision.knowledge_digest),
    )


__all__ = [
    "EMPTY_ASSET_DIGEST",
    "ProfileExecutionIdentity",
    "ProfileExecutionIdentityError",
    "resolve_profile_execution_identity",
    "unmanaged_profile_execution_identity",
]
