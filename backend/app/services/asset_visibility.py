from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage, KnowledgeBase, Mcp, Permission, Skill
from app.services.runtime_workspace import Principal


VISIBLE_STATUSES = {"active", "approved", "published", "enabled"}
ORG_VISIBLE_VALUES = {"organization", "org", "public"}


class AssetNotVisibleError(PermissionError):
    pass


@dataclass(frozen=True)
class AssetVisibilityRecord:
    asset_type: str
    asset_id: int
    org_id: int
    status: str
    owner_user_id: int | None = None
    visibility: str | None = "private"
    version_id: int | None = None


@dataclass(frozen=True)
class AssetPermissionRecord:
    asset_type: str
    asset_id: int
    org_id: int
    grantee_type: str
    grantee_id: str


@dataclass(frozen=True)
class VisibleAssetRef:
    asset_type: str
    asset_id: int
    version_id: int | None
    status: str
    visibility: str | None


class AssetVisibilityStore(Protocol):
    async def list_assets(
        self,
        *,
        org_id: int,
        asset_type: str,
        asset_ids: set[int] | None = None,
    ) -> list[AssetVisibilityRecord]:
        ...

    async def list_permissions(
        self,
        *,
        org_id: int,
        asset_type: str,
        asset_ids: set[int],
    ) -> list[AssetPermissionRecord]:
        ...


class AssetVisibilityService:
    def __init__(self, store: AssetVisibilityStore) -> None:
        self.store = store

    async def list_visible_assets(
        self,
        *,
        principal: Principal,
        asset_type: str,
        asset_ids: set[int] | None = None,
    ) -> list[VisibleAssetRef]:
        records = await self.store.list_assets(
            org_id=principal.org_id,
            asset_type=asset_type,
            asset_ids=asset_ids,
        )
        candidate_ids = {record.asset_id for record in records}
        permissions = []
        if candidate_ids:
            permissions = await self.store.list_permissions(
                org_id=principal.org_id,
                asset_type=asset_type,
                asset_ids=candidate_ids,
            )
        permission_index = self._permission_index(permissions)
        visible = [
            self._to_visible_ref(record)
            for record in records
            if self._is_visible(principal=principal, record=record, permission_index=permission_index)
        ]
        return sorted(visible, key=lambda item: (item.asset_type, item.asset_id))

    async def require_visible_assets(
        self,
        *,
        principal: Principal,
        asset_type: str,
        asset_ids: set[int],
    ) -> set[int]:
        if not asset_ids:
            return set()
        visible = await self.list_visible_assets(
            principal=principal,
            asset_type=asset_type,
            asset_ids=asset_ids,
        )
        visible_ids = {item.asset_id for item in visible}
        missing = sorted(asset_ids - visible_ids)
        if missing:
            raise AssetNotVisibleError(f"{asset_type} is not visible: {missing[0]}")
        return visible_ids

    @staticmethod
    def _permission_index(permissions: list[AssetPermissionRecord]) -> dict[tuple[str, int], list[AssetPermissionRecord]]:
        index: dict[tuple[str, int], list[AssetPermissionRecord]] = {}
        for permission in permissions:
            index.setdefault((permission.asset_type, permission.asset_id), []).append(permission)
        return index

    @staticmethod
    def _to_visible_ref(record: AssetVisibilityRecord) -> VisibleAssetRef:
        return VisibleAssetRef(
            asset_type=record.asset_type,
            asset_id=record.asset_id,
            version_id=record.version_id,
            status=record.status,
            visibility=record.visibility,
        )

    def _is_visible(
        self,
        *,
        principal: Principal,
        record: AssetVisibilityRecord,
        permission_index: dict[tuple[str, int], list[AssetPermissionRecord]],
    ) -> bool:
        if record.org_id != principal.org_id:
            return False
        if record.status not in VISIBLE_STATUSES:
            return False
        if record.owner_user_id == principal.user_id:
            return True
        if (record.visibility or "").lower() in ORG_VISIBLE_VALUES:
            return True
        return self._has_permission(
            principal=principal,
            permissions=permission_index.get((record.asset_type, record.asset_id), []),
        )

    @staticmethod
    def _has_permission(*, principal: Principal, permissions: list[AssetPermissionRecord]) -> bool:
        user_id = str(principal.user_id)
        org_id = str(principal.org_id)
        for permission in permissions:
            grantee_type = permission.grantee_type.lower()
            if grantee_type == "user" and permission.grantee_id == user_id:
                return True
            if grantee_type in {"org", "organization"} and permission.grantee_id == org_id:
                return True
            if grantee_type in {"all", "public"}:
                return True
        return False


class SqlAlchemyAssetVisibilityStore:
    ASSET_MODELS = {
        "expert_package": ExpertPackage,
        "skill": Skill,
        "mcp": Mcp,
        "knowledge_base": KnowledgeBase,
    }

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_assets(
        self,
        *,
        org_id: int,
        asset_type: str,
        asset_ids: set[int] | None = None,
    ) -> list[AssetVisibilityRecord]:
        model = self.ASSET_MODELS.get(asset_type)
        if model is None:
            return []
        clauses = [model.org_id == org_id]
        if asset_ids is not None:
            if not asset_ids:
                return []
            clauses.append(model.id.in_(asset_ids))
        result = await self.db.execute(select(model).where(*clauses))
        records = []
        for asset in result.scalars().all():
            records.append(
                AssetVisibilityRecord(
                    asset_type=asset_type,
                    asset_id=int(asset.id),
                    org_id=int(asset.org_id),
                    status=str(asset.status),
                    owner_user_id=int(asset.owner_user_id) if getattr(asset, "owner_user_id", None) else None,
                    visibility=getattr(asset, "visibility", None),
                    version_id=int(asset.current_version_id) if getattr(asset, "current_version_id", None) else None,
                )
            )
        return records

    async def list_permissions(
        self,
        *,
        org_id: int,
        asset_type: str,
        asset_ids: set[int],
    ) -> list[AssetPermissionRecord]:
        if not asset_ids:
            return []
        result = await self.db.execute(
            select(Permission).where(
                Permission.org_id == org_id,
                Permission.asset_type == asset_type,
                or_(Permission.asset_id == 0, Permission.asset_id.in_(asset_ids)),
            )
        )
        return [
            AssetPermissionRecord(
                asset_type=asset_type,
                asset_id=int(permission.asset_id),
                org_id=int(permission.org_id),
                grantee_type=str(permission.grantee_type),
                grantee_id=str(permission.grantee_id),
            )
            for permission in result.scalars().all()
        ]
