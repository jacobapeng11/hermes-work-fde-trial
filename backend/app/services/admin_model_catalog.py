from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelEntry, ModelProvider, ModelUpgradeLog, Permission, SysDept, SysUser, User
from app.services.admin_auth import AdminPrincipal
from app.services.model_defaults import DEFAULT_MODEL_CONTEXT_LENGTH
from app.services.provider_credentials import ProviderCredentialCodec
from app.schemas.model_runtime_capabilities import normalize_runtime_capabilities
from app.services.user_directory import COMPANY_ROOT_DEPARTMENT_IDS


SYNC_MODEL_CATALOG_RELEASE_STATUSES = frozenset({"publishing", "published", "failed"})
ASYNC_MODEL_CATALOG_RELEASE_STATUSES = frozenset({"pending"})
ALLOWED_RUNTIME_CAPABILITY_COMBINATIONS = frozenset(
    {
        ("enabled", "required"),
        ("provider_default", "not_supported"),
        ("disabled", "not_supported"),
    }
)


@dataclass(frozen=True)
class ModelVisibilityScope:
    organization: bool = True
    user_ids: list[int] = field(default_factory=list)
    department_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdminModelVisibilityUserRecord:
    id: int
    name: str
    user_code: str | None
    status: str


@dataclass(frozen=True)
class AdminModelConfigItem:
    provider_key: str
    provider_display_name: str
    provider_type: str
    api_endpoint: str
    api_key: str | None
    model_key: str
    model_display_name: str
    context_length: int | None
    tags: list[str]
    is_restricted: bool
    visibility_scope: ModelVisibilityScope | None = None
    model_status: str = "active"
    is_default: bool = False
    runtime_capabilities: dict[str, str] | None = None


@dataclass(frozen=True)
class ModelCatalogPublishIntent:
    release_id: int
    catalog_version: int
    previous_catalog_version: int | None
    config_hash: str
    status: str
    published_at: datetime
    model_entry_ids: tuple[int, ...] = ()
    provider_ids: tuple[int, ...] = ()
    reasoning_protocol_enabled_model_ids: tuple[int, ...] = ()
    reasoning_protocol_disabled_model_ids: tuple[int, ...] = ()
    restricted_model_count: int = 0
    user_grant_count: int = 0
    department_grant_count: int = 0


@dataclass(frozen=True)
class AdminModelConfigRecord:
    provider_id: int
    provider_key: str
    provider_display_name: str
    provider_type: str
    api_endpoint: str | None
    api_key_configured: bool
    provider_status: str
    provider_catalog_version: int
    model_entry_id: int
    model_key: str
    model_display_name: str
    context_length: int | None
    tags: list[str]
    is_restricted: bool
    is_default: bool
    model_status: str
    model_catalog_version: int
    runtime_capabilities: dict[str, str]
    visibility_scope: ModelVisibilityScope = field(
        default_factory=lambda: ModelVisibilityScope(organization=True)
    )
    visibility_users: list[AdminModelVisibilityUserRecord] = field(default_factory=list)


@dataclass(frozen=True)
class ModelCatalogReleaseRecord:
    release_id: int
    catalog_version: int | None
    previous_catalog_version: int | None
    config_hash: str | None
    status: str
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class AdminModelCatalogSnapshot:
    entries: list[AdminModelConfigRecord]
    latest_release: ModelCatalogReleaseRecord | None


class AdminModelCatalogError(ValueError):
    pass


class AdminModelCatalogStore(Protocol):
    async def list_catalog(self, *, org_id: int) -> AdminModelCatalogSnapshot:
        ...

    async def publish_catalog(
        self,
        *,
        org_id: int,
        operator_id: int,
        entries: list[AdminModelConfigItem],
        config_hash: str,
    ) -> ModelCatalogPublishIntent:
        ...

    async def normalize_catalog_entries(
        self,
        *,
        org_id: int,
        entries: list[AdminModelConfigItem],
    ) -> list[AdminModelConfigItem]:
        ...


class AdminModelCatalogService:
    ALLOWED_MODEL_STATUSES = frozenset({"active", "disabled"})

    def __init__(
        self,
        store: AdminModelCatalogStore,
        *,
        max_publish_attempts: int = 5,
        retry_base_delay_seconds: float = 0.05,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.max_publish_attempts = max(1, max_publish_attempts)
        self.retry_base_delay_seconds = max(0.0, retry_base_delay_seconds)
        self.sleep = sleep

    async def list_catalog(self, *, principal: AdminPrincipal) -> AdminModelCatalogSnapshot:
        return await self.store.list_catalog(org_id=principal.org_id)

    async def publish_catalog(
        self,
        *,
        principal: AdminPrincipal,
        entries: list[AdminModelConfigItem],
    ) -> ModelCatalogPublishIntent:
        if not entries:
            raise AdminModelCatalogError("at least one model configuration is required")
        entries = [
            replace(
                item,
                model_status=item.model_status.lower(),
                context_length=DEFAULT_MODEL_CONTEXT_LENGTH if item.context_length is None else item.context_length,
                runtime_capabilities=normalize_runtime_capabilities(
                    item.runtime_capabilities
                ),
                visibility_scope=self._normalize_scope(item),
                is_restricted=item.is_restricted
                if item.visibility_scope is None
                else not item.visibility_scope.organization,
            )
            for item in entries
        ]
        normalize_entries = getattr(self.store, "normalize_catalog_entries", None)
        if callable(normalize_entries):
            entries = await normalize_entries(org_id=principal.org_id, entries=entries)
        identities = {(item.provider_key, item.model_key) for item in entries}
        if len(identities) != len(entries):
            raise AdminModelCatalogError("duplicate provider/model configuration")
        for item in entries:
            model_status = item.model_status.lower()
            if model_status not in self.ALLOWED_MODEL_STATUSES:
                raise AdminModelCatalogError("model status must be active or disabled")
            if item.is_default and model_status != "active":
                raise AdminModelCatalogError("default model must be active")
            if item.provider_type != "openai_compatible":
                raise AdminModelCatalogError("only openai_compatible providers are supported")
            if not item.api_endpoint.startswith(("http://", "https://")):
                raise AdminModelCatalogError("provider endpoint must use http or https")
            if not item.provider_key.strip() or not item.model_key.strip() or not item.model_display_name.strip():
                raise AdminModelCatalogError("provider key, model key and model name are required")
            if item.context_length is None or item.context_length <= 0:
                raise AdminModelCatalogError("context length must be a positive integer")
            capabilities = normalize_runtime_capabilities(item.runtime_capabilities)
            capability_pair = (capabilities["thinking_mode"], capabilities["reasoning_replay"])
            if capability_pair not in ALLOWED_RUNTIME_CAPABILITY_COMBINATIONS:
                raise AdminModelCatalogError(
                    "runtime capabilities must use enabled/required, "
                    "provider_default/not_supported, or disabled/not_supported"
                )
        default_entries = [item for item in entries if item.is_default]
        if len(default_entries) > 1:
            raise AdminModelCatalogError("only one default model is allowed")
        config_hash = self.config_hash(entries)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_publish_attempts + 1):
            try:
                return await self.store.publish_catalog(
                    org_id=principal.org_id,
                    operator_id=principal.user_id,
                    entries=entries,
                    config_hash=config_hash,
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= self.max_publish_attempts:
                    break
                await self.sleep(self._retry_delay_seconds(attempt))
        assert last_exc is not None
        raise last_exc

    def _retry_delay_seconds(self, failed_attempt: int) -> float:
        return self.retry_base_delay_seconds * (2 ** max(0, failed_attempt - 1))

    def _normalize_scope(self, item: AdminModelConfigItem) -> ModelVisibilityScope:
        if item.visibility_scope is None:
            if item.is_restricted:
                raise AdminModelCatalogError(
                    "restricted model configuration requires visibility_scope"
                )
            return ModelVisibilityScope(organization=True)
        scope = item.visibility_scope
        user_ids = sorted({int(value) for value in scope.user_ids})
        if any(value <= 0 for value in user_ids):
            raise AdminModelCatalogError("visibility user ids must be positive")
        department_ids = sorted({
            value.strip() for value in scope.department_ids if value.strip()
        })
        if scope.organization and (user_ids or department_ids):
            raise AdminModelCatalogError(
                "organization visibility must not include users or departments"
            )
        if not scope.organization and not (user_ids or department_ids):
            raise AdminModelCatalogError(
                "custom model visibility requires at least one user or department"
            )
        return ModelVisibilityScope(
            organization=scope.organization,
            user_ids=user_ids,
            department_ids=department_ids,
        )

    @staticmethod
    def config_hash(entries: list[AdminModelConfigItem]) -> str:
        canonical = []
        for item in sorted(entries, key=lambda value: (value.provider_key, value.model_key)):
            canonical.append(
                {
                    "provider_key": item.provider_key,
                    "provider_display_name": item.provider_display_name,
                    "provider_type": item.provider_type,
                    "api_endpoint": item.api_endpoint,
                    "api_key_sha256": hashlib.sha256(item.api_key.encode()).hexdigest() if item.api_key else None,
                    "model_key": item.model_key,
                    "model_display_name": item.model_display_name,
                    "context_length": item.context_length,
                    "tags": sorted(set(item.tags)),
                    "is_restricted": item.is_restricted,
                    "visibility_scope": {
                        "organization": item.visibility_scope.organization,
                        "user_ids": item.visibility_scope.user_ids,
                        "department_ids": item.visibility_scope.department_ids,
                    },
                    "model_status": item.model_status,
                    "is_default": item.is_default,
                    "runtime_capabilities": normalize_runtime_capabilities(
                        item.runtime_capabilities
                    ),
                }
            )
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SqlAlchemyAdminModelCatalogStore:
    def __init__(self, db: AsyncSession, *, api_key_codec: ProviderCredentialCodec | None = None) -> None:
        self.db = db
        self.api_key_codec = api_key_codec or ProviderCredentialCodec()

    async def normalize_catalog_entries(
        self,
        *,
        org_id: int,
        entries: list[AdminModelConfigItem],
    ) -> list[AdminModelConfigItem]:
        scopes = [item.visibility_scope for item in entries]
        if any(scope is None for scope in scopes):
            normalized = []
            for item in entries:
                if item.visibility_scope is None:
                    if item.is_restricted:
                        raise AdminModelCatalogError(
                            "restricted model configuration requires visibility_scope"
                        )
                    item = replace(
                        item,
                        visibility_scope=ModelVisibilityScope(organization=True),
                    )
                normalized.append(item)
            entries = normalized

        requested_department_ids = sorted({
            department_id
            for item in entries
            if item.visibility_scope is not None and not item.visibility_scope.organization
            for department_id in item.visibility_scope.department_ids
        })
        requested_user_ids = sorted({
            user_id
            for item in entries
            if item.visibility_scope is not None and not item.visibility_scope.organization
            for user_id in item.visibility_scope.user_ids
        })
        if any(department_id.startswith("virtual:") for department_id in requested_department_ids):
            raise AdminModelCatalogError("invalid model visibility department")

        departments_by_id: dict[str, SysDept] = {}
        all_active_departments: list[SysDept] = []
        if requested_department_ids:
            all_active_departments = list(
                (
                    await self.db.execute(
                        select(SysDept).where(SysDept.status == 1)
                    )
                ).scalars().all()
            )
            departments_by_id = {
                str(item.dept_id): item for item in all_active_departments
            }
            valid_department_ids = set(departments_by_id) | COMPANY_ROOT_DEPARTMENT_IDS
            if not set(requested_department_ids).issubset(valid_department_ids):
                raise AdminModelCatalogError("invalid model visibility department")

        users_by_id: dict[int, User] = {}
        if requested_user_ids:
            users = (
                await self.db.execute(
                    select(User).where(
                        User.org_id == org_id,
                        User.id.in_(requested_user_ids),
                        User.status == "active",
                    )
                )
            ).scalars().all()
            users_by_id = {int(item.id): item for item in users}
            if set(users_by_id) != set(requested_user_ids):
                raise AdminModelCatalogError("invalid model visibility user")

        department_parent_ids: dict[str, str] = {
            company_root_id: "" for company_root_id in COMPANY_ROOT_DEPARTMENT_IDS
        }
        children_by_parent: dict[str, list[SysDept]] = {
            company_root_id: [] for company_root_id in COMPANY_ROOT_DEPARTMENT_IDS
        }
        for department in all_active_departments:
            department_id = str(department.dept_id)
            parent_id = str(department.parent_id or "")
            if not parent_id or parent_id not in departments_by_id:
                company_root_id = str(department.com_id or "")
                parent_id = (
                    company_root_id
                    if company_root_id in COMPANY_ROOT_DEPARTMENT_IDS
                    else ""
                )
            department_parent_ids[department_id] = parent_id
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(department)

        directory_departments_by_code: dict[str, str] = {}
        if requested_department_ids and requested_user_ids:
            directory_rows = (
                await self.db.execute(
                    select(SysUser.code, SysUser.dept_id, SysUser.com_id).where(
                        SysUser.code.in_(
                            [
                                user.user_code
                                for user in users_by_id.values()
                                if user.user_code
                            ]
                        )
                    )
                )
            ).all()
            directory_departments_by_code = {
                    str(row.code): (
                        str(row.dept_id)
                        if row.dept_id and str(row.dept_id) in departments_by_id
                        else str(row.com_id or "")
                    )
                for row in directory_rows
                if row.code
            }

        normalized_entries: list[AdminModelConfigItem] = []
        for item in entries:
            scope = item.visibility_scope
            if scope is None or scope.organization:
                normalized_entries.append(
                    replace(
                        item,
                        is_restricted=False,
                        visibility_scope=ModelVisibilityScope(organization=True),
                    )
                )
                continue
            normalized_entries.append(
                replace(
                    item,
                    is_restricted=True,
                    visibility_scope=self._normalize_entry_visibility_scope(
                        scope=scope,
                        department_parent_ids=department_parent_ids,
                        children_by_parent=children_by_parent,
                        users_by_id=users_by_id,
                        directory_departments_by_code=directory_departments_by_code,
                    ),
                )
            )
        return normalized_entries

    def _normalize_entry_visibility_scope(
        self,
        *,
        scope: ModelVisibilityScope,
        department_parent_ids: dict[str, str],
        children_by_parent: dict[str, list[SysDept]],
        users_by_id: dict[int, User],
        directory_departments_by_code: dict[str, str],
    ) -> ModelVisibilityScope:
        selected_department_ids = set(scope.department_ids)
        effective_department_ids = {
            department_id
            for department_id in selected_department_ids
            if not self._has_selected_ancestor(
                department_id,
                selected_department_ids,
                department_parent_ids,
            )
        }
        descendant_ids: set[str] = set()
        for root_id in effective_department_ids:
            stack = [root_id]
            while stack:
                current = stack.pop()
                if current in descendant_ids:
                    continue
                descendant_ids.add(current)
                stack.extend(
                    str(item.dept_id) for item in children_by_parent.get(current, [])
                )

        normalized_user_ids = [
            user_id
            for user_id in scope.user_ids
            if directory_departments_by_code.get(
                str(users_by_id[user_id].user_code or "")
            )
            not in descendant_ids
        ]
        return ModelVisibilityScope(
            organization=False,
            user_ids=normalized_user_ids,
            department_ids=sorted(effective_department_ids),
        )

    @staticmethod
    def _has_selected_ancestor(
        department_id: str,
        selected_ids: set[str],
        department_parent_ids: dict[str, str],
    ) -> bool:
        visited: set[str] = set()
        current = department_parent_ids.get(department_id, "")
        while current and current not in visited:
            if current in selected_ids:
                return True
            visited.add(current)
            current = department_parent_ids.get(current, "")
        return False

    async def list_catalog(self, *, org_id: int) -> AdminModelCatalogSnapshot:
        rows = (
            await self.db.execute(
                select(ModelProvider, ModelEntry)
                .join(ModelEntry, ModelEntry.provider_id == ModelProvider.id)
                .where(
                    ModelProvider.org_id == org_id,
                    ModelEntry.org_id == org_id,
                    ModelProvider.status.in_(("active", "disabled")),
                    ModelEntry.status.in_(("active", "disabled")),
                )
                .order_by(ModelProvider.provider_key.asc(), ModelEntry.model_key.asc())
            )
        ).all()
        latest = (
            await self.db.execute(
                select(ModelUpgradeLog)
                .where(
                    ModelUpgradeLog.org_id == org_id,
                    ModelUpgradeLog.action == "model_catalog_publish",
                )
                .order_by(ModelUpgradeLog.created_at.desc(), ModelUpgradeLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        model_ids = [int(model.id) for _provider, model in rows]
        permission_rows: list[Permission] = []
        if model_ids:
            permission_rows = list(
                (
                    await self.db.execute(
                        select(Permission).where(
                            Permission.org_id == org_id,
                            Permission.asset_type == "model_entry",
                            Permission.asset_id.in_(model_ids),
                            Permission.grantee_type.in_(("user", "department")),
                        )
                    )
                ).scalars().all()
            )
        visibility_user_ids = sorted({
            int(row.grantee_id)
            for row in permission_rows
            if row.grantee_type == "user"
        })
        visibility_users = (
            (
                await self.db.execute(
                    select(User).where(
                        User.org_id == org_id,
                        User.id.in_(visibility_user_ids),
                    )
                )
            ).scalars().all()
            if visibility_user_ids
            else []
        )
        users_by_id = {int(user.id): user for user in visibility_users}
        visibility_user_records = {
            user_id: (
                AdminModelVisibilityUserRecord(
                    id=user_id,
                    name=str(users_by_id[user_id].name),
                    user_code=users_by_id[user_id].user_code,
                    status=str(users_by_id[user_id].status),
                )
                if user_id in users_by_id
                else AdminModelVisibilityUserRecord(
                    id=user_id,
                    name=f"用户 {user_id}",
                    user_code=None,
                    status="inactive",
                )
            )
            for user_id in visibility_user_ids
        }

        return AdminModelCatalogSnapshot(
            entries=[
                AdminModelConfigRecord(
                    provider_id=int(provider.id),
                    provider_key=str(provider.provider_key),
                    provider_display_name=str(provider.display_name),
                    provider_type=str(provider.provider_type),
                    api_endpoint=provider.api_endpoint,
                    api_key_configured=bool(provider.api_key_encrypted),
                    provider_status=str(provider.status),
                    provider_catalog_version=int(provider.catalog_version or 0),
                    model_entry_id=int(model.id),
                    model_key=str(model.model_key),
                    model_display_name=str(model.display_name),
                    context_length=int(model.context_length) if model.context_length else DEFAULT_MODEL_CONTEXT_LENGTH,
                    tags=[tag.strip() for tag in (model.tags or "").split(",") if tag.strip()],
                    is_restricted=bool(model.is_restricted),
                    visibility_scope=(
                        ModelVisibilityScope(organization=True)
                        if not model.is_restricted
                        else ModelVisibilityScope(
                            organization=False,
                            user_ids=sorted(
                                int(row.grantee_id)
                                for row in permission_rows
                                if row.asset_id == int(model.id)
                                and row.grantee_type == "user"
                            ),
                            department_ids=sorted(
                                str(row.grantee_id)
                                for row in permission_rows
                                if row.asset_id == int(model.id)
                                and row.grantee_type == "department"
                            ),
                        )
                    ),
                    visibility_users=[
                        visibility_user_records[int(row.grantee_id)]
                        for row in permission_rows
                        if row.asset_id == int(model.id)
                        and row.grantee_type == "user"
                    ],
                    is_default=bool(model.is_default),
                    model_status=str(model.status),
                model_catalog_version=int(model.catalog_version or 0),
                runtime_capabilities=normalize_runtime_capabilities(
                    getattr(model, "runtime_capabilities", None)
                ),
                )
                for provider, model in rows
            ],
            latest_release=self._release_record(latest) if latest is not None else None,
        )

    async def publish_catalog(
        self,
        *,
        org_id: int,
        operator_id: int,
        entries: list[AdminModelConfigItem],
        config_hash: str,
    ) -> ModelCatalogPublishIntent:
        try:
            entries = await self.normalize_catalog_entries(org_id=org_id, entries=entries)
            latest = (
                await self.db.execute(
                    select(ModelUpgradeLog)
                    .where(
                        ModelUpgradeLog.org_id == org_id,
                        ModelUpgradeLog.action == "model_catalog_publish",
                        ModelUpgradeLog.status == "published",
                    )
                    .order_by(ModelUpgradeLog.catalog_version.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            previous_version = int(latest.catalog_version) if latest and latest.catalog_version is not None else None
            if latest is not None and latest.config_hash == config_hash and latest.catalog_version is not None:
                published_at = latest.finished_at or latest.created_at
                published_model_ids, published_provider_ids = await self._published_ids_for_version(
                    org_id=org_id,
                    catalog_version=int(latest.catalog_version),
                )
                models_by_key = await self._models_by_key(org_id=org_id)
                await self._replace_visibility_permissions(
                    org_id=org_id,
                    entries=entries,
                    models_by_key=models_by_key,
                )
                await self.db.commit()
                restricted_model_count = sum(1 for item in entries if item.is_restricted)
                user_grant_count = sum(
                    len(item.visibility_scope.user_ids)
                    for item in entries
                    if item.visibility_scope is not None and not item.visibility_scope.organization
                )
                department_grant_count = sum(
                    len(item.visibility_scope.department_ids)
                    for item in entries
                    if item.visibility_scope is not None and not item.visibility_scope.organization
                )
                return ModelCatalogPublishIntent(
                    release_id=int(latest.id),
                    catalog_version=int(latest.catalog_version),
                    previous_catalog_version=int(latest.previous_catalog_version) if latest.previous_catalog_version is not None else None,
                    config_hash=config_hash,
                    status="published",
                    published_at=published_at,
                    model_entry_ids=published_model_ids,
                    provider_ids=published_provider_ids,
                    restricted_model_count=restricted_model_count,
                    user_grant_count=user_grant_count,
                    department_grant_count=department_grant_count,
                )
            catalog_version = (previous_version or 0) + 1

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            log = ModelUpgradeLog(
                org_id=org_id,
                operator_id=operator_id,
                action="model_catalog_publish",
                scope_type="organization",
                scope_ids=[org_id],
                affected_user_count=0,
                affected_runtime_workspace_count=0,
                status="publishing",
                partial_errors=[],
                started_at=now,
                catalog_version=catalog_version,
                previous_catalog_version=previous_version,
                config_hash=config_hash,
            )
            self.db.add(log)
            await self.db.flush()

            providers = list((await self.db.execute(select(ModelProvider).where(ModelProvider.org_id == org_id))).scalars().all())
            providers_by_key = {str(provider.provider_key): provider for provider in providers}
            models = list((await self.db.execute(select(ModelEntry).where(ModelEntry.org_id == org_id))).scalars().all())
            models_by_key = {(int(model.provider_id), str(model.model_key)): model for model in models}
            included_model_ids: set[int] = set()
            included_provider_ids: set[int] = set()
            reasoning_protocol_enabled_model_ids: set[int] = set()
            reasoning_protocol_disabled_model_ids: set[int] = set()

            for item in entries:
                provider = providers_by_key.get(item.provider_key)
                if provider is None:
                    provider = ModelProvider(
                        org_id=org_id,
                        provider_key=item.provider_key,
                        display_name=item.provider_display_name,
                        provider_type=item.provider_type,
                        api_endpoint=item.api_endpoint,
                        status="active",
                        catalog_version=catalog_version,
                    )
                    self.db.add(provider)
                    await self.db.flush()
                    providers_by_key[item.provider_key] = provider
                provider.display_name = item.provider_display_name
                provider.provider_type = item.provider_type
                provider.api_endpoint = item.api_endpoint
                if item.api_key:
                    provider.api_key_encrypted = self.api_key_codec.encrypt(item.api_key)
                provider.status = "active"
                provider.catalog_version = catalog_version
                included_provider_ids.add(int(provider.id))

                model = models_by_key.get((int(provider.id), item.model_key))
                if model is None:
                    model = ModelEntry(
                        org_id=org_id,
                        provider_id=int(provider.id),
                        model_key=item.model_key,
                        display_name=item.model_display_name,
                        context_length=item.context_length,
                        tags=",".join(sorted(set(item.tags))),
                        is_restricted=item.is_restricted,
                        status="active",
                        catalog_version=catalog_version,
                    )
                    self.db.add(model)
                    await self.db.flush()
                    models_by_key[(int(provider.id), item.model_key)] = model
                model.display_name = item.model_display_name
                model.context_length = item.context_length
                model.tags = ",".join(sorted(set(item.tags)))
                model.is_restricted = item.is_restricted
                model.status = item.model_status
                model.is_default = item.is_default and item.model_status == "active"
                model.catalog_version = catalog_version
                model.runtime_capabilities = normalize_runtime_capabilities(
                    item.runtime_capabilities
                )
                included_model_ids.add(int(model.id))
                if (model.runtime_capabilities["thinking_mode"], model.runtime_capabilities["reasoning_replay"]) == ("enabled", "required"):
                    reasoning_protocol_enabled_model_ids.add(int(model.id))
                else:
                    reasoning_protocol_disabled_model_ids.add(int(model.id))

            for model in models:
                if int(model.id) not in included_model_ids:
                    model.status = "deleted"
                    model.is_default = False
            for provider in providers:
                if int(provider.id) not in included_provider_ids:
                    provider.status = "deleted"
            models_by_name: dict[tuple[str, str], ModelEntry] = {}
            for provider_key, provider in providers_by_key.items():
                for (provider_id, model_key), model in models_by_key.items():
                    if int(provider_id) == int(provider.id):
                        models_by_name[(provider_key, model_key)] = model
            await self._replace_visibility_permissions(
                org_id=org_id,
                entries=entries,
                models_by_key=models_by_name,
            )
            finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            log.status = "published"
            log.finished_at = finished_at
            await self.db.commit()
            await self.db.refresh(log)
            return ModelCatalogPublishIntent(
                release_id=int(log.id),
                catalog_version=catalog_version,
                previous_catalog_version=previous_version,
                config_hash=config_hash,
                status="published",
                published_at=finished_at,
                model_entry_ids=tuple(sorted(included_model_ids)),
                provider_ids=tuple(sorted(included_provider_ids)),
                reasoning_protocol_enabled_model_ids=tuple(sorted(reasoning_protocol_enabled_model_ids)),
                reasoning_protocol_disabled_model_ids=tuple(sorted(reasoning_protocol_disabled_model_ids)),
                restricted_model_count=sum(1 for item in entries if item.is_restricted),
                user_grant_count=sum(
                    len(item.visibility_scope.user_ids)
                    for item in entries
                    if item.visibility_scope is not None and not item.visibility_scope.organization
                ),
                department_grant_count=sum(
                    len(item.visibility_scope.department_ids)
                    for item in entries
                    if item.visibility_scope is not None and not item.visibility_scope.organization
                ),
            )
        except Exception as exc:
            await self.db.rollback()
            await self._record_failed_publish(
                org_id=org_id,
                operator_id=operator_id,
                config_hash=config_hash,
                error_message=str(exc),
            )
            raise

    async def _models_by_key(self, *, org_id: int) -> dict[tuple[str, str], ModelEntry]:
        rows = (
            await self.db.execute(
                select(ModelProvider, ModelEntry)
                .join(ModelEntry, ModelEntry.provider_id == ModelProvider.id)
                .where(
                    ModelProvider.org_id == org_id,
                    ModelEntry.org_id == org_id,
                )
            )
        ).all()
        return {
            (str(provider.provider_key), str(model.model_key)): model
            for provider, model in rows
        }

    async def _replace_visibility_permissions(
        self,
        *,
        org_id: int,
        entries: list[AdminModelConfigItem],
        models_by_key: dict[tuple[str, str], ModelEntry],
    ) -> None:
        await self.db.execute(
            delete(Permission).where(
                Permission.org_id == org_id,
                Permission.asset_type == "model_entry",
                Permission.asset_id > 0,
            )
        )
        for item in entries:
            model = models_by_key.get((item.provider_key, item.model_key))
            if model is None:
                raise AdminModelCatalogError(
                    "published model configuration is missing its model entry"
                )
            model.is_restricted = item.is_restricted
            if item.visibility_scope is None or item.visibility_scope.organization:
                continue
            for user_id in item.visibility_scope.user_ids:
                self.db.add(
                    Permission(
                        org_id=org_id,
                        asset_type="model_entry",
                        asset_id=int(model.id),
                        grantee_type="user",
                        grantee_id=str(user_id),
                        kb_role=None,
                    )
                )
            for department_id in item.visibility_scope.department_ids:
                self.db.add(
                    Permission(
                        org_id=org_id,
                        asset_type="model_entry",
                        asset_id=int(model.id),
                        grantee_type="department",
                        grantee_id=department_id,
                        kb_role=None,
                    )
                )

    async def _record_failed_publish(self, *, org_id: int, operator_id: int, config_hash: str, error_message: str) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        log = ModelUpgradeLog(
            org_id=org_id,
            operator_id=operator_id,
            action="model_catalog_publish",
            scope_type="organization",
            scope_ids=[org_id],
            affected_user_count=0,
            affected_runtime_workspace_count=0,
            status="failed",
            partial_errors=[],
            error_message=error_message[:4000],
            started_at=now,
            finished_at=now,
            config_hash=config_hash,
        )
        self.db.add(log)
        await self.db.commit()

    @staticmethod
    def _release_record(log: ModelUpgradeLog) -> ModelCatalogReleaseRecord:
        return ModelCatalogReleaseRecord(
            release_id=int(log.id),
            catalog_version=int(log.catalog_version) if log.catalog_version is not None else None,
            previous_catalog_version=int(log.previous_catalog_version) if log.previous_catalog_version is not None else None,
            config_hash=log.config_hash,
            status=str(log.status),
            error_message=log.error_message,
            started_at=log.started_at,
            finished_at=log.finished_at,
        )

    async def _published_ids_for_version(self, *, org_id: int, catalog_version: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
        models = (
            await self.db.execute(
                select(ModelEntry.id).where(
                    ModelEntry.org_id == org_id,
                    ModelEntry.catalog_version == catalog_version,
                )
            )
        ).scalars().all()
        providers = (
            await self.db.execute(
                select(ModelProvider.id).where(
                    ModelProvider.org_id == org_id,
                    ModelProvider.catalog_version == catalog_version,
                )
            )
        ).scalars().all()
        return tuple(sorted(int(value) for value in models)), tuple(sorted(int(value) for value in providers))


class InMemoryAdminModelCatalogStore:
    def __init__(self) -> None:
        self.next_release_id = 1
        self.catalog_version = 0
        self.last_hash: str | None = None
        self.last_intent: ModelCatalogPublishIntent | None = None
        self.entries: list[AdminModelConfigItem] = []

    async def list_catalog(self, *, org_id: int) -> AdminModelCatalogSnapshot:
        release = None
        if self.last_intent is not None:
            release = ModelCatalogReleaseRecord(
                release_id=self.last_intent.release_id,
                catalog_version=self.last_intent.catalog_version,
                previous_catalog_version=self.last_intent.previous_catalog_version,
                config_hash=self.last_intent.config_hash,
                status=self.last_intent.status,
                error_message=None,
                started_at=self.last_intent.published_at,
                finished_at=self.last_intent.published_at,
            )
        return AdminModelCatalogSnapshot(
            entries=[
                AdminModelConfigRecord(
                    provider_id=index + 1,
                    provider_key=item.provider_key,
                    provider_display_name=item.provider_display_name,
                    provider_type=item.provider_type,
                    api_endpoint=item.api_endpoint,
                    api_key_configured=bool(item.api_key),
                    provider_status="active",
                    provider_catalog_version=self.catalog_version,
                    model_entry_id=index + 1,
                    model_key=item.model_key,
                    model_display_name=item.model_display_name,
                    context_length=item.context_length or DEFAULT_MODEL_CONTEXT_LENGTH,
                    tags=sorted(set(item.tags)),
                    is_restricted=item.is_restricted,
                    visibility_scope=item.visibility_scope
                    or ModelVisibilityScope(organization=not item.is_restricted),
                    visibility_users=[],
                    is_default=item.is_default,
                    model_status=item.model_status,
                    model_catalog_version=self.catalog_version,
                    runtime_capabilities=normalize_runtime_capabilities(
                        item.runtime_capabilities
                    ),
                )
                for index, item in enumerate(self.entries)
            ],
            latest_release=release,
        )

    async def normalize_catalog_entries(
        self,
        *,
        org_id: int,
        entries: list[AdminModelConfigItem],
    ) -> list[AdminModelConfigItem]:
        return entries

    async def publish_catalog(self, *, org_id: int, operator_id: int, entries: list[AdminModelConfigItem], config_hash: str) -> ModelCatalogPublishIntent:
        if self.last_hash == config_hash and self.last_intent is not None:
            return self.last_intent
        previous = self.catalog_version or None
        self.catalog_version += 1
        intent = ModelCatalogPublishIntent(
            release_id=self.next_release_id,
            catalog_version=self.catalog_version,
            previous_catalog_version=previous,
            config_hash=config_hash,
            status="published",
            published_at=datetime.now(timezone.utc),
            model_entry_ids=tuple(range(1, len(entries) + 1)),
            provider_ids=tuple(sorted({index + 1 for index, _item in enumerate(entries)})),
            reasoning_protocol_enabled_model_ids=tuple(
                index + 1
                for index, item in enumerate(entries)
                if normalize_runtime_capabilities(item.runtime_capabilities) == {"thinking_mode": "enabled", "reasoning_replay": "required"}
            ),
            reasoning_protocol_disabled_model_ids=tuple(
                index + 1
                for index, item in enumerate(entries)
                if normalize_runtime_capabilities(item.runtime_capabilities) != {"thinking_mode": "enabled", "reasoning_replay": "required"}
            ),
        )
        self.next_release_id += 1
        self.last_hash = config_hash
        self.last_intent = intent
        self.entries = entries
        return intent
