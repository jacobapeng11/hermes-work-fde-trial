from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    CloudHermesProfile,
    ExpertPackage,
    ExpertProfileOperation,
    HermesRuntimeBinding,
    Organization,
    RuntimeAgentProfile,
    RuntimeRun,
    RuntimeSession,
    User,
)
from app.services.failed_expert_cleanup import (
    FailedExpertCleanupIdentity,
    FailedExpertCleanupRejected,
    FailedExpertCleanupService,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec
from scripts.cleanup_failed_experts import _apply_identity, build_parser


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


class FakeHermesProfiles:
    async def has_nonterminal_kanban_task(
        self,
        _endpoint,
        *,
        profile_name: str,
    ) -> bool:
        return profile_name == "expert-13"


class FakeOrchestrator:
    def __init__(self, db: AsyncSession, calls: list[int], fail_id: int | None) -> None:
        self.db = db
        self.calls = calls
        self.fail_id = fail_id

    async def uninstall(self, *, runtime_agent_profile_id: int, **_kwargs) -> None:
        self.calls.append(runtime_agent_profile_id)
        if runtime_agent_profile_id == self.fail_id:
            raise RuntimeError("simulated cleanup failure")
        profile = await self.db.get(RuntimeAgentProfile, runtime_agent_profile_id)
        assert profile is not None
        profile.status = "deleted"
        await self.db.commit()


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, name="Org", code="org-1", status="active"),
                User(id=1, org_id=1, name="示例用户甲", status="active"),
                CloudHermesProfile(
                    id=1,
                    org_id=1,
                    user_id=1,
                    profile_type="default_chat_agent",
                    name="默认数字人",
                    status="active",
                ),
            ]
        )
        await db.flush()
        package = ExpertPackage(
            id=50,
            org_id=1,
            owner_user_id=1,
            origin_runtime_workspace_id=1,
            slug="published",
            name="已发布专家",
            status="published",
            visibility="organization",
        )
        db.add(package)
        db.add_all(
            [
                HermesRuntimeBinding(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    runtime_id="runtime-1",
                    image="hermes:test",
                    k8s_namespace="test",
                    knative_service="runtime-1",
                    endpoint_base_url="http://runtime.invalid",
                    endpoint_host_header="runtime.invalid",
                    hermes_home_sub_path="users/1",
                    workspace_sub_path="workspaces/1",
                    hermes_home_mount_path="/home/hermes",
                    workspace_mount_path="/workspace",
                    api_key_encrypted=RuntimeApiKeyCodec().encrypt("secret"),
                    status="ready",
                ),
                RuntimeAgentProfile(
                    id=7,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-7",
                    profile_kind="expert",
                    display_name="错误专家",
                    status="error",
                ),
                RuntimeAgentProfile(
                    id=8,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-8",
                    profile_kind="expert",
                    display_name="正常专家",
                    status="ready",
                ),
                RuntimeAgentProfile(
                    id=9,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-9",
                    profile_kind="expert",
                    display_name="卡住专家",
                    status="provisioning",
                ),
                RuntimeAgentProfile(
                    id=11,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-11",
                    profile_kind="expert",
                    display_name="已发布错误专家",
                    expert_package_id=50,
                    status="error",
                ),
                RuntimeAgentProfile(
                    id=13,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-13",
                    profile_kind="expert",
                    display_name="看板活动专家",
                    status="error",
                ),
                ExpertProfileOperation(
                    id=90,
                    operation_id="operation-stale-90",
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    runtime_agent_profile_id=9,
                    active_profile_key=9,
                    action="create",
                    status="running",
                    step="projecting_capabilities",
                    created_at=now - timedelta(days=2),
                    updated_at=now - timedelta(days=2),
                ),
            ]
        )
        await db.commit()
    return engine, sessions


@pytest.mark.asyncio
async def test_dry_run_is_fail_closed_and_classifies_only_safe_candidates() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            report = await FailedExpertCleanupService(
                db,
                hermes_profiles=FakeHermesProfiles(),  # type: ignore[arg-type]
            ).dry_run(
                user_name="示例用户甲",
                stale_before=datetime.now(timezone.utc) - timedelta(days=1),
            )

            assert [item.runtime_agent_profile_id for item in report.candidates] == [7, 9]
            assert {
                item.runtime_agent_profile_id: item.reason
                for item in report.excluded
            } == {
                8: "ready",
                11: "blocked_published",
                13: "active_kanban",
            }
            assert report.identity == FailedExpertCleanupIdentity(1, 1, 1)
            assert report.expected_effects == (
                "delete Runtime expert Profile and workspace projection",
                "tombstone Backend expert Profile and clear execution binding",
                "preserve historical sessions, runs, packages, versions, and operations",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_rejects_ambiguous_exact_user_name() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add(User(id=2, org_id=1, name="示例用户甲", status="active"))
            await db.commit()
            with pytest.raises(FailedExpertCleanupRejected, match="exactly one"):
                await FailedExpertCleanupService(
                    db,
                    hermes_profiles=FakeHermesProfiles(),  # type: ignore[arg-type]
                ).dry_run(
                    user_name="示例用户甲",
                    stale_before=datetime.now(timezone.utc),
                )
            with pytest.raises(FailedExpertCleanupRejected, match="found 0"):
                await FailedExpertCleanupService(
                    db,
                    hermes_profiles=FakeHermesProfiles(),  # type: ignore[arg-type]
                ).dry_run(
                    user_name="不存在的用户",
                    stale_before=datetime.now(timezone.utc),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_excludes_profile_with_active_runtime_run() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            session = RuntimeSession(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                runtime_agent_profile_id=7,
                runtime_session_id="session-active-cleanup-guard",
                status="active",
            )
            db.add(session)
            await db.flush()
            db.add(
                RuntimeRun(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    session_id=session.id,
                    runtime_session_id=session.runtime_session_id,
                    runtime_run_id="run-active-cleanup-guard",
                    status="running",
                )
            )
            await db.commit()

            report = await FailedExpertCleanupService(
                db,
                hermes_profiles=FakeHermesProfiles(),  # type: ignore[arg-type]
            ).dry_run(
                user_name="示例用户甲",
                stale_before=datetime.now(timezone.utc) - timedelta(days=1),
            )

            assert [item.runtime_agent_profile_id for item in report.candidates] == [9]
            reasons = {
                item.runtime_agent_profile_id: item.reason for item in report.excluded
            }
            assert reasons[7] == "active_run"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_apply_marks_stale_operation_and_stops_after_first_failure() -> None:
    engine, sessions = await _database()
    calls: list[int] = []
    try:
        async with sessions() as db:
            service = FailedExpertCleanupService(
                db,
                hermes_profiles=FakeHermesProfiles(),  # type: ignore[arg-type]
                orchestrator_factory=lambda session: FakeOrchestrator(
                    session,
                    calls,
                    fail_id=9,
                ),  # type: ignore[arg-type]
            )
            report = await service.apply(
                user_name="示例用户甲",
                identity=FailedExpertCleanupIdentity(1, 1, 1),
                profile_ids=[9, 7],
                stale_before=datetime.now(timezone.utc) - timedelta(days=1),
            )

            assert calls == [9]
            assert report.results == (
                {
                    "runtime_agent_profile_id": 9,
                    "status": "failed",
                    "error": "RuntimeError",
                },
            )
            operation = await db.get(ExpertProfileOperation, 90)
            assert operation is not None
            assert operation.status == "failed"
            assert operation.active_profile_key is None
            assert operation.error_code == "operator_cleanup_requested"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_apply_processes_only_explicit_confirmed_profile_ids() -> None:
    engine, sessions = await _database()
    calls: list[int] = []
    try:
        async with sessions() as db:
            report = await FailedExpertCleanupService(
                db,
                hermes_profiles=FakeHermesProfiles(),  # type: ignore[arg-type]
                orchestrator_factory=lambda session: FakeOrchestrator(
                    session,
                    calls,
                    fail_id=None,
                ),  # type: ignore[arg-type]
            ).apply(
                user_name="示例用户甲",
                identity=FailedExpertCleanupIdentity(1, 1, 1),
                profile_ids=[7],
                stale_before=datetime.now(timezone.utc) - timedelta(days=1),
            )

            assert calls == [7]
            assert report.results == (
                {"runtime_agent_profile_id": 7, "status": "deleted"},
            )
            assert (await db.get(RuntimeAgentProfile, 9)).status == "provisioning"
    finally:
        await engine.dispose()


def test_cleanup_cli_defaults_to_dry_run_and_requires_stable_apply_identity() -> None:
    parser = build_parser()
    dry_run = parser.parse_args(["--user-name", "示例用户甲"])

    assert dry_run.apply is False
    assert dry_run.user_name == "示例用户甲"
    assert dry_run.profile_id == []

    incomplete = parser.parse_args(["--user-name", "示例用户甲", "--apply", "--profile-id", "7"])
    with pytest.raises(FailedExpertCleanupRejected, match="org-id"):
        _apply_identity(incomplete)

    complete = parser.parse_args([
        "--apply",
        "--org-id", "1",
        "--user-id", "1",
        "--runtime-workspace-id", "1",
        "--profile-id", "7",
    ])
    assert _apply_identity(complete) == FailedExpertCleanupIdentity(1, 1, 1)
