from __future__ import annotations

import httpx
import pytest

from app.api.v1.admin.application_agent_admins import get_application_agent_admin_service
from app.api.v1.admin.deps import get_admin_principal
from app.api.v1.application_agent_role_deps import get_application_agent_authorization_service
from app.api.v1.application_agent_role_deps import application_agent_admin_memory_store
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.main import app
from app.services.admin_auth import AdminPrincipal
from app.services.application_agent_admin import (
    ApplicationAgentAdminCandidate,
    ApplicationAgentAdminService,
    ApplicationAgentAuthorizationService,
)
from app.services.runtime_workspace import Principal


async def _platform_admin() -> AdminPrincipal:
    return AdminPrincipal(
        org_id=1,
        user_id=settings.DEV_PRINCIPAL_USER_ID,
        roles=("admin",),
    )


async def _user_principal() -> Principal:
    return Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID)


async def _admin_service() -> ApplicationAgentAdminService:
    return ApplicationAgentAdminService(application_agent_admin_memory_store)


async def _authorization_service() -> ApplicationAgentAuthorizationService:
    return ApplicationAgentAuthorizationService(application_agent_admin_memory_store)


def _install_overrides() -> None:
    app.dependency_overrides[get_admin_principal] = _platform_admin
    app.dependency_overrides[get_principal] = _user_principal
    app.dependency_overrides[get_application_agent_admin_service] = _admin_service
    app.dependency_overrides[get_application_agent_authorization_service] = _authorization_service


def _clear_overrides() -> None:
    for dependency in (
        get_admin_principal,
        get_principal,
        get_application_agent_admin_service,
        get_application_agent_authorization_service,
    ):
        app.dependency_overrides.pop(dependency, None)


@pytest.mark.asyncio
async def test_platform_admin_can_replace_application_agent_admin_members() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_admin_memory_store.candidates[(1, "operator-2")] = (
        ApplicationAgentAdminCandidate(
            user_id=2,
            user_code="operator-2",
            name="应用运维",
            department_name="研发部",
            status="active",
            is_application_agent_admin=False,
        )
    )
    _install_overrides()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            candidates = await client.get(
                "/api/v1/admin/application-agent-admins/candidates",
                params={"q": "dev"},
            )
            assert candidates.status_code == 200
            assert candidates.json() == [
                {
                    "user_id": settings.DEV_PRINCIPAL_USER_ID,
                    "user_code": "dev-user",
                    "name": "Development User",
                    "department_name": None,
                    "status": "active",
                    "is_application_agent_admin": False,
                }
            ]

            saved = await client.put(
                "/api/v1/admin/application-agent-admins",
                json={"user_codes": ["dev-user", "operator-2"]},
            )
            assert saved.status_code == 200
            assert [member["user_code"] for member in saved.json()] == [
                "dev-user",
                "operator-2",
            ]

            members = await client.get("/api/v1/admin/application-agent-admins")
            assert members.status_code == 200
            assert [member["user_code"] for member in members.json()] == [
                "dev-user",
                "operator-2",
            ]

            access = await client.get("/api/v1/user/application-agents/access")
            assert access.status_code == 200
            assert access.json()["can_manage_application_agents"] is True

            replaced = await client.put(
                "/api/v1/admin/application-agent-admins",
                json={"user_codes": ["operator-2"]},
            )
            assert replaced.status_code == 200
            assert [member["user_code"] for member in replaced.json()] == ["operator-2"]

            access_after_revoke = await client.get("/api/v1/user/application-agents/access")
            assert access_after_revoke.status_code == 200
            assert access_after_revoke.json()["can_manage_application_agents"] is False
    finally:
        application_agent_admin_memory_store.reset()
        _clear_overrides()


@pytest.mark.asyncio
async def test_application_agent_admin_replace_is_atomic_for_unknown_candidate() -> None:
    application_agent_admin_memory_store.reset()
    _install_overrides()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial = await client.put(
                "/api/v1/admin/application-agent-admins",
                json={"user_codes": ["dev-user"]},
            )
            assert initial.status_code == 200

            response = await client.put(
                "/api/v1/admin/application-agent-admins",
                json={"user_codes": ["dev-user", "unknown-user"]},
            )
            members = await client.get("/api/v1/admin/application-agent-admins")
    finally:
        application_agent_admin_memory_store.reset()
        _clear_overrides()

    assert response.status_code == 404
    assert response.json() == {"detail": "application_agent_admin_candidate_not_found"}
    assert [member["user_code"] for member in members.json()] == ["dev-user"]


@pytest.mark.asyncio
async def test_application_agent_admin_candidates_do_not_search_by_department() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_admin_memory_store.candidates[(1, "operator-2")] = (
        ApplicationAgentAdminCandidate(
            user_id=2,
            user_code="operator-2",
            name="应用运维",
            department_name="研发部",
            status="active",
            is_application_agent_admin=False,
        )
    )
    _install_overrides()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/admin/application-agent-admins/candidates",
                params={"q": "研发部"},
            )
    finally:
        application_agent_admin_memory_store.reset()
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == []
