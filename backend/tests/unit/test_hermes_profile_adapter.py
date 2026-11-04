from __future__ import annotations

from typing import Any

import pytest

from app.hermes_runtime.projection import KANBAN_ORCHESTRATOR_SYSTEM_PROMPT
from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint


class FakeDashboard:
    def __init__(
        self,
        responses: dict[tuple[str, str], Any],
        *,
        health_error: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.health_error = health_error

    async def health(self, endpoint: RuntimeEndpoint) -> dict[str, Any]:
        del endpoint
        self.calls.append(("GET", "/readyz", None))
        if self.health_error is not None:
            raise self.health_error
        return {"status": "ok"}

    async def dashboard_json_request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del endpoint
        self.calls.append((method, path, json_body))
        response = self.responses[(method, path)]
        if isinstance(response, Exception):
            raise response
        return response


def endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=10,
        runtime_id="rt-10",
        endpoint_base_url="http://runtime.invalid",
        endpoint_host_header="runtime.invalid",
        api_key="secret",
    )


@pytest.mark.asyncio
async def test_kanban_gate_finds_assigned_nonterminal_task_across_boards() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/plugins/kanban/boards"): {"boards": [{"slug": "team board"}]},
            (
                "GET",
                "/api/plugins/kanban/board?board=default&include_archived=true",
            ): {"columns": [{"tasks": []}]},
            (
                "GET",
                "/api/plugins/kanban/board?board=team%20board&include_archived=true",
            ): {
                "columns": [
                    {
                        "tasks": [
                            {
                                "id": "task-1",
                                "assignee": "expert-7",
                                "status": "in_progress",
                            }
                        ]
                    }
                ]
            },
        }
    )

    blocked = await HermesProfileAdapter(dashboard).has_nonterminal_kanban_task(
        endpoint(),
        profile_name="expert-7",
    )
    runtime_active = await HermesProfileAdapter(
        dashboard
    ).has_any_executable_kanban_task(endpoint())

    assert blocked is True
    assert runtime_active is True


@pytest.mark.asyncio
async def test_kanban_gate_ignores_terminal_and_other_profile_tasks() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/plugins/kanban/boards"): {"boards": []},
            (
                "GET",
                "/api/plugins/kanban/board?board=default&include_archived=true",
            ): {
                "columns": [
                    {
                        "tasks": [
                            {"assignee": "expert-7", "status": "done"},
                            {"assignee": "expert-7", "status": "archived"},
                            {"assignee": "expert-8", "status": "in_progress"},
                        ]
                    }
                ]
            },
        }
    )

    blocked = await HermesProfileAdapter(dashboard).has_nonterminal_kanban_task(
        endpoint(),
        profile_name="expert-7",
    )

    assert blocked is False


@pytest.mark.asyncio
async def test_kanban_gate_treats_missing_native_plugin_as_no_tasks() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/plugins/kanban/boards"): HermesProtocolError(
                "not found",
                status_code=404,
            )
        }
    )

    blocked = await HermesProfileAdapter(dashboard).has_nonterminal_kanban_task(
        endpoint(),
        profile_name="expert-7",
    )

    assert blocked is False


@pytest.mark.asyncio
async def test_blocked_kanban_task_still_blocks_profile_cleanup() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/plugins/kanban/boards"): {"boards": []},
            (
                "GET",
                "/api/plugins/kanban/board?board=default&include_archived=true",
            ): {
                "columns": [
                    {"tasks": [{"assignee": "expert-7", "status": "blocked"}]}
                ]
            },
        }
    )

    blocked = await HermesProfileAdapter(dashboard).has_nonterminal_kanban_task(
        endpoint(),
        profile_name="expert-7",
    )

    assert blocked is True


@pytest.mark.asyncio
async def test_blocked_kanban_task_does_not_keep_runtime_warm() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/plugins/kanban/boards"): {"boards": []},
            (
                "GET",
                "/api/plugins/kanban/board?board=default&include_archived=true",
            ): {
                "columns": [
                    {"tasks": [{"assignee": "expert-7", "status": "blocked"}]}
                ]
            },
        }
    )

    active = await HermesProfileAdapter(
        dashboard
    ).has_any_executable_kanban_task(endpoint())

    assert active is False


@pytest.mark.asyncio
async def test_read_soul_uses_public_profile_api_and_validates_payload() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/profiles/expert-7/soul"): {
                "content": "# 专家人格",
                "exists": True,
            }
        }
    )

    content = await HermesProfileAdapter(dashboard).read_soul(
        endpoint(),
        name="expert-7",
    )

    assert content == "# 专家人格"
    assert dashboard.calls == [("GET", "/api/profiles/expert-7/soul", None)]


@pytest.mark.asyncio
async def test_configure_profile_rebuilds_platform_model_without_credentials() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/config?profile=default"): {
                "agent": {
                    "disabled_toolsets": ["cronjob"],
                    "system_prompt": KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
                },
                "toolsets": ["kanban"],
                "skills": {
                    "disabled": [
                        "codex",
                        "github-auth",
                        "unknown-user-disabled",
                    ]
                },
                "platform": {
                    "skill_visibility": {
                        "policy_version": 1,
                        "platform_disabled": ["codex", "github-auth"],
                    }
                },
                "model": "session",
                "providers": {
                    "platform": {
                        "base_url": ("http://127.0.0.1:9121/model-relay/10/v1"),
                        "api": ("http://127.0.0.1:9121/model-relay/10/v1"),
                        "key_env": "OPENAI_API_KEY",
                        "transport": "chat_completions",
                        "default_model": "1",
                        "discover_models": False,
                    }
                },
                "custom_providers": [
                    {
                        "name": "private",
                        "api_key": "must-not-copy",
                    }
                ],
            },
            ("PUT", "/api/config?profile=expert-7"): {"ok": True},
        }
    )

    await HermesProfileAdapter(dashboard).configure_platform_profile(
        endpoint(),
        name="expert-7",
        runtime_workspace_id=10,
        runtime_agent_profile_id=7,
        config_generation=3,
        plugin_ids=["personal-knowledge"],
        default_model_entry_id=21,
        provider_models={
            "1": {
                "context_length": 128_000,
                "supports_vision": False,
            },
            "21": {
                "context_length": 256_000,
                "supports_vision": True,
            },
        },
        installed_skill_names=["github-auth"],
    )

    body = dashboard.calls[-1][2]
    assert body is not None
    config = body["config"]
    assert config["model"] == {
        "provider": "custom:platform",
        "default": "21",
        "base_url": "http://127.0.0.1:9121/model-relay/10/v1",
        "api_mode": "chat_completions",
        "default_model_entry_id": 21,
        "ollama_num_ctx": 0,
    }
    assert config["providers"] == {
        "platform": {
            "name": "platform",
            "base_url": "http://127.0.0.1:9121/model-relay/10/v1",
            "api": "http://127.0.0.1:9121/model-relay/10/v1",
            "key_env": "OPENAI_API_KEY",
            "transport": "chat_completions",
            "default_model": "21",
            "discover_models": False,
            "models": {
                "1": {
                    "context_length": 128_000,
                    "supports_vision": False,
                },
                "21": {
                    "context_length": 256_000,
                    "supports_vision": True,
                },
            },
        }
    }
    assert "custom_providers" not in config
    assert "toolsets" not in config
    assert config["agent"] == {"disabled_toolsets": ["cronjob"]}
    assert "system_prompt" not in config["agent"]
    assert config["plugins"]["enabled"] == [
        "kanban-lifecycle",
        "clarify-lifecycle",
        "platform-main-conversation-boundary-v1",
        "personal-knowledge",
    ]
    assert config["platform"]["runtime_agent_profile_id"] == 7
    assert config["workspace_dir"] == "/data/workspace/experts/7/shared"
    assert config["skills"]["external_dirs"] == [
        "/opt/hermes/skills",
        "/data/workspace/experts/7/skills",
    ]
    assert "hermes-desktop-plugins" in config["skills"]["disabled"]
    assert "codex" in config["skills"]["disabled"]
    assert "github-auth" not in config["skills"]["disabled"]
    assert "unknown-user-disabled" in config["skills"]["disabled"]
    assert "github-auth" not in config["platform"]["skill_visibility"]["platform_disabled"]


@pytest.mark.asyncio
async def test_replace_mcp_servers_requires_profile_scoped_persistence() -> None:
    dashboard = FakeDashboard(
        {
            ("PUT", "/api/mcp/servers?profile=expert-7"): {"ok": True},
            ("GET", "/api/mcp/servers?profile=expert-7"): {"servers": []},
        }
    )

    with pytest.raises(HermesProtocolError, match="MCP verification"):
        await HermesProfileAdapter(dashboard).replace_mcp_servers(
            endpoint(),
            name="expert-7",
            servers={
                "marker": {
                    "transport": "streamable-http",
                    "url": "http://mcp.invalid/mcp",
                }
            },
        )

    assert dashboard.calls[:3] == [
        (
            "PUT",
            "/api/mcp/servers?profile=expert-7",
            {
                "servers": {
                    "marker": {
                        "transport": "streamable-http",
                        "url": "http://mcp.invalid/mcp",
                    }
                },
                "profile": "expert-7",
            },
        ),
        ("GET", "/api/mcp/servers?profile=expert-7", None),
    ]


@pytest.mark.asyncio
async def test_replace_mcp_servers_accepts_matching_non_sensitive_summary() -> None:
    dashboard = FakeDashboard(
        {
            ("PUT", "/api/mcp/servers?profile=expert-7"): {"ok": True},
            ("GET", "/api/mcp/servers?profile=expert-7"): {
                "servers": [
                    {
                        "name": "marker",
                        "transport": "http",
                        "url": "http://mcp.invalid/mcp",
                        "command": None,
                        "args": [],
                        "env": {"MARKER_TOKEN": "***"},
                        "auth": "header",
                        "enabled": True,
                        "tools": ["get_marker"],
                    }
                ]
            },
            ("GET", "/api/config/raw?profile=expert-7"): {
                "yaml": "model: {}\n"
            },
            ("PUT", "/api/config/raw?profile=expert-7"): {"ok": True},
        }
    )

    await HermesProfileAdapter(dashboard).replace_mcp_servers(
        endpoint(),
        name="expert-7",
        servers={
            "marker": {
                "transport": "streamable-http",
                "url": "http://mcp.invalid/mcp",
                "headers": {"Authorization": "Bearer ${MARKER_TOKEN}"},
                "env": {"MARKER_TOKEN": "${MARKER_TOKEN}"},
                "tools": ["get_marker"],
            }
        },
    )

    assert dashboard.calls[-1] == (
        "PUT",
        "/api/config/raw?profile=expert-7",
        dashboard.calls[-1][2],
    )
    assert "mcp_discovery_timeout: 30" in dashboard.calls[-1][2]["yaml_text"]
    assert "hermes-cli" in dashboard.calls[-1][2]["yaml_text"]
    assert "mcp-marker" in dashboard.calls[-1][2]["yaml_text"]


@pytest.mark.asyncio
async def test_replace_profile_skills_uses_bounded_external_directory() -> None:
    dashboard = FakeDashboard(
        {
            ("DELETE", "/api/files"): {"ok": True},
            ("POST", "/api/files/upload"): {"ok": True},
            ("GET", "/api/config/raw?profile=expert-7"): {"yaml": "model: {}\n"},
            ("PUT", "/api/config/raw?profile=expert-7"): {"ok": True},
        }
    )

    await HermesProfileAdapter(dashboard).replace_profile_skills(
        endpoint(),
        name="expert-7",
        runtime_agent_profile_id=7,
        files={
            "expert/writer/SKILL.md": b"# Writer",
            "expert/writer/references/style.md": b"# Style",
        },
    )

    assert dashboard.calls[:3] == [
        (
            "DELETE",
            "/api/files",
            {
                "path": "/data/workspace/experts/7/skills",
                "recursive": True,
            },
        ),
        (
            "POST",
            "/api/files/upload",
            {
                "path": ("/data/workspace/experts/7/skills/expert/writer/SKILL.md"),
                "data_url": "data:application/octet-stream;base64,IyBXcml0ZXI=",
                "overwrite": True,
            },
        ),
        (
            "POST",
            "/api/files/upload",
            {
                "path": ("/data/workspace/experts/7/skills/expert/writer/references/style.md"),
                "data_url": "data:application/octet-stream;base64,IyBTdHlsZQ==",
                "overwrite": True,
            },
        ),
    ]
    assert dashboard.calls[3][0:2] == ("GET", "/api/config/raw?profile=expert-7")
    assert dashboard.calls[4][0:2] == ("PUT", "/api/config/raw?profile=expert-7")


@pytest.mark.asyncio
async def test_replace_profile_skills_treats_absent_empty_tree_as_success() -> None:
    dashboard = FakeDashboard(
        {
            ("DELETE", "/api/files"): HermesProtocolError(
                "not found",
                status_code=404,
            ),
            ("GET", "/api/config/raw?profile=expert-7"): {"yaml": "model: {}\n"},
            ("PUT", "/api/config/raw?profile=expert-7"): {"ok": True},
        }
    )

    await HermesProfileAdapter(dashboard).replace_profile_skills(
        endpoint(),
        name="expert-7",
        runtime_agent_profile_id=7,
        files={},
    )

    assert dashboard.calls[:1] == [
        (
            "DELETE",
            "/api/files",
            {
                "path": "/data/workspace/experts/7/skills",
                "recursive": True,
            },
        )
    ]
    assert dashboard.calls[1][0:2] == ("GET", "/api/config/raw?profile=expert-7")
    assert dashboard.calls[2][0:2] == ("PUT", "/api/config/raw?profile=expert-7")


@pytest.mark.asyncio
async def test_replace_profile_skills_validates_paths_before_deleting_tree() -> None:
    dashboard = FakeDashboard({})

    with pytest.raises(HermesProtocolError, match="skill path"):
        await HermesProfileAdapter(dashboard).replace_profile_skills(
            endpoint(),
            name="expert-7",
            runtime_agent_profile_id=7,
            files={"expert/../other/SKILL.md": b"unsafe"},
        )

    assert dashboard.calls == []


@pytest.mark.asyncio
async def test_compatibility_probe_requires_profiles_config_and_native_kanban() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/profiles"): {
                "profiles": [
                    {
                        "name": "default",
                        "is_default": True,
                        "description": "",
                        "skill_count": 0,
                    }
                ]
            },
            ("GET", "/api/config?profile=default"): {"model": "session"},
            ("GET", "/api/files?path=%2Fdata%2Fworkspace"): {"entries": []},
            ("GET", "/api/mcp/servers?profile=default"): {"servers": []},
            ("GET", "/api/plugins/kanban/boards"): {"boards": []},
        }
    )

    await HermesProfileAdapter(dashboard).assert_compatible(endpoint())

    assert [call[:2] for call in dashboard.calls] == [
        ("GET", "/readyz"),
        ("GET", "/api/profiles"),
        ("GET", "/api/config?profile=default"),
        ("GET", "/api/files?path=%2Fdata%2Fworkspace"),
        ("GET", "/api/mcp/servers?profile=default"),
        ("GET", "/api/plugins/kanban/boards"),
    ]


@pytest.mark.asyncio
async def test_compatibility_probe_stops_before_dashboard_when_runtime_not_ready() -> None:
    dashboard = FakeDashboard(
        {},
        health_error=HermesProtocolError(
            "Runtime Supervisor is not ready",
            status_code=503,
            path="/readyz",
        ),
    )

    with pytest.raises(HermesProtocolError) as failure:
        await HermesProfileAdapter(dashboard).assert_compatible(endpoint())

    assert failure.value.path == "/readyz"
    assert dashboard.calls == [("GET", "/readyz", None)]


@pytest.mark.asyncio
async def test_compatibility_probe_fails_closed_without_native_kanban() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/profiles"): {
                "profiles": [
                    {
                        "name": "default",
                        "is_default": True,
                        "description": "",
                        "skill_count": 0,
                    }
                ]
            },
            ("GET", "/api/config?profile=default"): {},
            ("GET", "/api/files?path=%2Fdata%2Fworkspace"): {"entries": []},
            ("GET", "/api/mcp/servers?profile=default"): {"servers": []},
            ("GET", "/api/plugins/kanban/boards"): HermesProtocolError(
                "not found",
                status_code=404,
            ),
        }
    )

    with pytest.raises(HermesProtocolError):
        await HermesProfileAdapter(dashboard).assert_compatible(endpoint())


@pytest.mark.asyncio
async def test_delete_workspace_is_bounded_to_one_platform_expert_root() -> None:
    dashboard = FakeDashboard(
        {
            ("DELETE", "/api/files"): {"ok": True},
            (
                "GET",
                "/api/files?path=%2Fdata%2Fworkspace%2Fexperts%2F7",
            ): HermesProtocolError("not found", status_code=404),
        }
    )

    await HermesProfileAdapter(dashboard).delete_workspace(
        endpoint(),
        runtime_agent_profile_id=7,
    )

    assert dashboard.calls == [
        (
            "DELETE",
            "/api/files",
            {
                "path": "/data/workspace/experts/7",
                "recursive": True,
            },
        ),
        (
            "GET",
            "/api/files?path=%2Fdata%2Fworkspace%2Fexperts%2F7",
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_delete_profile_is_idempotent_when_profile_is_absent() -> None:
    dashboard = FakeDashboard(
        {
            ("DELETE", "/api/profiles/expert-7"): HermesProtocolError(
                "not found",
                status_code=404,
            ),
            ("GET", "/api/profiles"): {"profiles": []},
        }
    )

    await HermesProfileAdapter(dashboard).delete_profile(
        endpoint(),
        name="expert-7",
    )

    assert [call[:2] for call in dashboard.calls] == [
        ("DELETE", "/api/profiles/expert-7"),
        ("GET", "/api/profiles"),
    ]


@pytest.mark.asyncio
async def test_delete_profile_does_not_swallow_non_404_failure() -> None:
    dashboard = FakeDashboard(
        {
            ("DELETE", "/api/profiles/expert-7"): HermesProtocolError(
                "forbidden",
                status_code=403,
            ),
        }
    )

    with pytest.raises(HermesProtocolError) as captured:
        await HermesProfileAdapter(dashboard).delete_profile(
            endpoint(),
            name="expert-7",
        )

    assert captured.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_workspace_is_idempotent_when_expert_root_is_absent() -> None:
    dashboard = FakeDashboard(
        {
            ("DELETE", "/api/files"): HermesProtocolError(
                "not found",
                status_code=404,
            ),
            (
                "GET",
                "/api/files?path=%2Fdata%2Fworkspace%2Fexperts%2F7",
            ): HermesProtocolError("not found", status_code=404),
        }
    )

    await HermesProfileAdapter(dashboard).delete_workspace(
        endpoint(),
        runtime_agent_profile_id=7,
    )

    assert dashboard.calls[0][2] == {
        "path": "/data/workspace/experts/7",
        "recursive": True,
    }


@pytest.mark.asyncio
async def test_ensure_workspace_creates_and_observes_bounded_shared_root() -> None:
    path = "/api/files?path=%2Fdata%2Fworkspace%2Fexperts%2F7%2Fshared"
    dashboard = FakeDashboard(
        {
            ("POST", "/api/files/mkdir"): {"ok": True},
            ("GET", path): {"entries": []},
        }
    )

    await HermesProfileAdapter(dashboard).ensure_workspace(
        endpoint(),
        runtime_agent_profile_id=7,
    )

    assert dashboard.calls == [
        (
            "POST",
            "/api/files/mkdir",
            {"path": "/data/workspace/experts/7/shared"},
        ),
        ("GET", path, None),
    ]


@pytest.mark.asyncio
async def test_verify_profile_projection_checks_identity_plugins_soul_and_workspace() -> None:
    workspace_path = "/api/files?path=%2Fdata%2Fworkspace%2Fexperts%2F7%2Fshared"
    dashboard = FakeDashboard(
        {
            ("GET", "/api/profiles"): {
                "profiles": [
                    {
                        "name": "expert-7",
                        "is_default": False,
                        "description": "写作专家",
                        "skill_count": 0,
                    }
                ]
            },
            ("GET", "/api/profiles/expert-7/soul"): {"content": "# 写作专家"},
            ("GET", "/api/config?profile=expert-7"): {
                "platform": {
                    "runtime_workspace_id": 10,
                    "runtime_agent_profile_id": 7,
                    "config_generation": 3,
                },
                "plugins": {
                    "enabled": [
                        "kanban-lifecycle",
                        "clarify-lifecycle",
                        "platform-main-conversation-boundary-v1",
                        "personal-knowledge",
                    ]
                },
                "skills": {
                    "external_dirs": [
                        "/opt/hermes/skills",
                        "/data/workspace/experts/7/skills",
                    ]
                },
            },
            ("GET", "/api/model/info?profile=expert-7"): {
                "model": "21",
                "provider": "custom:platform",
            },
            ("GET", workspace_path): {"entries": []},
        }
    )

    await HermesProfileAdapter(dashboard).verify_profile_projection(
        endpoint(),
        name="expert-7",
        description="写作专家",
        soul_content="# 写作专家",
        runtime_workspace_id=10,
        runtime_agent_profile_id=7,
        config_generation=3,
        plugin_ids=["personal-knowledge"],
        default_model_entry_id=21,
    )

    assert dashboard.calls[-1] == ("GET", workspace_path, None)


@pytest.mark.asyncio
async def test_verify_profile_projection_rejects_another_experts_skill_root() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/profiles"): {
                "profiles": [
                    {
                        "name": "expert-7",
                        "is_default": False,
                        "description": "写作专家",
                        "skill_count": 0,
                    }
                ]
            },
            ("GET", "/api/profiles/expert-7/soul"): {"content": "# 写作专家"},
            ("GET", "/api/config?profile=expert-7"): {
                "platform": {
                    "runtime_workspace_id": 10,
                    "runtime_agent_profile_id": 7,
                    "config_generation": 3,
                },
                "plugins": {
                    "enabled": [
                        "kanban-lifecycle",
                        "clarify-lifecycle",
                        "platform-main-conversation-boundary-v1",
                    ]
                },
                "skills": {
                    "external_dirs": [
                        "/opt/hermes/skills",
                        "/data/workspace/experts/8/skills",
                    ]
                },
            },
            ("GET", "/api/model/info?profile=expert-7"): {
                "model": "21",
                "provider": "custom:platform",
            },
        }
    )

    with pytest.raises(HermesProtocolError, match="Skill directory"):
        await HermesProfileAdapter(dashboard).verify_profile_projection(
            endpoint(),
            name="expert-7",
            description="写作专家",
            soul_content="# 写作专家",
            runtime_workspace_id=10,
            runtime_agent_profile_id=7,
            config_generation=3,
            plugin_ids=[],
            default_model_entry_id=21,
        )


@pytest.mark.asyncio
async def test_verify_profile_projection_rejects_non_platform_model() -> None:
    dashboard = FakeDashboard(
        {
            ("GET", "/api/profiles"): {
                "profiles": [
                    {
                        "name": "expert-7",
                        "is_default": False,
                        "description": "写作专家",
                        "skill_count": 0,
                    }
                ]
            },
            ("GET", "/api/profiles/expert-7/soul"): {"content": "# 写作专家"},
            ("GET", "/api/config?profile=expert-7"): {
                "platform": {
                    "runtime_workspace_id": 10,
                    "runtime_agent_profile_id": 7,
                    "config_generation": 3,
                },
                "plugins": {
                    "enabled": [
                        "kanban-lifecycle",
                        "clarify-lifecycle",
                        "platform-main-conversation-boundary-v1",
                    ]
                },
                "skills": {
                    "external_dirs": [
                        "/opt/hermes/skills",
                        "/data/workspace/experts/7/skills",
                    ]
                },
            },
            ("GET", "/api/model/info?profile=expert-7"): {
                "model": "21",
                "provider": "openrouter",
            },
        }
    )

    with pytest.raises(HermesProtocolError, match="platform model"):
        await HermesProfileAdapter(dashboard).verify_profile_projection(
            endpoint(),
            name="expert-7",
            description="写作专家",
            soul_content="# 写作专家",
            runtime_workspace_id=10,
            runtime_agent_profile_id=7,
            config_generation=3,
            plugin_ids=[],
            default_model_entry_id=21,
        )
