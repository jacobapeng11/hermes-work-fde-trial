from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.main import app
from app.services.admin_auth import AdminPrincipal


def test_admin_expert_directory_uses_admin_org_identity(monkeypatch) -> None:
    session = object()
    service = AsyncMock()
    service.search_users.return_value = [
        {"id": 12, "name": "示例用户乙", "user_code": "zhangyi"}
    ]
    service.department_tree.return_value = [
        {
            "department_id": "dept-a",
            "name": "研发部",
            "parent_id": None,
            "parent_name": None,
            "virtual": False,
            "children": [],
        }
    ]

    async def admin_principal() -> AdminPrincipal:
        return AdminPrincipal(org_id=9, user_id=7, roles=("admin",))

    async def database():
        yield session

    monkeypatch.setattr(
        "app.api.v1.admin.experts.UserDirectoryService",
        lambda db: service,
    )
    app.dependency_overrides[get_admin_principal] = admin_principal
    app.dependency_overrides[get_db] = database
    try:
        client = TestClient(app)
        users_response = client.get(
            "/api/v1/admin/experts/directory/users/search",
            params={"keyword": "张", "limit": 20},
        )
        departments_response = client.get(
            "/api/v1/admin/experts/directory/departments/tree"
        )
    finally:
        app.dependency_overrides.pop(get_admin_principal, None)
        app.dependency_overrides.pop(get_db, None)

    assert users_response.status_code == 200
    assert users_response.json() == [
        {"id": 12, "name": "示例用户乙", "user_code": "zhangyi"}
    ]
    search_principal = service.search_users.await_args.kwargs["principal"]
    assert (search_principal.org_id, search_principal.user_id) == (9, 7)
    service.search_users.assert_awaited_once_with(
        principal=search_principal,
        keyword="张",
        limit=20,
    )

    assert departments_response.status_code == 200
    assert departments_response.json() == [
        {
            "department_id": "dept-a",
            "name": "研发部",
            "parent_id": None,
            "parent_name": None,
            "virtual": False,
            "children": [],
        }
    ]
    service.department_tree.assert_awaited_once_with()
