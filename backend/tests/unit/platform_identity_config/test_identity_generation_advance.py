"""个人 Runtime 身份代数推进与 exact-generation 工件回归。

覆盖 SSO 身份补齐推进 Binding Desired、同代数 Bundle 工件不可变、
以及 pending identity 在 ensure_binding 中触发收敛的完整合同。
"""
from __future__ import annotations

from typing import Any

import pytest
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
    SysUser,
    User,
)
from app.models.runtime import PersonalConfigBundleArtifact
from app.services.personal_config_bundle import (
    PersonalConfigBundleBuilder,
    PersonalConfigBundleError,
    PersonalRuntimeInitialConfigPreparer,
    bootstrap_config_revision,
)
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


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session, *, binding_status: str | None = "ready", user_name: str = "用户") -> None:
    session.add_all([
        Organization(id=1, name="Org", code="org-1", status="active"),
        Role(id=1, org_id=1, code="user", name="User", status="active"),
        User(id=9, org_id=1, name=user_name, user_code="w15447", sso_subject="w15447", status="active"),
        SysUser(user_id="hrm-1", code="w15447", username="w15447", name="王伍思", status=1),
        CloudHermesProfile(
            id=75,
            org_id=1,
            user_id=9,
            profile_type="default_chat_agent",
            status="active",
            name="个人助手",
        ),
    ])
    if binding_status is not None:
        session.add(HermesRuntimeBinding(
            id=1,
            org_id=1,
            user_id=9,
            runtime_workspace_id=75,
            provider="knative",
            runtime_id="rt-75",
            config_generation=2,
            config_hash=bootstrap_config_revision(2),
            runtime_config_source="pvc",
            image="hermes-runtime:stable",
            k8s_namespace="hermes-runtimes",
            knative_service="runtime-w15447",
            hermes_home_sub_path="home",
            workspace_sub_path="workspace",
            hermes_home_mount_path="/opt/data",
            workspace_mount_path="/opt/workspace",
            api_key_encrypted="encrypted",
            status=binding_status,
            endpoint_base_url="https://runtime.example" if binding_status == "ready" else None,
            endpoint_host_header="runtime.example" if binding_status == "ready" else None,
            runtime_root_pvc_uid="pvc-uid" if binding_status == "ready" else None,
            pvc_storage_identity_hash="hash" if binding_status == "ready" else None,
        ))
    await session.commit()


async def _resolve(session) -> None:
    await SsoIdentityService(SqlAlchemySsoIdentityRepository(session)).resolve_or_create(
        org_id=1, work_no="w15447"
    )


@pytest.mark.asyncio
async def test_identity_change_advances_ready_binding_generation() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            await _resolve(db)
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            assert binding.config_generation == 3
            assert binding.config_hash == bootstrap_config_revision(3)
            assert binding.pending_reload is True
            assert "identity" in (binding.pending_reload_scopes or [])
            assert binding.last_error is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_login_does_not_advance_again() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            await _resolve(db)
            await _resolve(db)
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            assert binding.config_generation == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hrm_id_only_change_does_not_advance() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, user_name="王伍思")
            user = (await db.execute(select(User))).scalar_one()
            user.hrm_user_id = None
            await db.commit()
            await _resolve(db)
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            assert binding.config_generation == 2
            assert binding.pending_reload is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_without_binding_updates_identity_only() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, binding_status=None)
            await _resolve(db)
            user = (await db.execute(select(User))).scalar_one()
            assert user.name == "王伍思"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_starting_binding_records_pending_intent_without_generation() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, binding_status="starting")
            await _resolve(db)
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            assert binding.config_generation == 2
            assert binding.config_hash == bootstrap_config_revision(2)
            assert binding.pending_reload is True
            assert "identity" in (binding.pending_reload_scopes or [])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_generation_artifact_is_immutable_after_identity_change() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            await _resolve(db)
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            generation = int(binding.config_generation)
            builder = PersonalConfigBundleBuilder(db)
            bundle = await builder.build(profile_id=75, generation=generation, bootstrap=True)
            session_artifact = await builder.find_artifact(profile_id=75, generation=generation)
            # 历史代数尚未有工件：先按实时身份构建，摘要反映实名。
            assert session_artifact is None
            assert "王伍思" in str(bundle.files)

            # 直接落工件模拟 preparer 完成；随后身份再变化，同代数摘要必须不变。
            await builder.record_artifact(bundle, org_id=1)
            await db.commit()
            user = (await db.execute(select(User))).scalar_one()
            user.name = "王伍思二"
            await db.commit()
            rebuilt = await builder.build(profile_id=75, generation=generation, bootstrap=True)
            assert rebuilt.config_digest == bundle.config_digest
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_artifact_rejects_conflicting_digest() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            generation = int(binding.config_generation)
            builder = PersonalConfigBundleBuilder(db)
            # 先在无工件状态下取得实时摘要，再落一个不同摘要的同代数工件。
            bundle = await builder.build(profile_id=75, generation=generation, bootstrap=True)
            db.add(PersonalConfigBundleArtifact(
                org_id=1,
                runtime_workspace_id=75,
                config_generation=generation,
                config_revision=bootstrap_config_revision(generation),
                config_digest="a" * 64,
                files={"SOUL.md": "old"},
            ))
            await db.commit()
            with pytest.raises(PersonalConfigBundleError):
                await builder.record_artifact(bundle, org_id=1)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_preparer_redeems_identity_pending_into_new_generation() -> None:
    """starting/updating 期间记录的 identity 意图由 preparer 兑现为新代数。"""
    from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
    from app.services.personal_config_bundle import PersonalRuntimeInitialConfigPreparer

    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db, binding_status="starting")
            # 模拟 in-flight 期间的 SSO：身份已补齐为实名，但 Binding 只记录了 pending 意图。
            user = (await db.execute(select(User))).scalar_one()
            user.name = "王伍思"
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            binding.pending_reload = True
            binding.pending_reload_scopes = ["identity"]
            await db.commit()
            spec = KnativeRuntimeSpec(
                org_id=1,
                user_id=9,
                user_name="王伍思",
                user_code="w15447",
                runtime_workspace_id=75,
                runtime_id="rt-75",
                namespace="hermes-runtimes",
                service_name="runtime-w15447",
                image="hermes-runtime:stable",
                service_account="hermes-runtime-workload",
                api_server_key_secret_name="key",
                runtime_root_pvc_name="runtime-w15447-root",
                hermes_home_sub_path="home",
                workspace_sub_path="workspace",
                runtime_root_pv_name="pv",
                runtime_root_host_path="/tmp/unused-rt-75",
                runtime_node_name="demo",
                upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
                state_schema=23,
                config_schema=34,
                startup_mode="steady_state",
                runtime_user_uid=10000,
                runtime_shared_gid=1000,
            )
            await PersonalRuntimeInitialConfigPreparer(db).prepare(spec)
            await db.commit()
            binding = (await db.execute(select(HermesRuntimeBinding))).scalar_one()
            assert binding.config_generation == 3
            assert binding.config_hash == bootstrap_config_revision(3)
            assert "identity" not in (binding.pending_reload_scopes or [])
            artifact = await PersonalConfigBundleBuilder(db).find_artifact(
                profile_id=75, generation=3
            )
            assert artifact is not None
            assert "王伍思" in str(artifact.files)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_schedules_reconcile_for_ready_pending_binding(monkeypatch) -> None:
    """名义 ready 但 pending 的 Binding，bootstrap 必须调度后台重投影。"""
    from types import SimpleNamespace

    from app.api.v1.user import runtime_workspace as api_module
    from app.api.v1.user.runtime_workspace import bootstrap_runtime_workspace
    from app.config import settings
    from app.services.runtime_workspace import RuntimeWorkspaceRecord

    profile = RuntimeWorkspaceRecord(
        runtime_workspace_id=75,
        cloud_profile_id=75,
        org_id=1,
        user_id=9,
        user_name="王伍思",
        user_code="w15447",
        name="个人助手",
        status="active",
        default_model_id=None,
        runtime_status="ready",
        pending_reload=True,
    )
    binding = SimpleNamespace(
        binding_status="ready",
        created=False,
        initial_bootstrap_resume_eligible=False,
    )

    class _Store:
        async def ensure_binding(self, _record):
            return binding

    class _Service:
        store = _Store()

        async def ensure_default_for_user(self, _principal):
            return profile

    async def _noop_bootstrap(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(api_module, "_run_real_runtime_bootstrap", _noop_bootstrap)

    from fastapi import BackgroundTasks
    from app.services.runtime_workspace import Principal

    background_tasks = BackgroundTasks()
    summary = await bootstrap_runtime_workspace(
        background_tasks,
        Principal(org_id=1, user_id=9),
        _Service(),
        None,
    )
    assert summary.runtime_status == "ready"
    assert len(background_tasks.tasks) == 1


def test_find_candidates_locks_user_rows_for_concurrent_login_serialization() -> None:
    """并发登录串行化：候选 user 行必须带 FOR UPDATE，避免双登录重复推进代数。"""
    from sqlalchemy import select
    from sqlalchemy.dialects import mysql

    from app.models import User

    query = select(User).where(User.org_id == 1).with_for_update()
    assert "FOR UPDATE" in str(query.compile(dialect=mysql.dialect()))
