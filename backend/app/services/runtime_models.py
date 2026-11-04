from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Protocol, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from redis.exceptions import RedisError

from app.models import (
    ModelEntry,
    ModelProvider,
    ModelUpgradeLog,
    Permission,
    RuntimeRun,
    RuntimeSession,
    SysDept,
    SysUser,
    User,
)
from app.services.model_defaults import DEFAULT_MODEL_CONTEXT_LENGTH
from app.schemas.model_runtime_capabilities import normalize_runtime_capabilities
from app.services.runtime_workspace import Principal
from app.services.runtime_sessions import RuntimeSessionDeletedError
from app.services.model_catalog_cache import (
    ModelCatalogCacheCircuitOpenError,
    ModelCatalogCacheFillThrottledError,
    RedisModelCatalogCache,
)


@dataclass(frozen=True)
class RuntimeModelRecord:
    model_entry_id: int
    model_key: str
    display_name: str
    provider_id: int
    provider_display_name: str
    context_length: int | None = None
    tags: list[str] | None = None
    is_default: bool = False
    runtime_capabilities: dict[str, str] | None = None

    @property
    def supports_vision(self) -> bool:
        return (
            normalize_runtime_capabilities(self.runtime_capabilities).get("vision")
            == "native"
        )


@dataclass(frozen=True)
class ModelVisibilityGrantee:
    user_id: int
    department_ids: frozenset[str]
    fingerprint: str


async def resolve_model_visibility_grantee(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
) -> ModelVisibilityGrantee:
    """Resolve a user's department ancestor chain with bounded batch reads."""

    current_department = (
        select(SysUser.dept_id)
        .select_from(User)
        .outerjoin(SysUser, SysUser.code == User.user_code)
        .where(
            User.org_id == org_id,
            User.id == user_id,
            User.status == "active",
        )
    ).scalar_subquery()
    current_company = (
        select(SysUser.com_id)
        .select_from(User)
        .outerjoin(SysUser, SysUser.code == User.user_code)
        .where(
            User.org_id == org_id,
            User.id == user_id,
            User.status == "active",
        )
    ).scalar_subquery()
    department_ids: set[str] = set()
    department_rows = (
        await db.execute(
            select(
                SysDept.dept_id,
                SysDept.parent_id,
                current_department.label("current_department"),
                current_company.label("current_company"),
            ).where(SysDept.status == 1)
        )
    ).all()
    if department_rows:
        company_root = str(department_rows[0][3] or "")
        if company_root:
            department_ids.add(company_root)
        current = str(department_rows[0][2] or "")
        parents = {
            str(department_id): str(parent_id) if parent_id else ""
            for department_id, parent_id, *_ in department_rows
        }
        if current:
            while current and current not in department_ids and current in parents:
                department_ids.add(current)
                current = parents[current]
    fingerprint = hashlib.sha256(
        "\n".join(sorted(department_ids)).encode("utf-8")
    ).hexdigest()
    return ModelVisibilityGrantee(
        user_id=user_id,
        department_ids=department_ids,
        fingerprint=fingerprint,
    )


@dataclass(frozen=True)
class DefaultModelUpdateIntent:
    runtime_workspace_id: int
    model_entry_id: int
    projection_scope: str
    pending_reload: bool


@dataclass(frozen=True)
class RuntimeSessionModelState:
    runtime_workspace_id: int
    runtime_session_id: str
    current_model_entry_id: int | None
    next_model_entry_id: int | None
    active_run_count: int


@dataclass(frozen=True)
class SessionModelSelectionIntent:
    runtime_workspace_id: int
    runtime_session_id: str
    model_entry_id: int
    applies_to: str
    active_run_count: int


class RuntimeModelNotVisibleError(PermissionError):
    pass


class RuntimeSessionModelSwitchNotAllowedError(RuntimeError):
    pass


class RuntimeTurnModelNotVisibleError(RuntimeModelNotVisibleError):
    pass


class RuntimeTurnModelUnavailableError(RuntimeError):
    pass


def resolve_default_model_entry_id(
    models: Sequence[RuntimeModelRecord],
) -> int | None:
    """Return the visible admin default, or a stable visible fallback."""
    if not models:
        return None
    defaults = [
        model.model_entry_id for model in models if model.is_default
    ]
    return min(
        defaults
        or [model.model_entry_id for model in models]
    )


def resolve_runtime_turn_model_entry_id(
    *,
    requested_model_entry_id: int | None,
    session_model_entry_id: int | None,
    workspace_default_model_entry_id: int | None,
    visible_models: Sequence[RuntimeModelRecord],
) -> int:
    """Resolve one Turn model without database or transport side effects."""

    visible_model_ids = {
        model.model_entry_id for model in visible_models
    }
    explicit_model_entry_id = next(
        (
            int(model_entry_id)
            for model_entry_id in (
                requested_model_entry_id,
                session_model_entry_id,
                workspace_default_model_entry_id,
            )
            if model_entry_id is not None
        ),
        None,
    )
    if explicit_model_entry_id is not None:
        if explicit_model_entry_id not in visible_model_ids:
            raise RuntimeTurnModelNotVisibleError(
                "selected model entry is not visible"
            )
        return explicit_model_entry_id

    fallback_model_entry_id = resolve_default_model_entry_id(
        visible_models
    )
    if fallback_model_entry_id is None:
        raise RuntimeTurnModelUnavailableError(
            "no visible model entry is available"
        )
    return fallback_model_entry_id


class RuntimeModelStore(Protocol):
    async def list_visible_models(self, *, org_id: int, user_id: int) -> list[RuntimeModelRecord]:
        ...

    async def set_default_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        model_entry_id: int,
    ) -> DefaultModelUpdateIntent:
        ...

    async def get_session_status(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str | None:
        ...

    async def get_session_model_state(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionModelState | None:
        ...

    async def set_next_session_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        model_entry_id: int,
    ) -> SessionModelSelectionIntent:
        ...


class RuntimeModelService:
    def __init__(self, store: RuntimeModelStore) -> None:
        self.store = store

    async def list_models(self, principal: Principal) -> list[RuntimeModelRecord]:
        return await self.store.list_visible_models(org_id=principal.org_id, user_id=principal.user_id)

    async def set_default_model(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        model_entry_id: int,
    ) -> DefaultModelUpdateIntent:
        visible_model_ids = {
            model.model_entry_id for model in await self.store.list_visible_models(
                org_id=principal.org_id,
                user_id=principal.user_id,
            )
        }
        if model_entry_id not in visible_model_ids:
            raise RuntimeModelNotVisibleError("model entry is not visible to this user")

        return await self.store.set_default_model(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            model_entry_id=model_entry_id,
        )

    async def select_session_model(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
        model_entry_id: int,
    ) -> SessionModelSelectionIntent:
        visible_model_ids = {
            model.model_entry_id for model in await self.store.list_visible_models(
                org_id=principal.org_id,
                user_id=principal.user_id,
            )
        }
        if model_entry_id not in visible_model_ids:
            raise RuntimeModelNotVisibleError("model entry is not visible to this user")

        status_loader = getattr(self.store, "get_session_status", None)
        if callable(status_loader) and await status_loader(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        ) == "expert_deleted":
            raise RuntimeSessionDeletedError(RuntimeSessionDeletedError.code)

        state = await self.store.get_session_model_state(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if state is not None and state.active_run_count > 0:
            raise RuntimeSessionModelSwitchNotAllowedError(
                "cannot switch session model while a run is active"
            )

        return await self.store.set_next_session_model(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            model_entry_id=model_entry_id,
        )


class SqlAlchemyRuntimeModelStore:
    """Read and persist user-visible model/session state from the center DB."""

    def __init__(self, db: AsyncSession, cache: RedisModelCatalogCache | None = None) -> None:
        self.db = db
        self.cache = cache

    async def list_visible_models(self, *, org_id: int, user_id: int) -> list[RuntimeModelRecord]:
        if self.cache is not None:
            try:
                grantee = await resolve_model_visibility_grantee(
                    self.db,
                    org_id=org_id,
                    user_id=user_id,
                )
                payload = await self.cache.get_or_build(
                    org_id=org_id,
                    user_id=user_id,
                    visibility_fingerprint=grantee.fingerprint,
                    loader=lambda: self._list_visible_model_payload(
                        org_id=org_id,
                        user_id=user_id,
                        grantee=grantee,
                    ),
                )
            except (ModelCatalogCacheFillThrottledError, ModelCatalogCacheCircuitOpenError):
                raise
            except RedisError:
                # Redis is an optimization.  A Redis outage must not make the
                # center database unavailable, but a cold-cache DB failure is
                # still allowed to fail closed at the API boundary.
                return await self._list_visible_models_from_db_with_grantee(
                    org_id=org_id,
                    user_id=user_id,
                    grantee=grantee,
                )
            return [self._record_from_payload(item) for item in payload]
        return await self._list_visible_models_from_db(org_id=org_id, user_id=user_id)

    async def _list_visible_models_from_db(self, *, org_id: int, user_id: int) -> list[RuntimeModelRecord]:
        return await self._list_visible_models_from_db_with_grantee(
            org_id=org_id,
            user_id=user_id,
            grantee=None,
        )

    async def _list_visible_models_from_db_with_grantee(
        self,
        *,
        org_id: int,
        user_id: int,
        grantee: ModelVisibilityGrantee | None,
    ) -> list[RuntimeModelRecord]:
        if grantee is None:
            grantee = await resolve_model_visibility_grantee(
                self.db,
                org_id=org_id,
                user_id=user_id,
            )
        grantee_filters = [
            (Permission.grantee_type == "all") & (Permission.grantee_id == "*"),
            (Permission.grantee_type == "user")
            & (Permission.grantee_id == str(user_id)),
        ]
        if grantee.department_ids:
            grantee_filters.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(grantee.department_ids))
            )
        permission_exists = select(Permission.id).where(
            Permission.org_id == org_id,
            Permission.asset_type == "model_entry",
            Permission.asset_id == ModelEntry.id,
            or_(*grantee_filters),
        ).exists()
        published_release_exists = select(ModelUpgradeLog.id).where(
            ModelUpgradeLog.org_id == org_id,
            ModelUpgradeLog.action == "model_catalog_publish",
            ModelUpgradeLog.status == "published",
        ).exists()
        published_catalog_version = select(func.max(ModelUpgradeLog.catalog_version)).where(
            ModelUpgradeLog.org_id == org_id,
            ModelUpgradeLog.action == "model_catalog_publish",
            ModelUpgradeLog.status == "published",
        ).scalar_subquery()
        result = await self.db.execute(
            select(ModelEntry, ModelProvider)
            .join(ModelProvider, ModelProvider.id == ModelEntry.provider_id)
            .where(
                ModelEntry.org_id == org_id,
                ModelProvider.org_id == org_id,
                ModelEntry.status == "active",
                ModelProvider.status == "active",
                or_(ModelEntry.is_restricted.is_(False), permission_exists),
                or_(
                    ~published_release_exists,
                    (
                        (ModelEntry.catalog_version == published_catalog_version)
                        & (ModelProvider.catalog_version == published_catalog_version)
                    ),
                ),
            )
            .order_by(ModelEntry.id.asc())
        )
        return [self._record(entry, provider) for entry, provider in result.all()]

    async def _list_visible_model_payload(
        self,
        *,
        org_id: int,
        user_id: int,
        grantee: ModelVisibilityGrantee | None = None,
    ) -> list[dict]:
        return [
            self._record_payload(record)
            for record in await self._list_visible_models_from_db_with_grantee(
                org_id=org_id,
                user_id=user_id,
                grantee=grantee,
            )
        ]

    async def prewarm_visible_models(self, *, org_id: int, user_id: int) -> bool:
        if self.cache is None:
            await self._list_visible_model_payload(org_id=org_id, user_id=user_id)
            return True
        grantee = await resolve_model_visibility_grantee(
            self.db,
            org_id=org_id,
            user_id=user_id,
        )
        return await self.cache.prewarm_catalog(
            org_id=org_id,
            user_id=user_id,
            visibility_fingerprint=grantee.fingerprint,
            loader=lambda: self._list_visible_model_payload(
                org_id=org_id,
                user_id=user_id,
                grantee=grantee,
            ),
        )

    async def set_default_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        model_entry_id: int,
    ) -> DefaultModelUpdateIntent:
        raise RuntimeModelNotVisibleError("personal default model is not supported")

    async def get_session_status(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str | None:
        return await self.db.scalar(
            select(RuntimeSession.status).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
            )
        )

    async def get_session_model_state(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionModelState | None:
        result = await self.db.execute(
            select(RuntimeSession).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status == "active",
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            return None
        active_run_count = await self._active_run_count(session_id=int(session.id))
        selected_model_id = int(session.session_model_entry_id) if session.session_model_entry_id else None
        return RuntimeSessionModelState(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            current_model_entry_id=selected_model_id,
            next_model_entry_id=selected_model_id,
            active_run_count=active_run_count,
        )

    async def set_next_session_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        model_entry_id: int,
    ) -> SessionModelSelectionIntent:
        result = await self.db.execute(
            select(RuntimeSession)
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status == "active",
            )
            .with_for_update()
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise RuntimeModelNotVisibleError("runtime session is not available")
        active_run_count = await self._active_run_count(session_id=int(session.id))
        if active_run_count > 0:
            raise RuntimeSessionModelSwitchNotAllowedError(
                "cannot switch session model while a run is active"
            )
        previous_model_id = session.session_model_entry_id
        session.session_model_entry_id = model_entry_id
        events = list(session.model_switch_events or [])
        events.append({"from": previous_model_id, "to": model_entry_id})
        session.model_switch_events = events[-20:]
        await self.db.commit()
        return SessionModelSelectionIntent(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            model_entry_id=model_entry_id,
            applies_to="next_run",
            active_run_count=0,
        )

    async def _active_run_count(self, *, session_id: int) -> int:
        result = await self.db.execute(
            select(func.count(RuntimeRun.id)).where(
                RuntimeRun.session_id == session_id,
                RuntimeRun.status.in_(("accepted", "pending", "running")),
            )
        )
        return int(result.scalar_one())

    @staticmethod
    def _record(entry: ModelEntry, provider: ModelProvider) -> RuntimeModelRecord:
        return RuntimeModelRecord(
            model_entry_id=int(entry.id),
            model_key=str(entry.model_key),
            display_name=str(entry.display_name),
            provider_id=int(provider.id),
            provider_display_name=str(provider.display_name),
            context_length=int(entry.context_length) if entry.context_length else DEFAULT_MODEL_CONTEXT_LENGTH,
            tags=[tag.strip() for tag in (entry.tags or "").split(",") if tag.strip()],
            is_default=bool(getattr(entry, "is_default", False)),
            runtime_capabilities=normalize_runtime_capabilities(
                getattr(entry, "runtime_capabilities", None)
            ),
        )

    @staticmethod
    def _record_payload(record: RuntimeModelRecord) -> dict:
        return {
            "model_entry_id": record.model_entry_id,
            "model_key": record.model_key,
            "display_name": record.display_name,
            "provider_id": record.provider_id,
            "provider_display_name": record.provider_display_name,
            "context_length": record.context_length,
            "tags": record.tags,
            "is_default": record.is_default,
            "runtime_capabilities": record.runtime_capabilities,
        }

    @staticmethod
    def _record_from_payload(payload: dict) -> RuntimeModelRecord:
        return RuntimeModelRecord(
            model_entry_id=int(payload["model_entry_id"]),
            model_key=str(payload["model_key"]),
            display_name=str(payload["display_name"]),
            provider_id=int(payload["provider_id"]),
            provider_display_name=str(payload["provider_display_name"]),
            context_length=(
                int(payload["context_length"])
                if payload.get("context_length") is not None
                else DEFAULT_MODEL_CONTEXT_LENGTH
            ),
            tags=[str(tag) for tag in payload.get("tags") or []],
            is_default=bool(payload.get("is_default", False)),
            runtime_capabilities=(
                normalize_runtime_capabilities(payload.get("runtime_capabilities"))
                if payload.get("runtime_capabilities") is not None
                else None
            ),
        )
