from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
import yaml

from app.hermes_runtime.constants import PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID
from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.hermes_runtime.projection import (
    KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
    KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
)
from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentRuntimeBinding,
)
from app.services.application_agent_runtime import (
    ApplicationAgentCapabilityPlan,
    ApplicationAgentConfigBootstrapper,
    ApplicationAgentMcpProjection,
    ApplicationAgentRuntimeError,
    ApplicationAgentSkillProjection,
    SqlAlchemyApplicationAgentBindingStatusStore,
    SqlAlchemyApplicationAgentRuntimeManager,
)
from app.services.runtime_image_config import RuntimeImageConfig, StaticRuntimeImageConfigReader
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
    RuntimePlatformControlKeyProvision,
)
from app.services.runtime_maintenance import RuntimeMaintenanceError
from app.services.runtime_workspace import RuntimeBindingStatusPatch
from app.hermes_runtime.skill_visibility_policy import INITIAL_SKILL_POLICY_CONFIG_KEY


CURRENT_RUNTIME_IMAGE = "kind.local/hermes-agent:v0.19.1-cc4cab2f59-32285038ed-df7c7313-candidate"


class OpenMaintenanceService:
    async def require_admission_open(self, *, lock: bool = False) -> None:
        assert lock is True


class FrozenMaintenanceService:
    async def require_admission_open(self, *, lock: bool = False) -> None:
        assert lock is True
        raise RuntimeMaintenanceError(
            "MAINTENANCE_ADMISSION_FROZEN",
            retryable=True,
        )


class FakeBaseBootstrapper:
    async def ensure_config(self, spec: KnativeRuntimeSpec) -> None:
        home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "USER.md").write_text(
            "creator identity",
            encoding="utf-8",
        )
        (home / "memories" / "MEMORY.md").write_text(
            "creator memory",
            encoding="utf-8",
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "_config_version": 1,
                    "agent": {
                        "disabled_toolsets": ["cronjob"],
                        "system_prompt": KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
                    },
                    "memory": {
                        "memory_enabled": True,
                        "user_profile_enabled": True,
                    },
                    "toolsets": ["kanban"],
                    "plugins": {
                        "enabled": [
                            "enterprise-knowledge",
                            "personal-knowledge",
                            "kanban-lifecycle",
                            "expert-roster",
                            KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
                        ]
                    },
                    "model": {
                        "default": "1",
                        "default_model_entry_id": 1,
                    },
                    "providers": {
                        "platform": {
                            "default_model": "1",
                            "models": {
                                "1": {
                                    "context_length": 32768,
                                    "supports_vision": False,
                                },
                                "19": {
                                    "context_length": 131072,
                                    "supports_vision": False,
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )


class FakeScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class FakeDeleteSession:
    def __init__(
        self,
        binding: ApplicationAgentRuntimeBinding | None,
    ) -> None:
        self.binding = binding

    async def execute(self, _statement: object) -> FakeScalarResult:
        return FakeScalarResult(self.binding)


class FakeDeleteK8sClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def delete_kservice(self, *, namespace: str, name: str) -> None:
        self.calls.append(("kservice", name, namespace))

    async def delete_secret(self, *, namespace: str, name: str) -> None:
        self.calls.append(("secret", name, namespace))

    async def delete_persistent_volume_claim(
        self,
        *,
        namespace: str,
        name: str,
    ) -> None:
        self.calls.append(("pvc", name, namespace))

    async def delete_persistent_volume(self, *, name: str) -> None:
        self.calls.append(("pv", name, None))


class FakeEnsureResult:
    def __init__(
        self,
        *,
        first: object | None = None,
        scalar: object | None = None,
    ) -> None:
        self.first_value = first
        self.scalar_value = scalar

    def first(self) -> object | None:
        return self.first_value

    def scalar_one_or_none(self) -> object | None:
        return self.scalar_value


class FakeEnsureSession:
    def __init__(self, *results: FakeEnsureResult) -> None:
        self.results = list(results)
        self.execute_count = 0

    async def execute(self, _statement: object) -> FakeEnsureResult:
        self.execute_count += 1
        return self.results.pop(0)


class SqlAlchemyFakeEnsureSession(AsyncSession):
    """Minimal AsyncSession-shaped double for release-contract selection."""

    def __init__(self, *results: FakeEnsureResult) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeEnsureResult:
        self.statements.append(statement)
        return self.results.pop(0)


class FakeRuntimeApiKeyCodec:
    def decrypt(self, _value: str) -> str:
        return "api-key"


class FakePlatformControlKeyStore:
    async def ensure_active_key(self, **kwargs):
        return RuntimePlatformControlKeyProvision(
            material=RuntimePlatformControlKeyMaterial(
                binding_type="application_runtime",
                binding_id=int(kwargs["binding_id"]),
                runtime_id=str(kwargs["runtime_id"]),
                key_version=1,
                root_key=b"k" * 32,
            ),
            created=False,
        )


class FakeExistingPvcClient:
    async def get_persistent_volume_claim(
        self,
        *,
        namespace: str,
        name: str,
    ) -> dict:
        assert namespace == "hermes-runtimes"
        assert name == "application-agent-31-root"
        return {
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "5Gi"}},
            }
        }


def _spec(root: Path) -> KnativeRuntimeSpec:
    return KnativeRuntimeSpec(
        org_id=1,
        user_id=2,
        user_name="管理员",
        user_code="admin-2",
        runtime_workspace_id=31,
        runtime_id="application-agent-runtime-31",
        namespace="hermes-runtimes",
        service_name="application-agent-31",
        image="hermes:test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="application-agent-31-api-key",
        runtime_root_pvc_name="application-agent-31-root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="application-agent-31-root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="node-1",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        application_agent_id=31,
        min_scale=1,
        max_scale=1,
    )


@pytest.mark.asyncio
async def test_existing_application_runtime_preserves_bound_pvc_contract(
    tmp_path: Path,
) -> None:
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        SqlAlchemyFakeEnsureSession(),
        k8s_client=FakeExistingPvcClient(),  # type: ignore[arg-type]
    )
    desired = replace(
        _spec(tmp_path),
        pvc_storage_size="20Gi",
        runtime_root_access_mode="ReadWriteOncePod",
    )

    actual = await manager._preserve_existing_pvc_contract(
        desired,
        binding_created=False,
    )

    assert actual.pvc_storage_size == "5Gi"
    assert actual.runtime_root_access_mode == "ReadWriteOnce"


def test_ready_application_runtime_rebuild_uses_steady_state() -> None:
    assert (
        SqlAlchemyApplicationAgentRuntimeManager._runtime_startup_mode(
            binding_created=False,
            previous_binding_status="ready",
            runtime_root_pvc_uid="pvc-uid-31",
            pvc_storage_identity_hash="sha256:" + ("a" * 64),
        )
        == "steady_state"
    )
    assert (
        SqlAlchemyApplicationAgentRuntimeManager._runtime_startup_mode(
            binding_created=False,
            previous_binding_status="failed",
            runtime_root_pvc_uid="pvc-uid-31",
            pvc_storage_identity_hash="sha256:" + ("a" * 64),
        )
        == "steady_state"
    )


@pytest.mark.asyncio
async def test_new_application_runtime_uses_env_config_without_release_queries() -> None:
    session = SqlAlchemyFakeEnsureSession()
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,
        k8s_client=object(),  # type: ignore[arg-type]
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
    )

    contract = await manager._desired_runtime_contract(binding=None)

    assert contract == (
        CURRENT_RUNTIME_IMAGE,
        None,
        "f80f453ae0679347e38abc917c7f94f717bf96c5",
        25,
        34,
    )
    assert session.statements == []


@pytest.mark.asyncio
async def test_application_runtime_isolates_memory_and_enables_selected_knowledge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.chown", lambda *_args, **_kwargs: None)
    agent = ApplicationAgent(
        id=31,
        org_id=1,
        slug="ticket-assistant",
        name="工单助手",
        status="unpublished",
        created_by_user_id=2,
        updated_by_user_id=2,
        config={
            "system_prompt": "只处理工单。",
            "enterprise_knowledge_enabled": True,
            INITIAL_SKILL_POLICY_CONFIG_KEY: 1,
            "knowledge_bindings": [
                {
                    "knowledge_base_id": 7,
                    "access_mode": "delegated_user",
                }
            ],
        },
        config_generation=4,
        apply_status="applying",
    )

    await ApplicationAgentConfigBootstrapper(
        FakeBaseBootstrapper()  # type: ignore[arg-type]
    ).ensure_config(_spec(tmp_path), agent=agent)

    home = tmp_path / "home"
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["memory"] == {
        "memory_enabled": False,
        "user_profile_enabled": False,
    }
    assert config["toolsets"] == []
    assert config["agent"]["disabled_toolsets"] == [
        "cronjob",
        "clarify",
        "kanban",
    ]
    assert config["platform"]["skill_visibility"]["initial_policy_version"] == 1
    assert len(config["skills"]["disabled"]) == 70
    assert "system_prompt" not in config["agent"]
    assert config["plugins"]["enabled"] == [
        "enterprise-knowledge",
        "personal-knowledge",
        PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    ]
    assert KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID not in (config["plugins"]["enabled"])
    assert not (home / "memories" / "USER.md").exists()
    assert not (home / "memories" / "MEMORY.md").exists()
    assert "只处理工单" in (home / "SOUL.md").read_text(encoding="utf-8")
    for log_name in (
        "agent.log",
        "errors.log",
        "dashboard-auth.log",
        "gui.log",
    ):
        log_path = home / "logs" / log_name
        assert log_path.is_file()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o660


@pytest.mark.asyncio
async def test_application_runtime_projects_configured_model_over_admin_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.chown", lambda *_args, **_kwargs: None)
    agent = ApplicationAgent(
        id=31,
        org_id=1,
        slug="ticket-assistant",
        name="工单助手",
        status="unpublished",
        created_by_user_id=2,
        updated_by_user_id=2,
        config={"model_entry_id": 19},
        config_generation=4,
        apply_status="applying",
    )

    await ApplicationAgentConfigBootstrapper(
        FakeBaseBootstrapper()  # type: ignore[arg-type]
    ).ensure_config(_spec(tmp_path), agent=agent)

    home = tmp_path / "home"
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"]["default"] == "19"
    assert config["model"]["default_model_entry_id"] == 19
    assert config["providers"]["platform"]["default_model"] == "19"
    contract = json.loads(
        (home / "platform" / "application-agent.json").read_text(encoding="utf-8")
    )
    assert contract["model_entry_id"] == 19


@pytest.mark.asyncio
async def test_application_runtime_rejects_configured_model_not_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.chown", lambda *_args, **_kwargs: None)

    class MissingModelBootstrapper(FakeBaseBootstrapper):
        async def ensure_config(self, spec: KnativeRuntimeSpec) -> None:
            await super().ensure_config(spec)
            path = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path / "config.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            config["providers"]["platform"]["models"].pop("19")
            path.write_text(
                yaml.safe_dump(config),
                encoding="utf-8",
            )

    agent = ApplicationAgent(
        id=31,
        org_id=1,
        slug="ticket-assistant",
        name="工单助手",
        status="unpublished",
        created_by_user_id=2,
        updated_by_user_id=2,
        config={"model_entry_id": 19},
        config_generation=4,
        apply_status="applying",
    )

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_model_not_visible",
    ):
        await ApplicationAgentConfigBootstrapper(
            MissingModelBootstrapper()  # type: ignore[arg-type]
        ).ensure_config(_spec(tmp_path), agent=agent)


@pytest.mark.asyncio
async def test_application_runtime_projects_selected_skill_and_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.chown", lambda *_args, **_kwargs: None)
    source = tmp_path / "skill-source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: work-daily-report\n---\n\n# Daily report\n",
        encoding="utf-8",
    )
    private_source = tmp_path / "private-skill-source"
    private_source.mkdir()
    (private_source / "SKILL.md").write_text(
        "---\nname: private-ticket\n---\n\n# Private ticket\n",
        encoding="utf-8",
    )
    agent = ApplicationAgent(
        id=31,
        org_id=1,
        slug="ticket-assistant",
        name="工单助手",
        status="unpublished",
        created_by_user_id=2,
        updated_by_user_id=2,
        config={
            "skill_asset_ids": [27],
            "application_skill_ids": [77],
            "mcp_asset_ids": [11],
            "knowledge_bindings": [],
        },
        config_generation=5,
        apply_status="applying",
    )
    plan = ApplicationAgentCapabilityPlan(
        skills=(
            ApplicationAgentSkillProjection(
                asset_id=27,
                version_id=42,
                runtime_name="application-31-27",
                source_directory=source,
                deployment_sha256="sha256-test",
            ),
            ApplicationAgentSkillProjection(
                asset_id=77,
                version_id=77,
                runtime_name="private-ticket",
                source_directory=private_source,
                deployment_sha256="private-sha256-test",
                display_name="private-ticket",
                source_type="application_private",
                application_skill_id=77,
            ),
        ),
        mcps=(
            ApplicationAgentMcpProjection(
                asset_id=11,
                slug="my-http-server",
                name="Web Search MCP",
                runtime_config={
                    "name": "Web Search MCP",
                    "transport": "streamable-http",
                    "enabled": True,
                    "url": "https://mcp.example.test/search",
                },
            ),
        ),
    )

    receipt = await ApplicationAgentConfigBootstrapper(
        FakeBaseBootstrapper()  # type: ignore[arg-type]
    ).ensure_config(_spec(tmp_path), agent=agent, capabilities=plan)
    receipt.commit()

    home = tmp_path / "home"
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"] == [
        "enterprise-knowledge",
        "personal-knowledge",
        PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    ]
    assert config["mcp_servers"]["my-http-server"]["url"] == ("https://mcp.example.test/search")
    installed_skill = home / "skills" / "application" / "market-27" / "SKILL.md"
    assert "name: application-31-27" in installed_skill.read_text(encoding="utf-8")
    installed_private_skill = home / "skills" / "application" / "private-77" / "SKILL.md"
    assert "name: private-ticket" in (installed_private_skill.read_text(encoding="utf-8"))
    contract = json.loads(
        (home / "platform" / "application-agent.json").read_text(encoding="utf-8")
    )
    assert contract["skills"][0]["asset_id"] == 27
    assert contract["skills"][1]["source_type"] == "application_private"
    assert contract["skills"][1]["display_name"] == "private-ticket"
    assert contract["application_skill_ids"] == [77]
    assert contract["mcps"][0]["slug"] == "my-http-server"
    assert contract["capability_projection"] == {
        "version": 1,
        "config_generation": 5,
    }


def test_application_capability_plan_rejects_duplicate_runtime_skill_names(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_skill_name_conflict",
    ):
        ApplicationAgentCapabilityPlan(
            skills=(
                ApplicationAgentSkillProjection(
                    asset_id=1,
                    version_id=1,
                    runtime_name="Wiki-Hermes",
                    source_directory=tmp_path,
                    deployment_sha256="sha-a",
                ),
                ApplicationAgentSkillProjection(
                    asset_id=2,
                    version_id=2,
                    runtime_name="wiki-hermes",
                    source_directory=tmp_path,
                    deployment_sha256="sha-b",
                    source_type="application_private",
                    application_skill_id=2,
                ),
            )
        )


class FakeApplicationReloadAdapter:
    def __init__(
        self,
        *,
        listed_skills: list[dict[str, str]] | None = None,
        resolved_tools: list[dict[str, str]] | None = None,
    ) -> None:
        self.listed_skills = (
            listed_skills if listed_skills is not None else [{"name": "wiki-hermes"}]
        )
        self.skill_reloads = 0
        self.resolved_tools = (
            resolved_tools
            if resolved_tools is not None
            else [
                {"name": "enterprise_knowledge_read"},
                {"name": "enterprise_sql_context"},
                {"name": "enterprise_sql_query"},
                {"name": "enterprise_document_search"},
                {"name": "personal_knowledge_search"},
            ]
        )
        self.mcp_reloads = 0
        self.capability_reads = 0
        self.skill_list_reads = 0

    async def reload_skills(
        self,
        _endpoint: object,
    ) -> dict[str, object]:
        self.skill_reloads += 1
        return {
            "output": "1 skill(s) available",
            "result": {"total": 1},
        }

    async def reload_mcp(self, _endpoint: object) -> None:
        self.mcp_reloads += 1

    async def list_skills(
        self,
        _endpoint: object,
    ) -> list[dict[str, str]]:
        self.skill_list_reads += 1
        return self.listed_skills

    async def capabilities(self, _endpoint: object) -> dict[str, object]:
        self.capability_reads += 1
        return {
            "skills": [{"name": "wiki-hermes"}],
            "mcp_servers": [],
            "resolved_tools": self.resolved_tools,
        }


@pytest.mark.asyncio
async def test_application_reload_uses_native_skill_reload() -> None:
    adapter = FakeApplicationReloadAdapter()
    plan = ApplicationAgentCapabilityPlan(
        skills=(
            ApplicationAgentSkillProjection(
                asset_id=5,
                version_id=5,
                runtime_name="wiki-hermes",
                source_directory=Path("/tmp/wiki-hermes"),
                deployment_sha256="sha",
                display_name="wiki-hermes",
                source_type="application_private",
                application_skill_id=5,
            ),
        )
    )

    await ApplicationAgentConfigBootstrapper.reload_and_verify(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        capabilities=plan,
    )

    assert adapter.skill_reloads == 1
    assert adapter.mcp_reloads == 1


@pytest.mark.asyncio
async def test_application_skill_only_reload_skips_mcp_and_full_capabilities() -> None:
    adapter = FakeApplicationReloadAdapter()
    plan = ApplicationAgentCapabilityPlan(
        skills=(
            ApplicationAgentSkillProjection(
                asset_id=5,
                version_id=5,
                runtime_name="wiki-hermes",
                source_directory=Path("/tmp/wiki-hermes"),
                deployment_sha256="sha",
            ),
        )
    )

    await ApplicationAgentConfigBootstrapper.reload_skills_and_verify(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        capabilities=plan,
    )

    assert adapter.skill_reloads == 1
    assert adapter.skill_list_reads == 1
    assert adapter.mcp_reloads == 0
    assert adapter.capability_reads == 0


@pytest.mark.asyncio
async def test_application_skill_only_reload_verifies_uninstall() -> None:
    adapter = FakeApplicationReloadAdapter(listed_skills=[])

    await ApplicationAgentConfigBootstrapper.reload_skills_and_verify(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        capabilities=ApplicationAgentCapabilityPlan(),
    )

    assert adapter.skill_list_reads == 1
    assert adapter.mcp_reloads == 0
    assert adapter.capability_reads == 0


@pytest.mark.asyncio
async def test_application_skill_only_reload_accepts_stale_uninstall_discovery() -> None:
    adapter = FakeApplicationReloadAdapter()

    await ApplicationAgentConfigBootstrapper.reload_skills_and_verify(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        capabilities=ApplicationAgentCapabilityPlan(),
    )

    assert adapter.skill_list_reads == 1
    assert adapter.mcp_reloads == 0
    assert adapter.capability_reads == 0


@pytest.mark.asyncio
async def test_application_reload_accepts_native_response_without_prompt_cache_ack() -> None:
    adapter = FakeApplicationReloadAdapter()

    await ApplicationAgentConfigBootstrapper.reload_and_verify(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
        capabilities=ApplicationAgentCapabilityPlan(),
    )

    assert adapter.skill_reloads == 1
    assert adapter.mcp_reloads == 1


@pytest.mark.asyncio
async def test_application_reload_previous_capabilities_uses_native_skill_reload() -> None:
    adapter = FakeApplicationReloadAdapter()

    await SqlAlchemyApplicationAgentRuntimeManager._reload_previous_capabilities(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
    )

    assert adapter.skill_reloads == 1
    assert adapter.mcp_reloads == 1


@pytest.mark.asyncio
async def test_application_reload_previous_skills_uses_native_skill_reload() -> None:
    adapter = FakeApplicationReloadAdapter()

    await SqlAlchemyApplicationAgentRuntimeManager._reload_previous_skills(
        adapter=adapter,  # type: ignore[arg-type]
        endpoint=object(),  # type: ignore[arg-type]
    )

    assert adapter.skill_reloads == 1
    assert adapter.mcp_reloads == 0


@pytest.mark.asyncio
async def test_application_reload_requires_enterprise_knowledge_tools() -> None:
    adapter = FakeApplicationReloadAdapter(
        resolved_tools=[
            {"name": "personal_knowledge_search"},
        ]
    )

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_knowledge_plugin_reload_failed",
    ):
        await ApplicationAgentConfigBootstrapper.reload_and_verify(
            adapter=adapter,  # type: ignore[arg-type]
            endpoint=object(),  # type: ignore[arg-type]
            capabilities=ApplicationAgentCapabilityPlan(),
        )

    assert adapter.mcp_reloads == 1
    assert adapter.capability_reads == 1


@pytest.mark.asyncio
async def test_application_reload_requires_personal_knowledge_tool() -> None:
    adapter = FakeApplicationReloadAdapter(
        resolved_tools=[
            {"name": "enterprise_knowledge_read"},
            {"name": "enterprise_sql_context"},
            {"name": "enterprise_sql_query"},
            {"name": "enterprise_document_search"},
        ]
    )

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_knowledge_plugin_reload_failed",
    ):
        await ApplicationAgentConfigBootstrapper.reload_and_verify(
            adapter=adapter,  # type: ignore[arg-type]
            endpoint=object(),  # type: ignore[arg-type]
            capabilities=ApplicationAgentCapabilityPlan(),
        )


@pytest.mark.asyncio
async def test_application_runtime_delete_removes_exact_resources_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "application-agents"
    agent_root = runtime_root / "1" / "31"
    (agent_root / "home").mkdir(parents=True)
    (agent_root / "home" / "config.yaml").write_text(
        "model: test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        settings,
        "HERMES_APPLICATION_AGENT_RUNTIME_ROOT",
        str(runtime_root),
    )
    binding = ApplicationAgentRuntimeBinding(
        id=9,
        org_id=1,
        application_agent_id=31,
        runtime_id="application-agent-runtime-31",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-31",
        runtime_root_pvc="application-agent-31-root",
        runtime_root_host_path=str(agent_root),
        config_generation=1,
        image="hermes:test",
        api_key_encrypted="encrypted",
        status="stopped",
    )
    k8s = FakeDeleteK8sClient()
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        FakeDeleteSession(binding),  # type: ignore[arg-type]
        k8s_client=k8s,  # type: ignore[arg-type]
    )

    await manager.delete_runtime(org_id=1, agent_id=31)

    assert k8s.calls == [
        ("kservice", "application-agent-31", "hermes-runtimes"),
        ("secret", "application-agent-31-api-key", "hermes-runtimes"),
        (
            "secret",
            "application-agent-31-platform-control",
            "hermes-runtimes",
        ),
        ("pvc", "application-agent-31-root", "hermes-runtimes"),
        ("pv", "application-agent-31-root-pv", None),
    ]
    assert not agent_root.exists()


@pytest.mark.asyncio
async def test_application_runtime_delete_removes_uploads_without_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "application-agents"
    agent_root = runtime_root / "1" / "31"
    upload = agent_root / "uploads" / "skills" / "77"
    upload.mkdir(parents=True)
    (upload / "SKILL.md").write_text(
        "---\nname: private-skill\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        settings,
        "HERMES_APPLICATION_AGENT_RUNTIME_ROOT",
        str(runtime_root),
    )
    k8s = FakeDeleteK8sClient()
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        FakeDeleteSession(None),  # type: ignore[arg-type]
        k8s_client=k8s,  # type: ignore[arg-type]
    )

    await manager.delete_runtime(org_id=1, agent_id=31)

    assert not agent_root.exists()
    assert k8s.calls == []
    assert runtime_root.exists()


def test_application_runtime_delete_rejects_recorded_path_outside_agent_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "application-agents"
    outside = tmp_path / "other-agent"
    outside.mkdir()
    monkeypatch.setattr(
        settings,
        "HERMES_APPLICATION_AGENT_RUNTIME_ROOT",
        str(runtime_root),
    )

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_runtime_path_invalid",
    ):
        SqlAlchemyApplicationAgentRuntimeManager._delete_runtime_directory(
            org_id=1,
            agent_id=31,
            recorded_path=str(outside),
        )

    assert outside.exists()


def test_application_runtime_failed_apply_never_uses_current_binding_fast_path(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home" / "platform"
    home.mkdir(parents=True)
    (home / "application-agent.json").write_text(
        json.dumps(
            {
                "capability_projection": {
                    "version": 1,
                    "config_generation": 9,
                }
            }
        ),
        encoding="utf-8",
    )
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="unpublished",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={},
        config_generation=9,
        apply_status="failed",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_pvc_uid="pvc-uid-19",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root_host_path=str(tmp_path),
        config_generation=9,
        image="hermes:application-skill-refresh",
        api_key_encrypted="encrypted",
        status="ready",
    )

    assert not SqlAlchemyApplicationAgentRuntimeManager._binding_is_current(
        agent=agent,
        binding=binding,
    )

    agent.apply_status = "applied"
    assert SqlAlchemyApplicationAgentRuntimeManager._binding_is_current(
        agent=agent,
        binding=binding,
    )


async def test_application_runtime_reuse_requires_live_kservice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    k8s_client = SimpleNamespace(
        kservice_exists=AsyncMock(return_value=False),
    )
    manager = object.__new__(SqlAlchemyApplicationAgentRuntimeManager)
    manager.k8s_client = k8s_client
    monkeypatch.setattr(
        SqlAlchemyApplicationAgentRuntimeManager,
        "_binding_is_current",
        staticmethod(lambda **_kwargs: True),
    )
    binding = SimpleNamespace(
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
    )

    assert not await manager._binding_is_reusable(
        agent=SimpleNamespace(),
        binding=binding,
    )
    k8s_client.kservice_exists.assert_awaited_once_with(
        namespace="hermes-runtimes",
        name="application-agent-19",
    )


@pytest.mark.asyncio
async def test_application_runtime_reuse_rejects_stale_kservice_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    k8s_client = SimpleNamespace(
        kservice_exists=AsyncMock(return_value=True),
        get_kservice=AsyncMock(
            return_value={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "env": [
                                        {
                                            "name": "RUNTIME_CONFIG_REVISION",
                                            "value": "generation-13",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "latestCreatedRevisionName": "application-agent-19-00002",
                    "latestReadyRevisionName": "application-agent-19-00002",
                },
            }
        ),
    )
    manager = object.__new__(SqlAlchemyApplicationAgentRuntimeManager)
    manager.k8s_client = k8s_client
    monkeypatch.setattr(
        SqlAlchemyApplicationAgentRuntimeManager,
        "_binding_is_current",
        staticmethod(lambda **_kwargs: True),
    )
    binding = SimpleNamespace(
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
    )

    status = await manager._kservice_generation_status(
        namespace="hermes-runtimes",
        name="application-agent-19",
        expected_generation=14,
    )
    assert status == (True, True, False)
    assert await manager._binding_is_reusable(
        agent=SimpleNamespace(config_generation=14),
        binding=binding,
    )
    assert k8s_client.get_kservice.await_count == 2


@pytest.mark.asyncio
async def test_application_runtime_reuse_rejects_unready_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    k8s_client = SimpleNamespace(
        kservice_exists=AsyncMock(return_value=True),
        get_kservice=AsyncMock(
            return_value={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "env": [
                                        {
                                            "name": "RUNTIME_CONFIG_REVISION",
                                            "value": "generation-14",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "latestCreatedRevisionName": "application-agent-19-00002",
                    "latestReadyRevisionName": "application-agent-19-00002",
                },
            }
        ),
        list_kservice_pods=AsyncMock(
            return_value=[
                {
                    "metadata": {},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": False}],
                    },
                }
            ]
        ),
    )
    manager = object.__new__(SqlAlchemyApplicationAgentRuntimeManager)
    manager.k8s_client = k8s_client
    monkeypatch.setattr(
        SqlAlchemyApplicationAgentRuntimeManager,
        "_binding_is_current",
        staticmethod(lambda **_kwargs: True),
    )
    binding = SimpleNamespace(
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
    )

    assert not await manager._binding_is_reusable(
        agent=SimpleNamespace(config_generation=14),
        binding=binding,
    )


def test_application_runtime_binding_requires_matching_model_contract(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "home" / "platform"
    platform.mkdir(parents=True)
    contract = {
        "capability_projection": {
            "version": 1,
            "config_generation": 9,
        }
    }
    path = platform / "application-agent.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="published",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={"model_entry_id": 19},
        config_generation=9,
        apply_status="applied",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_pvc_uid="pvc-uid-19",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root_host_path=str(tmp_path),
        config_generation=9,
        image="hermes:test",
        api_key_encrypted="encrypted",
        status="ready",
    )

    assert not SqlAlchemyApplicationAgentRuntimeManager._binding_is_current(
        agent=agent,
        binding=binding,
    )

    contract["model_entry_id"] = 19
    path.write_text(json.dumps(contract), encoding="utf-8")
    assert SqlAlchemyApplicationAgentRuntimeManager._binding_is_current(
        agent=agent,
        binding=binding,
    )


def test_application_online_apply_can_coalesce_unapplied_generations(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "home" / "platform"
    platform.mkdir(parents=True)
    (platform / "application-agent.json").write_text(
        json.dumps(
            {
                "skills": [{"runtime_name": "wiki-hermes"}],
                "capability_projection": {
                    "version": 1,
                    "config_generation": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="unpublished",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={"skill_asset_ids": [5]},
        config_generation=10,
        apply_status="applying",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_pvc_uid="pvc-uid-19",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root_host_path=str(tmp_path),
        config_generation=9,
        image="hermes:application-skill-refresh",
        api_key_encrypted="encrypted",
        endpoint_base_url="http://127.0.0.1:8012",
        endpoint_host_header="application-agent-19.hermes.local",
        status="ready",
    )

    assert SqlAlchemyApplicationAgentRuntimeManager._binding_supports_online_apply(
        agent=agent,
        binding=binding,
    )
    binding.config_generation = 8
    assert SqlAlchemyApplicationAgentRuntimeManager._binding_supports_online_apply(
        agent=agent,
        binding=binding,
    )
    binding.config_generation = 10
    assert not SqlAlchemyApplicationAgentRuntimeManager._binding_supports_online_apply(
        agent=agent,
        binding=binding,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "apply_scope",
    ["settings_only", "skills_only", "full"],
)
async def test_application_online_scope_applies_without_kubernetes(
    tmp_path: Path,
    apply_scope: str,
) -> None:
    platform = tmp_path / "home" / "platform"
    platform.mkdir(parents=True)
    (platform / "application-agent.json").write_text(
        json.dumps(
            {
                "capability_projection": {
                    "version": 1,
                    "config_generation": 9,
                }
            }
        ),
        encoding="utf-8",
    )
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="unpublished",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={"skill_asset_ids": [5]},
        config_generation=10,
        apply_status="applying",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_pvc_uid="pvc-uid-19",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root_host_path=str(tmp_path),
        config_generation=9,
        image=CURRENT_RUNTIME_IMAGE,
        api_key_encrypted="encrypted",
        endpoint_base_url="http://127.0.0.1:8012",
        endpoint_host_header="application-agent-19.hermes.local",
        status="ready",
    )
    creator = SimpleNamespace(id=7, name="管理员", user_code="10002")
    session = FakeEnsureSession(
        FakeEnsureResult(first=(agent, creator)),
        FakeEnsureResult(scalar=binding),
    )
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,  # type: ignore[arg-type]
        k8s_client=object(),  # type: ignore[arg-type]
        codec=FakeRuntimeApiKeyCodec(),  # type: ignore[arg-type]
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
        platform_control_key_store=FakePlatformControlKeyStore(),
        maintenance_service=OpenMaintenanceService(),  # type: ignore[arg-type]
    )
    manager._apply_online_config = AsyncMock()  # type: ignore[method-assign]
    manager._active_run_count = AsyncMock(  # type: ignore[method-assign]
        return_value=1
    )

    await manager.ensure_runtime(
        org_id=1,
        agent_id=19,
        apply_scope=apply_scope,  # type: ignore[arg-type]
    )

    manager._apply_online_config.assert_awaited_once_with(
        agent=agent,
        creator=creator,
        binding=binding,
        api_key="api-key",
    )
    manager._active_run_count.assert_not_awaited()
    assert session.execute_count == 2


@pytest.mark.asyncio
async def test_application_availability_ignores_pending_config_and_active_run(
    tmp_path: Path,
) -> None:
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="unpublished",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={"knowledge_bindings": [{"knowledge_base_id": 5}]},
        config_generation=10,
        apply_status="failed",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_pvc_uid="pvc-uid-19",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root_host_path=str(tmp_path),
        config_generation=9,
        image=CURRENT_RUNTIME_IMAGE,
        api_key_encrypted="encrypted",
        endpoint_base_url="http://127.0.0.1:8012",
        endpoint_host_header="application-agent-19.hermes.local",
        status="ready",
    )
    creator = SimpleNamespace(id=7, name="管理员", user_code="10002")
    session = FakeEnsureSession(
        FakeEnsureResult(first=(agent, creator)),
        FakeEnsureResult(scalar=binding),
    )
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,  # type: ignore[arg-type]
        k8s_client=object(),  # type: ignore[arg-type]
        platform_control_key_store=FakePlatformControlKeyStore(),
        maintenance_service=OpenMaintenanceService(),  # type: ignore[arg-type]
    )
    manager._binding_is_available = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    manager._apply_online_config = AsyncMock()  # type: ignore[method-assign]
    manager._active_run_count = AsyncMock(  # type: ignore[method-assign]
        return_value=1
    )

    await manager.ensure_runtime_available(org_id=1, agent_id=19)

    manager._binding_is_available.assert_awaited_once_with(binding=binding)
    manager._apply_online_config.assert_not_awaited()
    manager._active_run_count.assert_not_awaited()
    assert session.execute_count == 2


@pytest.mark.asyncio
async def test_online_apply_receipt_only_advances_its_exact_generation() -> None:
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="unpublished",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={},
        config_generation=6,
        applied_generation=3,
        apply_status="applying",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_host_path="/runtime-19",
        config_generation=3,
        image=CURRENT_RUNTIME_IMAGE,
        api_key_encrypted="encrypted",
        status="ready",
    )
    revision = ApplicationAgentConfigRevision(
        id=4,
        org_id=1,
        application_agent_id=19,
        generation=4,
        config_snapshot={},
        config_hash="a" * 64,
        created_by_user_id=7,
        apply_status="applying",
    )

    class Session:
        values = [binding, agent, revision]
        commits = 0

        async def scalar(self, _statement: object):
            return self.values.pop(0)

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,  # type: ignore[arg-type]
        k8s_client=object(),  # type: ignore[arg-type]
        maintenance_service=OpenMaintenanceService(),  # type: ignore[arg-type]
    )

    await manager._record_online_apply_success(
        agent_id=19,
        generation=4,
    )

    assert binding.config_generation == 4
    assert agent.applied_generation == 4
    assert agent.config_generation == 6
    assert agent.apply_status == "applying"
    assert revision.apply_status == "applied"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_full_runtime_ready_receipt_does_not_apply_newer_desired() -> None:
    agent = ApplicationAgent(
        id=19,
        org_id=1,
        slug="test1",
        name="test1",
        status="unpublished",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={},
        config_generation=6,
        applied_generation=3,
        apply_status="applying",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=19,
        org_id=1,
        application_agent_id=19,
        runtime_id="application-agent-runtime-19",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-19",
        runtime_root_pvc="application-agent-19-root",
        runtime_root_host_path="/runtime-19",
        config_generation=3,
        image=CURRENT_RUNTIME_IMAGE,
        api_key_encrypted="encrypted",
        status="updating",
    )
    revision = ApplicationAgentConfigRevision(
        id=4,
        org_id=1,
        application_agent_id=19,
        generation=4,
        config_snapshot={},
        config_hash="a" * 64,
        created_by_user_id=7,
        apply_status="applying",
    )

    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one(self) -> object:
            return self.value

        def scalar_one_or_none(self) -> object:
            return self.value

    class Session:
        values = [binding, agent, revision]
        commits = 0

        async def execute(self, _statement: object) -> Result:
            return Result(self.values.pop(0))

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    store = SqlAlchemyApplicationAgentBindingStatusStore(
        session,  # type: ignore[arg-type]
        agent_id=19,
        expected_config_generation=4,
    )

    await store.update_binding_status(RuntimeBindingStatusPatch(
        runtime_workspace_id=19,
        knative_status_url=None,
        route_host="application-agent-19.hermes.local",
        endpoint_base_url="http://127.0.0.1:8012",
        endpoint_host_header="application-agent-19.hermes.local",
        status="ready",
        last_error=None,
    ))

    assert binding.config_generation == 4
    assert agent.applied_generation == 4
    assert agent.config_generation == 6
    assert agent.apply_status == "applying"
    assert revision.apply_status == "applied"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_application_runtime_reconcile_rejects_concurrent_update() -> None:
    agent = ApplicationAgent(
        id=25,
        org_id=1,
        slug="application-25",
        name="应用 Agent 25",
        status="published",
        created_by_user_id=7,
        updated_by_user_id=7,
        config={"model_entry_id": 21},
        config_generation=14,
        apply_status="applying",
    )
    binding = ApplicationAgentRuntimeBinding(
        id=17,
        org_id=1,
        application_agent_id=25,
        runtime_id="application-agent-runtime-25",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-25",
        runtime_root_pvc="application-agent-25-root-g3",
        runtime_root_pvc_uid="pvc-uid-25",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        runtime_root_host_path="/data/hermes/application-agents/1/25-g3",
        config_generation=13,
        image=CURRENT_RUNTIME_IMAGE,
        api_key_encrypted="encrypted",
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="application-agent-25.hermes-runtimes.example",
        status="updating",
    )
    creator = SimpleNamespace(id=7, name="管理员", user_code="10002")
    session = SqlAlchemyFakeEnsureSession(
        FakeEnsureResult(first=(agent, creator)),
        FakeEnsureResult(scalar=binding),
    )
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,
        k8s_client=object(),  # type: ignore[arg-type]
        codec=FakeRuntimeApiKeyCodec(),  # type: ignore[arg-type]
        runtime_image_config=StaticRuntimeImageConfigReader(
            image=CURRENT_RUNTIME_IMAGE,
        ),
        platform_control_key_store=FakePlatformControlKeyStore(),
        maintenance_service=OpenMaintenanceService(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_runtime_busy",
    ):
        await manager.ensure_runtime(org_id=1, agent_id=25)

    assert "FOR UPDATE" in str(session.statements[1])


def test_application_runtime_stale_update_can_reenter_reconcile() -> None:
    binding = ApplicationAgentRuntimeBinding(
        id=17,
        org_id=1,
        application_agent_id=25,
        runtime_id="application-agent-runtime-25",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-25",
        runtime_root_pvc="application-agent-25-root",
        runtime_root_host_path="/runtime-25",
        config_generation=4,
        image=CURRENT_RUNTIME_IMAGE,
        api_key_encrypted="encrypted",
        status="updating",
    )
    now = datetime.now(timezone.utc)
    binding.updated_at = now - timedelta(
        seconds=settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS + 1
    )

    assert SqlAlchemyApplicationAgentRuntimeManager._binding_update_is_stale(
        binding,
        now=now,
    )


@pytest.mark.asyncio
async def test_application_runtime_ensure_fails_before_query_while_frozen() -> None:
    session = FakeEnsureSession()
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,  # type: ignore[arg-type]
        k8s_client=object(),  # type: ignore[arg-type]
        maintenance_service=FrozenMaintenanceService(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="MAINTENANCE_ADMISSION_FROZEN",
    ):
        await manager.ensure_runtime(org_id=1, agent_id=25)

    assert session.execute_count == 0


@pytest.mark.asyncio
async def test_application_runtime_reload_owns_updating_transition_and_target_identity() -> None:
    binding = ApplicationAgentRuntimeBinding(
        id=17,
        org_id=1,
        application_agent_id=25,
        runtime_id="application-agent-runtime-25",
        k8s_namespace="hermes-runtimes",
        knative_service="application-agent-25",
        runtime_root_pvc="application-agent-25-root",
        runtime_root_pvc_uid="pvc-uid-25",
        pvc_storage_identity_hash="sha256:" + "a" * 64,
        runtime_root_host_path="/data/hermes/application-agents/1/25",
        config_generation=14,
        image=CURRENT_RUNTIME_IMAGE,
        image_digest="sha256:" + "c" * 64,
        api_key_encrypted="encrypted",
        status="ready",
    )

    class Session:
        commits = 0

        async def scalar(self, _statement: object):
            return binding

        async def commit(self) -> None:
            self.commits += 1

    class Maintenance:
        async def snapshot(self, *, lock: bool = False):
            assert lock is True
            return SimpleNamespace(
                maintenance_active=False,
                maintenance_epoch=None,
            )

    session = Session()
    manager = SqlAlchemyApplicationAgentRuntimeManager(
        session,  # type: ignore[arg-type]
        k8s_client=object(),  # type: ignore[arg-type]
        maintenance_service=Maintenance(),  # type: ignore[arg-type]
    )
    manager._converge_runtime = AsyncMock()  # type: ignore[method-assign]
    target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-new",
        manifest_digest="sha256:" + "d" * 64,
        config_id="sha256:" + "e" * 64,
    )

    await manager.reload_runtime(binding_id=17, target=target)

    assert binding.image == target.image
    assert binding.image_digest == target.config_id
    assert binding.status == "updating"
    assert session.commits == 1
    manager._converge_runtime.assert_awaited_once_with(
        org_id=1,
        agent_id=25,
        apply_scope="full",
        reload_owned=True,
        expected_binding_id=17,
    )
