from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    CloudHermesProfile,
    HermesRuntimeBinding,
    Organization,
    Role,
    User,
)
from app.config import settings
from app.main import app
from app.database import get_db
from app.services.auth import hash_password
from app.services.runtime_startup_context import RuntimeStartupContextService
from app.services.runtime_workspace import Principal
from app.services.sso_identity import (
    SqlAlchemySsoIdentityRepository,
    SsoIdentityService,
)


@compiles(BigInteger, "sqlite")
def _big_int_sqlite(_type: BigInteger, _compiler: Any, **_kwargs: Any) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _longtext_sqlite(_type: LONGTEXT, _compiler: Any, **_kwargs: Any) -> str:
    return "TEXT"


class FakePodLister:
    def __init__(self, pods: list[dict[str, Any]] | Exception | None = None) -> None:
        self.pods = pods
        self.calls: list[tuple[str, str]] = []

    async def list_kservice_pods(
        self,
        *,
        namespace: str,
        service_name: str,
    ) -> list[dict[str, Any]]:
        self.calls.append((namespace, service_name))
        if isinstance(self.pods, Exception):
            raise self.pods
        return self.pods or []


def ready_pod() -> dict[str, Any]:
    return {
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(
    session,
    *,
    login_count: int,
    with_runtime: bool,
) -> None:
    session.add_all([
        Organization(id=1, name="Org", code="org-1", status="active"),
        Role(id=1, org_id=1, code="user", name="User", status="active"),
        User(
            id=9,
            org_id=1,
            name="用户",
            user_code="w46974",
            sso_subject="w46974",
            status="active",
            login_count=login_count,
        ),
    ])
    if with_runtime:
        session.add_all([
            CloudHermesProfile(
                id=75,
                org_id=1,
                user_id=9,
                profile_type="default_chat_agent",
                status="active",
                name="个人助手",
            ),
            HermesRuntimeBinding(
                id=1,
                org_id=1,
                user_id=9,
                runtime_workspace_id=75,
                provider="knative",
                runtime_id="rt-75",
                config_generation=1,
                runtime_config_source="pvc",
                image="hermes-runtime:stable",
                k8s_namespace="hermes-runtimes",
                knative_service="runtime-w46974",
                hermes_home_sub_path="home",
                workspace_sub_path="workspace",
                hermes_home_mount_path="/opt/data",
                workspace_mount_path="/opt/workspace",
                api_key_encrypted="encrypted",
                status="ready",
            ),
        ])
    await session.commit()


async def _login(session) -> None:
    await SsoIdentityService(SqlAlchemySsoIdentityRepository(session)).resolve_or_create(
        org_id=1,
        work_no="w46974",
    )


@pytest.mark.asyncio
async def test_first_real_login_without_workspace_requests_first_allocation() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, login_count=0, with_runtime=False)
            await _login(db)
            user = (await db.execute(select(User))).scalar_one()
            pod_lister = FakePodLister()
            result = await RuntimeStartupContextService(
                db,
                pod_lister=pod_lister,
                namespace="hermes-runtimes",
            ).resolve(Principal(org_id=1, user_id=9))

            assert user.login_count == 1
            assert result.runtime_workspace_id is None
            assert result.context == "first_allocation"
            assert pod_lister.calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_returning_user_without_ready_pod_requests_environment_recovery() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, login_count=2, with_runtime=True)
            await _login(db)
            user = (await db.execute(select(User))).scalar_one()
            result = await RuntimeStartupContextService(
                db,
                pod_lister=FakePodLister([]),
                namespace="hermes-runtimes",
            ).resolve(Principal(org_id=1, user_id=9))

            assert user.login_count == 3
            assert result.runtime_workspace_id == 75
            assert result.context == "environment_recovery"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_running_ready_pod_hides_environment_hint() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, login_count=3, with_runtime=True)
            pod_lister = FakePodLister([ready_pod()])
            result = await RuntimeStartupContextService(
                db,
                pod_lister=pod_lister,
                namespace="hermes-runtimes",
            ).resolve(Principal(org_id=1, user_id=9))

            assert pod_lister.calls == [("hermes-runtimes", "runtime-w46974")]
            assert result.context == "none"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_kubernetes_failure_degrades_to_no_hint() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, login_count=3, with_runtime=True)
            result = await RuntimeStartupContextService(
                db,
                pod_lister=FakePodLister(RuntimeError("kubectl unavailable")),
                namespace="hermes-runtimes",
            ).resolve(Principal(org_id=1, user_id=9))

            assert result.context == "none"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_password_login_increments_durable_login_count() -> None:
    engine, sessions = await _database()
    original_runtime_mode = settings.RUNTIME_CHAT_MODE
    original_auth_mode = settings.HERMES_AUTH_MODE
    settings.RUNTIME_CHAT_MODE = "real"
    settings.HERMES_AUTH_MODE = "jwt"

    async def override_get_db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with sessions() as db:
            await _seed(db, login_count=2, with_runtime=False)
            user = (await db.execute(select(User))).scalar_one()
            user.password_hash = hash_password("secret", iterations=1)
            await db.commit()

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/auth/login",
                json={"identifier": "w46974", "password": "secret"},
            )

        assert response.status_code == 200
        async with sessions() as db:
            user = (await db.execute(select(User))).scalar_one()
            assert user.login_count == 3
            assert user.last_login_at is not None
    finally:
        settings.RUNTIME_CHAT_MODE = original_runtime_mode
        settings.HERMES_AUTH_MODE = original_auth_mode
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()
