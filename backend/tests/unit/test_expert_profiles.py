from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT

from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.protocol import HermesProtocolError
from app.models import (
    AssetRolloutBatch,
    Base,
    CloudHermesProfile,
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    ExpertProfileOperation,
    InstalledExpertBinding,
    KnowledgeBase,
    Mcp,
    ModelEntry,
    ModelProvider,
    Organization,
    Permission,
    RuntimeAgentProfile,
    RuntimeSession,
    Skill,
    SkillVersion,
    TemplateReview,
    User,
)
from app.schemas.experts import (
    ApproveExpertReviewRequest,
    CreateExpertRequest,
    ExpertCapabilitySelection,
    ExpertShareScope,
    UpdateExpertConfigurationRequest,
    UpdateExpertRequest,
)
from app.services.expert_errors import ExpertProfileError
from tests.unit.expert_service_fixture import ExpertServiceFixture as ExpertProfileOrchestrator
from app.services.expert_authoring import ExpertAuthoringService
from app.services.expert_release_cleanup import (
    cleanup_expired_expert_release_artifacts,
)
from app.services.runtime_sessions import (
    CreateRuntimeSessionCommand,
    RuntimeSessionService,
    SqlAlchemyRuntimeSessionStore,
)
from app.config import settings
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    # SQLite autoincrement requires the exact INTEGER primary-key spelling.
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


class FakeHermesProfiles:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_projection_once = False
        self.fail_delete_workspace_once = False
        self.compatibility_error: HermesProtocolError | None = None

    async def _record(self, event: str, **kwargs: Any) -> None:
        self.calls.append((event, kwargs))

    async def assert_compatible(self, _endpoint: RuntimeEndpoint) -> None:
        await self._record("assert_compatible")
        if self.compatibility_error is not None:
            raise self.compatibility_error

    async def create_profile(self, _endpoint: RuntimeEndpoint, **kwargs: Any) -> None:
        await self._record("create_profile", **kwargs)

    async def update_description(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("update_description", **kwargs)

    async def update_soul(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("update_soul", **kwargs)

    async def configure_platform_profile(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("configure_platform_profile", **kwargs)

    async def replace_profile_skills(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("replace_profile_skills", **kwargs)

    async def replace_mcp_servers(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("replace_mcp_servers", **kwargs)
        if self.fail_projection_once:
            self.fail_projection_once = False
            raise RuntimeError("projection failed")

    async def ensure_workspace(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("ensure_workspace", **kwargs)

    async def verify_profile_projection(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("verify_profile_projection", **kwargs)

    async def has_nonterminal_kanban_task(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> bool:
        await self._record("has_nonterminal_kanban_task", **kwargs)
        return False

    async def delete_profile(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("delete_profile", **kwargs)

    async def delete_workspace(
        self,
        _endpoint: RuntimeEndpoint,
        **kwargs: Any,
    ) -> None:
        await self._record("delete_workspace", **kwargs)
        if self.fail_delete_workspace_once:
            self.fail_delete_workspace_once = False
            raise RuntimeError("workspace deletion failed")


def _endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=1,
        runtime_id="rt-1",
        endpoint_base_url="http://runtime.invalid",
        endpoint_host_header="runtime.invalid",
        api_key="secret",
    )


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add_all(
                [
                    Organization(id=1, name="Org", code="org-1", status="active"),
                    User(id=1, org_id=1, name="User", status="active"),
                    User(id=2, org_id=1, name="Installer", status="active"),
                    User(id=3, org_id=1, name="Hidden", status="active"),
                    CloudHermesProfile(
                        id=1,
                        org_id=1,
                        user_id=1,
                        profile_type="default_chat_agent",
                        name="默认数字人",
                        status="active",
                    ),
                    CloudHermesProfile(
                        id=2,
                        org_id=1,
                        user_id=2,
                        profile_type="default_chat_agent",
                        name="安装者数字人",
                        status="active",
                    ),
                    CloudHermesProfile(
                        id=3,
                        org_id=1,
                        user_id=3,
                        profile_type="default_chat_agent",
                        name="无权限数字人",
                        status="active",
                    ),
                    ModelProvider(
                        id=1,
                        org_id=1,
                        provider_key="platform",
                        display_name="Platform",
                        provider_type="openai_compatible",
                        api_endpoint="http://provider.invalid/v1",
                        api_key_encrypted="encrypted",
                        status="active",
                    ),
                    ModelEntry(
                        id=1,
                        org_id=1,
                        provider_id=1,
                        model_key="model-default",
                        display_name="Default Model",
                        context_length=128_000,
                        is_default=True,
                        is_restricted=False,
                        status="active",
                    ),
                ]
            )
            await db.commit()
    except BaseException:
        await engine.dispose()
        raise
    return engine, sessions


@pytest.mark.asyncio
async def test_capability_catalog_awaits_asset_queries_and_returns_active_assets() -> None:
    class ScalarResult:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def scalars(self) -> ScalarResult:
            return self

        def all(self) -> list[Any]:
            return self.rows

    class CatalogSession:
        def __init__(self) -> None:
            self.responses = [
                [
                    (
                        Skill(
                            id=10,
                            org_id=1,
                            owner_user_id=1,
                            slug="wechat-writer",
                            name="微信写作",
                            description="公众号文章写作",
                            category_key="writing",
                            asset_scope="public",
                            visibility="public",
                            status="published",
                            current_version_id=11,
                        ),
                        SkillVersion(
                            id=11,
                            org_id=1,
                            skill_id=10,
                            version=1,
                            package_uri="/tmp/skill",
                            package_sha256="a" * 64,
                            manifest={},
                            status="published",
                        ),
                    )
                ],
                [
                    Mcp(
                        id=20,
                        org_id=1,
                        owner_user_id=1,
                        slug="web-search",
                        name="联网搜索",
                        description="检索公开资料",
                        visibility="public",
                        transport="stdio",
                        config={},
                        status="published",
                    )
                ],
                [],
                [],
                [],
            ]

        async def execute(self, _statement: Any) -> ScalarResult:
            return ScalarResult(self.responses.pop(0))

        async def scalar(self, _statement: Any) -> None:
            # This focused catalogue fixture has no default Runtime binding,
            # so installed assets are intentionally absent.
            return None

    catalog = await ExpertProfileOrchestrator(
        CatalogSession(),  # type: ignore[arg-type]
    ).capability_catalog(principal=Principal(org_id=1, user_id=1))

    assert [(item.asset_id, item.name) for item in catalog.market_skills] == [
        (10, "微信写作")
    ]
    assert catalog.market_skills[0].version_id == 11
    assert [(item.asset_id, item.name, item.kind) for item in catalog.market_mcps] == [
        (20, "联网搜索", "stdio")
    ]
    assert catalog.knowledge_bases == []


@pytest.mark.asyncio
async def test_release_artifact_cleanup_only_removes_expired_withdrawn_release(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path))
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            package = ExpertPackage(
                org_id=1,
                owner_user_id=1,
                origin_runtime_workspace_id=1,
                slug="withdrawn-expert",
                name="已撤回专家",
                status="withdrawn",
                visibility="private",
            )
            db.add(package)
            await db.flush()
            version = ExpertPackageVersion(
                org_id=1,
                expert_package_id=int(package.id),
                version=1,
                manifest={"release_artifact_schema": "v1"},
                status="withdrawn",
            )
            db.add(version)
            await db.flush()
            artifact_path = (
                tmp_path
                / "expert-release-artifacts"
                / "1"
                / str(package.id)
                / "v1"
                / "skills"
                / "public-skill-1-version-1"
            )
            artifact_path.mkdir(parents=True)
            (artifact_path / "SKILL.md").write_text("body", encoding="utf-8")
            artifact = ExpertVersionArtifact(
                org_id=1,
                expert_package_version_id=int(version.id),
                artifact_type="skill_package",
                source_type="public",
                source_asset_id=1,
                source_version_id=1,
                name="冻结技能",
                storage_uri=str(artifact_path),
                sha256="a" * 64,
                status="ready",
            )
            db.add(artifact)
            await db.flush()
            artifact.created_at = now - timedelta(days=31)
            await db.commit()

            removed = await cleanup_expired_expert_release_artifacts(
                db,
                now=now,
                retention_days=30,
            )
            assert removed == 1
            assert await db.get(ExpertVersionArtifact, artifact.id) is None
            assert not artifact_path.exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_guard", ["enabled_binding", "pending_review"])
async def test_release_artifact_cleanup_keeps_expired_release_with_live_guard(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    retention_guard: str,
) -> None:
    """GC-02/GC-03: installs and pending review always win over retention."""
    engine, sessions = await _database()
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path))
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            package = ExpertPackage(
                org_id=1,
                owner_user_id=1,
                origin_runtime_workspace_id=1,
                slug=f"retention-guard-{retention_guard}",
                name="保留期保护专家",
                status="withdrawn",
                visibility="private",
            )
            db.add(package)
            await db.flush()
            version = ExpertPackageVersion(
                org_id=1,
                expert_package_id=int(package.id),
                version=1,
                manifest={"release_artifact_schema": "v1"},
                status="withdrawn",
            )
            db.add(version)
            await db.flush()
            artifact_path = (
                tmp_path
                / "expert-release-artifacts"
                / "1"
                / str(package.id)
                / "v1"
                / "skills"
                / "public-skill-1-version-1"
            )
            artifact_path.mkdir(parents=True)
            (artifact_path / "SKILL.md").write_text("body", encoding="utf-8")
            artifact = ExpertVersionArtifact(
                org_id=1,
                expert_package_version_id=int(version.id),
                artifact_type="skill_package",
                source_type="public",
                source_asset_id=1,
                source_version_id=1,
                name="冻结技能",
                storage_uri=str(artifact_path),
                sha256="a" * 64,
                status="ready",
            )
            db.add(artifact)
            await db.flush()
            artifact.created_at = now - timedelta(days=31)
            if retention_guard == "enabled_binding":
                profile = RuntimeAgentProfile(
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=2,
                    hermes_profile_name="expert-retention-guard",
                    profile_kind="expert",
                    display_name="安装者专家",
                    ownership_type="market_installed",
                    expert_package_id=int(package.id),
                    capability_config={},
                    status="active",
                )
                db.add(profile)
                await db.flush()
                db.add(
                    InstalledExpertBinding(
                        org_id=1,
                        user_id=2,
                        runtime_workspace_id=2,
                        runtime_agent_profile_id=int(profile.id),
                        expert_package_id=int(package.id),
                        expert_package_version_id=int(version.id),
                        source_type="market",
                        status="enabled",
                    )
                )
            else:
                db.add(
                    TemplateReview(
                        org_id=1,
                        submitter_user_id=1,
                        expert_package_id=int(package.id),
                        expert_package_version_id=int(version.id),
                        review_snapshot={"schema_version": "expert.review/v2"},
                        status="pending",
                    )
                )
            await db.commit()

            removed = await cleanup_expired_expert_release_artifacts(
                db,
                now=now,
                retention_days=30,
            )

            assert removed == 0
            assert await db.get(ExpertVersionArtifact, artifact.id) is not None
            assert artifact_path.is_dir()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_inheritance_skips_deleted_mcp_bindings() -> None:
    class QueryResult:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows

        def scalars(self) -> QueryResult:
            return self

        def all(self) -> list[Any]:
            return self.rows

        def __iter__(self):
            return iter(self.rows)

    class InheritanceSession:
        async def scalars(self, statement: Any) -> QueryResult:
            return (await self.execute(statement)).scalars()

        async def execute(self, statement: Any) -> QueryResult:
            sql = str(statement)
            if "profile_skill_binding" in sql:
                return QueryResult([])
            if "profile_mcp_binding" in sql:
                # The source binding points at deleted MCP 7. A sanitized
                # inheritance query joins the active stable catalog and must
                # therefore return no row.
                return QueryResult([] if "JOIN mcp" in sql else [7])
            if "profile_knowledge_binding" in sql:
                return QueryResult([])
            raise AssertionError(f"unexpected inheritance query: {sql}")

    source = RuntimeAgentProfile(
        id=3,
        org_id=1,
        user_id=7,
        runtime_workspace_id=115,
        hermes_profile_name="default",
        profile_kind="default",
        display_name="默认数字人",
        ownership_type="default",
        default_workspace_key=115,
        config_generation=1,
        applied_config_generation=1,
        capability_config={},
        status="active",
    )
    capabilities = await ExpertProfileOrchestrator(
        InheritanceSession(),  # type: ignore[arg-type]
    )._inherited_capabilities(source)

    assert capabilities.mcp_ids == []


@pytest.mark.asyncio
async def test_inherited_expert_preserves_enterprise_knowledge_configuration() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            source = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="企业知识源专家",
                    soul_content="企业知识源专家人格",
                    capabilities=ExpertCapabilitySelection(
                        enterprise_knowledge_enabled=True,
                    ),
                ),
            )
            assert source.capabilities.enterprise_knowledge_enabled is True

            inherited = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="继承后的独立专家",
                    inherited_from_profile_id=source.runtime_agent_profile_id,
                ),
            )

            assert inherited.inherited_from_profile_id == (source.runtime_agent_profile_id)
            assert inherited.capabilities.enterprise_knowledge_enabled is True
            assert "enterprise-knowledge" in inherited.capabilities.plugin_ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retired_generation_drift_does_not_override_applied_revision() -> None:
    """Stable Desired/Applied revisions are the only expert asset gate."""
    engine, sessions = await _database()
    principal = Principal(org_id=1, user_id=1)
    try:
        async with sessions() as db:
            profile = RuntimeAgentProfile(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                hermes_profile_name="expert-drift",
                profile_kind="expert",
                display_name="漂移专家",
                ownership_type="self_created",
                config_generation=2,
                applied_config_generation=1,
                capability_config={},
                status="active",
            )
            db.add(profile)
            await db.commit()

            summary = await ExpertProfileOrchestrator(
                db,
            ).get_profile(
                principal=principal,
                runtime_workspace_id=1,
                runtime_agent_profile_id=int(profile.id),
            )
            assert summary.status == "active"
            assert summary.last_error is None

            created = await RuntimeSessionService(
                SqlAlchemyRuntimeSessionStore(db)
            ).create_session(
                principal=principal,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=1,
                    title="稳定版本专家可召唤",
                    runtime_agent_profile_id=int(profile.id),
                ),
            )
            assert created.runtime_agent_profile_id == int(profile.id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_desired_revision_drift_allows_session_on_old_applied_identity() -> None:
    engine, sessions = await _database()
    principal = Principal(org_id=1, user_id=1)
    try:
        async with sessions() as db:
            profile = RuntimeAgentProfile(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                hermes_profile_name="expert-revision-drift",
                profile_kind="expert",
                display_name="版本更新中专家",
                ownership_type="self_created",
                config_generation=1,
                applied_config_generation=1,
                desired_revision=2,
                applied_revision=1,
                capability_config={},
                status="active",
            )
            db.add(profile)
            await db.commit()

            created = await RuntimeSessionService(
                SqlAlchemyRuntimeSessionStore(db)
            ).create_session(
                principal=principal,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=1,
                    title="使用旧已生效版本",
                    runtime_agent_profile_id=int(profile.id),
                ),
            )
            assert created.runtime_agent_profile_id == profile.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_uninstalling_private_expert_releases_name_for_recreate() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            db.add(
                RuntimeAgentProfile(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="default",
                    profile_kind="default",
                    display_name="默认数字人",
                    ownership_type="default",
                    default_workspace_key=1,
                    status="active",
                )
            )
            await db.commit()
            first = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="可重新创建的专家",
                ),
            )
            db.add(
                RuntimeSession(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    runtime_agent_profile_id=first.runtime_agent_profile_id,
                    runtime_session_id="private-expert-history",
                    status="active",
                )
            )
            await db.commit()
            await orchestrator.uninstall(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=first.runtime_agent_profile_id,
            )

            installed = await orchestrator.list_installed(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
            )
            history = await db.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.runtime_session_id == "private-expert-history"
                )
            )
            assert all(
                item.runtime_agent_profile_id != first.runtime_agent_profile_id
                for item in installed
            )
            assert history is not None and history.status == "expert_deleted"
            with pytest.raises(PermissionError, match="runtime agent profile not found"):
                await RuntimeSessionService(
                    SqlAlchemyRuntimeSessionStore(db)
                ).create_session(
                    principal=Principal(org_id=1, user_id=1),
                    command=CreateRuntimeSessionCommand(
                        runtime_workspace_id=1,
                        title="已卸载专家不可再创建会话",
                        runtime_agent_profile_id=first.runtime_agent_profile_id,
                    ),
                )

            second = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="可重新创建的专家",
                ),
            )

            assert second.runtime_agent_profile_id != first.runtime_agent_profile_id
            retired_package = await db.get(ExpertPackage, first.expert_package_id)
            assert retired_package is not None
            assert retired_package.active_name_key is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_private_expert_saves_version_and_rejects_workspace_name_conflict() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="版本化专家",
                ),
            )
            await orchestrator.update(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
                command=UpdateExpertRequest(description="第二版"),
            )

            versions = (
                (
                    await db.execute(
                        select(ExpertPackageVersion)
                        .where(ExpertPackageVersion.expert_package_id == created.expert_package_id)
                        .order_by(ExpertPackageVersion.version)
                    )
                )
                .scalars()
                .all()
            )
            assert [(item.version, item.status) for item in versions] == [
                (1, "private"),
                (2, "private"),
            ]

            with pytest.raises(ExpertProfileError) as duplicate:
                await orchestrator.create(
                    principal=Principal(org_id=1, user_id=1),
                    command=CreateExpertRequest(
                        runtime_workspace_id=1,
                        name="  版本化专家  ",
                    ),
                )
            assert duplicate.value.code == "expert_name_conflict"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_published_expert_save_requires_summary_and_supersedes_pending_review() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="可发布专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert created.review_id is not None
            await orchestrator.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )

            with pytest.raises(ExpertProfileError) as missing_summary:
                await orchestrator.update(
                    principal=Principal(org_id=1, user_id=1),
                    runtime_workspace_id=1,
                    runtime_agent_profile_id=created.runtime_agent_profile_id,
                    command=UpdateExpertRequest(description="未说明改动"),
                )
            assert missing_summary.value.code == "expert_change_summary_required"

            first_release = await orchestrator.update(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
                command=UpdateExpertRequest(
                    description="第一轮市场改动",
                    change_summary="优化专家说明",
                ),
            )
            assert first_release.review_status == "pending"
            first_pending_id = first_release.review_id

            second_release = await orchestrator.update(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
                command=UpdateExpertRequest(
                    description="第二轮市场改动",
                    change_summary="补充发布说明",
                ),
            )
            assert second_release.review_status == "pending"
            assert second_release.review_id != first_pending_id
            superseded = await db.get(TemplateReview, first_pending_id)
            assert superseded is not None
            assert superseded.status == "superseded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_update_keeps_previous_market_version_discoverable() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="审核期间持续服务专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert created.review_id is not None
            await service.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )
            before = await service.list_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
            )
            old_version_id = before[0].expert_package_version_id

            pending = await service.update(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
                command=UpdateExpertRequest(
                    description="等待审核的新说明",
                    change_summary="更新说明",
                ),
            )
            assert pending.review_id is not None
            during = await service.list_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
            )
            assert during[0].expert_package_version_id == old_version_id
            await service.reject_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=pending.review_id,
                review_comment="暂不通过",
            )
            after = await service.list_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
            )
            assert after[0].expert_package_version_id == old_version_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approval_acquires_org_market_name_lease() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            first = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="市场唯一名",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            second = await service.create(
                principal=Principal(org_id=1, user_id=2),
                command=CreateExpertRequest(
                    runtime_workspace_id=2,
                    name="市场唯一名",
                    share_scope=ExpertShareScope(user_ids=[1]),
                ),
            )
            assert first.review_id is not None and second.review_id is not None
            await service.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=first.review_id,
                command=ApproveExpertReviewRequest(),
            )
            with pytest.raises(ExpertProfileError) as conflict:
                await service.approve_review(
                    org_id=1,
                    reviewer_user_id=1,
                    review_id=second.review_id,
                    command=ApproveExpertReviewRequest(),
                )
            assert conflict.value.code == "expert_market_name_conflict"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unified_editor_version_failure_does_not_leave_succeeded_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EX-CFG-SAGA-01: release failure cannot follow a succeeded operation."""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="版本故障专家",
                ),
            )
            version_count = int(await db.scalar(select(func.count(ExpertPackageVersion.id))) or 0)

            async def fail_version(_self: Any, **_kwargs: Any) -> ExpertPackageVersion:
                raise RuntimeError("injected version failure")

            monkeypatch.setattr(
                ExpertAuthoringService,
                "save_existing",
                fail_version,
            )
            with pytest.raises((ExpertProfileError, RuntimeError)):
                await orchestrator.save_configuration(
                    principal=Principal(org_id=1, user_id=1),
                    runtime_workspace_id=1,
                    runtime_agent_profile_id=created.runtime_agent_profile_id,
                    command=UpdateExpertConfigurationRequest(
                        name=created.display_name,
                        description="触发配置版本故障",
                        soul_content=created.soul_content,
                        capabilities=created.capabilities,
                        share_scope=ExpertShareScope(),
                    ),
                )

            operation = await db.scalar(
                select(ExpertProfileOperation)
                .where(
                    ExpertProfileOperation.runtime_agent_profile_id
                    == created.runtime_agent_profile_id,
                    ExpertProfileOperation.action == "configure",
                )
                .order_by(ExpertProfileOperation.id.desc())
            )
            # operation + desired profile + prepared version are one initial
            # transaction, so version preparation failure leaves no attempt.
            assert operation is None
            assert (
                int(await db.scalar(select(func.count(ExpertPackageVersion.id))) or 0)
                == version_count
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_submitted_scope_controls_market_and_install_is_unique() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="市场写作专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert created.review_id is not None
            assert created.review_status == "pending"

            approved = await orchestrator.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )

            assert approved.status == "approved"
            rollout = await db.scalar(select(AssetRolloutBatch))
            assert rollout is not None
            assert rollout.asset_type == "expert"
            assert rollout.asset_id == created.expert_package_id
            package = await db.get(ExpertPackage, created.expert_package_id)
            assert package is not None
            assert rollout.target_version_id == package.current_version_id
            permissions = (
                (
                    await db.execute(
                        select(Permission).where(
                            Permission.asset_type == "expert_package",
                            Permission.asset_id == created.expert_package_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [(item.grantee_type, item.grantee_id) for item in permissions] == [("user", "2")]

            visible = await orchestrator.list_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
            )
            hidden = await orchestrator.list_market(
                principal=Principal(org_id=1, user_id=3),
                runtime_workspace_id=3,
            )
            assert [item.expert_package_id for item in visible] == [created.expert_package_id]
            assert hidden == []

            installed = await orchestrator.install_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                expert_package_id=int(created.expert_package_id),
            )
            assert installed.ownership_type == "market_installed"
            binding = await db.scalar(
                select(InstalledExpertBinding).where(
                    InstalledExpertBinding.runtime_agent_profile_id
                    == installed.runtime_agent_profile_id
                )
            )
            assert binding is not None

            with pytest.raises(ExpertProfileError) as duplicate:
                await orchestrator.install_market(
                    principal=Principal(org_id=1, user_id=2),
                    runtime_workspace_id=2,
                    expert_package_id=int(created.expert_package_id),
                )
            assert duplicate.value.code == "expert_already_installed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_creator_uninstall_withdraws_market_item_but_keeps_other_install_usable() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(db)
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="共享洞察专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert created.review_id is not None
            await orchestrator.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )
            installed = await orchestrator.install_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                expert_package_id=int(created.expert_package_id),
            )
            installed_profile = await db.get(
                RuntimeAgentProfile,
                installed.runtime_agent_profile_id,
            )
            assert installed_profile is not None
            installed_profile.applied_revision = installed_profile.desired_revision
            installed_profile.applied_digest = installed_profile.desired_digest
            installed_profile.status = "active"
            db.add(
                RuntimeAgentProfile(
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=2,
                    hermes_profile_name="default",
                    profile_kind="default",
                    display_name="安装者数字人",
                    ownership_type="default",
                    default_workspace_key=2,
                    status="active",
                )
            )
            await db.commit()

            await orchestrator.uninstall(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
            )

            package = await db.get(ExpertPackage, created.expert_package_id)
            binding = await db.scalar(
                select(InstalledExpertBinding).where(
                    InstalledExpertBinding.runtime_agent_profile_id
                    == installed.runtime_agent_profile_id
                )
            )
            installed_profile = await db.get(
                RuntimeAgentProfile,
                installed.runtime_agent_profile_id,
            )
            installer_experts = await orchestrator.list_installed(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
            )
            assert package is not None and package.status == "deleted"
            assert binding is not None and binding.status == "enabled"
            assert installed_profile is not None and installed_profile.status == "active"
            assert len(installer_experts) == 1
            assert installer_experts[0].market_removed is True

            session = await RuntimeSessionService(
                SqlAlchemyRuntimeSessionStore(db)
            ).create_session(
                principal=Principal(org_id=1, user_id=2),
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=2,
                    title="继续使用已安装共享专家",
                    runtime_agent_profile_id=installed.runtime_agent_profile_id,
                ),
            )
            assert session.runtime_agent_profile_id == installed.runtime_agent_profile_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_market_detail_uses_frozen_artifacts_and_respects_visibility() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="冻结详情专家",
                    soul_content="面向获授权市场用户公开的人格设定",
                    capabilities=ExpertCapabilitySelection(
                        enterprise_knowledge_enabled=True,
                    ),
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert created.review_id is not None
            await orchestrator.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )
            version = await db.scalar(
                select(ExpertPackageVersion).where(
                    ExpertPackageVersion.expert_package_id == created.expert_package_id
                )
            )
            assert version is not None
            version.prompt_snapshot = "面向获授权市场用户公开的系统提示词"
            db.add_all(
                [
                    ExpertVersionArtifact(
                        org_id=1,
                        expert_package_version_id=int(version.id),
                        artifact_type="skill_package",
                        source_type="public",
                        source_asset_id=11,
                        source_version_id=12,
                        name="冻结写作 Skill",
                        description="按发布版本写作",
                        payload={"manifest": {"secret": "not-public"}},
                        status="ready",
                    ),
                    ExpertVersionArtifact(
                        org_id=1,
                        expert_package_version_id=int(version.id),
                        artifact_type="mcp_config",
                        source_type="public",
                        source_asset_id=21,
                        name="冻结搜索 MCP",
                        description="检索资料",
                        payload={"runtime_config": {"token": "not-public"}},
                        status="ready",
                    ),
                    ExpertVersionArtifact(
                        org_id=1,
                        expert_package_version_id=int(version.id),
                        artifact_type="knowledge_reference",
                        source_type="personal",
                        source_asset_id=31,
                        name="冻结品牌知识库",
                        description="品牌资料",
                        payload={"knowledge_base_id": 31},
                        status="ready",
                    ),
                ]
            )
            await db.commit()

            detail = await orchestrator.market_detail(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                expert_package_id=int(created.expert_package_id),
            )
            serialized = detail.model_dump()
            assert detail.soul_content == "面向获授权市场用户公开的人格设定"
            assert detail.system_prompt == "面向获授权市场用户公开的系统提示词"
            assert [item.model_dump() for item in detail.skills] == [
                {"name": "冻结写作 Skill", "description": "按发布版本写作"}
            ]
            assert [item.model_dump() for item in detail.mcps] == [
                {"name": "冻结搜索 MCP", "description": "检索资料"}
            ]
            assert [item.model_dump() for item in detail.knowledge_bases] == [
                {"name": "冻结品牌知识库", "description": "品牌资料"}
            ]
            assert "enterprise_knowledge_enabled" not in serialized
            assert "payload" not in serialized
            assert "source_asset_id" not in str(serialized)
            assert "source_version_id" not in str(serialized)
            assert "source_metadata" not in str(serialized)
            assert "storage_uri" not in str(serialized)
            assert "sha256" not in str(serialized)
            assert "token" not in str(serialized)
            assert "not-public" not in str(serialized)

            with pytest.raises(ExpertProfileError) as hidden:
                await orchestrator.market_detail(
                    principal=Principal(org_id=1, user_id=3),
                    runtime_workspace_id=3,
                    expert_package_id=int(created.expert_package_id),
                )
            assert hidden.value.code == "market_expert_not_found"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_creator_market_item_is_owned_and_cannot_be_installed_again() -> None:
    engine, sessions = await _database()
    hermes = FakeHermesProfiles()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="创建者自己的专家",
                    share_scope=ExpertShareScope(user_ids=[1]),
                ),
            )
            assert created.review_id is not None
            await orchestrator.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )

            market = await orchestrator.list_market(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
            )

            assert len(market) == 1
            assert market[0].installed is False
            assert market[0].allowed_actions.install is False
            assert market[0].installed_runtime_agent_profile_id is None
            assert (
                await db.scalar(
                    select(InstalledExpertBinding).where(
                        InstalledExpertBinding.runtime_agent_profile_id
                        == created.runtime_agent_profile_id
                    )
                )
                is None
            )
            create_calls = sum(name == "create_profile" for name, _ in hermes.calls)

            with pytest.raises(ExpertProfileError) as duplicate:
                await orchestrator.install_market(
                    principal=Principal(org_id=1, user_id=1),
                    runtime_workspace_id=1,
                    expert_package_id=int(created.expert_package_id),
                )

            assert duplicate.value.code == "expert_already_installed"
            assert sum(name == "create_profile" for name, _ in hermes.calls) == create_calls
            profiles = (
                (
                    await db.execute(
                        select(RuntimeAgentProfile).where(
                            RuntimeAgentProfile.expert_package_id == created.expert_package_id,
                            RuntimeAgentProfile.status != "deleted",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [int(profile.id) for profile in profiles] == [created.runtime_agent_profile_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_organization_scope_is_visible_to_org_and_removed_on_unpublish() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            orchestrator = ExpertProfileOrchestrator(
                db,
            )
            created = await orchestrator.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="全组织专家",
                    share_scope=ExpertShareScope(organization=True),
                ),
            )
            assert created.review_id is not None
            assert created.share_scope.organization is True

            approved = await orchestrator.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=created.review_id,
                command=ApproveExpertReviewRequest(),
            )
            assert approved.share_scope.organization is True
            assert [
                review.review_id for review in await orchestrator.list_admin_reviews(org_id=1)
            ] == [created.review_id]

            permission = await db.scalar(
                select(Permission).where(
                    Permission.org_id == 1,
                    Permission.asset_type == "expert_package",
                    Permission.asset_id == created.expert_package_id,
                )
            )
            assert permission is not None
            assert (permission.grantee_type, permission.grantee_id) == (
                "all",
                "*",
            )

            for user_id in (2, 3):
                visible = await orchestrator.list_market(
                    principal=Principal(org_id=1, user_id=user_id),
                    runtime_workspace_id=user_id,
                )
                assert [item.expert_package_id for item in visible] == [created.expert_package_id]

            assert (
                await orchestrator.list_market(
                    principal=Principal(org_id=2, user_id=99),
                    runtime_workspace_id=99,
                )
                == []
            )

            await orchestrator.admin_unpublish(
                org_id=1,
                expert_package_id=int(created.expert_package_id),
            )
            assert (
                await db.scalar(
                    select(Permission.id).where(
                        Permission.org_id == 1,
                        Permission.asset_type == "expert_package",
                        Permission.asset_id == created.expert_package_id,
                    )
                )
                is None
            )
            assert (
                await orchestrator.list_market(
                    principal=Principal(org_id=1, user_id=2),
                    runtime_workspace_id=2,
                )
                == []
            )
            assert await orchestrator.list_admin_reviews(org_id=1) == []
            assert (
                await orchestrator.list_admin_reviews(
                    org_id=1,
                    status="approved",
                )
                == []
            )
            persisted_review = await db.get(TemplateReview, created.review_id)
            assert persisted_review is not None
            assert persisted_review.status == "approved"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_organization_scope_requires_org_wide_private_kb_permission() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            knowledge_base = KnowledgeBase(
                org_id=1,
                owner_user_id=1,
                slug="private-kb",
                name="私有知识库",
                kind="personal",
                provider="filebase",
                visibility="private",
                status="active",
            )
            db.add(knowledge_base)
            await db.commit()
            await db.refresh(knowledge_base)
            orchestrator = ExpertProfileOrchestrator(
                db,
            )

            with pytest.raises(ExpertProfileError) as uncovered:
                await orchestrator._validate_capability_share_coverage(
                    org_id=1,
                    scope=ExpertShareScope(organization=True),
                    knowledge_base_ids=[int(knowledge_base.id)],
                )
            assert uncovered.value.code == "expert_capability_scope_not_covered"

            db.add(
                Permission(
                    org_id=1,
                    asset_type="knowledge_base",
                    asset_id=int(knowledge_base.id),
                    grantee_type="all",
                    grantee_id="*",
                    kb_role="retrieve",
                )
            )
            await db.commit()
            await orchestrator._validate_capability_share_coverage(
                org_id=1,
                scope=ExpertShareScope(organization=True),
                knowledge_base_ids=[int(knowledge_base.id)],
            )
    finally:
        await engine.dispose()
