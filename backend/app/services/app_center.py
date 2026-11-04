from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import defer, load_only
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppCenterApp, Permission, SysDept, SysUser, User
from app.schemas.app_center import (
    AppCenterListItem,
    AppShareScope,
)
from app.services.app_center_contracts import AppCenterError
from app.services.app_center_packages import (
    build_html_package,
    require_complete_html,
    validate_static_package,
)
from app.services.data_reports import (
    DataReportPackageInvalid,
    validate_live_data_package,
)
from app.services.user_directory import COMPANY_ROOT_DEPARTMENT_IDS
from app.services.runtime_workspace import Principal


logger = logging.getLogger(__name__)


def _local_access_url(app_id: int) -> str:
    base_url = settings.PUBLIC_WEB_BASE.rstrip("/")
    return f"{base_url}/api/v1/user/data-apps/{app_id}/index.html"


def _local_version(package: bytes) -> str:
    return hashlib.sha256(package).hexdigest()[:32]


def _validate_live_data_package(package: bytes) -> None:
    try:
        validate_live_data_package(package)
    except DataReportPackageInvalid as exc:
        raise AppCenterError(
            "APP_CENTER_PACKAGE_INVALID",
            str(exc),
            status_code=400,
            error_code=-14001,
        ) from exc


class AppCenterService:
    """Application package, visibility, and self-hosted publication policy."""

    def __init__(
        self,
        db: AsyncSession,
    ) -> None:
        self.db = db

    async def create_from_html(
        self,
        *,
        principal: Principal,
        name: str,
        description: str,
        html_content: str,
        share_mode: str,
        scope: AppShareScope | None,
        idempotency_key: str | None,
        source_session_id: str | None,
        source_run_id: str | None,
    ) -> AppCenterListItem:
        require_complete_html(html_content)
        package = build_html_package(html_content)
        _validate_live_data_package(package.content)
        normalized_scope = scope if share_mode == "scope" else None
        await self._validate_scope(principal.org_id, normalized_scope)
        fingerprint = _fingerprint(
            {
                "name": name,
                "description": description,
                "share_mode": share_mode,
                "scope": _scope_payload(normalized_scope),
                "package_sha256": hashlib.sha256(package.content).hexdigest(),
                "source_session_id": source_session_id,
                "source_run_id": source_run_id,
            }
        )
        existing = await self._find_idempotent(principal, idempotency_key, fingerprint)
        if existing is not None:
            return await self._item_for_record(existing)
        await self._require_capacity(principal)
        record = AppCenterApp(
            org_id=principal.org_id,
            name=name,
            description=description,
            source_type="agent_html",
            share_mode=share_mode,
            html_content=html_content,
            package_blob=package.content,
            access_url="pending",
            demo_id="pending",
            demo_code="self-hosted",
            publish_version_id=_local_version(package.content),
            creator_user_id=principal.user_id,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_key and fingerprint,
        )
        return await self._create_record(
            principal,
            record,
            normalized_scope,
        )

    async def create_from_package(
        self,
        *,
        principal: Principal,
        name: str,
        description: str,
        package: bytes,
        share_mode: str,
        scope: AppShareScope | None,
        idempotency_key: str | None,
        source_session_id: str | None = None,
        source_run_id: str | None = None,
    ) -> AppCenterListItem:
        validate_static_package(package)
        _validate_live_data_package(package)
        normalized_scope = scope if share_mode == "scope" else None
        await self._validate_scope(principal.org_id, normalized_scope)
        fingerprint = _fingerprint(
            {
                "name": name,
                "description": description,
                "share_mode": share_mode,
                "scope": _scope_payload(normalized_scope),
                "package_sha256": hashlib.sha256(package).hexdigest(),
                "source_session_id": source_session_id,
                "source_run_id": source_run_id,
            }
        )
        existing = await self._find_idempotent(principal, idempotency_key, fingerprint)
        if existing is not None:
            return await self._item_for_record(existing)
        await self._require_capacity(principal)
        record = AppCenterApp(
            org_id=principal.org_id,
            name=name,
            description=description,
            source_type="manual_zip",
            share_mode=share_mode,
            html_content=None,
            package_blob=package,
            access_url="pending",
            demo_id="pending",
            demo_code="self-hosted",
            publish_version_id=_local_version(package),
            creator_user_id=principal.user_id,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=idempotency_key and fingerprint,
        )
        return await self._create_record(
            principal,
            record,
            normalized_scope,
        )

    async def list_for_user(self, *, principal: Principal) -> list[AppCenterListItem]:
        records = await self._visible_records(principal)
        if not records:
            return []
        records = sorted(
            records,
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )
        permissions = await self._permissions_for_ids(
            principal.org_id,
            {record.id for record in records},
        )
        creator_names = await self._creator_names(
            {record.creator_user_id for record in records}
        )
        return [
            _to_list_item(
                record,
                permissions=permissions.get(record.id, []),
                creator_name=creator_names.get(
                    record.creator_user_id,
                    f"用户 {record.creator_user_id}",
                ),
                user_id=principal.user_id,
            )
            for record in records
        ]

    async def replace_permissions(
        self,
        *,
        principal: Principal,
        app_id: int,
        scope: AppShareScope,
    ) -> AppCenterListItem:
        record = await self._require_owned(principal, app_id)
        await self._validate_scope(principal.org_id, scope)
        record.share_mode = "scope"
        await self._replace_permissions(principal.org_id, app_id, scope)
        await self._commit()
        await self.db.refresh(record)
        return await self._item_for_record(record)

    async def update_from_html(
        self,
        *,
        principal: Principal,
        app_id: int,
        html_content: str,
        name: str | None = None,
        description: str | None = None,
    ) -> AppCenterListItem:
        require_complete_html(html_content)
        package = build_html_package(html_content)
        _validate_live_data_package(package.content)
        record = await self._require_owned(principal, app_id)
        unchanged_content = (
            record.html_content == html_content
            and record.package_blob == package.content
        )
        record.package_blob = package.content
        record.html_content = html_content
        record.publish_version_id = _local_version(package.content)
        record.access_url = _local_access_url(int(record.id))
        record.demo_id = str(record.id)
        if name is not None:
            record.name = name
        if description is not None:
            record.description = description
        if unchanged_content and name is None and description is None:
            return await self._item_for_record(record)
        return await self._update_record(record)

    async def update_from_package(
        self,
        *,
        principal: Principal,
        app_id: int,
        package: bytes,
    ) -> AppCenterListItem:
        validate_static_package(package)
        _validate_live_data_package(package)
        record = await self._require_owned(principal, app_id)
        if record.package_blob == package:
            return await self._item_for_record(record)
        record.package_blob = package
        record.html_content = None
        record.publish_version_id = _local_version(package)
        record.access_url = _local_access_url(int(record.id))
        record.demo_id = str(record.id)
        return await self._update_record(record)

    async def copy(
        self,
        *,
        principal: Principal,
        app_id: int,
    ) -> AppCenterListItem:
        source = await self._require_visible(principal, app_id)
        await self._require_capacity(principal)
        creator_name = await self._creator_name(source.creator_user_id)
        source_id = source.id
        source_name = source.name
        source_description = source.description
        source_type = source.source_type
        source_html = source.html_content
        source_package = source.package_blob
        _validate_live_data_package(source_package)
        await self._commit()
        base_name = _copy_name(source_name)
        record = AppCenterApp(
            org_id=principal.org_id,
            name=base_name,
            description=source_description,
            source_type=source_type,
            share_mode="scope",
            html_content=source_html,
            package_blob=source_package,
            access_url="pending",
            demo_id="pending",
            demo_code="self-hosted",
            publish_version_id=_local_version(source_package),
            creator_user_id=principal.user_id,
            copied_from_app_id=source_id,
            copied_from_creator_name=creator_name,
            source_session_id=None,
            source_run_id=None,
        )
        return await self._create_record(principal, record, None)

    async def package(
        self,
        *,
        principal: Principal,
        app_id: int,
    ) -> tuple[str, bytes]:
        record = await self._require_visible(principal, app_id)
        return _download_filename(record.name), record.package_blob

    async def consumer_package(
        self,
        *,
        principal: Principal,
        app_id: int,
    ) -> bytes:
        """Return a package for opening an app without granting list/manage access."""
        record = await self._require_consumer_record(
            principal,
            app_id,
            include_package=True,
        )
        return record.package_blob

    async def require_consumer_access(
        self,
        *,
        principal: Principal,
        app_id: int,
    ) -> None:
        await self._require_consumer_record(
            principal,
            app_id,
            include_package=False,
        )

    async def _require_consumer_record(
        self,
        principal: Principal,
        app_id: int,
        *,
        include_package: bool,
    ) -> AppCenterApp:
        statement = select(AppCenterApp).where(
            AppCenterApp.id == app_id,
            AppCenterApp.org_id == principal.org_id,
        )
        if not include_package:
            statement = statement.options(defer(AppCenterApp.package_blob))
        record = await self.db.scalar(statement)
        if record is None:
            raise AppCenterError(
                "APP_CENTER_APP_NOT_FOUND",
                "应用不存在或已删除",
                status_code=404,
                error_code=-14003,
            )
        if (
            record.creator_user_id == principal.user_id
            or record.share_mode == "link"
            or app_id in await self._permitted_app_ids(
                principal,
                app_ids={app_id},
            )
        ):
            return record
        raise AppCenterError(
            "APP_CENTER_PERMISSION_DENIED",
            "你没有该应用的访问权限",
            status_code=403,
            error_code=-14004,
        )

    async def public_package(self, *, app_id: int) -> bytes:
        record = await self.db.scalar(
            select(AppCenterApp).where(AppCenterApp.id == app_id)
        )
        if record is None:
            raise AppCenterError(
                "APP_CENTER_APP_NOT_FOUND",
                "应用不存在或已删除",
                status_code=404,
                error_code=-14003,
            )
        return record.package_blob

    async def delete(self, *, principal: Principal, app_id: int) -> None:
        record = await self._require_owned(principal, app_id)
        await self.db.execute(
            delete(Permission).where(
                Permission.org_id == principal.org_id,
                Permission.asset_type == "app_center_app",
                Permission.asset_id == app_id,
            )
        )
        await self.db.delete(record)
        await self._commit()

    async def _create_record(
        self,
        principal: Principal,
        record: AppCenterApp,
        scope: AppShareScope | None,
    ) -> AppCenterListItem:
        try:
            self.db.add(record)
            await self.db.flush()
            record.access_url = _local_access_url(int(record.id))
            record.demo_id = str(record.id)
            if record.share_mode == "scope" and scope is not None:
                await self._replace_permissions(principal.org_id, record.id, scope)
            elif record.share_mode == "scope":
                await self._replace_permissions(
                    principal.org_id,
                    record.id,
                    AppShareScope(),
                )
            await self._commit()
        except Exception as exc:
            await self.db.rollback()
            logger.exception(
                "app_center application could not be persisted (app_id=%s)",
                record.demo_id,
            )
            raise AppCenterError(
                "APP_CENTER_OPERATION_FAILED",
                "应用保存失败，请稍后重试",
                status_code=500,
                error_code=-14007,
            ) from exc
        await self.db.refresh(record)
        return await self._item_for_record(record)

    async def _update_record(self, record: AppCenterApp) -> AppCenterListItem:
        try:
            await self._commit()
        except Exception as exc:
            await self.db.rollback()
            logger.exception(
                "app_center application could not be persisted after update "
                "(app_id=%s, demo_id=%s)",
                record.id,
                record.demo_id,
            )
            raise AppCenterError(
                "APP_CENTER_OPERATION_FAILED",
                "应用更新失败，请稍后重试",
                status_code=500,
                error_code=-14007,
            ) from exc
        await self.db.refresh(record)
        return await self._item_for_record(record)

    async def _find_idempotent(
        self,
        principal: Principal,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> AppCenterApp | None:
        if not idempotency_key:
            return None
        record = await self.db.scalar(
            select(AppCenterApp).where(
                AppCenterApp.org_id == principal.org_id,
                AppCenterApp.creator_user_id == principal.user_id,
                AppCenterApp.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            return None
        if record.idempotency_fingerprint != fingerprint:
            raise AppCenterError(
                "APP_CENTER_IDEMPOTENCY_CONFLICT",
                "幂等键已用于其它应用内容，请刷新后重试",
                status_code=409,
                error_code=-14006,
            )
        return record

    async def _require_capacity(self, principal: Principal) -> None:
        total = await self.db.scalar(
            select(func.count(AppCenterApp.id)).where(
                AppCenterApp.org_id == principal.org_id,
                AppCenterApp.creator_user_id == principal.user_id,
            )
        )
        if int(total or 0) >= settings.APP_CENTER_MAX_APPS_PER_USER:
            raise AppCenterError(
                "APP_CENTER_LIMIT_REACHED",
                "每个用户最多创建 100 个应用",
                status_code=409,
                error_code=-14005,
            )

    async def _validate_scope(self, org_id: int, scope: AppShareScope | None) -> None:
        if scope is None or scope.organization:
            return
        if scope.user_ids:
            rows = await self.db.scalars(
                select(User.id).where(
                    User.org_id == org_id,
                    User.id.in_(scope.user_ids),
                    User.status == "active",
                )
            )
            if set(rows) != set(scope.user_ids):
                raise AppCenterError(
                    "APP_CENTER_PACKAGE_INVALID",
                    "分享范围包含不可用用户",
                    status_code=400,
                    error_code=-14001,
                )
        if scope.department_ids:
            requested = {
                str(department_id) for department_id in scope.department_ids
            }
            rows = await self.db.scalars(
                select(SysDept.dept_id).where(
                    SysDept.dept_id.in_(scope.department_ids),
                    SysDept.status == 1,
                )
            )
            valid = set(rows) | set(COMPANY_ROOT_DEPARTMENT_IDS)
            if not requested.issubset(valid):
                raise AppCenterError(
                    "APP_CENTER_PACKAGE_INVALID",
                    "分享范围包含不可用部门",
                    status_code=400,
                    error_code=-14001,
                )

    async def _replace_permissions(
        self,
        org_id: int,
        app_id: int,
        scope: AppShareScope,
    ) -> None:
        await self.db.execute(
            delete(Permission).where(
                Permission.org_id == org_id,
                Permission.asset_type == "app_center_app",
                Permission.asset_id == app_id,
            )
        )
        if scope.organization:
            self.db.add(
                Permission(
                    org_id=org_id,
                    asset_type="app_center_app",
                    asset_id=app_id,
                    grantee_type="all",
                    grantee_id="*",
                    kb_role=None,
                )
            )
            return
        for user_id in scope.user_ids:
            self.db.add(
                Permission(
                    org_id=org_id,
                    asset_type="app_center_app",
                    asset_id=app_id,
                    grantee_type="user",
                    grantee_id=str(user_id),
                    kb_role=None,
                )
            )
        for department_id in scope.department_ids:
            self.db.add(
                Permission(
                    org_id=org_id,
                    asset_type="app_center_app",
                    asset_id=app_id,
                    grantee_type="department",
                    grantee_id=department_id,
                    kb_role=None,
                )
            )

    async def _visible_records(self, principal: Principal) -> list[AppCenterApp]:
        permission_ids = await self._permitted_app_ids(principal)
        clauses = [AppCenterApp.org_id == principal.org_id]
        if permission_ids:
            clauses.append(
                or_(
                    AppCenterApp.creator_user_id == principal.user_id,
                    AppCenterApp.id.in_(permission_ids),
                )
            )
        else:
            clauses.append(AppCenterApp.creator_user_id == principal.user_id)
        return list(
            await self.db.scalars(
                select(AppCenterApp)
                .options(
                    load_only(
                        AppCenterApp.id,
                        AppCenterApp.name,
                        AppCenterApp.description,
                        AppCenterApp.access_url,
                        AppCenterApp.source_type,
                        AppCenterApp.share_mode,
                        AppCenterApp.creator_user_id,
                        AppCenterApp.copied_from_app_id,
                        AppCenterApp.copied_from_creator_name,
                        AppCenterApp.source_session_id,
                        AppCenterApp.updated_at,
                        AppCenterApp.publish_version_id,
                    )
                )
                .where(*clauses)
            )
        )

    async def _permitted_app_ids(
        self,
        principal: Principal,
        *,
        app_ids: set[int] | None = None,
    ) -> set[int]:
        if app_ids is not None and not app_ids:
            return set()
        ancestors = await self._department_ancestors(principal)
        grantees = [
            (Permission.grantee_type == "all") & (Permission.grantee_id == "*"),
            (Permission.grantee_type == "user")
            & (Permission.grantee_id == str(principal.user_id)),
        ]
        if ancestors:
            grantees.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(ancestors))
            )
        clauses = [
            Permission.org_id == principal.org_id,
            Permission.asset_type == "app_center_app",
            Permission.kb_role.is_(None),
            or_(*grantees),
        ]
        if app_ids is not None:
            clauses.append(Permission.asset_id.in_(app_ids))
        rows = await self.db.scalars(
            select(Permission.asset_id).where(*clauses)
        )
        return {int(item) for item in rows}

    async def _department_ancestors(self, principal: Principal) -> frozenset[str]:
        user_code = await self.db.scalar(
            select(User.user_code).where(
                User.id == principal.user_id,
                User.org_id == principal.org_id,
                User.status == "active",
            )
        )
        if not user_code:
            return frozenset()
        directory_user = await self.db.execute(
            select(SysUser.dept_id, SysUser.com_id).where(SysUser.code == user_code)
        )
        row = directory_user.first()
        current = str(row.dept_id or "") if row else ""
        company_root = str(row.com_id or "") if row else ""
        ancestors: set[str] = set()
        if company_root:
            ancestors.add(company_root)
        while current and current not in ancestors:
            ancestors.add(current)
            current = str(
                await self.db.scalar(
                    select(SysDept.parent_id).where(
                        SysDept.dept_id == current,
                        SysDept.status == 1,
                    )
                )
                or ""
            )
        return frozenset(ancestors)

    async def _permissions_for_ids(
        self,
        org_id: int,
        app_ids: set[int],
    ) -> dict[int, list[Permission]]:
        if not app_ids:
            return {}
        rows = await self.db.scalars(
            select(Permission).where(
                Permission.org_id == org_id,
                Permission.asset_type == "app_center_app",
                Permission.asset_id.in_(app_ids),
                Permission.kb_role.is_(None),
            )
        )
        result: dict[int, list[Permission]] = {}
        for item in rows:
            result.setdefault(int(item.asset_id), []).append(item)
        return result

    async def _creator_names(self, user_ids: set[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        rows = await self.db.execute(
            select(User.id, User.name).where(User.id.in_(user_ids))
        )
        return {int(row.id): str(row.name) for row in rows}

    async def _creator_name(self, user_id: int) -> str:
        name = await self.db.scalar(select(User.name).where(User.id == user_id))
        return str(name or f"用户 {user_id}")

    async def _item_for_record(self, record: AppCenterApp) -> AppCenterListItem:
        permissions = await self._permissions_for_ids(
            record.org_id,
            {record.id},
        )
        return _to_list_item(
            record,
            permissions=permissions.get(record.id, []),
            creator_name=await self._creator_name(record.creator_user_id),
            user_id=record.creator_user_id,
        )

    async def _require_owned(
        self,
        principal: Principal,
        app_id: int,
    ) -> AppCenterApp:
        record = await self.db.scalar(
            select(AppCenterApp).where(
                AppCenterApp.id == app_id,
                AppCenterApp.org_id == principal.org_id,
                AppCenterApp.creator_user_id == principal.user_id,
            )
        )
        if record is None:
            try:
                await self._require_visible(principal, app_id)
            except AppCenterError as exc:
                if exc.code == "APP_CENTER_APP_NOT_FOUND":
                    raise
            raise AppCenterError(
                "APP_CENTER_PERMISSION_DENIED",
                "只有应用创建者可以管理该应用",
                status_code=403,
                error_code=-14004,
            )
        return record

    async def _require_visible(
        self,
        principal: Principal,
        app_id: int,
    ) -> AppCenterApp:
        record = await self.db.scalar(
            select(AppCenterApp).where(
                AppCenterApp.id == app_id,
                AppCenterApp.org_id == principal.org_id,
            )
        )
        if record is None:
            raise AppCenterError(
                "APP_CENTER_APP_NOT_FOUND",
                "应用不存在或已删除",
                status_code=404,
                error_code=-14003,
            )
        if record.creator_user_id == principal.user_id:
            return record
        if app_id in await self._permitted_app_ids(principal, app_ids={app_id}):
            return record
        raise AppCenterError(
            "APP_CENTER_PERMISSION_DENIED",
            "你没有该应用的访问权限",
            status_code=403,
            error_code=-14004,
        )

    async def _commit(self) -> None:
        await self.db.commit()


def _to_list_item(
    record: AppCenterApp,
    *,
    permissions: Sequence[Permission],
    creator_name: str,
    user_id: int,
) -> AppCenterListItem:
    scope = _scope_from_permissions(permissions)
    return AppCenterListItem(
        id=int(record.id),
        name=record.name,
        description=record.description,
        access_url=_local_access_url(int(record.id)),
        source_type=record.source_type,
        share_mode=record.share_mode,
        creator_name=creator_name,
        mine=record.creator_user_id == user_id,
        copied_from_app_id=(
            int(record.copied_from_app_id)
            if record.copied_from_app_id is not None
            else None
        ),
        copied_from_creator_name=record.copied_from_creator_name,
        source_session_id=record.source_session_id,
        updated_at=record.updated_at,
        scope_summary=_scope_summary(record.share_mode, permissions),
        publish_version_id=record.publish_version_id,
        scope=scope,
    )


def _scope_from_permissions(permissions: Sequence[Permission]) -> AppShareScope:
    user_ids = [
        int(item.grantee_id)
        for item in permissions
        if item.grantee_type == "user"
    ]
    department_ids = [
        item.grantee_id
        for item in permissions
        if item.grantee_type == "department"
    ]
    organization = any(
        item.grantee_type in {"all", "organization", "public"}
        for item in permissions
    )
    return AppShareScope(
        organization=organization,
        user_ids=sorted(set(user_ids)),
        department_ids=sorted(set(department_ids)),
    )


def _scope_summary(share_mode: str, permissions: Sequence[Permission]) -> str:
    if share_mode == "link":
        return "链接分享"
    if any(item.grantee_type in {"all", "organization", "public"} for item in permissions):
        return "全员"
    departments = sum(item.grantee_type == "department" for item in permissions)
    users = sum(item.grantee_type == "user" for item in permissions)
    if departments:
        return f"{departments} 个部门"
    if users:
        return f"{users} 人"
    return "仅自己"


def _scope_payload(scope: AppShareScope | None) -> dict[str, object]:
    if scope is None:
        return {"organization": False, "user_ids": [], "department_ids": []}
    return {
        "organization": scope.organization,
        "user_ids": scope.user_ids,
        "department_ids": scope.department_ids,
    }


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _copy_name(name: str) -> str:
    value = f"{name}-副本"
    return value[:50]


def _download_filename(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", name).strip("-") or "app"
    return f"{safe[:50]}.zip"


__all__ = [
    "AppCenterError",
    "AppCenterService",
]
