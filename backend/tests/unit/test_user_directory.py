from unittest.mock import AsyncMock

import pytest

from app.main import create_app
from app.services.runtime_workspace import Principal
from app.services.user_directory import (
    DepartmentDirectoryRecord,
    PermissionDirectoryUserRecord,
    UserDirectoryService,
    build_department_tree,
    build_organization_directory,
    build_personal_knowledge_permission_directory,
)


def _flatten(nodes):
    result = []
    for node in nodes:
        result.append(node.department_id)
        result.extend(_flatten(node.children))
    return result


def test_build_department_tree_is_stable_and_cycle_safe() -> None:
    tree = build_department_tree(
        [
            DepartmentDirectoryRecord("child", "root", "子部门", 2, "1"),
            DepartmentDirectoryRecord("root", None, "总部", 1, "1"),
            DepartmentDirectoryRecord("orphan", "missing", "孤立部门", 3, "1"),
            DepartmentDirectoryRecord("cycle-a", "cycle-b", "循环甲", 4, "1"),
            DepartmentDirectoryRecord("cycle-b", "cycle-a", "循环乙", 5, "1"),
        ]
    )

    assert [node.department_id for node in tree] == ["1"]
    assert tree[0].virtual is False
    plain_children = [node for node in tree[0].children if node.department_id != "1"]
    assert plain_children[0].department_id == "root"
    assert plain_children[0].children[0].department_id == "child"
    flattened = _flatten(tree)
    assert len(flattened) == len(set(flattened)) == 6
    assert set(flattened) == {"1", "root", "child", "orphan", "cycle-a", "cycle-b"}


def test_user_directory_routes_are_registered() -> None:
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in (route.methods or set())
    }
    assert ("/api/v1/user/directory/users/search", "GET") in routes
    assert ("/api/v1/user/directory/departments/tree", "GET") in routes
    assert ("/api/v1/user/directory/tree", "GET") in routes


def test_build_organization_directory_has_one_root_with_departments_and_users() -> None:
    tree = build_organization_directory(
        organization_id=3,
        organization_name="示例业务技术",
        departments=[
            DepartmentDirectoryRecord("root", None, "总部", 1, "1"),
            DepartmentDirectoryRecord("dev", "root", "研发中心", 2, "1"),
            DepartmentDirectoryRecord("orphan", "missing", "外地办事处", 3, "1"),
        ],
        users=[
            PermissionDirectoryUserRecord(12, "示例用户乙", "zhangyi", "dev", "1"),
            PermissionDirectoryUserRecord(13, "李明", "liming", None, None),
        ],
    )

    assert len(tree) == 1
    organization = tree[0]
    assert organization.node_type == "organization"
    assert organization.organization_id == 3
    assert organization.name == "示例业务技术"
    assert [child.department_id for child in organization.children] == ["1"]
    company = organization.children[0]
    assert company.virtual is False
    plain = [node for node in company.children if node.node_type == "department"]
    assert [node.department_id for node in plain] == ["root", "orphan"]
    dev = plain[0].children[0]
    # 双无效用户（无部门且公司未知）不再进入目录。
    assert dev.node_type == "department"
    assert dev.children[0].node_type == "user"
    assert dev.children[0].user_id == 12


def test_organization_directory_search_keeps_the_single_organization_root() -> None:
    records = [
        DepartmentDirectoryRecord("root", None, "总部", 1, "1"),
        DepartmentDirectoryRecord("dev", "root", "研发中心", 2, "1"),
    ]
    users = [PermissionDirectoryUserRecord(12, "示例用户乙", "zhangyi", "dev", "1")]

    matched_user = build_organization_directory(
        organization_id=3,
        organization_name="示例业务技术",
        departments=records,
        users=users,
        keyword="zhangyi",
    )
    matched_organization = build_organization_directory(
        organization_id=3,
        organization_name="示例业务技术",
        departments=records,
        users=users,
        keyword="示例业务",
    )
    missed = build_organization_directory(
        organization_id=3,
        organization_name="示例业务技术",
        departments=records,
        users=users,
        keyword="不存在",
    )

    assert matched_user[0].children[0].children[0].children[0].children[0].user_id == 12
    assert len(matched_organization[0].children) == 1
    assert missed == []


def test_build_personal_knowledge_permission_directory_mixes_users_and_departments() -> None:
    tree = build_personal_knowledge_permission_directory(
        [
            DepartmentDirectoryRecord("root", None, "总部", 1, "1"),
            DepartmentDirectoryRecord("dev", "root", "研发中心", 2, "1"),
        ],
        [
            PermissionDirectoryUserRecord(12, "示例用户乙", "zhangyi", "dev", "1"),
        ],
    )

    company = tree[0]
    assert company.node_type == "department"
    assert company.department_id == "1"
    root = company.children[0]
    assert root.node_type == "department"
    department = root.children[0]
    assert department.node_type == "department"
    assert department.children[0].node_type == "user"
    assert department.children[0].user_id == 12


def test_permission_directory_search_keeps_user_ancestors() -> None:
    tree = build_personal_knowledge_permission_directory(
        [
            DepartmentDirectoryRecord("root", None, "总部", 1, "1"),
            DepartmentDirectoryRecord("dev", "root", "研发中心", 2, "1"),
        ],
        [PermissionDirectoryUserRecord(12, "示例用户乙", "zhangyi", "dev", "1")],
        keyword="zhangyi",
    )

    assert tree[0].department_id == "1"
    root = tree[0].children[0]
    assert root.department_id == "root"
    assert root.children[0].department_id == "dev"
    assert root.children[0].children[0].user_id == 12


@pytest.mark.asyncio
async def test_empty_user_keyword_does_not_query_the_database() -> None:
    session = AsyncMock()
    result = await UserDirectoryService(session).search_users(
        principal=Principal(org_id=3, user_id=8),
        keyword="   ",
        limit=50,
    )

    assert result == []
    session.execute.assert_not_awaited()
