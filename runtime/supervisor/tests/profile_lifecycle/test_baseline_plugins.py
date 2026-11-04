from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
from typing import Any
import zipfile

import pytest

from platform_supervisor.asset_contracts import ProfileProjectionApplyRequest
from platform_supervisor.profile_projection import (
    PLATFORM_BASELINE_PROFILE_PLUGINS,
    ProfileProjectionController,
)
from platform_supervisor.profile_projection_state import ProfileProjectionStateStore


def _skill_archive() -> tuple[bytes, str]:
    files = {"SKILL.md": b"# Profile probe\n"}
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(value)
        digest.update(b"\0")
    return content.getvalue(), digest.hexdigest()


class FakeDownloader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = 0

    async def download(self, **_kwargs: Any) -> bytes:
        self.calls += 1
        return self.content


class FakeDashboard:
    def __init__(self, *, initial_plugins: list[str]) -> None:
        self.documents: dict[str, dict[str, Any]] = {
            "default": {
                "model": "model-1",
                "compression": {
                    "max_attempts": 10,
                    "target_ratio": 0.1,
                    "proactive_prune_tokens": 64000,
                },
            },
            "expert-173": {
                "plugins": {"enabled": list(initial_plugins)},
                "platform": {},
            },
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
        self.documents.setdefault(
            profile_name,
            {"plugins": {"enabled": []}, "platform": {}},
        )

    async def read_profile_config(self, profile_name: str) -> dict[str, Any]:
        return self.documents[profile_name]

    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None:
        self.writes += 1
        self.documents[profile_name] = document


def _request(
    sha256: str,
    *,
    revision: int = 1,
    expected: int = 0,
    plugin_ids: list[str] | None = None,
) -> ProfileProjectionApplyRequest:
    return ProfileProjectionApplyRequest(
        operation_id=f"profile-operation-{revision:04d}",
        runtime_id="rt-119",
        binding_id=119,
        runtime_workspace_id=119,
        runtime_agent_profile_id=173,
        hermes_profile_name="expert-173",
        desired_revision=revision,
        expected_applied_revision=expected,
        expected_effective_generation=expected,
        digests={
            "profile": f"sha256:{'a' * 64}",
            "skill": f"sha256:{'b' * 64}",
            "mcp": f"sha256:{'c' * 64}",
            "knowledge": f"sha256:{'d' * 64}",
        },
        profile_config={
            "description": "Profile probe",
            "soul_content": "Profile probe soul",
            "plugin_ids": [] if plugin_ids is None else plugin_ids,
            "platform_toolsets": {"cli": ["hermes-cli"]},
            "mcp_servers": {},
        },
        artifacts=[
            {
                "asset_type": "skill",
                "asset_id": 11,
                "version_id": 12,
                "storage_uri": "object://skills/11/12",
                "sha256": sha256,
                "target_relative_path": "skills/releases/11/current",
            }
        ],
    )


async def _apply_once(
    tmp_path: Path,
    *,
    dashboard: FakeDashboard,
    request: ProfileProjectionApplyRequest,
):
    content, digest = _skill_archive()
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(content),
    )
    try:
        return await controller.apply(request)
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_fresh_profile_with_empty_plugins_gets_platform_baseline(
    tmp_path: Path,
) -> None:
    _, digest = _skill_archive()
    dashboard = FakeDashboard(initial_plugins=[])
    request = _request(digest, plugin_ids=[])

    result = await _apply_once(tmp_path, dashboard=dashboard, request=request)

    assert result.status == "applied"
    assert dashboard.documents["expert-173"]["plugins"]["enabled"] == list(
        PLATFORM_BASELINE_PROFILE_PLUGINS
    )
    projection = dashboard.documents["expert-173"]["platform"]["asset_projection"]
    assert projection["managed_plugin_ids"] == []


@pytest.mark.asyncio
async def test_business_plugins_are_added_and_removed_without_losing_baseline(
    tmp_path: Path,
) -> None:
    _, digest = _skill_archive()
    dashboard = FakeDashboard(initial_plugins=[])
    first = _request(digest, plugin_ids=["business-report"])

    first_result = await _apply_once(
        tmp_path,
        dashboard=dashboard,
        request=first,
    )

    assert first_result.status == "applied"
    assert dashboard.documents["expert-173"]["plugins"]["enabled"] == [
        *PLATFORM_BASELINE_PROFILE_PLUGINS,
        "business-report",
    ]
    projection = dashboard.documents["expert-173"]["platform"]["asset_projection"]
    assert projection["managed_plugin_ids"] == ["business-report"]

    second = _request(
        digest,
        revision=2,
        expected=1,
        plugin_ids=[],
    )
    second_result = await _apply_once(
        tmp_path,
        dashboard=dashboard,
        request=second,
    )

    assert second_result.status == "applied"
    assert dashboard.documents["expert-173"]["plugins"]["enabled"] == list(
        PLATFORM_BASELINE_PROFILE_PLUGINS
    )
    projection = dashboard.documents["expert-173"]["platform"]["asset_projection"]
    assert projection["managed_plugin_ids"] == []


@pytest.mark.asyncio
async def test_same_revision_baseline_drift_is_rewritten(
    tmp_path: Path,
) -> None:
    _, digest = _skill_archive()
    dashboard = FakeDashboard(
        initial_plugins=list(PLATFORM_BASELINE_PROFILE_PLUGINS)
    )
    request = _request(digest, plugin_ids=[])

    first = await _apply_once(tmp_path, dashboard=dashboard, request=request)
    assert first.status == "applied"
    assert dashboard.writes == 1

    dashboard.documents["expert-173"]["plugins"]["enabled"] = [
        "kanban-lifecycle",
        "platform-main-conversation-boundary-v1",
    ]
    healed = await _apply_once(tmp_path, dashboard=dashboard, request=request)

    assert healed.status == "applied"
    assert dashboard.writes == 2
    assert dashboard.documents["expert-173"]["plugins"]["enabled"] == list(
        PLATFORM_BASELINE_PROFILE_PLUGINS
    )


@pytest.mark.asyncio
async def test_plugin_output_is_deduplicated_and_stably_ordered(
    tmp_path: Path,
) -> None:
    _, digest = _skill_archive()
    dashboard = FakeDashboard(
        initial_plugins=[
            "clarify-lifecycle",
            "custom-plugin",
            "kanban-lifecycle",
            "clarify-lifecycle",
        ]
    )
    request = _request(digest, plugin_ids=["business-report", "custom-plugin"])

    result = await _apply_once(tmp_path, dashboard=dashboard, request=request)

    assert result.status == "applied"
    assert dashboard.documents["expert-173"]["plugins"]["enabled"] == [
        *PLATFORM_BASELINE_PROFILE_PLUGINS,
        "custom-plugin",
        "business-report",
    ]
