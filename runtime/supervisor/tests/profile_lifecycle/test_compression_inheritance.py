from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from platform_supervisor.asset_contracts import ProfileProjectionApplyRequest
from platform_supervisor.profile_projection import ProfileProjectionController
from platform_supervisor.profile_projection_state import ProfileProjectionStateStore


PLATFORM_COMPRESSION = {
    "max_attempts": 10,
    "target_ratio": 0.1,
    "proactive_prune_tokens": 64000,
}


class FakeDownloader:
    async def download(self, **_kwargs: Any) -> bytes:
        return b""


class FakeDashboard:
    def __init__(self, compression: dict[str, Any] | None) -> None:
        self.documents: dict[str, dict[str, Any]] = {
            "default": {
                "model": "1",
                **({"compression": deepcopy(compression)} if compression else {}),
            },
            "expert-173": {"platform": {}},
        }
        self.writes = 0

    async def ensure_profile(
        self,
        *,
        profile_name: str,
        description: str,
        soul_content: str,
    ) -> None:
        del description, soul_content
        self.documents.setdefault(profile_name, {"platform": {}})

    async def read_profile_config(self, profile_name: str) -> dict[str, Any]:
        return self.documents[profile_name]

    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None:
        self.writes += 1
        self.documents[profile_name] = document


def _request() -> ProfileProjectionApplyRequest:
    return ProfileProjectionApplyRequest(
        operation_id="profile-operation-0001",
        runtime_id="rt-119",
        binding_id=119,
        runtime_workspace_id=119,
        runtime_agent_profile_id=173,
        hermes_profile_name="expert-173",
        desired_revision=1,
        expected_applied_revision=0,
        expected_effective_generation=0,
        digests={
            "profile": f"sha256:{'a' * 64}",
            "skill": f"sha256:{'b' * 64}",
            "mcp": f"sha256:{'c' * 64}",
            "knowledge": f"sha256:{'d' * 64}",
        },
        profile_config={
            "description": "Profile probe",
            "soul_content": "Profile probe soul",
            "plugin_ids": [],
            "platform_toolsets": {"cli": ["hermes-cli"]},
            "mcp_servers": {},
        },
    )


@pytest.mark.asyncio
async def test_named_profile_inherits_and_heals_platform_compression(
    tmp_path: Path,
) -> None:
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FakeDashboard(PLATFORM_COMPRESSION)
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(),
    )
    request = _request()

    assert (await controller.apply(request)).status == "applied"
    assert dashboard.documents["expert-173"]["compression"] == PLATFORM_COMPRESSION
    assert dashboard.writes == 1

    repeated = await controller.apply(request)
    assert repeated.status == "applied"
    assert dashboard.writes == 1

    dashboard.documents["expert-173"]["compression"] = {
        "max_attempts": 3,
        "target_ratio": 0.2,
        "proactive_prune_tokens": 0,
    }
    healed = await controller.apply(request)

    assert healed.status == "applied"
    assert dashboard.writes == 2
    assert dashboard.documents["expert-173"]["compression"] == PLATFORM_COMPRESSION
    await state.close()


@pytest.mark.asyncio
async def test_named_profile_projection_fails_closed_without_root_compression(
    tmp_path: Path,
) -> None:
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FakeDashboard(None)
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(),
    )

    result = await controller.apply(_request())

    assert result.status == "failed"
    assert result.error_code == "PROFILE_COMPRESSION_CONFIG_INVALID"
    assert dashboard.writes == 0
    await state.close()
