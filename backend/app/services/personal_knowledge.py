from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    CloudHermesProfile,
    KnowledgeBase,
    KnowledgeBaseFile,
    Permission,
    ProfileKnowledgeBinding,
    RuntimeAgentProfile,
    SysDept,
    SysUser,
    User,
)
from app.schemas.personal_knowledge import (
    PersonalKnowledgeBindingResponse,
    PersonalKnowledgeCreate,
    PersonalKnowledgeDetail,
    PersonalKnowledgePermissionDepartmentGrant,
    PersonalKnowledgePermissionReplace,
    PersonalKnowledgePermissionResponse,
    PersonalKnowledgePermissionUserGrant,
    PersonalKnowledgeSelectedUser,
    PersonalKnowledgeSummary,
    PersonalKnowledgeUpdate,
)
from app.services.personal_knowledge_acl import (
    EffectiveRole,
    PersonalKnowledgeAclError,
    PersonalKnowledgeAclService,
)
from app.services.personal_knowledge_provider import (
    PersonalKnowledgeLifecycleProvider,
    PersonalKnowledgeProviderError,
    build_personal_knowledge_provider,
)
from app.services.runtime_workspace import Principal
from app.services.user_directory import COMPANY_ROOT_DEPARTMENT_IDS, UserDirectoryService
from app.schemas.user_directory import PersonalKnowledgePermissionDirectoryNode
from app.services.runtime_agent_profiles import (
    RuntimeAgentProfileNotFoundError,
    SqlAlchemyRuntimeAgentProfileStore,
    resolve_default_runtime_agent_profile_id,
)
from app.schemas.profile_desired import DefaultProfileAssetMutation
from app.services.profile_desired_mutations import ProfileDesiredMutationService


ROLE_ORDER = ("download", "preview", "retrieve")


class PersonalKnowledgeError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _slug(name: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]
    return f"{stem or 'personal-kb'}-{uuid.uuid4().hex[:10]}"


class PersonalKnowledgeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: PersonalKnowledgeLifecycleProvider | None = None,
        providers: (
            Mapping[str, PersonalKnowledgeLifecycleProvider] | None
        ) = None,
        provider_name: str | None = None,
        acl: PersonalKnowledgeAclService | None = None,
    ) -> None:
        self.session = session
        self._provider_override = provider
        self._providers = dict(providers or {})
        self.provider_name = (
            provider_name or settings.PERSONAL_KNOWLEDGE_PROVIDER
        )
        self.acl = acl or PersonalKnowledgeAclService(session)

    def _provider_for(
        self,
        provider_name: str,
    ) -> PersonalKnowledgeLifecycleProvider:
        provider = self._providers.get(provider_name)
        if provider is not None:
            return provider
        if self._provider_override is not None:
            return self._provider_override
        return build_personal_knowledge_provider(provider_name)

    async def _file_counts(self, knowledge_base_id: int) -> tuple[int, int]:
        total = await self.session.scalar(
            select(func.count(KnowledgeBaseFile.id)).where(
                KnowledgeBaseFile.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseFile.is_deleted == 0,
            )
        )
        ready = await self.session.scalar(
            select(func.count(KnowledgeBaseFile.id)).where(
                KnowledgeBaseFile.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseFile.is_deleted == 0,
                KnowledgeBaseFile.file_status == 1,
            )
        )
        return int(total or 0), int(ready or 0)

    async def _owner_name(self, owner_user_id: int) -> str:
        return str(
            await self.session.scalar(select(User.name).where(User.id == owner_user_id))
            or "未知用户"
        )

    async def _runtime_enabled(
        self,
        *,
        runtime_workspace_id: int | None,
        runtime_agent_profile_id: int | None = None,
        knowledge_base_id: int,
    ) -> bool:
        if runtime_workspace_id is None:
            return False
        clauses = [
                ProfileKnowledgeBinding.runtime_workspace_id == runtime_workspace_id,
                ProfileKnowledgeBinding.knowledge_base_id == knowledge_base_id,
                ProfileKnowledgeBinding.status == "enabled",
        ]
        if runtime_agent_profile_id is not None:
            clauses.append(
                ProfileKnowledgeBinding.runtime_agent_profile_id
                == runtime_agent_profile_id
            )
        binding_id = await self.session.scalar(
            select(ProfileKnowledgeBinding.id).where(*clauses)
        )
        return binding_id is not None

    async def _summary(
        self,
        knowledge_base: KnowledgeBase,
        role: EffectiveRole,
        *,
        runtime_workspace_id: int | None = None,
        runtime_agent_profile_id: int | None = None,
        runtime_enabled_override: bool | None = None,
    ) -> PersonalKnowledgeSummary:
        file_count, ready_file_count = await self._file_counts(int(knowledge_base.id))
        return PersonalKnowledgeSummary(
            knowledge_base_id=int(knowledge_base.id),
            name=str(knowledge_base.name),
            description=knowledge_base.description,
            owner_user_id=int(knowledge_base.owner_user_id),
            owner_name=await self._owner_name(int(knowledge_base.owner_user_id)),
            effective_role=role,
            runtime_enabled=(
                runtime_enabled_override
                if runtime_enabled_override is not None
                else await self._runtime_enabled(
                    runtime_workspace_id=runtime_workspace_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    knowledge_base_id=int(knowledge_base.id),
                )
            ),
            file_count=file_count,
            ready_file_count=ready_file_count,
            visibility=(
                "shared" if str(knowledge_base.visibility) == "shared" else "private"
            ),
            status=str(knowledge_base.status),
            created_at=knowledge_base.created_at,
            updated_at=knowledge_base.updated_at,
        )

    async def list_for_user(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int | None,
        runtime_agent_profile_id: int | None = None,
        keyword: str | None,
        ownership: str,
    ) -> list[PersonalKnowledgeSummary]:
        if (
            runtime_workspace_id is not None
            and runtime_agent_profile_id is None
        ):
            try:
                runtime_agent_profile_id = (
                    await resolve_default_runtime_agent_profile_id(
                        self.session,
                        runtime_workspace_id=runtime_workspace_id,
                        org_id=principal.org_id,
                        user_id=principal.user_id,
                    )
                )
            except RuntimeAgentProfileNotFoundError as exc:
                raise PersonalKnowledgeError(
                    "runtime_agent_profile_not_found",
                    status_code=404,
                ) from exc
        statement = select(KnowledgeBase).where(
            KnowledgeBase.org_id == principal.org_id,
            KnowledgeBase.kind == "personal",
            KnowledgeBase.status == "active",
        )
        if keyword:
            statement = statement.where(
                KnowledgeBase.name.like(f"%{keyword.strip()}%")
            )
        knowledge_bases = (
            await self.session.execute(statement.order_by(KnowledgeBase.updated_at.desc()))
        ).scalars()
        summaries: list[PersonalKnowledgeSummary] = []
        for knowledge_base in knowledge_bases:
            role = await self.acl.effective_role(
                principal=principal,
                knowledge_base=knowledge_base,
            )
            if role is None:
                continue
            if ownership == "owned" and role != "owner":
                continue
            if ownership == "shared" and role == "owner":
                continue
            summaries.append(
                await self._summary(
                    knowledge_base,
                    role,
                    runtime_workspace_id=runtime_workspace_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                )
            )
        return summaries

    async def create(
        self,
        *,
        principal: Principal,
        command: PersonalKnowledgeCreate,
    ) -> PersonalKnowledgeDetail:
        return await self._create_owned(
            principal=principal,
            command=command,
            kind="personal",
            application_agent_id=None,
        )

    async def _create_owned(
        self,
        *,
        principal: Principal,
        command: PersonalKnowledgeCreate,
        kind: str,
        application_agent_id: int | None,
    ) -> PersonalKnowledgeDetail:
        try:
            lifecycle_provider = self._provider_for(self.provider_name)
            dataset_id = await lifecycle_provider.create_dataset(command.name)
        except PersonalKnowledgeProviderError as exc:
            raise PersonalKnowledgeError(
                "create_dataset_failed",
                status_code=502,
            ) from exc
        knowledge_base = KnowledgeBase(
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            application_agent_id=application_agent_id,
            slug=_slug(command.name),
            name=command.name,
            description=command.description,
            kind=kind,
            provider=self.provider_name,
            dataset_id=dataset_id,
            provider_capabilities=(
                {
                    "vector": True,
                    "keyword": True,
                    "wiki": True,
                    "graph": False,
                }
                if self.provider_name == "weknora"
                else None
            ),
            provider_capabilities_synced_at=(
                datetime.utcnow() if self.provider_name == "weknora" else None
            ),
            visibility="private",
            status="active",
        )
        self.session.add(knowledge_base)
        try:
            await self.session.commit()
            await self.session.refresh(knowledge_base)
        except Exception:
            await self.session.rollback()
            try:
                await lifecycle_provider.delete_dataset(dataset_id)
            except PersonalKnowledgeProviderError:
                pass
            raise
        return await self.detail(
            principal=principal,
            knowledge_base_id=int(knowledge_base.id),
        )

    async def detail(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
    ) -> PersonalKnowledgeDetail:
        knowledge_base, role = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="retrieve",
        )
        if role == "retrieve":
            raise PersonalKnowledgeError(
                "personal_knowledge_detail_forbidden",
                status_code=403,
            )
        summary = await self._summary(knowledge_base, role)
        return PersonalKnowledgeDetail(
            **summary.model_dump(),
            can_enter=True,
            can_preview=True,
            can_download=role in {"owner", "download"},
            can_manage=role == "owner",
        )

    async def update(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        command: PersonalKnowledgeUpdate,
    ) -> PersonalKnowledgeDetail:
        knowledge_base, _ = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="manage",
        )
        knowledge_base.name = command.name
        knowledge_base.description = command.description
        await self.session.commit()
        await self.session.refresh(knowledge_base)
        return await self.detail(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )

    async def delete(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
    ) -> None:
        knowledge_base, _ = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="manage",
        )
        try:
            lifecycle_provider = self._provider_for(
                str(knowledge_base.provider or "")
            )
            dataset_id = str(knowledge_base.dataset_id or "")
            if not dataset_id:
                raise PersonalKnowledgeProviderError("delete_dataset_failed")
            await lifecycle_provider.delete_dataset(dataset_id)
        except PersonalKnowledgeProviderError as exc:
            raise PersonalKnowledgeError(
                "delete_dataset_failed",
                status_code=502,
            ) from exc
        from app.services.application_agent_personal_knowledge import (
            revoke_bindings_for_deleted_knowledge,
        )

        await revoke_bindings_for_deleted_knowledge(
            self.session,
            knowledge_base_id=int(knowledge_base.id),
        )
        await self._remove_default_profile_desired_bindings(knowledge_base)
        await self.session.execute(
            delete(ProfileKnowledgeBinding).where(
                ProfileKnowledgeBinding.knowledge_base_id == knowledge_base.id
            )
        )
        await self.session.execute(
            delete(Permission).where(
                Permission.asset_type == "knowledge_base",
                Permission.asset_id == knowledge_base.id,
            )
        )
        await self.session.execute(
            delete(KnowledgeBaseFile).where(
                KnowledgeBaseFile.knowledge_base_id == knowledge_base.id
            )
        )
        await self.session.delete(knowledge_base)
        await self.session.commit()

    async def _remove_default_profile_desired_bindings(
        self,
        knowledge_base: KnowledgeBase,
    ) -> None:
        """Advance default Profile Desired heads before the library is deleted."""

        rows = (
            await self.session.execute(
                select(ProfileKnowledgeBinding, RuntimeAgentProfile)
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.id
                    == ProfileKnowledgeBinding.runtime_agent_profile_id,
                )
                .where(
                    ProfileKnowledgeBinding.knowledge_base_id
                    == int(knowledge_base.id),
                    RuntimeAgentProfile.profile_kind == "default",
                    RuntimeAgentProfile.status == "active",
                )
            )
        ).all()
        for binding, profile in rows:
            await ProfileDesiredMutationService(
                self.session
            ).mutate_default_asset(
                DefaultProfileAssetMutation(
                    org_id=int(profile.org_id),
                    user_id=int(profile.user_id),
                    runtime_workspace_id=int(profile.runtime_workspace_id),
                    runtime_agent_profile_id=int(binding.runtime_agent_profile_id),
                    asset_type="knowledge",
                    action="uninstall",
                    asset_id=int(knowledge_base.id),
                    operation_id=(
                        f"knowledge-delete-{knowledge_base.id}-{binding.id}"
                    ),
                )
            )

    async def _permission_response(
        self,
        knowledge_base_id: int,
        *,
        org_id: int,
        normalized_targets: list[str] | None = None,
    ) -> PersonalKnowledgePermissionResponse:
        targets: dict[str, dict[str, object]] = {
            role: {"user_ids": [], "department_ids": [], "all": False}
            for role in ROLE_ORDER
        }
        selected_user_ids: set[int] = set()
        permissions = list(
            (
                await self.session.execute(
                select(Permission).where(
                    Permission.org_id == org_id,
                    Permission.asset_type == "knowledge_base",
                    Permission.asset_id == knowledge_base_id,
                )
                )
            ).scalars()
        )
        selected_department_ids: set[str] = set()
        for permission in permissions:
            role = str(permission.kb_role or "")
            if role not in targets:
                continue
            if permission.grantee_type == "user":
                user_id = int(permission.grantee_id)
                targets[role]["user_ids"].append(user_id)  # type: ignore[union-attr]
                selected_user_ids.add(user_id)
            elif permission.grantee_type == "department":
                department_id = str(permission.grantee_id)
                targets[role]["department_ids"].append(department_id)  # type: ignore[union-attr]
                selected_department_ids.add(department_id)
            elif permission.grantee_type == "all":
                targets[role]["all"] = True
        selected_users: list[PersonalKnowledgeSelectedUser] = []
        users_by_id: dict[int, object] = {}
        if selected_user_ids:
            user_rows = (
                await self.session.execute(
                    select(User.id, User.name, User.user_code, User.status).where(
                        User.org_id == org_id,
                        User.id.in_(selected_user_ids),
                    )
                )
            ).all()
            users_by_id = {int(row.id): row for row in user_rows}
        user_codes = {
            str(row.user_code)
            for row in users_by_id.values()
            if getattr(row, "user_code", None)
        }
        directory_by_code: dict[str, str | None] = {}
        if user_codes:
            directory_rows = (
                await self.session.execute(
                    select(SysUser.code, SysUser.dept_id).where(SysUser.code.in_(user_codes))
                )
            ).all()
            directory_by_code = {
                str(row.code): str(row.dept_id) if row.dept_id is not None else None
                for row in directory_rows
                if row.code
            }
        relevant_department_ids = selected_department_ids | {
            department_id for department_id in directory_by_code.values() if department_id
        }
        department_rows = []
        if relevant_department_ids:
            department_rows = (
                await self.session.execute(
                    select(
                        SysDept.dept_id,
                        SysDept.parent_id,
                        SysDept.name,
                    )
                )
            ).all()
        departments_by_id = {str(row.dept_id): row for row in department_rows}

        def department_path(department_id: str | None) -> list[str]:
            current = department_id
            names: list[str] = []
            seen: set[str] = set()
            while current and current not in seen:
                seen.add(current)
                row = departments_by_id.get(current)
                if row is None:
                    break
                names.append(str(row.name or row.dept_id))
                current = str(row.parent_id) if row.parent_id is not None else None
            names.reverse()
            return names

        for user_id in sorted(selected_user_ids):
            row = users_by_id.get(user_id)
            if row is None:
                selected_users.append(
                    PersonalKnowledgeSelectedUser(
                        id=user_id,
                        status="missing",
                    )
                )
                continue
            selected_users.append(
                PersonalKnowledgeSelectedUser(
                    id=user_id,
                    name=str(row.name),
                    user_code=str(row.user_code) if row.user_code else None,
                    status="active" if row.status == "active" else "inactive",
                )
            )
        grants: list[
            PersonalKnowledgePermissionDepartmentGrant | PersonalKnowledgePermissionUserGrant
        ] = []
        for permission in permissions:
            role = str(permission.kb_role or "")
            if role not in targets:
                continue
            if permission.grantee_type == "department":
                department_id = str(permission.grantee_id)
                row = departments_by_id.get(department_id)
                grants.append(
                    PersonalKnowledgePermissionDepartmentGrant(
                        target_id=department_id,
                        name=(
                            str(row.name or row.dept_id)
                            if row is not None
                            else f"已失效部门（{department_id}）"
                        ),
                        department_path=department_path(department_id),
                        role=role,
                    )
                )
            elif permission.grantee_type == "user":
                user_id = int(permission.grantee_id)
                row = users_by_id.get(user_id)
                user_code = str(row.user_code) if row is not None and row.user_code else None
                department_id = directory_by_code.get(user_code) if user_code else None
                grants.append(
                    PersonalKnowledgePermissionUserGrant(
                        target_id=user_id,
                        name=str(row.name) if row is not None else None,
                        user_code=user_code,
                        department_id=department_id,
                        department_path=department_path(department_id),
                        status=(
                            "missing"
                            if row is None
                            else "active" if row.status == "active" else "inactive"
                        ),
                        role=role,
                    )
                )
        grants.sort(
            key=lambda grant: (
                ROLE_ORDER.index(grant.role),
                0 if grant.target_type == "department" else 1,
                str(grant.target_id),
            )
        )
        return PersonalKnowledgePermissionResponse(
            download=targets["download"],
            preview=targets["preview"],
            retrieve=targets["retrieve"],
            normalized_targets=normalized_targets or [],
            selected_users=selected_users,
            grants=grants,
        )

    async def permissions(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
    ) -> PersonalKnowledgePermissionResponse:
        await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="manage",
        )
        return await self._permission_response(
            knowledge_base_id,
            org_id=principal.org_id,
        )

    async def permission_directory(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        keyword: str = "",
    ) -> list[PersonalKnowledgePermissionDirectoryNode]:
        await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="manage",
        )
        return await UserDirectoryService(
            self.session
        ).personal_knowledge_permission_directory(
            principal=principal,
            keyword=keyword,
        )

    async def _validate_permission_targets(
        self,
        *,
        principal: Principal,
        user_ids: set[int],
        department_ids: set[str],
        existing_user_ids: set[int],
    ) -> None:
        if user_ids:
            valid_users = set(
                (
                    await self.session.execute(
                        select(User.id).where(
                            User.org_id == principal.org_id,
                            User.id.in_(user_ids),
                            User.status == "active",
                        )
                    )
                ).scalars()
            )
            if user_ids - valid_users - existing_user_ids:
                raise PersonalKnowledgeError(
                    "invalid_permission_user",
                    status_code=400,
                )
        if department_ids:
            if any(str(dept_id).startswith("virtual:") for dept_id in department_ids):
                raise PersonalKnowledgeError(
                    "invalid_permission_department",
                    status_code=400,
                )
            # 公司虚拟根（com_id 即根部门 ID）是合法授权目标。
            requested = {str(dept_id) for dept_id in department_ids}
            valid_departments = set(
                (
                    await self.session.execute(
                        select(SysDept.dept_id).where(
                            SysDept.dept_id.in_(department_ids),
                            SysDept.status == 1,
                        )
                    )
                ).scalars()
            )
            if (valid_departments | COMPANY_ROOT_DEPARTMENT_IDS) & requested != requested:
                raise PersonalKnowledgeError(
                    "invalid_permission_department",
                    status_code=400,
                )

    async def replace_permissions(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        command: PersonalKnowledgePermissionReplace,
    ) -> PersonalKnowledgePermissionResponse:
        knowledge_base, _ = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="manage",
        )
        selected: dict[tuple[str, str], str] = {}
        normalized: list[str] = []
        all_user_ids: set[int] = set()
        all_department_ids: set[str] = set()
        for role in ROLE_ORDER:
            target = getattr(command, role)
            all_user_ids.update(target.user_ids)
            all_department_ids.update(target.department_ids)
            candidates = [
                *(("user", str(user_id)) for user_id in target.user_ids),
                *(("department", str(department_id)) for department_id in target.department_ids),
                *((("all", "*"),) if target.all else ()),
            ]
            for candidate in candidates:
                if candidate in selected:
                    normalized.append(f"{candidate[0]}:{candidate[1]}")
                    continue
                selected[candidate] = role
        existing_user_ids = set(
            int(user_id)
            for user_id in (
                await self.session.execute(
                    select(Permission.grantee_id).where(
                        Permission.org_id == principal.org_id,
                        Permission.asset_type == "knowledge_base",
                        Permission.asset_id == knowledge_base_id,
                        Permission.grantee_type == "user",
                    )
                )
            ).scalars()
        )
        await self._validate_permission_targets(
            principal=principal,
            user_ids=all_user_ids,
            department_ids=all_department_ids,
            existing_user_ids=existing_user_ids,
        )
        await self.session.execute(
            delete(Permission).where(
                Permission.org_id == principal.org_id,
                Permission.asset_type == "knowledge_base",
                Permission.asset_id == knowledge_base_id,
            )
        )
        for (grantee_type, grantee_id), role in selected.items():
            self.session.add(
                Permission(
                    org_id=principal.org_id,
                    asset_type="knowledge_base",
                    asset_id=knowledge_base_id,
                    grantee_type=grantee_type,
                    grantee_id=grantee_id,
                    kb_role=role,
                )
            )
        knowledge_base.visibility = "shared" if selected else "private"
        await self.session.flush()
        await self._disable_revoked_bindings(knowledge_base)
        from app.services.application_agent_personal_knowledge import (
            reconcile_bindings_after_acl_change,
        )

        await reconcile_bindings_after_acl_change(
            self.session,
            knowledge_base=knowledge_base,
        )
        await self.session.commit()
        return await self._permission_response(
            knowledge_base_id,
            org_id=principal.org_id,
            normalized_targets=normalized,
        )

    async def _disable_revoked_bindings(self, knowledge_base: KnowledgeBase) -> None:
        rows = (
            await self.session.execute(
                select(ProfileKnowledgeBinding, RuntimeAgentProfile)
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.id == ProfileKnowledgeBinding.runtime_agent_profile_id,
                )
                .where(
                    ProfileKnowledgeBinding.knowledge_base_id == knowledge_base.id,
                    ProfileKnowledgeBinding.status == "enabled",
                )
            )
        ).all()
        for binding, profile in rows:
            if str(profile.profile_kind) != "default":
                continue
            role = await self.acl.effective_role(
                principal=Principal(
                    org_id=int(profile.org_id),
                    user_id=int(profile.user_id),
                ),
                knowledge_base=knowledge_base,
            )
            if role is None:
                await ProfileDesiredMutationService(self.session).mutate_default_asset(
                    DefaultProfileAssetMutation(
                        org_id=int(profile.org_id),
                        user_id=int(profile.user_id),
                        runtime_workspace_id=int(profile.runtime_workspace_id),
                        runtime_agent_profile_id=int(binding.runtime_agent_profile_id),
                        asset_type="knowledge",
                        action="uninstall",
                        asset_id=int(knowledge_base.id),
                        operation_id=(
                            f"knowledge-acl-revoke-{knowledge_base.id}-{binding.id}"
                        ),
                    )
                )

    async def set_runtime_enabled(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None = None,
        knowledge_base_id: int,
        enabled: bool,
    ) -> PersonalKnowledgeBindingResponse:
        profile = await self.session.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.org_id == principal.org_id,
                CloudHermesProfile.user_id == principal.user_id,
                CloudHermesProfile.status == "active",
            ).with_for_update()
        )
        if profile is None:
            raise PersonalKnowledgeError("runtime_workspace_not_found", status_code=404)
        try:
            if runtime_agent_profile_id is None:
                runtime_agent_profile_id = (
                    await resolve_default_runtime_agent_profile_id(
                        self.session,
                        runtime_workspace_id=runtime_workspace_id,
                        org_id=principal.org_id,
                        user_id=principal.user_id,
                    )
                )
            else:
                await SqlAlchemyRuntimeAgentProfileStore(
                    self.session
                ).require_owned(
                    principal=principal,
                    runtime_workspace_id=runtime_workspace_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                )
        except RuntimeAgentProfileNotFoundError as exc:
            raise PersonalKnowledgeError(
                "runtime_agent_profile_not_found",
                status_code=404,
            ) from exc
        knowledge_base, _role = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="retrieve",
        )
        if knowledge_base.status != "active":
            raise PersonalKnowledgeError("personal_knowledge_inactive", status_code=409)
        binding = await self.session.scalar(
            select(ProfileKnowledgeBinding).where(
                ProfileKnowledgeBinding.runtime_workspace_id == runtime_workspace_id,
                ProfileKnowledgeBinding.runtime_agent_profile_id
                == runtime_agent_profile_id,
                ProfileKnowledgeBinding.knowledge_base_id == knowledge_base_id,
            )
        )
        if enabled and (binding is None or binding.status != "enabled"):
            enabled_count = await self.session.scalar(
                select(func.count(ProfileKnowledgeBinding.id)).where(
                    ProfileKnowledgeBinding.runtime_workspace_id == runtime_workspace_id,
                    ProfileKnowledgeBinding.runtime_agent_profile_id
                    == runtime_agent_profile_id,
                    ProfileKnowledgeBinding.status == "enabled",
                )
            )
            if int(enabled_count or 0) >= 20:
                raise PersonalKnowledgeError("personal_knowledge_binding_limit", status_code=409)
        await ProfileDesiredMutationService(self.session).mutate_profile_asset(
            DefaultProfileAssetMutation(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
                asset_type="knowledge",
                action="install" if enabled else "uninstall",
                asset_id=knowledge_base_id,
                operation_id=(
                    f"knowledge-binding-{runtime_agent_profile_id}-{knowledge_base_id}-{int(enabled)}"
                ),
            )
        )
        await self.session.commit()
        _total, ready = await self._file_counts(knowledge_base_id)
        return PersonalKnowledgeBindingResponse(
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            knowledge_base_id=knowledge_base_id,
            enabled=enabled,
            warning="no_ready_files" if enabled and ready == 0 else None,
        )


def translate_acl_error(exc: PersonalKnowledgeAclError) -> PersonalKnowledgeError:
    return PersonalKnowledgeError(exc.code, status_code=exc.status_code)
