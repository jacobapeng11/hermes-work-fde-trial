from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
from typing import Any
import zipfile

import pytest

from platform_supervisor.asset_contracts import ProfileProjectionApplyRequest
from platform_supervisor.profile_projection import ProfileProjectionController
from platform_supervisor.profile_projection_state import ProfileProjectionStateStore


def _archive() -> tuple[bytes, str]:
    files = {"SKILL.md": b"# Current\n"}
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


class _Downloader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = 0

    async def download(self, **_kwargs) -> bytes:
        self.calls += 1
        return self.content


class _Dashboard:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {
            "default": {
                "model": "1",
                "providers": {"platform": {"default_model": "1"}},
                "compression": {
                    "max_attempts": 10,
                    "target_ratio": 0.1,
                    "proactive_prune_tokens": 64000,
                },
            },
            "expert-173": {
                "model": "1",
                "providers": {"platform": {"default_model": "1"}},
                "skills": {"external_dirs": []},
                "plugins": {"enabled": []},
                "platform": {},
            }
        }

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
            {
                "model": "1",
                "providers": {"platform": {"default_model": "1"}},
                "skills": {"external_dirs": []},
                "plugins": {"enabled": []},
                "platform": {},
            },
        )

    async def read_profile_config(self, profile_name: str) -> dict[str, Any]:
        return self.documents[profile_name]

    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None:
        self.documents[profile_name] = document


def _request(digest: str) -> dict[str, Any]:
    return {
        "operation_id": "profile-operation-0001",
        "runtime_id": "rt-119",
        "binding_id": 119,
        "runtime_workspace_id": 119,
        "runtime_agent_profile_id": 173,
        "hermes_profile_name": "expert-173",
        "desired_revision": 1,
        "expected_applied_revision": 0,
        "expected_effective_generation": 0,
        "digests": {
            "profile": f"sha256:{'a' * 64}",
            "skill": f"sha256:{'b' * 64}",
            "mcp": f"sha256:{'c' * 64}",
            "knowledge": f"sha256:{'d' * 64}",
        },
        "profile_config": {
            "description": "Alpha",
            "soul_content": "Alpha soul",
            "platform_toolsets": {"cli": []},
            "mcp_servers": {
                "crm": {
                    "transport": "streamable_http",
                    "url": "https://crm.invalid/mcp",
                    "enabled": True,
                }
            },
        },
        "artifacts": [
            {
                "asset_type": "skill",
                "asset_id": 11,
                "version_id": 12,
                "storage_uri": "object://skills/11/12",
                "sha256": digest,
                "target_relative_path": "skills/releases/11/current",
            }
        ],
    }


@pytest.mark.asyncio
async def test_reused_projection_prunes_legacy_skill_release(tmp_path: Path) -> None:
    content, digest = _archive()
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    downloader = _Downloader(content)
    dashboard = _Dashboard()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=downloader,
    )
    request = ProfileProjectionApplyRequest.model_validate(_request(digest))

    await controller.apply(request)
    legacy = tmp_path / "home/platform/profile-assets/skills/releases/11/12"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# Legacy\n", encoding="utf-8")
    dashboard.documents["expert-173"]["skills"]["external_dirs"].append(
        str(legacy)
    )

    result = await controller.apply(request)

    assert result.status == "applied"
    assert not legacy.exists()
    assert dashboard.documents["expert-173"]["skills"]["external_dirs"] == [
        str(tmp_path / "home/platform/profile-assets/skills/releases/11/current")
    ]
    assert downloader.calls == 1
    await state.close()
