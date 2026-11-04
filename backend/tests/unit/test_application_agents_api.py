from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import app.api.v1.user.application_agents as application_agents_api

from app.api.v1.application_agent_role_deps import application_agent_admin_memory_store
from app.api.v1.application_agent_role_deps import get_application_agent_authorization_service
from app.api.v1.user.application_agents import (
    application_agent_memory_service,
    application_agent_skill_memory_service,
    application_agent_studio_invocation_memory_service,
    get_application_agent_runtime_manager,
    get_application_agent_service,
    _is_market_skill_only_update,
    _is_settings_only_update,
)
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.main import app
from app.schemas.application_agent import (
    ApplicationAgentCreateRequest,
    ApplicationAgentSummary,
    ApplicationAgentUpdateRequest,
)
from app.services.application_agent_admin import ApplicationAgentAuthorizationService
from app.services.application_agents import ApplicationAgentError
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
)
from app.services.runtime_workspace import Principal


async def _principal() -> Principal:
    return Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID)


async def _authorization_service() -> ApplicationAgentAuthorizationService:
    return ApplicationAgentAuthorizationService(application_agent_admin_memory_store)


async def _agent_service():
    return application_agent_memory_service


def _skill_zip(name: str = "private-ticket") -> bytes:
    payload = BytesIO()
    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: 应用专用 Skill\n---\n",
        )
    return payload.getvalue()


def test_application_agent_contract_has_no_description_field() -> None:
    for schema in (
        ApplicationAgentCreateRequest,
        ApplicationAgentUpdateRequest,
        ApplicationAgentSummary,
    ):
        assert "description" not in schema.model_fields


@pytest.mark.asyncio
async def test_viewer_cannot_read_personal_knowledge_candidate_metadata() -> None:
    class ViewerRejectedCatalogService:
        async def catalog(self, **_kwargs: object) -> object:
            raise ApplicationAgentError(
                "application_agent_owner_required",
                status_code=403,
            )

    response = Response()
    with pytest.raises(HTTPException) as caught:
        await application_agents_api.list_application_agent_personal_knowledge(
            agent_id=31,
            response=response,
            principal=Principal(org_id=1, user_id=8),
            service=ViewerRejectedCatalogService(),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == "application_agent_owner_required"
    assert response.headers["Cache-Control"] == "private, no-store"


class TrackingRuntimeManager:
    def __init__(self) -> None:
        self.apply_scopes: list[str] = []
        self.availability_calls: list[tuple[int, int]] = []

    async def ensure_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
        apply_scope: str = "full",
    ) -> None:
        del org_id, agent_id
        self.apply_scopes.append(apply_scope)

    async def ensure_runtime_available(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None:
        self.availability_calls.append((org_id, agent_id))

    async def delete_runtime(self, *, org_id: int, agent_id: int) -> None:
        del org_id, agent_id


@pytest.mark.asyncio
async def test_application_studio_refresh_prepares_knowledge_before_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    target = SimpleNamespace(
        runtime_session_id="studio-31-7-session",
        api_server_key="runtime-secret",
    )

    class AgentService:
        async def get_owned_agent(self, **_kwargs: object) -> object:
            return SimpleNamespace(id=31)

    class GrantService:
        def __init__(self, _db: object) -> None:
            return None

        async def application_target(self, **_kwargs: object) -> object:
            calls.append("target")
            return target

        def issue_session_grant(self, **_kwargs: object) -> str:
            calls.append("grant")
            return "session-grant"

    class PreparedService:
        async def prepare_application_agent_studio(
            self,
            **kwargs: object,
        ) -> None:
            assert kwargs == {
                "application_agent_id": 31,
                "subject_user_id": 7,
                "runtime_session_id": "studio-31-7-session",
                "runtime_token": "runtime-secret",
            }
            calls.append("knowledge")

    monkeypatch.setattr(
        application_agents_api,
        "RuntimeEdgeGrantService",
        GrantService,
    )
    monkeypatch.setattr(
        application_agents_api,
        "shared_runtime_prepared_knowledge_service",
        lambda: PreparedService(),
    )

    response = (
        await application_agents_api.refresh_application_agent_runtime_edge_grant(
            agent_id=31,
            payload=application_agents_api.RuntimeEdgeGrantRequest(
                runtime_workspace_id=31,
                runtime_session_id="studio-31-7-session",
            ),
            principal=Principal(org_id=1, user_id=7),
            service=AgentService(),
            db=object(),  # type: ignore[arg-type]
        )
    )

    assert response == "session-grant"
    assert calls == ["target", "knowledge", "grant"]


@pytest.mark.asyncio
async def test_application_studio_prepare_fails_closed_when_knowledge_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = False
    target = SimpleNamespace(
        runtime_session_id="studio-31-7-session",
        api_server_key="runtime-secret",
    )

    class AgentService:
        async def get_owned_agent(self, **_kwargs: object) -> object:
            return SimpleNamespace(id=31)

    class RuntimeManager:
        async def ensure_runtime_available(self, **_kwargs: object) -> None:
            return None

    class GrantService:
        def __init__(self, _db: object) -> None:
            return None

        async def application_target(self, **_kwargs: object) -> object:
            return target

        def issue_bootstrap(self, **_kwargs: object) -> str:
            nonlocal issued
            issued = True
            return "bootstrap"

    class PreparedService:
        async def prepare_application_agent_studio(
            self,
            **_kwargs: object,
        ) -> None:
            raise PreparedKnowledgeStoreUnavailable("redis unavailable")

    monkeypatch.setattr(
        application_agents_api,
        "RuntimeEdgeGrantService",
        GrantService,
    )
    monkeypatch.setattr(
        application_agents_api,
        "shared_runtime_prepared_knowledge_service",
        lambda: PreparedService(),
    )

    with pytest.raises(HTTPException) as caught:
        await application_agents_api.prepare_application_agent_runtime_edge(
            agent_id=31,
            payload=application_agents_api.RuntimeEdgeGrantRequest(
                runtime_workspace_id=31,
                runtime_session_id="studio-31-7-session",
            ),
            request=Request({"type": "http", "headers": []}),
            principal=Principal(org_id=1, user_id=7),
            service=AgentService(),
            runtime_manager=RuntimeManager(),
            db=object(),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == "application_studio_knowledge_unavailable"
    assert issued is False


@pytest.mark.asyncio
async def test_application_studio_warmup_checks_availability_without_config_apply() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    created = await application_agent_memory_service.create_agent(
        principal=Principal(
            org_id=1,
            user_id=settings.DEV_PRINCIPAL_USER_ID,
        ),
        payload=ApplicationAgentCreateRequest(
            slug="availability-only-warmup",
            name="可用性预热",
        ),
    )
    assert created.config.get("_initial_skill_policy_version") == 1
    application_agent_memory_service.agents[
        created.application_agent_id
    ] = replace(
        created,
        runtime_status="ready",
        applied_generation=created.config_generation,
        apply_status="failed",
    )
    runtime = TrackingRuntimeManager()

    async def runtime_manager() -> TrackingRuntimeManager:
        return runtime

    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    app.dependency_overrides[
        get_application_agent_runtime_manager
    ] = runtime_manager
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}/studio/warmup"
            )
        assert response.status_code == 200
        assert runtime.availability_calls == [
            (1, created.application_agent_id)
        ]
        assert runtime.apply_scopes == []
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
            get_application_agent_runtime_manager,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()


@pytest.mark.asyncio
async def test_application_studio_backend_stream_route_is_retired() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    application_agent_studio_invocation_memory_service.reset()
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    created = await application_agent_memory_service.create_agent(
        principal=Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID),
        payload=ApplicationAgentCreateRequest(
            slug="timing-agent",
            name="Timing Agent",
        ),
    )
    async def studio_principal(_request: Request) -> Principal:
        return Principal(
            org_id=1,
            user_id=settings.DEV_PRINCIPAL_USER_ID,
        )

    app.dependency_overrides[get_principal] = studio_principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}/studio/chat/stream",
                json={
                    "input": "direct Runtime Edge only",
                    "request_id": "studio-web-test-request",
                },
            )
        assert response.status_code == 404
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()
        application_agent_studio_invocation_memory_service.reset()


@pytest.mark.asyncio
async def test_market_skill_only_update_scope_is_server_derived() -> None:
    application_agent_memory_service.reset()
    before = await application_agent_memory_service.create_agent(
        principal=Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID),
        payload=ApplicationAgentCreateRequest(
            slug="skill-scope",
            name="Skill Scope",
        ),
    )
    after = replace(
        before,
        config={
            **before.config,
            "skill_asset_ids": [7],
        },
        config_generation=before.config_generation + 1,
    )

    assert _is_market_skill_only_update(before, after)
    assert not _is_market_skill_only_update(
        before,
        replace(
            after,
            config={
                **after.config,
                "mcp_asset_ids": [3],
            },
        ),
    )
    assert not _is_market_skill_only_update(
        before,
        replace(after, name="Renamed"),
    )
    application_agent_memory_service.reset()


@pytest.mark.asyncio
async def test_market_skill_update_routes_to_skill_hot_reload() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    created = await application_agent_memory_service.create_agent(
        principal=Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID),
        payload=ApplicationAgentCreateRequest(
            slug="market-skill-hot-reload",
            name="市场 Skill 热更新",
        ),
    )
    application_agent_memory_service.agents[
        created.application_agent_id
    ] = replace(
        created,
        runtime_status="ready",
        applied_generation=created.config_generation,
        apply_status="applied",
    )
    runtime = TrackingRuntimeManager()

    async def runtime_manager() -> TrackingRuntimeManager:
        return runtime

    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    app.dependency_overrides[
        get_application_agent_runtime_manager
    ] = runtime_manager
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}",
                json={
                    "name": created.name,
                    "avatar_url": created.avatar_url,
                    "config": {
                        **created.config,
                        "skill_asset_ids": [7],
                    },
                    "expected_config_generation": (
                        created.config_generation
                    ),
                },
            )
            assert response.status_code == 200
            assert runtime.apply_scopes == ["skills_only"]
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
            get_application_agent_runtime_manager,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()


@pytest.mark.asyncio
async def test_settings_update_routes_to_online_apply() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    created = await application_agent_memory_service.create_agent(
        principal=Principal(
            org_id=1,
            user_id=settings.DEV_PRINCIPAL_USER_ID,
        ),
        payload=ApplicationAgentCreateRequest(
            slug="settings-online-apply",
            name="配置在线生效",
        ),
    )
    application_agent_memory_service.agents[
        created.application_agent_id
    ] = replace(
        created,
        runtime_status="ready",
        applied_generation=created.config_generation,
        apply_status="applied",
    )
    runtime = TrackingRuntimeManager()

    async def runtime_manager() -> TrackingRuntimeManager:
        return runtime

    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    app.dependency_overrides[
        get_application_agent_runtime_manager
    ] = runtime_manager
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}",
                json={
                    "name": created.name,
                    "avatar_url": created.avatar_url,
                    "config": {
                        **created.config,
                        "system_prompt": "下一条消息生效",
                    },
                    "expected_config_generation": (
                        created.config_generation
                    ),
                },
            )
            assert response.status_code == 200
            assert runtime.apply_scopes == ["settings_only"]
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
            get_application_agent_runtime_manager,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()


def test_settings_only_update_scope_is_server_derived() -> None:
    before = SimpleNamespace(
        name="Agent",
        avatar_url=None,
        config={
            "system_prompt": "old",
            "model_entry_id": None,
            "memory_enabled": True,
            "skill_asset_ids": [],
        },
    )
    after = SimpleNamespace(
        name=before.name,
        avatar_url=before.avatar_url,
        config={
            **before.config,
            "system_prompt": "new",
        },
    )

    assert _is_settings_only_update(before, after)
    assert not _is_settings_only_update(
        before,
        SimpleNamespace(
            name=after.name,
            avatar_url=after.avatar_url,
            config={
                **after.config,
                "mcp_asset_ids": [3],
            },
        ),
    )


@pytest.mark.asyncio
async def test_only_application_agent_admin_can_create_configure_and_publish() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    application_agent_studio_invocation_memory_service.reset()
    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[get_application_agent_authorization_service] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.get("/api/v1/user/application-agents")
            assert denied.status_code == 403
            assert denied.json() == {"detail": "application_agent_admin_required"}

            await application_agent_admin_memory_store.replace(
                org_id=1,
                user_codes=["dev-user"],
                actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
            )
            created = await client.post(
                "/api/v1/user/application-agents",
                json={
                    "slug": "ticket-assistant",
                    "name": "工单助手",
                    "config": {
                        "system_prompt": "只处理工单问题。",
                        "memory_enabled": True,
                    },
                },
            )
            assert created.status_code == 201
            agent_id = created.json()["application_agent_id"]
            assert "description" not in created.json()
            assert created.json()["status"] == "unpublished"
            assert created.json()["runtime_status"] == "absent"

            studio_workspace = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/warmup"
            )
            assert studio_workspace.status_code == 200
            assert studio_workspace.json()["runtime_status"] == "ready"

            created_session = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/sessions",
                json={"title": "新调试会话", "source_type": "web"},
            )
            assert created_session.status_code == 201
            managed_studio_session_id = created_session.json()[
                "runtime_session_id"
            ]
            listed_sessions = await client.get(
                f"/api/v1/user/application-agents/{agent_id}/studio/sessions"
            )
            assert listed_sessions.status_code == 200
            assert managed_studio_session_id in {
                item["runtime_session_id"]
                for item in listed_sessions.json()
            }
            prewarmed = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/sessions/"
                f"{managed_studio_session_id}/prewarm"
            )
            assert prewarmed.status_code == 404

            streamed = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/chat/stream",
                json={
                    "input": "SSE 调试",
                    "runtime_session_id": managed_studio_session_id,
                },
            )
            assert streamed.status_code == 404

            studio = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/responses",
                json={"input": "调试未发布 Agent"},
            )
            assert studio.status_code == 404

            publish_without_client = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/publish"
            )
            assert publish_without_client.status_code == 409
            assert publish_without_client.json() == {"detail": "application_agent_client_required"}

            new_client = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/clients",
                json={
                    "name": "工单系统",
                    "allow_user_delegation": True,
                    "allow_service_invocation": False,
                    "scopes": ["responses:create"],
                },
            )
            assert new_client.status_code == 201
            assert new_client.json()["client_secret"].startswith("hca_")
            assert new_client.json()["auth_mode"] == "delegated_user"
            application_client_id = new_client.json()[
                "application_client_id"
            ]

            invalid_client = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/clients",
                json={
                    "name": "错误双模式",
                    "allow_user_delegation": True,
                    "allow_service_invocation": True,
                    "scopes": ["responses:create"],
                },
            )
            assert invalid_client.status_code == 422

            clients = await client.get(f"/api/v1/user/application-agents/{agent_id}/clients")
            assert clients.status_code == 200
            assert "client_secret" not in clients.json()[0]

            published = await client.post(f"/api/v1/user/application-agents/{agent_id}/publish")
            assert published.status_code == 200
            assert published.json()["status"] == "published"
            assert published.json()["runtime_status"] == "ready"

            disabled = await client.post(f"/api/v1/user/application-agents/{agent_id}/disable")
            assert disabled.status_code == 200
            assert disabled.json()["status"] == "disabled"
            assert disabled.json()["runtime_status"] == "stopped"
            assert disabled.json()["lifecycle_status"] == "active"

            resumed = await client.post(f"/api/v1/user/application-agents/{agent_id}/resume")
            assert resumed.status_code == 200
            assert resumed.json()["status"] == "published"

            deleted_client = await client.delete(
                f"/api/v1/user/application-agents/{agent_id}/clients/"
                f"{application_client_id}"
            )
            assert deleted_client.status_code == 204
            assert (
                await client.get(
                    f"/api/v1/user/application-agents/{agent_id}/clients"
                )
            ).json() == []
            assert (
                await client.get(
                    f"/api/v1/user/application-agents/{agent_id}"
                )
            ).json()["status"] == "published"

            deleted = await client.delete(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert deleted.status_code == 202
            deleted_detail = await client.get(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert deleted_detail.status_code == 200
            assert deleted_detail.json()["lifecycle_status"] == "deleting"
            assert (
                await client.get("/api/v1/user/application-agents")
            ).json()[0]["lifecycle_status"] == "deleting"
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()
        application_agent_studio_invocation_memory_service.reset()


@pytest.mark.asyncio
async def test_application_agent_private_skill_upload_is_scoped_and_removed() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    application_agent_skill_memory_service.reset()
    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    runtime = TrackingRuntimeManager()

    async def runtime_manager() -> TrackingRuntimeManager:
        return runtime

    app.dependency_overrides[
        get_application_agent_runtime_manager
    ] = runtime_manager
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/user/application-agents",
                json={
                    "slug": "private-skill-agent",
                    "name": "私有 Skill Agent",
                },
            )
            assert created.status_code == 201
            agent_id = created.json()["application_agent_id"]
            current = application_agent_memory_service.agents[agent_id]
            application_agent_memory_service.agents[agent_id] = replace(
                current,
                runtime_status="ready",
                applied_generation=current.config_generation,
                apply_status="applied",
            )

            uploaded = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/skills/upload",
                files={
                    "file": (
                        "private-ticket.zip",
                        _skill_zip(),
                        "application/zip",
                    )
                },
                data={"category_key": "personal", "description": "应用工单专用说明"},
            )
            assert uploaded.status_code == 201
            skill_id = uploaded.json()["application_agent_skill_id"]
            assert uploaded.json()["application_agent_id"] == agent_id
            assert uploaded.json()["description"] == "应用工单专用说明"

            updated_description = await client.put(
                f"/api/v1/user/application-agents/{agent_id}/skills/{skill_id}",
                json={"description": ""},
            )
            assert updated_description.status_code == 200
            assert updated_description.json()["description"] == "应用专用 Skill"

            listed = await client.get(
                f"/api/v1/user/application-agents/{agent_id}/skills"
            )
            assert [item["application_agent_skill_id"] for item in listed.json()] == [
                skill_id
            ]
            detail = await client.get(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert detail.json()["config"]["application_skill_ids"] == [
                skill_id
            ]

            duplicate = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/skills/upload",
                files={
                    "file": (
                        "private-ticket.zip",
                        _skill_zip(),
                        "application/zip",
                    )
                },
            )
            assert duplicate.status_code == 409
            assert duplicate.json() == {
                "detail": "application_agent_skill_name_conflict"
            }

            removed = await client.delete(
                f"/api/v1/user/application-agents/{agent_id}/skills/{skill_id}"
            )
            assert removed.status_code == 204
            assert (
                await client.get(
                    f"/api/v1/user/application-agents/{agent_id}/skills"
                )
            ).json() == []
            detail = await client.get(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert detail.json()["config"]["application_skill_ids"] == []
            assert runtime.apply_scopes == [
                "skills_only",
                "skills_only",
            ]
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
            get_application_agent_runtime_manager,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()
        application_agent_skill_memory_service.reset()


@pytest.mark.asyncio
async def test_application_agent_management_is_scoped_to_org_admin() -> None:
    application_agent_memory_service.reset()
    creator = Principal(org_id=1, user_id=11)
    another_admin = Principal(org_id=1, user_id=12)
    other_org_admin = Principal(org_id=2, user_id=11)

    created = await application_agent_memory_service.create_agent(
        principal=creator,
        payload=ApplicationAgentCreateRequest(
            slug="creator-owned-agent",
            name="创建者专属 Agent",
        ),
    )

    assert [
        agent.application_agent_id
        for agent in await application_agent_memory_service.list_agents(
            principal=creator
        )
    ] == [created.application_agent_id]
    assert await application_agent_memory_service.list_agents(
        principal=another_admin
    ) == [replace(created, access_mode="viewer")]
    assert await application_agent_memory_service.list_agents(
        principal=other_org_admin
    ) == []

    viewer_record = await application_agent_memory_service.get_agent(
        principal=another_admin,
        agent_id=created.application_agent_id,
    )
    assert viewer_record == replace(created, access_mode="viewer")
    assert created.access_mode == "owner"

    with pytest.raises(ApplicationAgentError) as owner_required:
        await application_agent_memory_service.get_owned_agent(
            principal=another_admin,
            agent_id=created.application_agent_id,
        )
    assert owner_required.value.code == "application_agent_owner_required"
    assert owner_required.value.status_code == 403

    with pytest.raises(ApplicationAgentError) as viewer_update:
        await application_agent_memory_service.update_agent(
            principal=another_admin,
            agent_id=created.application_agent_id,
            payload=ApplicationAgentUpdateRequest(
                name=created.name,
                avatar_url=created.avatar_url,
                config=created.config,
                expected_config_generation=created.config_generation,
            ),
        )
    assert viewer_update.value.code == "application_agent_owner_required"
    assert viewer_update.value.status_code == 403

    with pytest.raises(ApplicationAgentError) as hidden:
        await application_agent_memory_service.get_agent(
            principal=other_org_admin,
            agent_id=created.application_agent_id,
        )
    assert hidden.value.code == "application_agent_not_found"
    assert hidden.value.status_code == 404

    with pytest.raises(ApplicationAgentError) as conflict:
        await application_agent_memory_service.update_agent(
            principal=creator,
            agent_id=created.application_agent_id,
            payload=ApplicationAgentUpdateRequest(
                name=created.name,
                avatar_url=created.avatar_url,
                config=created.config,
                expected_config_generation=created.config_generation + 1,
            ),
        )
    assert conflict.value.code == "application_agent_config_conflict"
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_application_agent_marks_agent_deleting_and_returns_accepted() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    created = await application_agent_memory_service.create_agent(
        principal=Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID),
        payload=ApplicationAgentCreateRequest(
            slug="delete-retry-agent",
            name="删除重试 Agent",
        ),
    )
    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.delete(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}"
            )
            assert response.status_code == 202
            detail = await client.get(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}"
            )
            assert detail.status_code == 200
            payload = detail.json()
            assert payload["status"] == "disabled"
            assert payload["runtime_status"] == "stopped"
            assert payload["lifecycle_status"] == "deleting"
            assert payload["delete_requested_at"] is not None
            assert payload["delete_last_error_code"] is None

            listed = await client.get("/api/v1/user/application-agents")
            assert listed.status_code == 200
            assert listed.json()[0]["lifecycle_status"] == "deleting"

            second = await client.delete(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}"
            )
            assert second.status_code == 202
            second_detail = await client.get(
                "/api/v1/user/application-agents/"
                f"{created.application_agent_id}"
            )
            assert second_detail.status_code == 200
            assert second_detail.json()["lifecycle_status"] == "deleting"
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()


@pytest.mark.asyncio
async def test_application_agent_api_create_use_delete_flow() -> None:
    """Exercise the management API flow without browser/runtime side effects."""
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    application_agent_studio_invocation_memory_service.reset()
    await application_agent_admin_memory_store.replace(
        org_id=1,
        user_codes=["dev-user"],
        actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
    )
    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/v1/user/application-agents",
                json={"slug": "api-flow-agent", "name": "API 流程助手"},
            )
            assert created.status_code == 201
            agent_id = created.json()["application_agent_id"]
            assert created.json()["lifecycle_status"] == "active"

            ready = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/warmup"
            )
            assert ready.status_code == 200
            assert ready.json()["runtime_status"] == "ready"

            used = await client.post(
                f"/api/v1/user/application-agents/{agent_id}/studio/sessions",
                json={"title": "API 使用会话", "source_type": "web"},
            )
            assert used.status_code == 201
            assert used.json()["runtime_session_id"]

            deleted = await client.delete(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert deleted.status_code == 202
            deleting = await client.get(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert deleting.status_code == 200
            assert deleting.json()["lifecycle_status"] == "deleting"

            # Repeated requests are accepted while the background reconciler
            # is still draining the same agent; they do not resurrect it.
            repeated = await client.delete(
                f"/api/v1/user/application-agents/{agent_id}"
            )
            assert repeated.status_code == 202
            assert (
                await client.get(
                    f"/api/v1/user/application-agents/{agent_id}"
                )
            ).json()["lifecycle_status"] == "deleting"
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()
        application_agent_studio_invocation_memory_service.reset()
