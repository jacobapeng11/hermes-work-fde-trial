from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import app.hermes_runtime.dashboard_protocol as runtime_control
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "runtime-control-v1"
)


def endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=10,
        runtime_id="rt-10",
        endpoint_base_url="http://runtime.invalid",
        endpoint_host_header="runtime.invalid",
        api_key="secret",
    )


def upstream_error(*, status_code: int, path: str) -> dict:
    payload = json.loads(
        (FIXTURE_ROOT / "dashboard-upstream-error.json").read_text(
            encoding="utf-8"
        )
    )
    payload["detail"]["upstream_status_code"] = status_code
    payload["detail"]["upstream_path"] = path
    return payload


@pytest.mark.asyncio
async def test_missing_skill_tree_crosses_control_seam_as_idempotent_404(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads((await request.aread()).decode("utf-8"))
        calls.append(payload)
        params = payload["params"]
        assert isinstance(params, dict)
        if len(calls) == 1:
            assert params == {
                "method": "DELETE",
                "path": "/api/files",
                "json_body": {
                    "path": "/data/workspace/experts/7/skills",
                    "recursive": True,
                },
            }
            return httpx.Response(
                502,
                json=upstream_error(status_code=404, path="/api/files"),
            )
        if len(calls) == 2:
            assert params == {
                "method": "GET",
                "path": "/api/config/raw?profile=expert-7",
                "json_body": None,
            }
            return httpx.Response(
                200,
                json={"result": {"yaml": "model: {}\n"}},
            )
        assert len(calls) == 3
        assert params["method"] == "PUT"
        assert params["path"] == "/api/config/raw?profile=expert-7"
        assert isinstance(params["json_body"], dict)
        return httpx.Response(200, json={"result": {"ok": True}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    try:
        await HermesProfileAdapter(
            HermesDashboardProtocolAdapter()
        ).replace_profile_skills(
            endpoint(),
            name="expert-7",
            runtime_agent_profile_id=7,
            files={},
        )
        assert len(calls) == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_404_dashboard_error_is_not_swallowed_by_profile_adapter(
    monkeypatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            502,
            json=upstream_error(status_code=500, path="/api/files"),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def shared_client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(runtime_control, "shared_runtime_http_client", shared_client)
    try:
        with pytest.raises(HermesProtocolError) as captured:
            await HermesProfileAdapter(
                HermesDashboardProtocolAdapter()
            ).replace_profile_skills(
                endpoint(),
                name="expert-7",
                runtime_agent_profile_id=7,
                files={},
            )
        assert captured.value.status_code == 500
        assert captured.value.path == "/api/files"
    finally:
        await client.aclose()
