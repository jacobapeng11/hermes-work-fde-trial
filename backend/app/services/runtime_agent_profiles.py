from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CloudHermesProfile, RuntimeAgentProfile

if TYPE_CHECKING:
    from app.services.runtime_workspace import Principal


DEFAULT_HERMES_PROFILE_NAME = "default"
ACTIVE_AGENT_PROFILE_STATUSES = frozenset({"active"})


def normalize_profile_display_name(name: str) -> str:
    """Return the canonical key used by the Workspace active-name lease."""
    return " ".join(name.split()).casefold()


def executable_runtime_agent_profile_clauses() -> tuple[object, ...]:
    """SQL predicates shared by every Runtime Profile execution gate."""
    return (
        RuntimeAgentProfile.status.in_(ACTIVE_AGENT_PROFILE_STATUSES),
        or_(
            RuntimeAgentProfile.applied_revision > 0,
            and_(
                RuntimeAgentProfile.desired_revision == 0,
                RuntimeAgentProfile.applied_revision == 0,
            ),
        ),
    )


class RuntimeAgentProfileError(RuntimeError):
    pass


class RuntimeAgentProfileNotFoundError(RuntimeAgentProfileError):
    pass


@dataclass(frozen=True)
class RuntimeAgentProfileRecord:
    runtime_agent_profile_id: int
    runtime_workspace_id: int
    org_id: int
    user_id: int
    hermes_profile_name: str
    profile_kind: str
    display_name: str
    description: str | None
    avatar_url: str | None
    ownership_type: str
    inherited_from_profile_id: int | None
    expert_package_id: int | None
    soul_content: str | None
    capability_config: dict
    config_generation: int
    applied_config_generation: int
    status: str
    last_error: str | None


class SqlAlchemyRuntimeAgentProfileStore:
    """Platform source of truth for Profiles inside one Hermes Runtime."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def ensure_default(
        self,
        *,
        workspace: CloudHermesProfile,
    ) -> RuntimeAgentProfile:
        existing = (
            await self.db.execute(
                select(RuntimeAgentProfile).where(
                    RuntimeAgentProfile.runtime_workspace_id == int(workspace.id),
                    RuntimeAgentProfile.profile_kind == "default",
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing
        values = {
            "org_id": int(workspace.org_id),
            "user_id": int(workspace.user_id),
            "runtime_workspace_id": int(workspace.id),
            "hermes_profile_name": DEFAULT_HERMES_PROFILE_NAME,
            "profile_kind": "default",
            "display_name": str(workspace.name),
            "description": workspace.description,
            "avatar_url": workspace.avatar_url,
            "ownership_type": "default",
            "default_workspace_key": int(workspace.id),
            "config_generation": 1,
            "applied_config_generation": 1,
            "capability_config": {},
            "status": "active",
        }
        dialect_name = self.db.get_bind().dialect.name
        if dialect_name == "mysql":
            statement = mysql_insert(RuntimeAgentProfile).values(**values).prefix_with(
                "IGNORE"
            )
        elif dialect_name == "sqlite":
            statement = (
                sqlite_insert(RuntimeAgentProfile)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["default_workspace_key"])
            )
        elif dialect_name == "postgresql":
            statement = (
                postgresql_insert(RuntimeAgentProfile)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["default_workspace_key"])
            )
        else:
            # All supported production/test dialects use an atomic
            # insert-if-absent above. Keep the fallback explicit so an
            # unsupported dialect cannot silently create duplicate defaults.
            profile = RuntimeAgentProfile(
                **values,
            )
            self.db.add(profile)
            await self.db.flush()
            return profile
        await self.db.execute(statement)
        # A locking/current read observes a concurrent winner even when the
        # transaction's earlier consistent read saw no row.
        profile = (
            await self.db.execute(
                select(RuntimeAgentProfile)
                .where(
                    RuntimeAgentProfile.default_workspace_key
                    == int(workspace.id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if profile is None:
            raise RuntimeAgentProfileError(
                "failed to create default runtime agent profile"
            )
        return profile

    async def get_default(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        ready_only: bool = True,
    ) -> RuntimeAgentProfileRecord:
        clauses = [
            RuntimeAgentProfile.org_id == org_id,
            RuntimeAgentProfile.user_id == user_id,
            RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
            RuntimeAgentProfile.profile_kind == "default",
        ]
        if ready_only:
            clauses.extend(executable_runtime_agent_profile_clauses())
        profile = (
            await self.db.execute(select(RuntimeAgentProfile).where(*clauses))
        ).scalars().first()
        if profile is None:
            raise RuntimeAgentProfileNotFoundError("default runtime agent profile not found")
        return self._record(profile)

    async def require_owned(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        ready_only: bool = True,
    ) -> RuntimeAgentProfileRecord:
        clauses = [
            RuntimeAgentProfile.id == runtime_agent_profile_id,
            RuntimeAgentProfile.org_id == principal.org_id,
            RuntimeAgentProfile.user_id == principal.user_id,
            RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
        ]
        if ready_only:
            clauses.extend(executable_runtime_agent_profile_clauses())
        profile = (
            await self.db.execute(select(RuntimeAgentProfile).where(*clauses))
        ).scalar_one_or_none()
        if profile is None:
            raise RuntimeAgentProfileNotFoundError("runtime agent profile not found")
        return self._record(profile)

    async def list_owned(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        include_unavailable: bool = False,
    ) -> list[RuntimeAgentProfileRecord]:
        clauses = [
            RuntimeAgentProfile.org_id == principal.org_id,
            RuntimeAgentProfile.user_id == principal.user_id,
            RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
        ]
        if not include_unavailable:
            clauses.extend(executable_runtime_agent_profile_clauses())
        result = await self.db.execute(
            select(RuntimeAgentProfile)
            .where(*clauses)
            .order_by(
                RuntimeAgentProfile.profile_kind.asc(),
                RuntimeAgentProfile.created_at.asc(),
                RuntimeAgentProfile.id.asc(),
            )
        )
        return [self._record(profile) for profile in result.scalars().all()]

    async def create_expert_row(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        display_name: str,
        description: str | None,
        avatar_url: str | None,
        soul_content: str,
        ownership_type: str,
        inherited_from_profile_id: int | None = None,
        expert_package_id: int | None = None,
        capability_config: dict | None = None,
    ) -> RuntimeAgentProfile:
        workspace = (
            await self.db.execute(
                select(CloudHermesProfile).where(
                    CloudHermesProfile.id == runtime_workspace_id,
                    CloudHermesProfile.org_id == principal.org_id,
                    CloudHermesProfile.user_id == principal.user_id,
                    CloudHermesProfile.status == "active",
                )
            )
        ).scalar_one_or_none()
        if workspace is None:
            raise RuntimeAgentProfileNotFoundError("runtime workspace not found")
        if inherited_from_profile_id is not None:
            await self.require_owned(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=inherited_from_profile_id,
                ready_only=False,
            )
        row = RuntimeAgentProfile(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            hermes_profile_name=f"expert-pending-{uuid4().hex}",
            profile_kind="expert",
            display_name=display_name,
            active_display_name_key=normalize_profile_display_name(display_name),
            description=description,
            avatar_url=avatar_url,
            ownership_type=ownership_type,
            inherited_from_profile_id=inherited_from_profile_id,
            expert_package_id=expert_package_id,
            config_generation=1,
            applied_config_generation=0,
            soul_content=soul_content,
            capability_config=dict(capability_config or {}),
            status="active",
        )
        try:
            async with self.db.begin_nested():
                self.db.add(row)
                await self.db.flush()
                row.hermes_profile_name = f"expert-{int(row.id)}"
                await self.db.flush()
        except IntegrityError as exc:
            raise RuntimeAgentProfileError("PROFILE_DISPLAY_NAME_CONFLICT") from exc
        return row

    @staticmethod
    def _record(profile: RuntimeAgentProfile) -> RuntimeAgentProfileRecord:
        return RuntimeAgentProfileRecord(
            runtime_agent_profile_id=int(profile.id),
            runtime_workspace_id=int(profile.runtime_workspace_id),
            org_id=int(profile.org_id),
            user_id=int(profile.user_id),
            hermes_profile_name=str(profile.hermes_profile_name),
            profile_kind=str(profile.profile_kind),
            display_name=str(profile.display_name),
            description=profile.description,
            avatar_url=profile.avatar_url,
            ownership_type=str(profile.ownership_type),
            inherited_from_profile_id=(
                int(profile.inherited_from_profile_id)
                if profile.inherited_from_profile_id is not None
                else None
            ),
            expert_package_id=(
                int(profile.expert_package_id)
                if profile.expert_package_id is not None
                else None
            ),
            soul_content=profile.soul_content,
            capability_config=dict(profile.capability_config or {}),
            config_generation=int(profile.config_generation),
            applied_config_generation=int(profile.applied_config_generation),
            status=str(profile.status),
            last_error=profile.last_error,
        )


async def resolve_default_runtime_agent_profile_id(
    db: AsyncSession,
    *,
    runtime_workspace_id: int,
    org_id: int | None = None,
    user_id: int | None = None,
) -> int:
    clauses = [
        RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
        RuntimeAgentProfile.profile_kind == "default",
        RuntimeAgentProfile.hermes_profile_name == DEFAULT_HERMES_PROFILE_NAME,
        *executable_runtime_agent_profile_clauses(),
    ]
    if org_id is not None:
        clauses.append(RuntimeAgentProfile.org_id == org_id)
    if user_id is not None:
        clauses.append(RuntimeAgentProfile.user_id == user_id)
    profile_id = await db.scalar(
        select(RuntimeAgentProfile.id).where(*clauses).limit(1)
    )
    if profile_id is None:
        raise RuntimeAgentProfileNotFoundError(
            "default runtime agent profile not found"
        )
    return int(profile_id)
