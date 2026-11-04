from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Organization, SysDept, User
from app.models.identity import SysUser
from app.schemas.user_directory import (
    DepartmentTreeNode,
    OrganizationDirectoryNode,
    OrganizationDirectoryOrganizationNode,
    PersonalKnowledgePermissionDirectoryDepartmentNode,
    PersonalKnowledgePermissionDirectoryNode,
    PersonalKnowledgePermissionDirectoryUserNode,
    UserDirectorySearchItem,
)
from app.services.runtime_workspace import Principal


# Customer-specific organization IDs are supplied through backend/.env.
COMPANY_DIRECTORY_NAMES: dict[str, str] = settings.COMPANY_DIRECTORY_NAMES
# 公司虚拟根直接使用 com_id 作为稳定 dept_id（与 HRM/旧平台一致），
# 勾选公司根等价于授权该公司全部人员。
COMPANY_ROOT_DEPARTMENT_IDS: frozenset[str] = frozenset(COMPANY_DIRECTORY_NAMES)
ROOT_COMPANY_COM_ID = next(iter(COMPANY_DIRECTORY_NAMES), "1")
NON_SELECTABLE_ID_PREFIXES: tuple[str, ...] = ("virtual:",)


@dataclass(frozen=True)
class DepartmentDirectoryRecord:
    department_id: str
    parent_id: str | None
    name: str
    order_num: int | None = None
    com_id: str | None = None
    dep_grade: int | None = None
    status: int = 1
    parent_name: str | None = None


@dataclass(frozen=True)
class PermissionDirectoryUserRecord:
    # 平台 User 表 id；未开户的通讯录人员为 None（仅展示不可授权）。
    user_id: int | None
    name: str
    user_code: str | None
    department_id: str | None
    com_id: str | None = None
    # 是否完成过首次登录；预开户账号在首次登录前仍展示“未激活”。
    logged_in: bool = False


def _record_key(record: DepartmentDirectoryRecord) -> tuple[int, int, str, str]:
    grade = record.dep_grade if record.dep_grade is not None else 2**31 - 1
    order = record.order_num if record.order_num is not None else 2**31 - 1
    return (grade, order, record.name, record.department_id)


def _effective_department_records(
    records: list[DepartmentDirectoryRecord],
) -> list[DepartmentDirectoryRecord]:
    """按 HRM 规则过滤部门：status=1、公司白名单、任一祖先停用则整支剔除。

    停用祖先检查在应用层沿全量 parent 链向上遍历（含 status=0 的行），
    与旧平台 SQL 内 NOT EXISTS 递归语义一致。
    """
    by_id = {record.department_id: record for record in records}
    effective: list[DepartmentDirectoryRecord] = []
    for record in records:
        if record.status != 1:
            continue
        if record.com_id not in COMPANY_DIRECTORY_NAMES:
            continue
        seen: set[str] = set()
        current: DepartmentDirectoryRecord | None = record
        disabled = False
        while current is not None and current.department_id not in seen:
            seen.add(current.department_id)
            parent = by_id.get(current.parent_id or "")
            if parent is None:
                break
            if parent.status != 1:
                disabled = True
                break
            current = parent
        if not disabled:
            effective.append(record)
    return effective


def _department_links(
    effective: list[DepartmentDirectoryRecord],
) -> tuple[dict[str, list[DepartmentDirectoryRecord]], dict[str, list[DepartmentDirectoryRecord]]]:
    """把有效部门挂到父节点；parent 为空/断链/被剔除时挂公司虚拟根。"""
    ordered = sorted(effective, key=_record_key)
    by_id = {record.department_id: record for record in ordered}
    child_map: dict[str, list[DepartmentDirectoryRecord]] = {}
    company_children: dict[str, list[DepartmentDirectoryRecord]] = {
        com_id: [] for com_id in COMPANY_DIRECTORY_NAMES
    }
    for record in ordered:
        parent = by_id.get(record.parent_id or "")
        if parent is not None and parent.department_id != record.department_id:
            child_map.setdefault(parent.department_id, []).append(record)
        else:
            company_children[record.com_id].append(record)
    return child_map, company_children


def build_department_tree(records: list[DepartmentDirectoryRecord]) -> list[DepartmentTreeNode]:
    effective = _effective_department_records(records)
    if not effective:
        return []
    child_map, company_children = _department_links(effective)
    emitted: set[str] = set()

    def emit(record: DepartmentDirectoryRecord, lineage: frozenset[str]) -> DepartmentTreeNode | None:
        if record.department_id in emitted or record.department_id in lineage:
            return None
        emitted.add(record.department_id)
        next_lineage = lineage | {record.department_id}
        child_nodes = [
            node
            for child in child_map.get(record.department_id, [])
            if (node := emit(child, next_lineage)) is not None
        ]
        return DepartmentTreeNode(
            department_id=record.department_id,
            name=record.name or record.department_id,
            parent_id=record.parent_id,
            parent_name=record.parent_name,
            children=child_nodes,
        )

    roots: list[DepartmentTreeNode] = []
    for com_id in sorted(COMPANY_DIRECTORY_NAMES):
        children = [
            node
            for record in company_children[com_id]
            if (node := emit(record, frozenset())) is not None
        ]
        if children:
            roots.append(
                DepartmentTreeNode(
                    department_id=com_id,
                    name=COMPANY_DIRECTORY_NAMES[com_id],
                    parent_id=None,
                    parent_name=None,
                    children=children,
                )
            )
    # 环兜底：有效但未被任何入口到达的部门仍挂到其公司根，避免静默丢失。
    for com_id in sorted(COMPANY_DIRECTORY_NAMES):
        leftover = [
            record
            for record in sorted(effective, key=_record_key)
            if record.com_id == com_id and record.department_id not in emitted
        ]
        for record in leftover:
            node = emit(record, frozenset())
            if node is None:
                continue
            company_root = next((item for item in roots if item.department_id == com_id), None)
            if company_root is None:
                roots.append(
                    DepartmentTreeNode(
                        department_id=com_id,
                        name=COMPANY_DIRECTORY_NAMES[com_id],
                        parent_id=None,
                        parent_name=None,
                        children=[node],
                    )
                )
            else:
                company_root.children.append(node)
    return roots


def build_personal_knowledge_permission_directory(
    departments: list[DepartmentDirectoryRecord],
    users: list[PermissionDirectoryUserRecord],
    *,
    keyword: str = "",
    include_users: bool = True,
) -> list[PersonalKnowledgePermissionDirectoryNode]:
    """构建组织目录树。

    include_users=False 时返回纯部门树（对齐旧平台权限管理交互：部门树里
    不嵌人员，人员通过独立搜索接口选择），users 参数被忽略。
    """
    effective = _effective_department_records(departments)
    by_id = {record.department_id: record for record in effective}

    users_by_department: dict[str, list[PermissionDirectoryUserRecord]] = {}
    company_direct_users: dict[str, list[PermissionDirectoryUserRecord]] = {
        com_id: [] for com_id in COMPANY_DIRECTORY_NAMES
    }
    for user in sorted(users, key=lambda item: (item.name, item.user_code or "", item.user_id or 0)):
        if user.department_id and user.department_id in by_id:
            users_by_department.setdefault(user.department_id, []).append(user)
        elif user.com_id is not None and user.com_id in COMPANY_DIRECTORY_NAMES:
            company_direct_users[user.com_id].append(user)
        # dept 与 com 均无效的用户不进入目录（文档 9.2：只能命中全员/本人/owner 授权）。

    child_map, company_children = _department_links(effective)

    def user_node(user: PermissionDirectoryUserRecord) -> PersonalKnowledgePermissionDirectoryUserNode:
        return PersonalKnowledgePermissionDirectoryUserNode(
            user_id=user.user_id,
            logged_in=user.logged_in,
            name=user.name,
            user_code=user.user_code,
            department_id=user.department_id,
        )

    emitted: set[str] = set()

    def emit(
        record: DepartmentDirectoryRecord,
        lineage: frozenset[str],
    ) -> PersonalKnowledgePermissionDirectoryDepartmentNode | None:
        if record.department_id in emitted or record.department_id in lineage:
            return None
        emitted.add(record.department_id)
        next_lineage = lineage | {record.department_id}
        department_children = [
            node
            for child in child_map.get(record.department_id, [])
            if (node := emit(child, next_lineage)) is not None
        ]
        direct_users = (
            [user_node(user) for user in users_by_department.get(record.department_id, [])]
            if include_users
            else []
        )
        return PersonalKnowledgePermissionDirectoryDepartmentNode(
            department_id=record.department_id,
            name=record.name or record.department_id,
            parent_id=record.parent_id,
            parent_name=record.parent_name,
            children=[*department_children, *direct_users],
        )

    def company_node(
        com_id: str,
        children_nodes: list[PersonalKnowledgePermissionDirectoryNode],
    ) -> PersonalKnowledgePermissionDirectoryDepartmentNode:
        return PersonalKnowledgePermissionDirectoryDepartmentNode(
            department_id=com_id,
            name=COMPANY_DIRECTORY_NAMES[com_id],
            children=children_nodes,
        )

    result: list[PersonalKnowledgePermissionDirectoryNode] = []
    company_index: dict[str, PersonalKnowledgePermissionDirectoryDepartmentNode] = {}
    for com_id in sorted(COMPANY_DIRECTORY_NAMES):
        forest = [
            node
            for record in company_children[com_id]
            if (node := emit(record, frozenset())) is not None
        ]
        direct_users = (
            [user_node(user) for user in company_direct_users[com_id]]
            if include_users
            else []
        )
        children = [*forest, *direct_users]
        if children:
            node = company_node(com_id, children)
            result.append(node)
            company_index[com_id] = node
    # 环兜底：挂到对应公司根，避免部门静默丢失。
    for record in sorted(effective, key=_record_key):
        if record.department_id in emitted:
            continue
        node = emit(record, frozenset())
        if node is None:
            continue
        company_root = company_index.get(record.com_id)
        if company_root is None:
            company_root = company_node(record.com_id, [node])
            result.append(company_root)
            company_index[record.com_id] = company_root
        else:
            company_root.children.append(node)

    normalized = keyword.strip().casefold()
    if not normalized:
        return result

    def prune(
        node: PersonalKnowledgePermissionDirectoryNode,
    ) -> PersonalKnowledgePermissionDirectoryNode | None:
        if node.node_type == "user":
            searchable = f"{node.name} {node.user_code or ''}".casefold()
            return node if normalized in searchable else None
        matching_children = [
            matched for child in node.children if (matched := prune(child)) is not None
        ]
        if normalized not in node.name.casefold() and not matching_children:
            return None
        return node.model_copy(update={"children": matching_children})

    return [matched for node in result if (matched := prune(node)) is not None]


def build_organization_directory(
    *,
    organization_id: int,
    organization_name: str,
    departments: list[DepartmentDirectoryRecord],
    users: list[PermissionDirectoryUserRecord],
    keyword: str = "",
    include_users: bool = True,
) -> list[OrganizationDirectoryNode]:
    normalized = keyword.strip().casefold()
    organization_matches = bool(normalized and normalized in organization_name.casefold())
    children = build_personal_knowledge_permission_directory(
        departments,
        users,
        keyword="" if organization_matches else keyword,
        include_users=include_users,
    )
    if normalized and not organization_matches and not children:
        return []
    return [
        OrganizationDirectoryOrganizationNode(
            organization_id=organization_id,
            name=organization_name,
            children=children,
        )
    ]


class UserDirectoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_users(
        self,
        *,
        principal: Principal,
        keyword: str,
        limit: int = 50,
    ) -> list[UserDirectorySearchItem]:
        normalized = keyword.strip()
        if not normalized:
            return []
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        rows = (
            await self.session.execute(
                select(User.id, User.name, User.user_code)
                .where(
                    User.org_id == principal.org_id,
                    User.status == "active",
                    or_(
                        User.name.ilike(pattern, escape="\\"),
                        User.user_code.ilike(pattern, escape="\\"),
                    ),
                )
                .order_by(User.name, User.user_code, User.id)
                .limit(max(1, min(limit, 50)))
            )
        ).all()
        return [UserDirectorySearchItem(id=int(row.id), name=str(row.name), user_code=row.user_code) for row in rows]

    async def provision_users_by_codes(
        self,
        *,
        principal: Principal,
        user_codes: list[str],
    ) -> list[UserDirectorySearchItem]:
        """按通讯录工号为用户预创建平台账号（幂等）。

        用于“先授权、后登录”的场景：权限分配仍保存平台 user.id；
        用户首次 SSO 登录时 sso_identity 会按 user_code 复用本行，
        预分配权限随即生效。已存在的账号直接返回，不重复创建。
        """
        normalized = sorted({str(code).strip() for code in user_codes if str(code).strip()})
        if not normalized:
            return []
        directory_rows = (
            await self.session.execute(
                select(SysUser).where(
                    SysUser.code.in_(normalized),
                    SysUser.status == 1,
                )
            )
        ).scalars().all()
        directory_by_code: dict[str, SysUser] = {}
        for row in directory_rows:
            code = str(row.code or "").strip()
            if code:
                directory_by_code.setdefault(code, row)
        existing_rows = (
            await self.session.execute(
                select(User).where(
                    User.org_id == principal.org_id,
                    User.user_code.in_(normalized),
                )
            )
        ).scalars().all()
        users_by_code: dict[str, User] = {
            str(row.user_code): row for row in existing_rows if row.user_code
        }
        for code in normalized:
            if code in users_by_code:
                continue
            directory_user = directory_by_code.get(code)
            if directory_user is None:
                raise ValueError(f"directory user not found: {code}")
            user = User(
                org_id=principal.org_id,
                user_code=code,
                name=(
                    str(directory_user.name or directory_user.username or code).strip()
                    or code
                ),
                email=str(directory_user.email) if directory_user.email else None,
                phone=str(directory_user.mobile) if directory_user.mobile else None,
                hrm_user_id=str(directory_user.user_id),
                status="active",
            )
            self.session.add(user)
            users_by_code[code] = user
        await self.session.flush()
        return [
            UserDirectorySearchItem(
                id=int(user.id),
                name=str(user.name),
                user_code=str(user.user_code) if user.user_code else None,
            )
            for code in normalized
            if (user := users_by_code.get(code)) is not None and user.id is not None
        ]

    async def department_tree(self) -> list[DepartmentTreeNode]:
        rows = (
            await self.session.execute(
                select(
                    SysDept.dept_id,
                    SysDept.parent_id,
                    SysDept.parent_name,
                    SysDept.name,
                    SysDept.order_num,
                    SysDept.com_id,
                    SysDept.dep_grade,
                    SysDept.status,
                )
            )
        ).all()
        return build_department_tree(
            [
                DepartmentDirectoryRecord(
                    department_id=str(row.dept_id),
                    parent_id=str(row.parent_id) if row.parent_id is not None else None,
                    name=str(row.name or row.dept_id),
                    order_num=row.order_num,
                    com_id=str(row.com_id) if row.com_id is not None else None,
                    dep_grade=row.dep_grade,
                    status=int(row.status),
                    parent_name=str(row.parent_name) if row.parent_name else None,
                )
                for row in rows
            ]
        )

    async def personal_knowledge_permission_directory(
        self,
        *,
        principal: Principal,
        keyword: str = "",
        include_users: bool = True,
    ) -> list[PersonalKnowledgePermissionDirectoryNode]:
        department_rows = (
            await self.session.execute(
                select(
                    SysDept.dept_id,
                    SysDept.parent_id,
                    SysDept.parent_name,
                    SysDept.name,
                    SysDept.order_num,
                    SysDept.com_id,
                    SysDept.dep_grade,
                    SysDept.status,
                )
            )
        ).all()
        # 人员按 sys_user 全量通讯录展示（在职）；平台 User 表仅用于
        # 标记可授权身份——同一 org 内按 user_code 关联且 active 的
        # 登录用户才有平台 user_id，未开户人员展示但不可勾选授权。
        user_rows = (
            await self.session.execute(
                select(
                    SysUser.code,
                    SysUser.name,
                    SysUser.dept_id,
                    SysUser.com_id,
                    User.id.label("platform_user_id"),
                    User.last_login_at.label("last_login_at"),
                ).outerjoin(
                    User,
                    (User.user_code == SysUser.code)
                    & (User.org_id == principal.org_id)
                    & (User.status == "active"),
                ).where(SysUser.status == 1)
            )
        ).all()
        return build_personal_knowledge_permission_directory(
            [
                DepartmentDirectoryRecord(
                    department_id=str(row.dept_id),
                    parent_id=str(row.parent_id) if row.parent_id is not None else None,
                    name=str(row.name or row.dept_id),
                    order_num=row.order_num,
                    com_id=str(row.com_id) if row.com_id is not None else None,
                    dep_grade=row.dep_grade,
                    status=int(row.status),
                    parent_name=str(row.parent_name) if row.parent_name else None,
                )
                for row in department_rows
            ],
            [
                PermissionDirectoryUserRecord(
                    user_id=int(row.platform_user_id) if row.platform_user_id is not None else None,
                    logged_in=row.last_login_at is not None,
                    name=str(row.name),
                    user_code=str(row.code) if row.code else None,
                    department_id=str(row.dept_id) if row.dept_id is not None else None,
                    com_id=str(row.com_id) if row.com_id is not None else None,
                )
                for row in user_rows
            ],
            keyword=keyword,
            include_users=include_users,
        )

    async def organization_directory(
        self,
        *,
        principal: Principal,
        keyword: str = "",
    ) -> list[OrganizationDirectoryNode]:
        organization_name = (
            await self.session.execute(
                select(Organization.name).where(Organization.id == principal.org_id)
            )
        ).scalar_one_or_none()
        normalized_name = str(organization_name or "当前组织")
        normalized_keyword = keyword.strip()
        child_keyword = (
            ""
            if normalized_keyword and normalized_keyword.casefold() in normalized_name.casefold()
            else normalized_keyword
        )
        children = await self.personal_knowledge_permission_directory(
            principal=principal,
            keyword=child_keyword,
        )
        if normalized_keyword and child_keyword and not children:
            return []
        return [
            OrganizationDirectoryOrganizationNode(
                organization_id=principal.org_id,
                name=normalized_name,
                children=children,
            )
        ]
