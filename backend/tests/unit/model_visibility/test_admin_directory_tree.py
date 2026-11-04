from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.main import app
from app.schemas.user_directory import (
    OrganizationDirectoryOrganizationNode,
    PersonalKnowledgePermissionDirectoryDepartmentNode,
    PersonalKnowledgePermissionDirectoryUserNode,
)
from app.services.admin_auth import AdminPrincipal


def test_admin_model_directory_tree_uses_admin_organization_directory(monkeypatch) -> None:
    session = object()
    service = AsyncMock()
    service.organization_directory.return_value = [
        OrganizationDirectoryOrganizationNode(
            organization_id=9,
            name="示例业务技术",
            children=[
                PersonalKnowledgePermissionDirectoryDepartmentNode(
                    department_id="dev",
                    name="研发中心",
                    children=[
                        PersonalKnowledgePermissionDirectoryUserNode(
                            user_id=12,
                            name="示例用户乙",
                            user_code="zhangyi",
                            department_id="dev",
                        )
                    ],
                ),
                PersonalKnowledgePermissionDirectoryDepartmentNode(
                    department_id="virtual:unassigned",
                    name="未归属部门",
                    virtual=True,
                    children=[
                        PersonalKnowledgePermissionDirectoryUserNode(
                            user_id=13,
                            name="李明",
                            user_code="liming",
                            department_id=None,
                        )
                    ],
                ),
            ],
        )
    ]

    async def admin_principal() -> AdminPrincipal:
        return AdminPrincipal(org_id=9, user_id=7, roles=("admin",))

    async def database():
        yield session

    monkeypatch.setattr(
        "app.api.v1.admin.model_config.UserDirectoryService",
        lambda db: service,
    )
    app.dependency_overrides[get_admin_principal] = admin_principal
    app.dependency_overrides[get_db] = database
    try:
        response = TestClient(app).get(
            "/api/v1/admin/model-config/directory/tree"
        )
    finally:
        app.dependency_overrides.pop(get_admin_principal, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == [
        {
            "node_type": "organization",
            "organization_id": 9,
            "name": "示例业务技术",
            "children": [
                {
                    "node_type": "department",
                    "department_id": "dev",
                    "name": "研发中心",
                    "parent_id": None,
                    "parent_name": None,
                    "virtual": False,
                    "children": [
                        {
                            "node_type": "user",
                            "user_id": 12,
                            "logged_in": False,
                            "name": "示例用户乙",
                            "user_code": "zhangyi",
                            "department_id": "dev",
                            "status": "active",
                        }
                    ],
                },
                {
                    "node_type": "department",
                    "department_id": "virtual:unassigned",
                    "name": "未归属部门",
                    "parent_id": None,
                    "parent_name": None,
                    "virtual": True,
                    "children": [
                        {
                            "node_type": "user",
                            "user_id": 13,
                            "logged_in": False,
                            "name": "李明",
                            "user_code": "liming",
                            "department_id": None,
                            "status": "active",
                        }
                    ],
                },
            ],
        }
    ]
    principal = service.organization_directory.await_args.kwargs["principal"]
    assert (principal.org_id, principal.user_id) == (9, 7)
    service.organization_directory.assert_awaited_once_with(
        principal=principal,
        keyword="",
    )
