from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentConversation,
    ApplicationAgentPersonalKnowledgeBinding,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    CloudHermesProfile,
    HermesRuntimeBinding,
    InstalledExpertBinding,
    ModelEntry,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeRun,
    RuntimeSession,
    User,
)
from app.services.admin_auth import AdminPrincipal


@dataclass(frozen=True)
class AdminRuntimeWorkspaceResourceRecord:
    runtime_workspace_id: int
    cloud_profile_id: int | None
    org_id: int
    user_id: int
    employee_no: str | None
    user_name: str
    runtime_name: str
    profile_status: str
    binding_status: str | None
    runtime_status: str | None
    default_model_entry_id: int | None
    default_model_display_name: str | None
    session_count: int
    run_count: int
    active_run_count: int
    installed_expert_count: int
    installed_skill_count: int
    installed_mcp_count: int
    installed_knowledge_base_count: int
    pending_reload: bool
    latest_error: str | None
    runtime_kind: str = "personal"
    application_agent_id: int | None = None
    has_runtime_binding: bool = True
    kservice_name: str | None = None

    @property
    def resource_key(self) -> str:
        return f"{self.runtime_kind}:{self.runtime_workspace_id}"


@dataclass(frozen=True)
class AdminRuntimeWorkspaceResourceFilter:
    employee_no: str | None = None
    user_name: str | None = None
    runtime_status: str | None = None


class AdminResourceViewStore(Protocol):
    async def list_runtime_workspaces(
        self,
        *,
        org_id: int,
        filters: AdminRuntimeWorkspaceResourceFilter,
    ) -> list[AdminRuntimeWorkspaceResourceRecord]:
        ...


class AdminResourceViewService:
    def __init__(self, store: AdminResourceViewStore) -> None:
        self.store = store

    async def list_runtime_workspaces(
        self,
        principal: AdminPrincipal,
        filters: AdminRuntimeWorkspaceResourceFilter | None = None,
    ) -> list[AdminRuntimeWorkspaceResourceRecord]:
        return await self.store.list_runtime_workspaces(
            org_id=principal.org_id,
            filters=filters or AdminRuntimeWorkspaceResourceFilter(),
        )


class InMemoryAdminResourceViewStore:
    def __init__(self, records: list[AdminRuntimeWorkspaceResourceRecord] | None = None) -> None:
        self.records = records

    async def list_runtime_workspaces(
        self,
        *,
        org_id: int,
        filters: AdminRuntimeWorkspaceResourceFilter,
    ) -> list[AdminRuntimeWorkspaceResourceRecord]:
        records = self.records or [
            AdminRuntimeWorkspaceResourceRecord(
                runtime_workspace_id=1,
                cloud_profile_id=1,
                org_id=org_id,
                user_id=settings.DEV_PRINCIPAL_USER_ID,
                employee_no="dev-user",
                user_name="Admin User",
                runtime_name="默认数字人",
                profile_status="active",
                binding_status="ready",
                runtime_status="ready",
                default_model_entry_id=1,
                default_model_display_name="GPT-4.1 Mini",
                session_count=2,
                run_count=5,
                active_run_count=1,
                installed_expert_count=1,
                installed_skill_count=2,
                installed_mcp_count=1,
                installed_knowledge_base_count=1,
                pending_reload=False,
                latest_error=None,
            )
        ]
        records = [record for record in records if record.org_id == org_id]
        if filters.employee_no:
            records = [record for record in records if record.employee_no == filters.employee_no]
        if filters.user_name:
            needle = filters.user_name.lower()
            records = [
                record
                for record in records
                if needle in record.user_name.lower()
                or needle in record.runtime_name.lower()
            ]
        if filters.runtime_status:
            records = [record for record in records if record.runtime_status == filters.runtime_status]
        return records


class SqlAlchemyAdminResourceViewStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_runtime_workspaces(
        self,
        *,
        org_id: int,
        filters: AdminRuntimeWorkspaceResourceFilter,
    ) -> list[AdminRuntimeWorkspaceResourceRecord]:
        conditions = [
            CloudHermesProfile.org_id == org_id,
            CloudHermesProfile.profile_type == "default_chat_agent",
        ]
        if filters.employee_no:
            conditions.append(User.user_code == filters.employee_no)
        if filters.user_name:
            conditions.append(User.name.like(f"%{filters.user_name}%"))
        if filters.runtime_status:
            conditions.append(HermesRuntimeBinding.status == filters.runtime_status)
        personal_result = await self.db.execute(
            select(CloudHermesProfile, User, HermesRuntimeBinding, ModelEntry)
            .outerjoin(
                User,
                and_(
                    User.id == CloudHermesProfile.user_id,
                    User.org_id == org_id,
                ),
            )
            .outerjoin(
                HermesRuntimeBinding,
                HermesRuntimeBinding.runtime_workspace_id == CloudHermesProfile.id,
            )
            .outerjoin(ModelEntry, ModelEntry.id == CloudHermesProfile.default_model_id)
            .where(*conditions)
            .order_by(CloudHermesProfile.user_id.asc(), CloudHermesProfile.id.asc())
        )

        records: list[AdminRuntimeWorkspaceResourceRecord] = []
        for profile, user, binding, model_entry in personal_result.all():
            runtime_workspace_id = int(profile.id)
            latest_run_error = await self._latest_run_error(org_id=org_id, runtime_workspace_id=runtime_workspace_id)
            records.append(
                AdminRuntimeWorkspaceResourceRecord(
                    runtime_workspace_id=runtime_workspace_id,
                    cloud_profile_id=runtime_workspace_id,
                    org_id=int(profile.org_id),
                    user_id=int(profile.user_id),
                    employee_no=(str(user.user_code) if user is not None and user.user_code else None),
                    user_name=(str(user.name) if user is not None else "Unknown User"),
                    runtime_name=str(profile.name),
                    profile_status=str(profile.status),
                    binding_status=str(binding.status) if binding is not None else None,
                    runtime_status=str(binding.status) if binding is not None else None,
                    default_model_entry_id=int(model_entry.id) if model_entry is not None else None,
                    default_model_display_name=str(model_entry.display_name) if model_entry is not None else None,
                    session_count=await self._count(
                        RuntimeSession,
                        org_id=org_id,
                        runtime_workspace_id=runtime_workspace_id,
                    ),
                    run_count=await self._count(RuntimeRun, org_id=org_id, runtime_workspace_id=runtime_workspace_id),
                    active_run_count=int(getattr(binding, "active_run_count", 0)) if binding is not None else 0,
                    installed_expert_count=await self._enabled_count(
                        InstalledExpertBinding,
                        org_id=org_id,
                        runtime_workspace_id=runtime_workspace_id,
                    ),
                    installed_skill_count=await self._enabled_count(
                        ProfileSkillBinding,
                        org_id=org_id,
                        runtime_workspace_id=runtime_workspace_id,
                    ),
                    installed_mcp_count=await self._enabled_count(
                        ProfileMcpBinding,
                        org_id=org_id,
                        runtime_workspace_id=runtime_workspace_id,
                    ),
                    installed_knowledge_base_count=await self._enabled_count(
                        ProfileKnowledgeBinding,
                        org_id=org_id,
                        runtime_workspace_id=runtime_workspace_id,
                    ),
                    pending_reload=bool(getattr(binding, "pending_reload", False)) if binding is not None else False,
                    latest_error=(str(binding.last_error) if binding is not None and binding.last_error else latest_run_error),
                    has_runtime_binding=binding is not None,
                    kservice_name=(
                        str(binding.knative_service)
                        if binding is not None
                        else None
                    ),
                )
            )

        application_conditions = [ApplicationAgent.org_id == org_id]
        if filters.employee_no:
            application_conditions.append(User.user_code == filters.employee_no)
        if filters.user_name:
            application_conditions.append(
                or_(
                    ApplicationAgent.name.like(f"%{filters.user_name}%"),
                    User.name.like(f"%{filters.user_name}%"),
                )
            )
        if filters.runtime_status:
            application_conditions.append(
                ApplicationAgentRuntimeBinding.status == filters.runtime_status
            )
        application_result = await self.db.execute(
            select(
                ApplicationAgent,
                User,
                ApplicationAgentRuntimeBinding,
            )
            .outerjoin(
                User,
                and_(
                    User.id == ApplicationAgent.created_by_user_id,
                    User.org_id == org_id,
                ),
            )
            .outerjoin(
                ApplicationAgentRuntimeBinding,
                ApplicationAgentRuntimeBinding.application_agent_id
                == ApplicationAgent.id,
            )
            .where(*application_conditions)
            .order_by(
                ApplicationAgent.created_by_user_id.asc(),
                ApplicationAgent.id.asc(),
            )
        )
        for agent, creator, binding in application_result.all():
            agent_id = int(agent.id)
            config = agent.config if isinstance(agent.config, dict) else {}
            model_entry = await self._model_entry(config.get("model_entry_id"))
            records.append(
                AdminRuntimeWorkspaceResourceRecord(
                    runtime_workspace_id=agent_id,
                    cloud_profile_id=None,
                    org_id=int(agent.org_id),
                    user_id=int(agent.created_by_user_id),
                    employee_no=(
                        str(creator.user_code)
                        if creator is not None and creator.user_code
                        else None
                    ),
                    user_name=(
                        str(creator.name)
                        if creator is not None
                        else "Unknown User"
                    ),
                    runtime_name=str(agent.name),
                    profile_status=str(agent.status),
                    binding_status=(
                        str(binding.status) if binding is not None else None
                    ),
                    runtime_status=(
                        str(binding.status) if binding is not None else None
                    ),
                    default_model_entry_id=(
                        int(model_entry.id)
                        if model_entry is not None
                        else None
                    ),
                    default_model_display_name=(
                        str(model_entry.display_name)
                        if model_entry is not None
                        else None
                    ),
                    session_count=await self._application_count(
                        ApplicationAgentConversation,
                        org_id=org_id,
                        application_agent_id=agent_id,
                    ),
                    run_count=await self._application_count(
                        ApplicationAgentRun,
                        org_id=org_id,
                        application_agent_id=agent_id,
                    ),
                    active_run_count=(
                        int(binding.active_run_count)
                        if binding is not None
                        else 0
                    ),
                    installed_expert_count=0,
                    installed_skill_count=self._configured_id_count(
                        config.get("skill_asset_ids")
                    ),
                    installed_mcp_count=self._configured_id_count(
                        config.get("mcp_asset_ids")
                    ),
                    installed_knowledge_base_count=(
                        await self._application_enabled_count(
                            ApplicationAgentPersonalKnowledgeBinding,
                            org_id=org_id,
                            application_agent_id=agent_id,
                        )
                    ),
                    pending_reload=(
                        str(agent.apply_status) == "applying"
                        or (
                            binding is not None
                            and int(binding.config_generation)
                            != int(agent.config_generation)
                        )
                    ),
                    latest_error=(
                        str(binding.last_error_code)
                        if binding is not None and binding.last_error_code
                        else (
                            str(agent.last_apply_error_code)
                            if agent.last_apply_error_code
                            else await self._latest_application_run_error(
                                org_id=org_id,
                                application_agent_id=agent_id,
                            )
                        )
                    ),
                    runtime_kind="application",
                    application_agent_id=agent_id,
                    has_runtime_binding=binding is not None,
                    kservice_name=(
                        str(binding.knative_service)
                        if binding is not None
                        else None
                    ),
                )
            )
        return records

    async def _model_entry(self, model_entry_id: object) -> ModelEntry | None:
        if not isinstance(model_entry_id, int) or model_entry_id <= 0:
            return None
        return (
            await self.db.execute(
                select(ModelEntry).where(ModelEntry.id == model_entry_id)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _configured_id_count(value: object) -> int:
        if not isinstance(value, list):
            return 0
        return len({item for item in value if isinstance(item, int) and item > 0})

    @staticmethod
    def _knowledge_binding_count(value: object) -> int:
        if not isinstance(value, list):
            return 0
        return len({
            item.get("knowledge_base_id")
            for item in value
            if isinstance(item, dict)
            and isinstance(item.get("knowledge_base_id"), int)
            and item["knowledge_base_id"] > 0
        })

    async def _count(self, model: type, *, org_id: int, runtime_workspace_id: int) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(model).where(
                model.org_id == org_id,
                model.runtime_workspace_id == runtime_workspace_id,
            )
        )
        return int(result.scalar_one())

    async def _enabled_count(self, model: type, *, org_id: int, runtime_workspace_id: int) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(model).where(
                model.org_id == org_id,
                model.runtime_workspace_id == runtime_workspace_id,
                model.status == "enabled",
            )
        )
        return int(result.scalar_one())

    async def _application_count(
        self,
        model: type,
        *,
        org_id: int,
        application_agent_id: int,
    ) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(model).where(
                model.org_id == org_id,
                model.application_agent_id == application_agent_id,
            )
        )
        return int(result.scalar_one())

    async def _application_enabled_count(
        self,
        model: type,
        *,
        org_id: int,
        application_agent_id: int,
    ) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(model).where(
                model.org_id == org_id,
                model.application_agent_id == application_agent_id,
                model.status == "enabled",
            )
        )
        return int(result.scalar_one())

    async def _latest_run_error(self, *, org_id: int, runtime_workspace_id: int) -> str | None:
        result = await self.db.execute(
            select(RuntimeRun.error_message)
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                RuntimeRun.error_message.is_not(None),
            )
            .order_by(RuntimeRun.created_at.desc(), RuntimeRun.id.desc())
            .limit(1)
        )
        value = result.scalar_one_or_none()
        return str(value) if value else None

    async def _latest_application_run_error(
        self,
        *,
        org_id: int,
        application_agent_id: int,
    ) -> str | None:
        result = await self.db.execute(
            select(ApplicationAgentRun.error_code)
            .where(
                ApplicationAgentRun.org_id == org_id,
                ApplicationAgentRun.application_agent_id
                == application_agent_id,
                ApplicationAgentRun.error_code.is_not(None),
            )
            .order_by(
                ApplicationAgentRun.created_at.desc(),
                ApplicationAgentRun.id.desc(),
            )
            .limit(1)
        )
        value = result.scalar_one_or_none()
        return str(value) if value else None
