from __future__ import annotations

import os
import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app.hermes_runtime.constants import (
    HERMES_0191_CONFIG_SCHEMA,
    HERMES_0191_STATE_SCHEMA,
    PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
)
from app.hermes_runtime.projection import (
    DEFAULT_RUNTIME_SOUL,
    DESKTOP_LOCAL_PLUGIN_ID,
    KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
    KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
    ProfileConfigRenderer,
    ProjectionInput,
    desktop_local_plugin_artifacts,
)
from app.hermes_runtime.skill_visibility_policy import (
    BUNDLED_HERMES_SKILL_ROOT,
    READONLY_HERMES_SKILL_ROOT,
)
from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.services.runtime_config_bootstrap import (
    LocalRuntimeConfigBootstrapper,
    RuntimeConfigPreflightError,
    _RETIRED_SKILL_HOOK_COMMAND,
)
from app.services.runtime_user_memory import (
    PLATFORM_FILE_DELIVERY_BEGIN,
    PLATFORM_FILE_DELIVERY_END,
    merge_platform_soul,
)
from app.services.runtime_workspace import (
    RuntimeBindingEnsureResult,
    RuntimeKServiceWarmupRunner,
    RuntimeWorkspaceRecord,
    RuntimeWorkspaceService,
)
from scripts.hermes_019_pvc_preflight import MARKER_RELATIVE_PATH


def _spec(root: Path) -> KnativeRuntimeSpec:
    return KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-9",
        runtime_workspace_id=123,
        runtime_id="rt-123",
        namespace="hermes-runtimes",
        service_name="hermes-runtime-123",
        image="hermes-agent:test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="key",
        runtime_root_pvc_name="root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="root-pv",
        runtime_root_host_path=str(root),
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=HERMES_0191_STATE_SCHEMA,
        config_schema=HERMES_0191_CONFIG_SCHEMA,
        startup_mode="steady_state",
        runtime_user_uid=os.getuid(),
        runtime_shared_gid=os.getgid(),
    )


def _valid_existing_config() -> str:
    return (
        "_config_version: 33\n"
        "agent:\n"
        "  disabled_toolsets:\n"
        "    - cronjob\n"
        f"  system_prompt: {json.dumps(KANBAN_ORCHESTRATOR_SYSTEM_PROMPT, ensure_ascii=False)}\n"
        "toolsets:\n"
        "  - kanban\n"
        "plugins:\n"
        "  enabled:\n"
        "    - enterprise-knowledge\n"
        "    - personal-knowledge\n"
        "    - kanban-lifecycle\n"
        "    - clarify-lifecycle\n"
        "    - expert-roster\n"
        "    - kanban-orchestrator-guard\n"
        f"    - {DESKTOP_LOCAL_PLUGIN_ID}\n"
        f"    - {PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID}\n"
        "skills:\n"
        "  creation_nudge_interval: 0\n"
        "  disabled: []\n"
        "model:\n"
        "  provider: custom:platform\n"
        "  default: '9'\n"
        "  default_model_entry_id: 9\n"
        "  ollama_num_ctx: 0\n"
        "providers:\n"
        "  platform:\n"
        "    base_url: http://127.0.0.1:9121/model-relay/123/v1\n"
        "    key_env: OPENAI_API_KEY\n"
        "    transport: chat_completions\n"
        "    default_model: '9'\n"
        "    discover_models: false\n"
        "    models:\n"
        "      '9':\n"
        "        context_length: 1000000\n"
        "        supports_vision: false\n"
        "terminal:\n"
        "  cwd: /data/workspace\n"
        "workspace_dir: /data/workspace\n"
        "platform:\n"
        "  runtime_workspace_id: 123\n"
        "  runtime_id: rt-123\n"
        "  config_generation: 0\n"
        "mcp_servers: {}\n"
    )


def test_profile_config_renderer_outputs_complete_secret_free_baseline() -> None:
    payload = ProjectionInput(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        config_generation=0,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=Path("/data/hermes/home"),
        workspace_dir=Path("/data/workspace"),
        name="Default",
    )

    rendered = ProfileConfigRenderer.render_config(payload)
    document = yaml.safe_load(rendered)

    assert document["_config_version"] == 33
    assert document["agent"] == {
        "disabled_toolsets": ["cronjob"],
        "system_prompt": KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
    }
    assert document["toolsets"] == ["kanban"]
    assert document["kanban"]["max_in_progress_per_profile"] == 1
    assert document["browser"] == {
        "cloud_provider": "local",
        "headed": False,
        "allow_private_urls": False,
        "restrict_evaluate": True,
        "inactivity_timeout": 120,
    }
    assert "api_server" not in document
    assert document["model"]["default"] == "session"
    assert isinstance(document["model"]["default"], str)
    assert document["model"]["ollama_num_ctx"] == 0
    assert document["providers"]["platform"]["default_model"] == "session"
    assert document["providers"]["platform"]["discover_models"] is False
    assert document["providers"]["platform"]["models"] == {}
    assert document["terminal"] == {"cwd": "/data/workspace"}
    assert document["workspace_dir"] == "/data/workspace"
    assert document["platform"]["runtime_workspace_id"] == 123
    assert document["platform"]["runtime_id"] == "rt-123"
    assert document["platform"]["config_generation"] == 0
    assert document["platform"]["skill_visibility"]["policy_version"] == 1
    assert len(document["platform"]["skill_visibility"]["platform_disabled"]) == 70
    assert "github-auth" in document["skills"]["disabled"]
    assert "hermes-desktop-plugins" in document["skills"]["disabled"]
    assert "hermes-agent" not in document["skills"]["disabled"]
    assert document["skills"]["external_dirs"] == ["/opt/hermes/skills"]
    assert document["skills"]["creation_nudge_interval"] == 0
    assert document["plugins"]["enabled"] == [
        "enterprise-knowledge",
        "personal-knowledge",
        "kanban-lifecycle",
        "clarify-lifecycle",
        "expert-roster",
        KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
        DESKTOP_LOCAL_PLUGIN_ID,
        PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    ]
    assert document["mcp_servers"] == {}
    assert "runtime-api-key-value" not in rendered


def test_profile_config_renderer_outputs_target_safety_invariants() -> None:
    payload = ProjectionInput(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        config_generation=0,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=Path("/data/hermes/home"),
        workspace_dir=Path("/data/workspace"),
        name="Default",
        config_schema=34,
    )

    document = yaml.safe_load(ProfileConfigRenderer.render_config(payload))

    assert document["_config_version"] == 34
    assert document["display"] == {"busy_input_mode": "queue"}
    assert document["desktop"] == {"auto_continue": {"enabled": False}}
    assert document["sessions"] == {"cjk_fts": True}
    assert document["platform"]["skill_visibility"] == {
        "policy_version": 2,
        "user_disabled": [],
        "platform_disabled": ["hermes-desktop-plugins", "pptx-generator"],
    }
    assert document["skills"]["disabled"] == [
        "hermes-desktop-plugins", "pptx-generator"
    ]


def test_profile_config_renderer_can_pin_initial_skill_policy_on_schema_34() -> None:
    payload = ProjectionInput(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        config_generation=0,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=Path("/data/hermes/home"),
        workspace_dir=Path("/data/workspace"),
        name="Fresh",
        config_schema=34,
        skill_policy_version=1,
    )

    document = yaml.safe_load(ProfileConfigRenderer.render_config(payload))

    assert document["platform"]["skill_visibility"]["policy_version"] == 1
    assert document["platform"]["skill_visibility"]["initial_policy_version"] == 1
    assert len(document["skills"]["disabled"]) == 70


def test_existing_config_preserves_declared_skill_policy_version(
    tmp_path: Path,
) -> None:
    spec = replace(_spec(tmp_path / "runtime"), config_schema=34)
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    home.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["platform"]["skill_visibility"] = {
        "policy_version": 1,
        "initial_policy_version": 1,
        "platform_disabled": [],
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )

    LocalRuntimeConfigBootstrapper()._ensure_config(spec)

    normalized = yaml.safe_load(
        (home / "config.yaml").read_text(encoding="utf-8")
    )
    assert normalized["platform"]["skill_visibility"]["policy_version"] == 1
    assert normalized["platform"]["skill_visibility"]["initial_policy_version"] == 1


def test_target_bootstrap_normalizes_only_the_closed_33_to_34_fields(
    tmp_path: Path,
) -> None:
    spec = replace(
        _spec(tmp_path / "runtime"),
        upstream_commit="f80f453ae0679347e38abc917c7f94f717bf96c5",
        state_schema=25,
        config_schema=34,
    )
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    home.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["sessions"] = {"cjk_fts": False}
    document["display"] = {"busy_input_mode": "interrupt", "theme": "dark"}
    document["desktop"] = {
        "auto_continue": {"enabled": True, "max_attempts": 2},
        "window": {"width": 900},
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )

    LocalRuntimeConfigBootstrapper()._ensure_config(spec)

    normalized = yaml.safe_load(
        (home / "config.yaml").read_text(encoding="utf-8")
    )
    assert normalized["_config_version"] == 34
    assert normalized["sessions"] == {"cjk_fts": False}
    assert normalized["display"] == {
        "busy_input_mode": "queue",
        "theme": "dark",
    }
    assert normalized["desktop"] == {
        "auto_continue": {"enabled": False, "max_attempts": 2},
        "window": {"width": 900},
    }
    assert normalized["platform"]["skill_visibility"] == {
        "policy_version": 2,
        "platform_disabled": ["hermes-desktop-plugins", "pptx-generator"],
        "user_disabled": [],
    }
    assert normalized["skills"]["disabled"] == [
        "hermes-desktop-plugins", "pptx-generator"
    ]
    marker = json.loads((home / MARKER_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert marker["config_schema"] == 34
    assert marker["state_schema"] == 25


def test_profile_config_renderer_projects_visible_models_without_discovery() -> None:
    payload = ProjectionInput(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        config_generation=2,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=Path("/data/hermes/home"),
        workspace_dir=Path("/data/workspace"),
        name="Default",
        visible_models=(
            {
                "model_entry_id": 11,
                "context_length": 128_000,
                "tags": ["vision"],
            },
            {
                "model_entry_id": 7,
                "context_length": 1_000_000,
                "tags": ["chat", "fast"],
            },
        ),
    )

    document = yaml.safe_load(ProfileConfigRenderer.render_config(payload))

    assert document["model"]["default"] == "7"
    assert document["model"]["default_model_entry_id"] == 7
    assert document["providers"]["platform"]["discover_models"] is False
    assert document["providers"]["platform"]["models"] == {
        "7": {"context_length": 1_000_000, "supports_vision": False},
        "11": {"context_length": 128_000, "supports_vision": True},
    }


def test_profile_config_renderer_prefers_visible_admin_default_model() -> None:
    payload = ProjectionInput(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        config_generation=2,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=Path("/data/hermes/home"),
        workspace_dir=Path("/data/workspace"),
        name="Default",
        default_model_id=7,
        visible_models=(
            {
                "model_entry_id": 7,
                "context_length": 1_000_000,
                "tags": ["chat"],
                "is_default": False,
            },
            {
                "model_entry_id": 21,
                "context_length": 256_000,
                "tags": ["vision"],
                "is_default": True,
            },
        ),
    )

    document = yaml.safe_load(ProfileConfigRenderer.render_config(payload))

    assert document["model"]["default"] == "21"
    assert document["model"]["default_model_entry_id"] == 21
    assert document["providers"]["platform"]["default_model"] == "21"


def test_profile_config_renderer_rejects_invalid_visible_model_context() -> None:
    payload = ProjectionInput(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        config_generation=2,
        model_relay_base_url="http://127.0.0.1:9121/model-relay",
        hermes_home=Path("/data/hermes/home"),
        workspace_dir=Path("/data/workspace"),
        name="Default",
        visible_models=({"model_entry_id": 7, "context_length": 0},),
    )

    with pytest.raises(ValueError, match="context_length"):
        ProfileConfigRenderer.render_config(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda document: document.update({"_config_version": 32}), "schema 33"),
        (lambda document: document["model"].update({"default": 9}), "model.default"),
        (lambda document: document["terminal"].update({"cwd": "/wrong"}), "terminal.cwd"),
        (
            lambda document: document["providers"]["platform"].update({"default_model": 9}),
            "providers.platform.default_model",
        ),
        (lambda document: document.pop("toolsets"), "toolsets"),
        (lambda document: document.update({"toolsets": "kanban"}), "toolsets"),
        (lambda document: document.update({"toolsets": ["web"]}), "toolsets"),
        (
            lambda document: document["agent"].pop("system_prompt"),
            "agent.system_prompt",
        ),
        (
            lambda document: document["plugins"]["enabled"].remove(
                KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID
            ),
            "plugins.enabled",
        ),
    ],
)
def test_runtime_config_preflight_rejects_hermes_019_contract_violation(
    mutation,
    message: str,
) -> None:
    document = yaml.safe_load(_valid_existing_config())
    mutation(document)

    with pytest.raises(RuntimeConfigPreflightError, match=message):
        LocalRuntimeConfigBootstrapper._validate_document(document, source="test config")


def test_existing_state_requires_matching_release_migration_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    home.mkdir(parents=True)
    (home / "state.db").write_bytes(b"old-state")

    with pytest.raises(RuntimeConfigPreflightError, match="migration marker"):
        LocalRuntimeConfigBootstrapper()._ensure_config(spec)


def test_fresh_state_is_initialized_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path / "runtime")

    LocalRuntimeConfigBootstrapper()._ensure_config(spec)

    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    marker = json.loads((home / MARKER_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert marker["mode"] == "fresh"
    assert marker["config_schema"] == 33
    assert marker["state_schema"] == HERMES_0191_STATE_SCHEMA
    assert not (home / "state.db").exists()


@pytest.mark.parametrize("orphan", ["state.db-wal", "state.db-shm"])
def test_existing_state_artifact_requires_migration_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan: str,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    home.mkdir(parents=True)
    (home / orphan).write_bytes(b"orphaned-state")

    with pytest.raises(RuntimeConfigPreflightError, match="migration marker"):
        LocalRuntimeConfigBootstrapper()._ensure_config(spec)


def test_existing_state_accepts_matching_release_migration_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    marker = home / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    (home / "state.db").write_bytes(b"migrated-state")
    marker.write_text(
        json.dumps(
            {
                "config_schema": HERMES_0191_CONFIG_SCHEMA,
                "state_schema": HERMES_0191_STATE_SCHEMA,
                "image_commit": spec.upstream_commit,
                "completed_at": "2026-07-22T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    LocalRuntimeConfigBootstrapper()._ensure_config(spec)


def test_existing_state_rejects_marker_from_a_different_release_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    marker = home / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "config_schema": HERMES_0191_CONFIG_SCHEMA,
                "state_schema": HERMES_0191_STATE_SCHEMA,
                "image_commit": "f" * 40,
                "completed_at": "2026-08-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeConfigPreflightError,
        match="conflicts with release schema",
    ):
        LocalRuntimeConfigBootstrapper()._ensure_config(spec)


def test_sync_bootstrap_refreshes_platform_soul_when_config_exists(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        _valid_existing_config(),
        encoding="utf-8",
    )
    soul_path = home / "SOUL.md"
    soul_path.write_text("upstream default persona\n", encoding="utf-8")

    LocalRuntimeConfigBootstrapper()._ensure_config(spec)

    assert soul_path.read_text(encoding="utf-8") == merge_platform_soul(
        DEFAULT_RUNTIME_SOUL,
        name=spec.user_name,
        user_code=spec.user_code,
    )
    assert stat.S_IMODE(soul_path.stat().st_mode) == 0o660
    assert soul_path.stat().st_uid == spec.runtime_user_uid
    assert soul_path.stat().st_gid == spec.runtime_shared_gid
    assert list(home.glob(".SOUL.md.*.tmp")) == []


def test_sync_bootstrap_generates_soul_with_public_chinese_rule(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path

    LocalRuntimeConfigBootstrapper()._ensure_config(spec)

    soul_path = home / "SOUL.md"
    assert soul_path.is_file()
    assert (
        "当用户使用中文时，所有面向用户公开展示的内容，包括 reasoning 摘要和进度说明，"
        "统一使用简体中文。"
    ) in soul_path.read_text(encoding="utf-8")
    assert "expert_list" in soul_path.read_text(encoding="utf-8")
    assert "kanban_create" in soul_path.read_text(encoding="utf-8")
    assert "delegate_task" in soul_path.read_text(encoding="utf-8")
    soul = soul_path.read_text(encoding="utf-8")
    assert "姓名：测试用户" in soul
    assert "工号：employee-9" in soul
    assert PLATFORM_FILE_DELIVERY_BEGIN in soul
    assert PLATFORM_FILE_DELIVERY_END in soul
    assert not (home / "memories" / "USER.md").exists()


@pytest.mark.asyncio
async def test_bootstrap_generates_pvc_config_atomically(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "runtime")
    spec_root = Path(spec.runtime_root_host_path)
    (spec_root / spec.hermes_home_sub_path).mkdir(parents=True)

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    config_path = spec_root / "home" / "config.yaml"
    assert config_path.is_file()
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["_config_version"] == 33
    assert document["agent"] == {
        "disabled_toolsets": ["cronjob"],
        "system_prompt": KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
    }
    assert document["toolsets"] == ["kanban"]

    assert "api_server" not in document
    assert document["terminal"] == {"cwd": "/data/workspace"}
    assert document["workspace_dir"] == "/data/workspace"
    assert document["platform"]["runtime_id"] == "rt-123"
    assert document["platform"]["config_generation"] == 0
    assert document["platform"]["runtime_workspace_id"] == 123
    assert document["plugins"]["enabled"] == [
        "enterprise-knowledge",
        "personal-knowledge",
        "kanban-lifecycle",
        "clarify-lifecycle",
        "expert-roster",
        KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
        DESKTOP_LOCAL_PLUGIN_ID,
        PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    ]
    assert document["mcp_servers"] == {}
    assert document["providers"]["platform"]["discover_models"] is False
    assert document["providers"]["platform"]["models"] == {}
    assert document["skills"]["external_dirs"] == [
        "/opt/hermes/skills",
        BUNDLED_HERMES_SKILL_ROOT,
        READONLY_HERMES_SKILL_ROOT,
    ]
    assert "runtime-api-key" not in config_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o660
    assert config_path.stat().st_uid == spec.runtime_user_uid
    assert config_path.stat().st_gid == spec.runtime_shared_gid
    assert list(config_path.parent.glob(".config.*.tmp")) == []
    guard_root = spec_root / "home" / "plugins" / KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID
    assert (guard_root / "plugin.yaml").is_file()
    assert (guard_root / "__init__.py").is_file()
    soul = (spec_root / "home" / "SOUL.md").read_text(encoding="utf-8")
    assert "姓名：测试用户" in soul
    assert "工号：employee-9" in soul
    assert PLATFORM_FILE_DELIVERY_BEGIN in soul
    assert PLATFORM_FILE_DELIVERY_END in soul
    assert not (spec_root / "home" / "memories" / "USER.md").exists()


@pytest.mark.asyncio
async def test_bootstrap_excludes_orchestrator_contract_for_application_role(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    spec_root = Path(spec.runtime_root_host_path)
    (spec_root / spec.hermes_home_sub_path).mkdir(parents=True)
    bootstrapper = LocalRuntimeConfigBootstrapper(kanban_orchestrator_enabled=False)

    await bootstrapper.ensure_config(spec)
    await bootstrapper.ensure_config(spec)

    home = spec_root / "home"
    document = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert document["agent"] == {"disabled_toolsets": ["cronjob"]}
    assert document["toolsets"] == []
    assert document["plugins"]["enabled"] == [
        "enterprise-knowledge",
        "personal-knowledge",
        "clarify-lifecycle",
        DESKTOP_LOCAL_PLUGIN_ID,
        PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    ]
    assert not (home / "plugins" / KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID).exists()


def test_desktop_local_plugin_artifacts_compile_without_null_bytes() -> None:
    artifacts = desktop_local_plugin_artifacts()
    source = artifacts[f"plugins/{DESKTOP_LOCAL_PLUGIN_ID}/__init__.py"]

    assert "\x00" not in source
    assert r"\x00" in source
    for tool_name in (
        "desktop_browser_session_start",
        "desktop_browser_navigate",
        "desktop_browser_snapshot",
        "desktop_browser_click",
        "desktop_browser_type",
        "desktop_browser_press",
        "desktop_browser_scroll",
        "desktop_browser_screenshot",
        "desktop_browser_session_close",
        "desktop_browser_read",
        "desktop_browser_wait",
        "desktop_browser_hover",
        "desktop_browser_select",
        "desktop_browser_upload",
        "desktop_browser_downloads",
        "desktop_browser_back",
        "desktop_browser_reload",
        "desktop_browser_tab_list",
        "desktop_browser_tab_new",
        "desktop_browser_tab_select",
        "desktop_browser_tab_close",
    ):
        assert tool_name in source
    # The routing gate: cloud substitutes are blocked once per session+tool
    # while the desktop is online; a deliberate retry goes through.
    assert 'ctx.register_hook("pre_tool_call", _pre_tool_call)' in source
    assert "/internal/v1/desktop-status" in source
    assert "_cloud_tool_block_seen" in source
    compile(source, f"plugins/{DESKTOP_LOCAL_PLUGIN_ID}/__init__.py", "exec")


def test_desktop_plugin_gate_allows_cloud_tools_when_probe_fails() -> None:
    artifacts = desktop_local_plugin_artifacts()
    source = artifacts[f"plugins/{DESKTOP_LOCAL_PLUGIN_ID}/__init__.py"]
    namespace: dict[str, object] = {}
    exec(compile(source, "plugin", "exec"), namespace)  # noqa: S102 - trusted platform artifact
    # No reachable supervisor -> the probe fails -> the gate stays open and
    # routing falls back to the prompt discipline (web sessions, old stacks).
    assert namespace["_pre_tool_call"](tool_name="terminal", session_id="rs-gate") is None
    assert namespace["_pre_tool_call"](tool_name="browser_exec", session_id="rs-gate") is None
    assert namespace["_pre_tool_call"](tool_name="read_file", session_id="rs-gate") is None
    assert namespace["_pre_tool_call"](tool_name="search_files", session_id="rs-gate") is None
    assert namespace["_pre_tool_call"](tool_name="desktop_command_run", session_id="rs-gate") is None


def test_desktop_plugin_gate_blocks_cloud_tools_when_desktop_online(monkeypatch) -> None:
    artifacts = desktop_local_plugin_artifacts()
    source = artifacts[f"plugins/{DESKTOP_LOCAL_PLUGIN_ID}/__init__.py"]
    namespace: dict[str, object] = {}
    exec(compile(source, "plugin", "exec"), namespace)  # noqa: S102 - trusted platform artifact

    class _FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, limit=-1): return b'{"online": true}'

    monkeypatch.setitem(namespace, "urlopen", lambda request, timeout=None: _FakeResponse())
    monkeypatch.setenv("HERMES_RUNTIME_SUPERVISOR_URL", "http://127.0.0.1:1")
    namespace["_desktop_status_cache"].clear()

    gate = namespace["_pre_tool_call"]
    first = gate(tool_name="terminal", session_id="rs-online")
    assert first is not None and first.get("action") == "block"
    # 首拦放行：被引导后再次调用视为显式确认走云端。
    assert gate(tool_name="terminal", session_id="rs-online") is None
    # 前缀匹配云端浏览器工具族。
    browser = gate(tool_name="browser_exec", session_id="rs-online")
    assert browser is not None and browser.get("action") == "block"
    # 本地读场景与搜索场景同样首拦引导到桌面工作文件夹。
    read_file = gate(tool_name="read_file", session_id="rs-online")
    assert read_file is not None and read_file.get("action") == "block"
    assert gate(tool_name="read_file", session_id="rs-online") is None
    search_files = gate(tool_name="search_files", session_id="rs-online")
    assert search_files is not None and search_files.get("action") == "block"
    assert gate(tool_name="search_files", session_id="rs-online") is None
    # 桌面工具永不拦。
    assert gate(tool_name="desktop_command_run", session_id="rs-online") is None


def test_default_runtime_soul_prefers_desktop_for_local_requests() -> None:
    assert "默认必须使用" in DEFAULT_RUNTIME_SOUL
    # 禁止性纪律已由插件 pre_tool_call 闸门执行，soul 只解释机制与继续方式。
    assert "自动拦截并引导改用桌面工具" in DEFAULT_RUNTIME_SOUL
    assert "操作系统的默认浏览器" in DEFAULT_RUNTIME_SOUL
    assert "本地文件产物" in DEFAULT_RUNTIME_SOUL
    assert "禁止把长脚本直接塞进 command 参数" in DEFAULT_RUNTIME_SOUL
    assert "Skill 脚本的执行位置" in DEFAULT_RUNTIME_SOUL
    assert "root 字段是工作文件夹的绝对路径" in DEFAULT_RUNTIME_SOUL
    assert "无需探测本地库" in DEFAULT_RUNTIME_SOUL


@pytest.mark.asyncio
async def test_bootstrap_preserves_existing_valid_config(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    original = _valid_existing_config()
    config_path.write_text(original, encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "hooks" not in document
    assert document["skills"]["external_dirs"] == ["/opt/hermes/skills"]
    assert "hermes-desktop-plugins" in document["skills"]["disabled"]
    hook_path = config_path.parent / ".platform" / "hooks" / "skill-runtime-sync.py"
    initial_config = config_path.read_bytes()
    await LocalRuntimeConfigBootstrapper().ensure_config(spec)
    assert not hook_path.exists()
    assert config_path.read_bytes() == initial_config


@pytest.mark.asyncio
async def test_bootstrap_prepends_official_skills_without_dropping_external_dirs(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["skills"]["external_dirs"] = ["/data/user-managed-skills"]
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    normalized = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert normalized["skills"]["external_dirs"] == [
        "/opt/hermes/skills",
        "/data/user-managed-skills",
    ]


@pytest.mark.asyncio
async def test_bootstrap_converges_platform_compression_on_existing_config(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["compression"] = {"max_attempts": 3}
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    normalized = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert normalized["compression"] == ProfileConfigRenderer.COMPRESSION_CONFIG


@pytest.mark.asyncio
async def test_bootstrap_disables_hermes_automatic_skill_creation(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        _valid_existing_config().replace(
            "  creation_nudge_interval: 0\n",
            "  creation_nudge_interval: 10\n",
        ),
        encoding="utf-8",
    )

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["skills"]["creation_nudge_interval"] == 0
    initial_config = config_path.read_bytes()
    await LocalRuntimeConfigBootstrapper().ensure_config(spec)
    assert config_path.read_bytes() == initial_config


@pytest.mark.asyncio
async def test_bootstrap_normalizes_existing_config_metadata_only(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / "home"
    config_path = home / "config.yaml"
    unmanaged_path = home / "skills" / "user-owned.txt"
    unmanaged_path.parent.mkdir(parents=True)
    config_path.write_text(_valid_existing_config(), encoding="utf-8")
    unmanaged_path.write_text("leave me alone\n", encoding="utf-8")
    unmanaged_path.chmod(0o604)
    bootstrapper = LocalRuntimeConfigBootstrapper()
    await bootstrapper.ensure_config(spec)
    original = config_path.read_bytes()
    config_path.chmod(0o640)

    await bootstrapper.ensure_config(spec)

    assert config_path.read_bytes() == original
    assert config_path.stat().st_uid == spec.runtime_user_uid
    assert config_path.stat().st_gid == spec.runtime_shared_gid
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o660
    assert unmanaged_path.read_text(encoding="utf-8") == "leave me alone\n"
    assert stat.S_IMODE(unmanaged_path.stat().st_mode) == 0o604


def test_managed_config_metadata_uses_runtime_owner_and_projection_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = tmp_path / "config.yaml"
    config_path.touch()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "app.services.runtime_config_bootstrap.os.chown",
        lambda *args: calls.append(("chown", *args)),
    )
    monkeypatch.setattr(
        "app.services.runtime_config_bootstrap.os.chmod",
        lambda *args: calls.append(("chmod", *args)),
    )

    LocalRuntimeConfigBootstrapper._normalize_managed_config_metadata(
        config_path,
        spec,
    )

    assert calls == [
        (
            "chown",
            config_path,
            spec.runtime_user_uid,
            spec.runtime_shared_gid,
        ),
        ("chmod", config_path, 0o660),
    ]


@pytest.mark.parametrize("operation", ["chown", "chmod"])
def test_managed_config_metadata_failure_blocks_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = tmp_path / "config.yaml"
    config_path.touch()

    def fail(*_args: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(
        f"app.services.runtime_config_bootstrap.os.{operation}",
        fail,
    )

    with pytest.raises(
        RuntimeConfigPreflightError,
        match="cannot normalize Runtime config ownership and permissions",
    ):
        LocalRuntimeConfigBootstrapper._normalize_managed_config_metadata(
            config_path,
            spec,
        )


@pytest.mark.asyncio
async def test_bootstrap_adds_kanban_to_existing_default_profile_toolsets(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["toolsets"] = ["web"]
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)
    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    normalized = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert normalized["toolsets"] == ["web", "kanban"]


@pytest.mark.asyncio
async def test_bootstrap_adds_default_orchestrator_contract_to_existing_config(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["agent"].pop("system_prompt")
    document["plugins"]["enabled"].remove(KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID)
    config_path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    normalized = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert normalized["agent"]["system_prompt"] == KANBAN_ORCHESTRATOR_SYSTEM_PROMPT
    assert KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID in normalized["plugins"]["enabled"]
    guard_root = config_path.parent / "plugins" / KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID
    assert (guard_root / "plugin.yaml").is_file()
    assert (guard_root / "__init__.py").is_file()


@pytest.mark.asyncio
async def test_bootstrap_rejects_non_list_default_profile_toolsets(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["toolsets"] = "kanban"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(RuntimeConfigPreflightError, match="toolsets"):
        await LocalRuntimeConfigBootstrapper().ensure_config(spec)


@pytest.mark.asyncio
async def test_bootstrap_removes_only_the_retired_platform_hook_config(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    document = yaml.safe_load(_valid_existing_config())
    document["hooks"] = {
        "post_tool_call": [
            {"matcher": "skill_manage", "command": _RETIRED_SKILL_HOOK_COMMAND, "timeout": 5},
            {"matcher": "another_tool", "command": "/user/owned-hook.py", "timeout": 10},
        ]
    }
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    cleaned = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cleaned["hooks"]["post_tool_call"] == [
        {"matcher": "another_tool", "command": "/user/owned-hook.py", "timeout": 10}
    ]


@pytest.mark.asyncio
async def test_bootstrap_keeps_a_modified_retired_script_but_never_references_it(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    config_path = home / "config.yaml"
    hook_path = home / ".platform" / "hooks" / "skill-runtime-sync.py"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("user modified", encoding="utf-8")
    document = yaml.safe_load(_valid_existing_config())
    document["hooks"] = {
        "post_tool_call": [
            {"matcher": "skill_manage", "command": _RETIRED_SKILL_HOOK_COMMAND, "timeout": 5}
        ]
    }
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    assert hook_path.read_text(encoding="utf-8") == "user modified"
    cleaned = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert cleaned["hooks"]["post_tool_call"] == []


@pytest.mark.asyncio
async def test_bootstrap_normalizes_legacy_gateway_config_and_preserves_agent_settings(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    legacy = _valid_existing_config().replace(
        "agent:\n  disabled_toolsets:\n    - cronjob\n",
        "api_server:\n  host: 0.0.0.0\n  port: 8642\nagent:\n  disabled_toolsets:\n    - browser\n",
    )
    config_path.write_text(legacy, encoding="utf-8")

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "api_server" not in document
    assert document["agent"]["disabled_toolsets"] == ["browser", "cronjob"]


@pytest.mark.asyncio
async def test_bootstrap_refreshes_existing_config_with_visible_model_metadata(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    old_config = (
        _valid_existing_config()
        .replace("  ollama_num_ctx: 0\n", "")
        .replace(
            "    discover_models: false\n"
            "    models:\n"
            "      '9':\n"
            "        context_length: 1000000\n"
            "        supports_vision: false\n",
            "",
        )
    )
    config_path.write_text(old_config, encoding="utf-8")

    class ModelStore:
        async def list_visible_models(self, **_kwargs):
            return [
                type(
                    "Model",
                    (),
                    {
                        "model_entry_id": 7,
                        "context_length": 128_000,
                        "tags": [],
                    },
                )()
            ]

    await LocalRuntimeConfigBootstrapper(model_store=ModelStore()).ensure_config(spec)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["model"]["default"] == "7"
    assert document["model"]["ollama_num_ctx"] == 0
    assert document["providers"]["platform"]["default_model"] == "7"
    assert document["providers"]["platform"]["discover_models"] is False
    assert document["providers"]["platform"]["models"] == {
        "7": {"context_length": 128_000, "supports_vision": False}
    }


@pytest.mark.asyncio
async def test_existing_config_still_refreshes_platform_identity(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path / "runtime")
    bootstrapper = LocalRuntimeConfigBootstrapper()
    await bootstrapper.ensure_config(spec)
    home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
    soul_path = home / "SOUL.md"
    soul_path.write_text(
        soul_path.read_text(encoding="utf-8").replace("测试用户", "旧姓名"),
        encoding="utf-8",
    )
    user_path = home / "memories" / "USER.md"
    user_path.write_text("## User Preferences\n\n喜欢简洁回答。\n", encoding="utf-8")
    memory_path = user_path.with_name("MEMORY.md")
    memory_path.write_text("# Long-term Memory\n\n已有记忆。\n", encoding="utf-8")

    await bootstrapper.ensure_config(spec)

    assert "姓名：测试用户" in soul_path.read_text(encoding="utf-8")
    assert "旧姓名" not in soul_path.read_text(encoding="utf-8")
    assert user_path.read_text(encoding="utf-8") == (
        "## User Preferences\n\n喜欢简洁回答。\n"
    )
    assert memory_path.read_text(encoding="utf-8") == ("# Long-term Memory\n\n已有记忆。\n")


@pytest.mark.asyncio
async def test_bootstrap_does_not_clobber_concurrently_published_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    original_link = os.link
    concurrent_config = _valid_existing_config()

    def publish_concurrently(
        source: str | bytes, destination: str | bytes, *args, **kwargs
    ) -> None:
        if Path(destination) == config_path:
            Path(destination).write_text(concurrent_config, encoding="utf-8")
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr("app.services.runtime_config_bootstrap.os.link", publish_concurrently)

    await LocalRuntimeConfigBootstrapper().ensure_config(spec)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["platform"]["runtime_workspace_id"] == 123
    assert "hooks" not in document
    assert list(config_path.parent.glob(".config.*.tmp")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", ["", "[]", ": invalid\n", "api_key: supersecret\n"])
async def test_bootstrap_rejects_invalid_or_secret_config(tmp_path: Path, contents: str) -> None:
    spec = _spec(tmp_path / "runtime")
    config_path = Path(spec.runtime_root_host_path) / "home" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeConfigPreflightError) as error:
        await LocalRuntimeConfigBootstrapper().ensure_config(spec)
    assert "supersecret" not in str(error.value)


class _BindingStore:
    async def ensure_binding(self, record: RuntimeWorkspaceRecord) -> RuntimeBindingEnsureResult:
        return RuntimeBindingEnsureResult(
            plan=RuntimeWorkspaceService.build_default_binding_plan(record),
            api_server_key="key",
        )


class _FailingTopologyReconciler:
    async def reconcile(self, _record, _binding):
        raise RuntimeConfigPreflightError("invalid config")


class _OpenAdmission:
    async def require_new_work_allowed(self, *, lock: bool = False) -> None:
        assert lock is True


@pytest.mark.asyncio
async def test_warmup_propagates_topology_preflight_failure() -> None:
    runner = RuntimeKServiceWarmupRunner(
        binding_store=_BindingStore(),
        topology_reconciler=_FailingTopologyReconciler(),
        admission_service=_OpenAdmission(),
    )
    record = RuntimeWorkspaceRecord(123, 123, 7, 9, "测试用户", "employee-9", "Default", "active")

    with pytest.raises(RuntimeConfigPreflightError):
        await runner.warmup(record)
