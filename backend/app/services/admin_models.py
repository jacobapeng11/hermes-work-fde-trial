from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelEntry, ModelProvider, ModelUpgradeLog
from app.services.admin_auth import AdminPrincipal
from app.services.model_defaults import DEFAULT_MODEL_CONTEXT_LENGTH


class AdminModelGovernanceError(ValueError):
    pass


@dataclass(frozen=True)
class AdminModelRecord:
    model_entry_id: int
    model_key: str
    display_name: str
    provider_id: int
    provider_key: str
    provider_display_name: str
    status: str
    is_default: bool
    is_restricted: bool
    context_length: int | None
    tags: list[str]
    org_id: int = 1


@dataclass(frozen=True)
class AdminModelUpdateIntent:
    model_entry_id: int
    status: str
    is_default: bool
    upgrade_log_id: int | None
    affected_runtime_workspace_count: int


class AdminModelGovernanceStore(Protocol):
    async def list_models(self, *, org_id: int) -> list[AdminModelRecord]:
        ...

    async def set_model_status(
        self,
        *,
        org_id: int,
        operator_id: int,
        model_entry_id: int,
        status: str,
    ) -> AdminModelUpdateIntent:
        ...

    async def set_default_model(
        self,
        *,
        org_id: int,
        operator_id: int,
        model_entry_id: int,
    ) -> AdminModelUpdateIntent:
        ...


class AdminModelGovernanceService:
    ALLOWED_STATUSES = {"active", "disabled"}

    def __init__(self, store: AdminModelGovernanceStore) -> None:
        self.store = store

    async def list_models(self, principal: AdminPrincipal) -> list[AdminModelRecord]:
        return await self.store.list_models(org_id=principal.org_id)

    async def set_model_status(
        self,
        *,
        principal: AdminPrincipal,
        model_entry_id: int,
        status: str,
    ) -> AdminModelUpdateIntent:
        normalized = status.lower()
        if normalized not in self.ALLOWED_STATUSES:
            raise AdminModelGovernanceError("model status must be active or disabled")
        return await self.store.set_model_status(
            org_id=principal.org_id,
            operator_id=principal.user_id,
            model_entry_id=model_entry_id,
            status=normalized,
        )

    async def set_default_model(
        self,
        *,
        principal: AdminPrincipal,
        model_entry_id: int,
    ) -> AdminModelUpdateIntent:
        return await self.store.set_default_model(
            org_id=principal.org_id,
            operator_id=principal.user_id,
            model_entry_id=model_entry_id,
        )


class InMemoryAdminModelGovernanceStore:
    def __init__(self) -> None:
        self.models = {
            1: AdminModelRecord(
                model_entry_id=1,
                model_key="gpt-4.1-mini",
                display_name="GPT-4.1 Mini",
                provider_id=1,
                provider_key="platform-openai",
                provider_display_name="Platform OpenAI",
                status="active",
                is_default=True,
                is_restricted=False,
                context_length=128000,
                tags=["chat", "fast"],
            ),
            2: AdminModelRecord(
                model_entry_id=2,
                model_key="gpt-4.1",
                display_name="GPT-4.1",
                provider_id=1,
                provider_key="platform-openai",
                provider_display_name="Platform OpenAI",
                status="active",
                is_default=False,
                is_restricted=True,
                context_length=128000,
                tags=["chat"],
            ),
        }
        self.next_log_id = 1

    async def list_models(self, *, org_id: int) -> list[AdminModelRecord]:
        return [model for model in self.models.values() if model.org_id == org_id]

    async def set_model_status(self, *, org_id: int, operator_id: int, model_entry_id: int, status: str) -> AdminModelUpdateIntent:
        model = self.models.get(model_entry_id)
        if model is None or model.org_id != org_id:
            raise AdminModelGovernanceError("model entry not found")
        updated = AdminModelRecord(**{**model.__dict__, "status": status})
        self.models[model_entry_id] = updated
        log_id = self._next_log()
        return AdminModelUpdateIntent(
            model_entry_id=model_entry_id,
            status=status,
            is_default=updated.is_default,
            upgrade_log_id=log_id,
            affected_runtime_workspace_count=0,
        )

    async def set_default_model(self, *, org_id: int, operator_id: int, model_entry_id: int) -> AdminModelUpdateIntent:
        model = self.models.get(model_entry_id)
        if model is None or model.org_id != org_id:
            raise AdminModelGovernanceError("model entry not found")
        if model.status != "active":
            raise AdminModelGovernanceError("default model must be active")
        for item in list(self.models.values()):
            if item.org_id == org_id:
                self.models[item.model_entry_id] = AdminModelRecord(
                    **{**item.__dict__, "is_default": item.model_entry_id == model_entry_id}
                )
        log_id = self._next_log()
        return AdminModelUpdateIntent(
            model_entry_id=model_entry_id,
            status="active",
            is_default=True,
            upgrade_log_id=log_id,
            affected_runtime_workspace_count=0,
        )

    def _next_log(self) -> int:
        value = self.next_log_id
        self.next_log_id += 1
        return value


class SqlAlchemyAdminModelGovernanceStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_models(self, *, org_id: int) -> list[AdminModelRecord]:
        result = await self.db.execute(
            select(ModelEntry, ModelProvider)
            .join(ModelProvider, ModelProvider.id == ModelEntry.provider_id)
            .where(
                ModelEntry.org_id == org_id,
                ModelProvider.org_id == org_id,
                ModelEntry.status != "deleted",
                ModelProvider.status != "deleted",
            )
            .order_by(ModelEntry.is_default.desc(), ModelEntry.id.asc())
        )
        return [self._record(entry, provider) for entry, provider in result.all()]

    async def set_model_status(
        self,
        *,
        org_id: int,
        operator_id: int,
        model_entry_id: int,
        status: str,
    ) -> AdminModelUpdateIntent:
        entry = await self._get_entry(org_id=org_id, model_entry_id=model_entry_id)
        entry.status = status
        if status != "active":
            entry.is_default = False
        log = self._upgrade_log(
            org_id=org_id,
            operator_id=operator_id,
            action=f"model_{status}",
            source_model_entry_id=model_entry_id,
            target_model_entry_id=None,
        )
        self.db.add(log)
        await self.db.commit()
        await self.db.refresh(log)
        return AdminModelUpdateIntent(
            model_entry_id=int(entry.id),
            status=str(entry.status),
            is_default=bool(entry.is_default),
            upgrade_log_id=int(log.id),
            affected_runtime_workspace_count=int(log.affected_runtime_workspace_count),
        )

    async def set_default_model(
        self,
        *,
        org_id: int,
        operator_id: int,
        model_entry_id: int,
    ) -> AdminModelUpdateIntent:
        entry = await self._get_entry(org_id=org_id, model_entry_id=model_entry_id)
        if entry.status != "active":
            raise AdminModelGovernanceError("default model must be active")
        await self.db.execute(update(ModelEntry).where(ModelEntry.org_id == org_id).values(is_default=False))
        entry.is_default = True
        log = self._upgrade_log(
            org_id=org_id,
            operator_id=operator_id,
            action="set_default_model",
            source_model_entry_id=None,
            target_model_entry_id=model_entry_id,
        )
        self.db.add(log)
        await self.db.commit()
        await self.db.refresh(log)
        return AdminModelUpdateIntent(
            model_entry_id=int(entry.id),
            status=str(entry.status),
            is_default=True,
            upgrade_log_id=int(log.id),
            affected_runtime_workspace_count=int(log.affected_runtime_workspace_count),
        )

    async def _get_entry(self, *, org_id: int, model_entry_id: int) -> ModelEntry:
        entry = (
            await self.db.execute(
                select(ModelEntry).where(
                    ModelEntry.org_id == org_id,
                    ModelEntry.id == model_entry_id,
                    ModelEntry.status != "deleted",
                )
            )
        ).scalar_one_or_none()
        if entry is None:
            raise AdminModelGovernanceError("model entry not found")
        return entry

    @staticmethod
    def _record(entry: ModelEntry, provider: ModelProvider) -> AdminModelRecord:
        return AdminModelRecord(
            model_entry_id=int(entry.id),
            model_key=str(entry.model_key),
            display_name=str(entry.display_name),
            provider_id=int(provider.id),
            provider_key=str(provider.provider_key),
            provider_display_name=str(provider.display_name),
            status=str(entry.status),
            is_default=bool(entry.is_default),
            is_restricted=bool(entry.is_restricted),
            context_length=int(entry.context_length) if entry.context_length else DEFAULT_MODEL_CONTEXT_LENGTH,
            tags=[tag.strip() for tag in (entry.tags or "").split(",") if tag.strip()],
            org_id=int(entry.org_id),
        )

    @staticmethod
    def _upgrade_log(
        *,
        org_id: int,
        operator_id: int,
        action: str,
        source_model_entry_id: int | None,
        target_model_entry_id: int | None,
    ) -> ModelUpgradeLog:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return ModelUpgradeLog(
            org_id=org_id,
            operator_id=operator_id,
            action=action,
            source_model_entry_id=source_model_entry_id,
            target_model_entry_id=target_model_entry_id,
            scope_type="organization",
            scope_ids=[org_id],
            affected_user_count=0,
            affected_runtime_workspace_count=0,
            status="completed",
            partial_errors=[],
            started_at=now,
            finished_at=now,
        )
