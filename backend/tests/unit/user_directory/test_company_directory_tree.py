"""组织目录公司层与 HRM 部门树行为回归。

对齐旧平台合同：公司虚拟根与真实部门平级、根 ID 直接使用 com_id 且可选；
parent 为空/断链挂公司根；停用部门及后代整支剔除；公司白名单外的部门不返回；
无部门用户按公司根直挂，双向无效用户不进入目录。
"""

import pytest
from sqlalchemy import select
from app.schemas.experts import ExpertShareScope
from app.services.user_directory import (
    COMPANY_DIRECTORY_NAMES,
    DepartmentDirectoryRecord,
    PermissionDirectoryUserRecord,
    build_department_tree,
    build_personal_knowledge_permission_directory,
)


def _dept(
    dept_id: str,
    parent_id: str | None,
    name: str,
    com_id: str = "1",
    *,
    order_num: int | None = None,
    status: int = 1,
) -> DepartmentDirectoryRecord:
    return DepartmentDirectoryRecord(
        department_id=dept_id,
        parent_id=parent_id,
        name=name,
        order_num=order_num,
        com_id=com_id,
        status=status,
    )


def _flatten_ids(nodes) -> list[str]:
    result: list[str] = []
    for node in nodes:
        result.append(node.department_id)
        result.extend(_flatten_ids(node.children))
    return result


def test_department_tree_returns_parallel_company_roots() -> None:
    tree = build_department_tree([
        _dept("example-hq", None, "示例总部", "1"),
        _dept("example-dev", "example-hq", "平台部", "1"),
        _dept("branch-hq", None, "示例业务总部", "2"),
    ])

    assert [node.department_id for node in tree] == ["1", "2"]
    hermes = tree[0]
    assert hermes.name == "示例公司"
    assert hermes.virtual is False
    assert [child.department_id for child in hermes.children] == ["example-hq"]
    assert hermes.children[0].children[0].department_id == "example-dev"
    xinrui = tree[1]
    assert xinrui.name == "示例业务公司"
    assert [child.department_id for child in xinrui.children] == ["branch-hq"]


def test_department_tree_attaches_broken_roots_to_company_root() -> None:
    tree = build_department_tree([
        _dept("a", "missing-parent", "断链甲", "2"),
        _dept("b", "a", "正常子部门", "2"),
    ])

    xinrui = tree[0]
    assert xinrui.department_id == "2"
    assert [child.department_id for child in xinrui.children] == ["a"]
    assert xinrui.children[0].children[0].department_id == "b"


def test_department_tree_drops_disabled_departments_and_descendants() -> None:
    tree = build_department_tree([
        _dept("root", None, "根", "1"),
        _dept("child", "root", "停用子部门", "1", status=0),
        _dept("grandchild", "child", "停用后代", "1"),
        _dept("alive", None, "正常部门", "1"),
    ])

    flattened = _flatten_ids(tree)
    assert "child" not in flattened
    assert "grandchild" not in flattened
    assert "alive" in flattened


def test_department_tree_drops_unknown_company_departments() -> None:
    tree = build_department_tree([
        _dept("known", None, "已知部门", "1"),
        _dept("unknown", None, "未知公司部门", "99"),
    ])

    assert _flatten_ids(tree) == ["1", "known"]


def test_permission_directory_attaches_users_without_department_to_company_root() -> None:
    tree = build_personal_knowledge_permission_directory(
        [
            _dept("example-hq", None, "示例总部", "1"),
            _dept("branch-hq", None, "示例业务总部", "2"),
        ],
        [
            PermissionDirectoryUserRecord(1, "甲", "jia", "example-hq", "1"),
            PermissionDirectoryUserRecord(2, "乙", "yi", None, "1"),
            PermissionDirectoryUserRecord(3, "丙", "bing", None, "2"),
            PermissionDirectoryUserRecord(4, "丁", "ding", None, "3"),
            PermissionDirectoryUserRecord(5, "戊", "wu", "missing-dept", None),
        ],
    )

    assert [node.department_id for node in tree] == ["1", "2", "3"]
    hermes = tree[0]
    department_children = [
        child for child in hermes.children if child.node_type == "department"
    ]
    assert [child.department_id for child in department_children] == ["example-hq"]
    assert department_children[0].children[0].user_id == 1
    direct_users = [child for child in hermes.children if child.node_type == "user"]
    assert [node.user_id for node in direct_users] == [2]
    xinrui = tree[1]
    xinrui_users = [child for child in xinrui.children if child.node_type == "user"]
    assert [node.user_id for node in xinrui_users] == [3]
    pocket = tree[2]
    assert pocket.name == "示例子公司"
    assert [node.user_id for node in pocket.children] == [4]
    # 双向无效的用户（戊）不进入目录，也不再虚构“未归属部门”。
    assert all(node.department_id != "virtual:unassigned" for node in tree)
    assert 5 not in [
        node.user_id for node in pocket.children if node.node_type == "user"
    ]


def test_permission_directory_company_names_are_stable() -> None:
    assert COMPANY_DIRECTORY_NAMES == {
        "1": "示例公司",
        "2": "示例业务公司",
        "3": "示例子公司",
    }


def test_permission_directory_search_prunes_inside_company_roots() -> None:
    tree = build_personal_knowledge_permission_directory(
        [
            _dept("example-hq", None, "示例总部", "1"),
            _dept("branch-hq", None, "示例业务总部", "2"),
        ],
        [
            PermissionDirectoryUserRecord(3, "丙", "bing", None, "2"),
        ],
        keyword="bing",
    )

    assert [node.department_id for node in tree] == ["2"]
    assert tree[0].children[0].user_id == 3


@pytest.mark.parametrize("company_root", ["1", "2", "3"])
def test_share_scope_keeps_company_root_ids(company_root: str) -> None:
    scope = ExpertShareScope(
        organization=False,
        user_ids=[],
        department_ids=[company_root, "real-dept"],
    )

    assert scope.department_ids == sorted([company_root, "real-dept"])



def test_permission_directory_include_users_false_returns_department_only_tree() -> None:
    tree = build_personal_knowledge_permission_directory(
        [
            _dept("example-hq", None, "示例总部", "1"),
            _dept("example-dev", "example-hq", "平台部", "1"),
        ],
        [
            PermissionDirectoryUserRecord(1, "甲", "jia", "example-dev", "1"),
            PermissionDirectoryUserRecord(2, "乙", "yi", None, "1"),
        ],
        include_users=False,
    )

    assert [node.department_id for node in tree] == ["1"]
    users: list[object] = []
    departments: list[str] = []

    def walk(node) -> None:
        if node.node_type == "user":
            users.append(node)
        else:
            departments.append(node.department_id)
            for child in node.children:
                walk(child)

    for root in tree:
        walk(root)
    assert users == []
    assert departments == ["1", "example-hq", "example-dev"]


@pytest.mark.asyncio
async def test_provision_users_by_codes_is_idempotent() -> None:
    from typing import Any

    from sqlalchemy import BigInteger
    from sqlalchemy.dialects.mysql import LONGTEXT
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.compiler import compiles

    from app.models import Base, Organization
    from app.models.identity import SysUser, User
    from app.services.runtime_workspace import Principal
    from app.services.user_directory import UserDirectoryService

    @compiles(BigInteger, "sqlite")
    def _big_int_sqlite(_type: BigInteger, _compiler: Any, **_kwargs: Any) -> str:
        return "INTEGER"

    @compiles(LONGTEXT, "sqlite")
    def _longtext_sqlite(_type: LONGTEXT, _compiler: Any, **_kwargs: Any) -> str:
        return "TEXT"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            db.add_all([
                Organization(id=1, name="Org", code="org-1", status="active"),
                SysUser(
                    user_id="dir-1",
                    code="70001",
                    username="70001",
                    name="预授权用户",
                    status=1,
                ),
                SysUser(
                    user_id="dir-2",
                    code="70002",
                    username="70002",
                    name="离职用户",
                    status=0,
                ),
            ])
            await db.commit()
            service = UserDirectoryService(db)
            principal = Principal(org_id=1, user_id=1)

            created = await service.provision_users_by_codes(
                principal=principal,
                user_codes=["70001"],
            )
            assert [item.user_code for item in created] == ["70001"]
            assert created[0].id > 0
            await db.commit()

            again = await service.provision_users_by_codes(
                principal=principal,
                user_codes=["70001"],
            )
            assert again[0].id == created[0].id
            await db.commit()

            rows = (await db.execute(select(User.user_code))).scalars().all()
            assert list(rows) == ["70001"]

            with pytest.raises(ValueError):
                await service.provision_users_by_codes(
                    principal=principal,
                    user_codes=["70002"],
                )
    finally:
        await engine.dispose()
