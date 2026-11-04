from __future__ import annotations

from io import BytesIO
import json

import httpx
import pytest

import app.hermes_runtime.dashboard_protocol as runtime_control
from app.hermes_runtime.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
    RuntimeControlBusy,
)
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.protocol import HermesProtocolError
from app.schemas.asset_projection import (
    ProfileProjectionApplyRequest,
    RuntimeMcpRegistryApplyRequest,
)


def endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=7,
        runtime_id="runtime-7",
        endpoint_base_url="http://kourier.test",
        endpoint_host_header="runtime-7.hermes.test",
        api_key="runtime-secret",
    )


@pytest.mark.asyncio
async def test_control_client_uses_supervisor_bearer_and_host(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"state": "HERMES_READY"})
        payload = json.loads((await request.aread()).decode("utf-8"))
        assert payload["action"] == "reload_skills_and_list"
        return httpx.Response(200, json={"result": [{"name": "search"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    adapter = HermesDashboardProtocolAdapter()
    assert (await adapter.health(endpoint()))["transport"] == "runtime-supervisor"
    assert await adapter.reload_skills_and_list(endpoint()) == [{"name": "search"}]
    assert requests[1].headers["authorization"] == "Bearer runtime-secret"
    assert requests[1].headers["host"] == "runtime-7.hermes.test"
    await client.aclose()


@pytest.mark.asyncio
async def test_health_wraps_transport_failure_as_runtime_unavailable(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    try:
        with pytest.raises(HermesProtocolError) as failure:
            await HermesDashboardProtocolAdapter().health(endpoint())
    finally:
        await client.aclose()

    assert failure.value.path == "/readyz"
    assert failure.value.status_code == 503


@pytest.mark.asyncio
async def test_control_client_lists_mcp_for_explicit_profile(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode("utf-8"))
        assert payload == {
            "action": "list_profile_mcp_servers",
            "params": {"profile_name": "expert-7"},
        }
        return httpx.Response(200, json={"result": [{"name": "docs"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    try:
        assert await HermesDashboardProtocolAdapter().list_profile_mcp_servers(
            endpoint(),
            profile_name="expert-7",
        ) == [{"name": "docs"}]
    finally:
        await client.aclose()


def test_control_client_keeps_busy_error_semantics() -> None:
    response = httpx.Response(409, json={"detail": "RUNTIME_CONTROL_BUSY"})

    with pytest.raises(RuntimeControlBusy) as captured:
        HermesDashboardProtocolAdapter._raise_for_status(
            response,
            path="/internal/v1/control",
        )

    assert captured.value.status_code == 409


def test_control_client_does_not_mask_projection_conflict_as_busy() -> None:
    response = httpx.Response(
        409,
        json={"detail": "PROFILE_EFFECTIVE_GENERATION_STALE"},
    )

    with pytest.raises(HermesProtocolError) as captured:
        HermesDashboardProtocolAdapter._raise_for_status(
            response,
            path="/internal/v1/control",
        )

    assert not isinstance(captured.value, RuntimeControlBusy)
    assert str(captured.value) == "PROFILE_EFFECTIVE_GENERATION_STALE"
    assert captured.value.status_code == 409


@pytest.mark.asyncio
async def test_control_client_applies_and_reads_profile_projection(monkeypatch) -> None:
    actions: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode("utf-8"))
        actions.append(payload["action"])
        params = payload["params"]
        if payload["action"] == "profile_projection.status":
            params = {
                "operation_id": "profile-operation-0001",
                "runtime_agent_profile_id": 173,
                "desired_revision": 7,
                "applied_revision": 7,
                "status": "applied",
                "digests": {
                    "profile": f"sha256:{'a' * 64}",
                    "skill": f"sha256:{'b' * 64}",
                    "mcp": f"sha256:{'c' * 64}",
                    "knowledge": f"sha256:{'d' * 64}",
                },
                "error_code": None,
                "error_message": None,
                "schema_version": 1,
            }
        else:
            params = {
                "operation_id": params["operation_id"],
                "runtime_agent_profile_id": params["runtime_agent_profile_id"],
                "desired_revision": params["desired_revision"],
                "applied_revision": params["desired_revision"],
                "status": "applied",
                "digests": params["digests"],
                "error_code": None,
                "error_message": None,
                "schema_version": 1,
            }
        return httpx.Response(200, json={"result": params})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    request = ProfileProjectionApplyRequest(
        operation_id="profile-operation-0001",
        runtime_id="runtime-7",
        binding_id=119,
        runtime_workspace_id=7,
        runtime_agent_profile_id=173,
        hermes_profile_name="expert-173",
        desired_revision=7,
        expected_applied_revision=6,
        digests={
            "profile": f"sha256:{'a' * 64}",
            "skill": f"sha256:{'b' * 64}",
            "mcp": f"sha256:{'c' * 64}",
            "knowledge": f"sha256:{'d' * 64}",
        },
        profile_config={},
    )
    adapter = HermesDashboardProtocolAdapter()
    assert (await adapter.apply_profile_projection(endpoint(), request)).status == "applied"
    assert (
        await adapter.get_profile_projection_status(
            endpoint(),
            runtime_agent_profile_id=173,
        )
    ).applied_revision == 7
    assert actions == ["profile_projection.apply", "profile_projection.status"]
    await client.aclose()


@pytest.mark.asyncio
async def test_control_client_applies_and_reads_runtime_mcp_registry(
    monkeypatch,
) -> None:
    actions: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode("utf-8"))
        action = str(payload["action"])
        actions.append(action)
        params = payload.get("params") or {}
        if action == "runtime_mcp_registry.apply":
            operation_id = params["operation_id"]
            desired_revision = params["desired_revision"]
            registry_digest = params["registry_digest"]
        else:
            operation_id = "registry-operation-1"
            desired_revision = 1
            registry_digest = "sha256:" + "f" * 64
        return httpx.Response(
            200,
            json={
                "result": {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "desired_revision": desired_revision,
                    "applied_revision": desired_revision,
                    "status": "ready",
                    "registry_digest": registry_digest,
                    "servers": [],
                    "affected_active_run_ids": ["run-working-1"],
                    "error_code": None,
                    "error_message": None,
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    request = RuntimeMcpRegistryApplyRequest(
        operation_id="registry-operation-1",
        runtime_id="runtime-7",
        binding_id=119,
        runtime_workspace_id=7,
        desired_revision=1,
        expected_applied_revision=0,
        registry_digest="sha256:" + "f" * 64,
        servers=[],
    )
    adapter = HermesDashboardProtocolAdapter()

    applied = await adapter.apply_runtime_mcp_registry(endpoint(), request)
    status = await adapter.get_runtime_mcp_registry_status(endpoint())

    assert applied.status == status.status == "ready"
    assert applied.affected_active_run_ids == ["run-working-1"]
    assert actions == [
        "runtime_mcp_registry.apply",
        "runtime_mcp_registry.status",
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_control_client_streams_internal_artifact_upload(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/v1/files/content"
        assert request.url.params["path"] == "workspace/sessions/rs-1/input.txt"
        assert await request.aread() == b"hello"
        return httpx.Response(
            200,
            json={
                "entry": {
                    "relative_path": "workspace/sessions/rs-1/input.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 5,
                    "mtime": 1.0,
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    artifact = await HermesDashboardProtocolAdapter().upload_managed_artifact(
        endpoint(),
        relative_path="workspace/sessions/rs-1/input.txt",
        content=BytesIO(b"hello"),
        filename="input.txt",
        mime_type="text/plain",
    )
    assert artifact.size_bytes == 5
    await client.aclose()
