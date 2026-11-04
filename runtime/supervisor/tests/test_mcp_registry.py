from __future__ import annotations

from pathlib import Path

import pytest

from platform_supervisor.asset_contracts import RuntimeMcpRegistryApplyRequest
from platform_supervisor.mcp_registry import (
    McpRegistryExecutionGate,
    RuntimeMcpRegistryController,
)
from platform_supervisor.mcp_registry_state import McpRegistryStateStore


def _request(
    *,
    credential_refs: dict[str, str] | None = None,
    transport: str = "streamable_http",
    connection_config: dict | None = None,
    server_slug: str = "crm",
):
    return RuntimeMcpRegistryApplyRequest(
        operation_id="registry-operation-0001",
        runtime_id="rt-119",
        binding_id=119,
        runtime_workspace_id=119,
        desired_revision=1,
        expected_applied_revision=0,
        registry_digest="sha256:" + "f" * 64,
        servers=[
            {
                "mcp_id": 21,
                "mcp_version_id": 22,
                "server_slug": server_slug,
                "transport": transport,
                "connection_config": connection_config
                or {"url": "http://mcp-crm/mcp"},
                "credential_refs": credential_refs or {},
                "config_digest": "sha256:" + "1" * 64,
            }
        ],
    )


class FakeDashboard:
    def __init__(self) -> None:
        self.document = {
            "model": "1",
            "platform_toolsets": {"cli": ["hermes-cli", "mcp-legacy"]},
        }
        self.reload_calls = 0
        self.list_calls = 0
        self.observed = [
            {
                "slug": "crm",
                "status": "connected",
                "tools": [
                    {
                        "name": "mcp_crm_search_customer",
                        "description": "search",
                        "inputSchema": {"type": "object"},
                    }
                ],
            }
        ]

    async def read_profile_config(self, profile_name: str):
        assert profile_name == "default"
        return self.document

    async def write_profile_config(self, profile_name: str, document):
        assert profile_name == "default"
        self.document = document

    async def reload_mcp_registry_and_list(self):
        self.reload_calls += 1
        return self.observed

    async def list_mcp_registry_servers(self):
        self.list_calls += 1
        return self.observed


@pytest.mark.asyncio
async def test_registry_reload_is_immediate_and_default_gets_no_mcp_tools(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: ("run-working-1",),
    )
    wake_calls = 0

    async def wake() -> None:
        nonlocal wake_calls
        wake_calls += 1

    controller.bind_terminal_sink(wake)

    response = await controller.apply(_request())

    assert response.status == "ready"
    assert response.applied_revision == 1
    assert response.affected_active_run_ids == ["run-working-1"]
    assert response.servers[0].tool_names == ["mcp_crm_search_customer"]
    assert dashboard.reload_calls == 1
    assert wake_calls == 1
    assert dashboard.document["mcp_servers"] == {
        "crm": {
            "url": "http://mcp-crm/mcp",
            "transport": "streamable_http",
            "enabled": True,
        }
    }
    assert dashboard.document["platform_toolsets"]["cli"] == [
        "hermes-cli",
        "no_mcp",
    ]

    repeated = await controller.apply(_request())
    assert repeated.status == "ready"
    assert dashboard.reload_calls == 1
    assert wake_calls == 1

    dashboard.document["mcp_servers"] = {}
    dashboard.document.pop("platform_toolsets")
    healed = await controller.apply(_request())
    assert healed.status == "ready"
    assert dashboard.reload_calls == 2
    assert wake_calls == 2
    assert dashboard.document["platform_toolsets"]["cli"][0] == "hermes-cli"
    await state.close()


@pytest.mark.asyncio
async def test_registry_rejects_invalid_connection_before_state_or_dashboard_write(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
    )
    request = _request()
    request.servers[0].connection_config = {}

    response = await controller.apply(request)

    assert response.status == "failed"
    assert response.error_code == "MCP_CONFIG_INVALID"
    assert dashboard.reload_calls == 0
    assert "mcp_servers" not in dashboard.document
    assert await state.get() is None
    await state.close()


@pytest.mark.asyncio
async def test_registry_reload_gate_waits_new_run_until_terminal_state(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    request = _request()
    await state.begin(request, affected_run_ids=["run-working-1"])
    delegated = []

    async def resolve(identity):
        delegated.append(identity)
        return f"resolved:{identity}"

    gate = McpRegistryExecutionGate(state=state, resolver=resolve)

    assert await gate("new-run") is None
    assert delegated == []

    await state.finish(request, status="failed", servers=[])
    assert await gate("new-run") == "resolved:new-run"
    assert delegated == ["new-run"]
    await state.close()


def test_registry_projection_preserves_only_valid_explicit_default_mcp_access() -> None:
    request = _request()
    current = {
        "platform_toolsets": {
            "cli": ["hermes-cli", "mcp-crm", "mcp-removed", "no_mcp"]
        }
    }

    projected = RuntimeMcpRegistryController._project_default_config(
        current,
        request,
    )

    assert projected["platform_toolsets"]["cli"] == [
        "hermes-cli",
        "mcp-crm",
    ]


def test_registry_projection_restores_required_cli_baseline() -> None:
    projected = RuntimeMcpRegistryController._project_default_config(
        {"platform_toolsets": {"cli": ["mcp-removed"]}},
        _request(),
    )

    assert projected["platform_toolsets"]["cli"] == [
        "hermes-cli",
        "no_mcp",
    ]


def test_registry_accepts_mcp_id_based_server_alias_without_contract_change() -> None:
    request = _request(server_slug="asset-21")
    current = {
        "platform_toolsets": {
            "cli": ["hermes-cli", "mcp-asset-21", "mcp-crm"]
        }
    }

    projected = RuntimeMcpRegistryController._project_default_config(current, request)

    assert projected["mcp_servers"] == {
        "asset-21": {
            "url": "http://mcp-crm/mcp",
            "transport": "streamable_http",
            "enabled": True,
        }
    }
    assert projected["platform_toolsets"]["cli"] == [
        "hermes-cli",
        "mcp-asset-21",
    ]


@pytest.mark.asyncio
async def test_registry_projects_http_credentials_and_reloads(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
    )

    response = await controller.apply(
        _request(
            credential_refs={
                "Authorization": "Bearer test-token",
                "X-Tenant": "tenant-119",
            },
            connection_config={
                "url": "http://mcp-crm/mcp",
                "headers": {"X-Tenant": "stale-tenant"},
            },
        )
    )

    assert response.status == "ready"
    assert dashboard.document["mcp_servers"]["crm"]["headers"] == {
        "Authorization": "Bearer test-token",
        "X-Tenant": "tenant-119",
    }
    assert dashboard.reload_calls == 1
    await state.close()


@pytest.mark.asyncio
async def test_registry_waits_for_tool_discovery_without_reloading_again(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    ready = dashboard.observed
    dashboard.observed = []
    observations = [[], [], ready]

    async def list_servers():
        dashboard.list_calls += 1
        return observations.pop(0)

    dashboard.list_mcp_registry_servers = list_servers
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
        discovery_attempts=4,
        discovery_interval_seconds=0.25,
        sleep=sleep,
    )

    response = await controller.apply(_request())

    assert response.status == "ready"
    assert dashboard.reload_calls == 1
    assert dashboard.list_calls == 3
    assert sleeps == [0.25, 0.25, 0.25]
    await state.close()


@pytest.mark.asyncio
async def test_registry_discovery_timeout_does_not_repeat_reload(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    dashboard.observed = []

    async def sleep(_delay: float) -> None:
        return None

    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
        discovery_attempts=2,
        discovery_interval_seconds=0.01,
        sleep=sleep,
    )

    response = await controller.apply(_request())

    assert response.status == "failed"
    assert response.error_code == "MCP_SERVER_NOT_INITIALIZED"
    assert dashboard.reload_calls == 1
    assert dashboard.list_calls == 2
    await state.close()


@pytest.mark.asyncio
async def test_registry_projects_stdio_credentials_to_env(tmp_path: Path) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
    )

    response = await controller.apply(
        _request(
            credential_refs={"CRM_API_KEY": "test-token"},
            transport="stdio",
            connection_config={
                "command": "crm-mcp",
                "env": {"CRM_REGION": "cn"},
            },
        )
    )

    assert response.status == "ready"
    assert dashboard.document["mcp_servers"]["crm"] == {
        "command": "crm-mcp",
        "env": {"CRM_REGION": "cn", "CRM_API_KEY": "test-token"},
        "transport": "stdio",
        "enabled": True,
    }
    assert dashboard.reload_calls == 1
    await state.close()


@pytest.mark.asyncio
async def test_registry_rejects_invalid_credential_target_without_reload(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
    )

    response = await controller.apply(
        _request(
            credential_refs={"Authorization": "Bearer test-token"},
            connection_config={
                "url": "http://mcp-crm/mcp",
                "headers": ["invalid"],
            },
        )
    )

    assert response.status == "failed"
    assert response.error_code == "MCP_CREDENTIAL_CONFIG_INVALID"
    assert dashboard.reload_calls == 0
    await state.close()


@pytest.mark.asyncio
async def test_registry_rejects_colliding_observed_tool_names(
    tmp_path: Path,
) -> None:
    state = McpRegistryStateStore(tmp_path / "registry.db")
    await state.start()
    dashboard = FakeDashboard()
    request = _request().model_copy(
        update={
            "servers": [
                _request().servers[0],
                _request().servers[0].model_copy(
                    update={
                        "mcp_id": 31,
                        "mcp_version_id": 32,
                        "server_slug": "ticket",
                    }
                ),
            ]
        }
    )
    dashboard.observed.append(
        {
            "slug": "ticket",
            "tools": [{"name": "mcp_crm_search_customer"}],
        }
    )
    controller = RuntimeMcpRegistryController(
        runtime_id="rt-119",
        binding_id="119",
        state=state,
        dashboard=dashboard,
        active_run_ids=lambda: (),
    )

    response = await controller.apply(request)

    assert response.status == "failed"
    assert response.error_code == "MCP_TOOL_NAME_COLLISION"
    await state.close()
