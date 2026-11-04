import json
import importlib.util
from pathlib import Path

import pytest
import yaml

from app.hermes_runtime import projection as projection_module
from app.config import settings
from app.hermes_runtime.projection import (
    DEFAULT_RUNTIME_SOUL,
    HermesWorkspaceProjector,
    KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
    KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
    ProfileConfigRenderer,
    ProjectionInput,
)
from app.services.runtime_config_bundle import config_bundle_digest


def test_profile_config_renderer_allows_runtime_fields_and_env_placeholders() -> None:
    entry = {
        "mcp_id": 31,
        "slug": "crm",
        "name": "CRM",
        "transport": "sse",
        "url": "https://mcp.example.test/sse",
        "headers": {"Authorization": "${MCP_CRM_TOKEN}"},
        "env": {"CRM_REGION": "${CRM_REGION}"},
        "description": "catalog metadata",
    }

    runtime = ProfileConfigRenderer.runtime_entry(entry, path="installed_mcps[0]")

    assert runtime == {
        "transport": "sse",
        "url": "https://mcp.example.test/sse",
        "headers": {"Authorization": "${MCP_CRM_TOKEN}"},
        "env": {"CRM_REGION": "${CRM_REGION}"},
    }


def test_projector_preserves_env_placeholder_mapping_keys(tmp_path: Path) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="Smoke",
        installed_mcps=(
            {
                "mcp_id": 31,
                "slug": "crm",
                "name": "CRM",
                "transport": "stdio",
                "command": "crm",
                "env": {"TOKEN": "${MCP_TOKEN}"},
            },
        ),
    )
    artifacts = HermesWorkspaceProjector().render_artifacts(payload)
    assert 'TOKEN: "${MCP_TOKEN}"' in artifacts["config.yaml"]


def test_profile_config_explicitly_disables_dashboard_turn_isolation(
    tmp_path: Path,
) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="No Compute Host",
    )

    document = yaml.safe_load(ProfileConfigRenderer.render_config(payload))

    assert document["dashboard"] == {"turn_isolation": False}
    assert document["model"]["provider"] == "custom:platform"
    assert "plugins" in document
    assert document["compression"] == {
        "max_attempts": 10,
        "target_ratio": 0.10,
        "proactive_prune_tokens": 64000,
    }


def test_profile_config_hides_legacy_skills_without_hiding_installed_names(
    tmp_path: Path,
) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="Curated Skills",
        installed_skills=(
            {"runtime_skill_name": "github-auth", "slug": "github-auth"},
            {"slug": "unknown-personal-skill"},
        ),
        user_disabled_skills=("hermes-agent", "unknown-personal-skill"),
    )

    document = yaml.safe_load(ProfileConfigRenderer.render_config(payload))
    disabled = set(document["skills"]["disabled"])
    platform_disabled = set(document["platform"]["skill_visibility"]["platform_disabled"])

    assert "codex" in disabled
    assert "hermes-desktop-plugins" in disabled
    assert "github-auth" not in platform_disabled
    assert "github-auth" not in disabled
    assert "hermes-agent" in disabled
    assert "unknown-personal-skill" in disabled
    assert "unknown-personal-skill" not in platform_disabled
    assert document["skills"]["external_dirs"] == ["/opt/hermes/skills"]


@pytest.mark.parametrize(
    "entry, message",
    [
        (
            {"slug": "crm", "url": "https://example.test", "unexpected": "x"},
            "unknown MCP config key",
        ),
        ({"slug": "crm", "headers": {"Authorization": "Bearer real-token"}}, "placeholders"),
        ({"slug": "crm", "env": {"MCP_TOKEN": "real-token"}}, "placeholders"),
        ({"slug": "crm", "api_key": "real-token"}, "forbidden MCP config key"),
        ({"slug": "crm", "args": "--readonly"}, "list of strings"),
        ({"slug": "crm", "url": "file:///etc/passwd"}, "http or https URL"),
        ({"slug": "crm", "transport": "streamable_http"}, "streamable_http transport requires url"),
        ({"slug": "crm", "transport": "stdio"}, "stdio transport requires command"),
    ],
)
def test_profile_config_renderer_rejects_unsafe_mcp_config(entry: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ProfileConfigRenderer.validate_mcp_entry(entry, path="installed_mcps[0]")


def test_projector_writes_minimal_home_without_upstream_secrets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=home,
        workspace_dir=workspace,
        name="默认数字人",
        default_model_id=9,
        visible_models=(
            {
                "model_entry_id": 9,
                "context_length": 128_000,
                "tags": ["chat"],
            },
            {
                "model_entry_id": 12,
                "context_length": 1_000_000,
                "tags": ["vision"],
            },
        ),
    )

    projection = HermesWorkspaceProjector().render(payload)

    assert (home / "config.yaml").exists()
    assert not (home / ".env").exists()
    assert (home / "SOUL.md").exists()
    assert (home / "memories").is_dir()
    assert not (home / "memories" / "MEMORY.md").exists()
    assert not (home / "memories" / "USER.md").exists()
    assert (home / "PERSONAL" / "PERSONAL_KB.md").exists()
    assert (home / "platform" / "config.snapshot.json").exists()
    assert (home / "platform" / "installed-experts.json").exists()
    assert (home / "platform" / "projection.json").exists()
    guard_root = home / "plugins" / KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID
    assert (guard_root / "plugin.yaml").is_file()
    assert (guard_root / "__init__.py").is_file()
    assert projection["runtime_workspace_id"] == 101
    assert projection["config_generation"] == 7
    assert projection["requires_reload"] is True
    assert projection["scopes"] == [
        "persona",
        "skills",
        "mcp",
        "knowledge",
        "experts",
        "model",
        "security",
    ]

    config = (home / "config.yaml").read_text()
    document = yaml.safe_load(config)
    assert document["_config_version"] == 33
    assert document["agent"]["system_prompt"] == KANBAN_ORCHESTRATOR_SYSTEM_PROMPT
    assert document["toolsets"] == ["kanban"]
    assert document["kanban"]["max_in_progress_per_profile"] == 1
    assert KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID in document["plugins"]["enabled"]
    assert document["model"]["default"] == "9"
    assert isinstance(document["model"]["default"], str)
    assert document["model"]["default_model_entry_id"] == 9
    assert document["model"]["ollama_num_ctx"] == 0
    assert document["providers"]["platform"]["default_model"] == "9"
    assert document["providers"]["platform"]["discover_models"] is False
    assert document["providers"]["platform"]["models"] == {
        "9": {"context_length": 128_000, "supports_vision": False},
        "12": {"context_length": 1_000_000, "supports_vision": True},
    }
    assert document["terminal"] == {"cwd": str(workspace)}
    assert "provider: custom:platform" in config
    assert "providers:" in config
    assert "key_env: OPENAI_API_KEY" in config
    assert "default_model_entry_id: 9" in config
    assert "http://127.0.0.1:9121/model-relay/101/v1" in config
    assert "runtime-api-key" not in config
    assert "provider_key" not in config

    config_snapshot = json.loads((home / "platform" / "config.snapshot.json").read_text())
    assert config_snapshot["runtime_workspace_id"] == 101
    assert config_snapshot["runtime_id"] == "org-1-rt-101"
    assert config_snapshot["default_model_id"] == 9
    assert config_snapshot["visible_model_ids"] == [9, 12]
    assert "provider_key" not in config_snapshot

    installed_experts = json.loads((home / "platform" / "installed-experts.json").read_text())
    assert installed_experts == []

    projection_json = json.loads((home / "platform" / "projection.json").read_text())
    assert projection_json == projection


def test_projector_can_render_pure_artifacts_without_touching_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=home,
        workspace_dir=workspace,
        name="默认数字人",
    )

    artifacts = HermesWorkspaceProjector().render_artifacts(payload)

    assert not home.exists()
    assert "config.yaml" in artifacts
    assert "platform/projection.json" in artifacts
    assert f"plugins/{KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID}/plugin.yaml" in artifacts
    assert f"plugins/{KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID}/__init__.py" in artifacts


def test_projection_hash_covers_kanban_orchestrator_guard_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="默认数字人",
    )
    baseline = HermesWorkspaceProjector().render_artifacts(payload)
    baseline_hash = json.loads(baseline["platform/projection.json"])["config_hash"]

    monkeypatch.setattr(
        projection_module,
        "KANBAN_ORCHESTRATOR_GUARD_SOURCE",
        projection_module.KANBAN_ORCHESTRATOR_GUARD_SOURCE + "\n# changed\n",
    )
    changed = HermesWorkspaceProjector().render_artifacts(payload)
    changed_hash = json.loads(changed["platform/projection.json"])["config_hash"]

    assert changed_hash != baseline_hash


def test_projected_kanban_orchestrator_guard_blocks_only_worker_default_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = HermesWorkspaceProjector().render_artifacts(
        ProjectionInput(
            org_id=1,
            user_id=2,
            runtime_workspace_id=101,
            runtime_id="org-1-rt-101",
            config_generation=7,
            model_relay_base_url="http://127.0.0.1:9121/model-relay",
            hermes_home=tmp_path / "home",
            workspace_dir=tmp_path / "workspace",
            name="默认数字人",
        )
    )
    source = artifacts[f"plugins/{KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID}/__init__.py"]
    plugin_path = tmp_path / "guard_plugin.py"
    plugin_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("guard_plugin", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Context:
        callback = None

        def register_hook(self, name, callback):
            assert name == "pre_tool_call"
            self.callback = callback

    context = Context()
    module.register(context)
    assert context.callback is not None
    assert context.callback(tool_name="kanban_list", args={}) is None
    assert context.callback(tool_name="kanban_show", args={"task_id": "T1"}) is None
    for tool_name in (
        "kanban_show",
        "kanban_complete",
        "kanban_block",
        "kanban_heartbeat",
    ):
        blocked = context.callback(
            tool_name=tool_name,
            args={},
            session_id="session-1",
            tool_call_id=f"call-{tool_name}",
        )
        assert blocked["action"] == "block"
        assert "Orchestrator" in blocked["message"]

    monkeypatch.setenv("HERMES_KANBAN_TASK", "T-worker")
    assert context.callback(tool_name="kanban_show", args={}) is None


def test_projection_does_not_render_platform_user_memory(tmp_path: Path) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="默认数字人",
    )

    artifacts = HermesWorkspaceProjector().render_artifacts(payload)

    assert "memories/USER.md" not in artifacts
    assert "memories/MEMORY.md" not in artifacts
    assert artifacts["SOUL.md"] == DEFAULT_RUNTIME_SOUL
    assert "expert_list" in artifacts["SOUL.md"]
    assert "kanban_create" in artifacts["SOUL.md"]
    assert "delegate_task" in artifacts["SOUL.md"]


def test_projection_render_preserves_existing_user_memory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "USER.md").write_text("platform identity", encoding="utf-8")
    (memories / "MEMORY.md").write_text("agent memory", encoding="utf-8")
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=7,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=home,
        workspace_dir=tmp_path / "workspace",
        name="默认数字人",
    )

    HermesWorkspaceProjector().render(payload)

    assert (memories / "USER.md").read_text(encoding="utf-8") == "platform identity"
    assert (memories / "MEMORY.md").read_text(encoding="utf-8") == "agent memory"


def test_projector_renders_installed_assets_without_secrets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=8,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=home,
        workspace_dir=workspace,
        name="默认数字人",
        default_model_id=9,
        installed_experts=(
            {
                "expert_package_id": 11,
                "expert_package_version_id": 12,
                "plugin_id": "sales-copilot",
                "name": "Sales Copilot",
                "version": "1.0.0",
                "description": "Sales support expert",
                "skills": ["summarize"],
                "mcp_servers": ["crm"],
                "knowledge_bases": ["sales-kb"],
            },
        ),
        installed_skills=(
            {
                "skill_id": 21,
                "skill_version_id": 22,
                "slug": "summarize",
                "name": "Summarize",
                "version": "2.0.0",
            },
        ),
        installed_mcps=(
            {
                "mcp_id": 31,
                "slug": "crm",
                "name": "CRM",
                "transport": "stdio",
                "command": "crm-mcp",
                "args": ["--readonly"],
                "enabled": True,
            },
        ),
        bound_knowledge=(
            {
                "knowledge_base_id": 41,
                "slug": "sales-kb",
                "name": "Sales KB",
                "binding_policy": "optional",
            },
        ),
    )

    projection = HermesWorkspaceProjector().render(payload)

    plugin_manifest = (home / "plugins" / "sales-copilot" / "plugin.yaml").read_text()
    assert "id: sales-copilot" in plugin_manifest
    assert "name: Sales Copilot" in plugin_manifest
    assert "expert_package_id: 11" in plugin_manifest
    assert "api_key" not in plugin_manifest
    assert "mcp_secret" not in plugin_manifest
    assert "provider_key" not in plugin_manifest

    skill_snapshot = json.loads((home / "platform" / "installed-skills.json").read_text())
    assert skill_snapshot == [
        {
            "name": "Summarize",
            "skill_id": 21,
            "skill_version_id": 22,
            "slug": "summarize",
            "version": "2.0.0",
        }
    ]
    assert (home / "skills" / "other" / "summarize" / "skill.snapshot.json").exists()

    mcp_snapshot = json.loads((home / "platform" / "installed-mcps.json").read_text())
    assert mcp_snapshot == [
        {
            "args": ["--readonly"],
            "command": "crm-mcp",
            "enabled": True,
            "mcp_id": 31,
            "name": "CRM",
            "slug": "crm",
            "transport": "stdio",
        }
    ]

    knowledge_snapshot = json.loads((home / "platform" / "bound-knowledge.json").read_text())
    assert knowledge_snapshot == [
        {
            "binding_policy": "optional",
            "knowledge_base_id": 41,
            "name": "Sales KB",
            "slug": "sales-kb",
        }
    ]

    installed_experts = json.loads((home / "platform" / "installed-experts.json").read_text())
    assert installed_experts[0]["plugin_id"] == "sales-copilot"

    config = (home / "config.yaml").read_text()
    assert "plugins:" in config
    assert "  enabled:" in config
    assert "    - sales-copilot" in config
    assert "mcp_servers:" in config
    assert "  crm:" in config
    assert "    transport: stdio" in config
    assert "    command: crm-mcp" in config
    assert "api_key" not in config
    assert "mcp_secret" not in config
    assert "provider_key" not in config

    config_snapshot = json.loads((home / "platform" / "config.snapshot.json").read_text())
    assert config_snapshot["asset_counts"] == {
        "bound_knowledge": 1,
        "installed_experts": 1,
        "installed_mcps": 1,
        "installed_skills": 1,
    }
    assert projection["requires_reload"] is True


def test_projector_includes_public_skill_files_in_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_skill = tmp_path / "public-assets" / "skills" / "summary" / "1"
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path / "public-assets"))
    (public_skill / "prompts").mkdir(parents=True)
    (public_skill / "SKILL.md").write_text("# Public Summary\n", encoding="utf-8")
    (public_skill / "prompts" / "start.md").write_text("Summarize this.", encoding="utf-8")

    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=9,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="默认数字人",
        installed_skills=(
            {
                "source": "public_asset",
                "skill_id": 21,
                "slug": "summary",
                "name": "Summary",
                "version": "1",
                "storage_uri": str(public_skill),
                "manifest": {"files": ["SKILL.md", "prompts/start.md"], "runtime_id": "summary"},
            },
        ),
    )

    artifacts = HermesWorkspaceProjector().render_artifacts(payload)

    assert artifacts["skills/other/summary/SKILL.md"] == "# Public Summary\n"
    assert artifacts["skills/other/summary/prompts/start.md"] == "Summarize this."
    assert "skills/other/summary/skill.snapshot.json" in artifacts
    assert "runtime_id" not in artifacts["skills/other/summary/skill.snapshot.json"]


def test_projector_includes_personal_skill_files_in_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal_skill = tmp_path / "personal-skills" / "1" / "2" / "private-summary" / "1"
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path))
    personal_skill.mkdir(parents=True)
    (personal_skill / "SKILL.md").write_text("# Private Summary\n", encoding="utf-8")

    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=10,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="默认数字人",
        installed_skills=(
            {
                "source": "personal_asset",
                "skill_id": 22,
                "slug": "private-summary",
                "name": "Private Summary",
                "version": 1,
                "storage_uri": str(personal_skill),
                "manifest": {"files": ["SKILL.md"]},
            },
        ),
    )

    artifacts = HermesWorkspaceProjector().render_artifacts(payload)

    assert artifacts["skills/personal/private-summary/SKILL.md"] == "# Private Summary\n"
    assert "skills/personal/private-summary/skill.snapshot.json" in artifacts


def test_projector_rejects_secret_keys_in_installed_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        config_generation=8,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=home,
        workspace_dir=workspace,
        name="默认数字人",
        installed_mcps=(
            {
                "mcp_id": 31,
                "slug": "crm",
                "name": "CRM",
                "mcp_secret": "do-not-project",
            },
        ),
    )

    with pytest.raises(ValueError, match="forbidden asset key"):
        HermesWorkspaceProjector().render(payload)


def test_same_generation_render_is_hash_stable(tmp_path: Path) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=12,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="Stable",
    )

    first = HermesWorkspaceProjector().render_artifacts(payload)
    second = HermesWorkspaceProjector().render_artifacts(payload)

    assert first == second
    assert config_bundle_digest(first) == config_bundle_digest(second)
    assert json.loads(first["platform/projection.json"])["rendered_at"] == "generation:12"


def test_projector_rejects_duplicate_normalized_mcp_slugs(tmp_path: Path) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=13,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="Duplicate",
        installed_mcps=(
            {"slug": "CRM!!!", "name": "One", "transport": "stdio", "command": "one"},
            {"slug": "crm", "name": "Two", "transport": "stdio", "command": "two"},
        ),
    )

    with pytest.raises(ValueError, match="duplicate normalized MCP slug"):
        HermesWorkspaceProjector().render_artifacts(payload)


def test_projector_round_trips_mcp_mapping_without_storage_metadata(tmp_path: Path) -> None:
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=14,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="MCP",
        installed_mcps=(
            {
                "mcp_id": 31,
                "slug": "crm",
                "name": "CRM",
                "transport": "sse",
                "url": "https://mcp.example.test/sse",
                "headers": {"Authorization": "${MCP_CRM_TOKEN}"},
                "env": {"CRM_REGION": "${CRM_REGION}"},
            },
        ),
    )

    document = yaml.safe_load(HermesWorkspaceProjector().render_artifacts(payload)["config.yaml"])

    assert document["mcp_servers"]["crm"] == {
        "name": "CRM",
        "transport": "sse",
        "enabled": True,
        "url": "https://mcp.example.test/sse",
        "headers": {"Authorization": "${MCP_CRM_TOKEN}"},
        "env": {"CRM_REGION": "${CRM_REGION}"},
    }


def test_skill_artifacts_reject_symlink_escape_and_hide_storage_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "assets"
    skill_root = root / "skills" / "summary" / "1"
    skill_root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret host content", encoding="utf-8")
    (skill_root / "safe.md").write_text("safe", encoding="utf-8")
    (skill_root / "escape.md").symlink_to(outside)
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(root))
    payload = ProjectionInput(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_id="rt-101",
        config_generation=15,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        name="Skill",
        installed_skills=(
            {
                "source": "public_asset",
                "slug": "summary",
                "storage_uri": str(skill_root),
                "manifest": {"files": ["safe.md", "escape.md"]},
            },
        ),
    )

    with pytest.raises(ValueError, match="escapes"):
        HermesWorkspaceProjector().render_artifacts(payload)

    safe_payload = ProjectionInput(
        **{
            **payload.__dict__,
            "installed_skills": (
                {**payload.installed_skills[0], "manifest": {"files": ["safe.md"]}},
            ),
        }
    )
    artifacts = HermesWorkspaceProjector().render_artifacts(safe_payload)
    assert "storage_uri" not in artifacts["platform/installed-skills.json"]
    assert "storage_uri" not in artifacts["skills/other/summary/skill.snapshot.json"]
