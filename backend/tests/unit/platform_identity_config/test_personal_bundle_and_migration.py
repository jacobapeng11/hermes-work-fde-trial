from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.hermes_runtime.projection import DEFAULT_RUNTIME_SOUL
from app.services.runtime_config_bundle import personal_config_bundle_files
from app.services.runtime_user_memory import (
    PLATFORM_FILE_DELIVERY_BEGIN,
    PLATFORM_IDENTITY_BEGIN,
)
from app.services.runtime_workspace import LocalRuntimeDirectoryProvisioner


def test_personal_bundle_renders_platform_identity_into_soul() -> None:
    files = personal_config_bundle_files(
        artifacts={
            "config.yaml": "model: demo\n",
            "SOUL.md": DEFAULT_RUNTIME_SOUL,
        },
        name="示例用户乙",
        user_code="employee-7",
    )

    soul = files["SOUL.md"]
    assert "姓名：示例用户乙" in soul
    assert "工号：employee-7" in soul
    assert PLATFORM_IDENTITY_BEGIN in soul
    assert PLATFORM_FILE_DELIVERY_BEGIN in soul
    assert "USER.md" not in soul


def test_directory_preflight_migrates_legacy_user_identity_blocks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    home = root / "home"
    memories = home / "memories"
    memories.mkdir(parents=True)
    (memories / "USER.md").write_text(
        "# User Profile\n\n"
        "<!-- PLATFORM_IDENTITY_BEGIN -->\n旧身份\n"
        "<!-- PLATFORM_IDENTITY_END -->\n\n"
        "## User Preferences\n\n喜欢简洁回答。\n",
        encoding="utf-8",
    )
    spec = SimpleNamespace(
        config_schema=33,
        runtime_root_host_path=str(root),
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        supervisor_data_sub_path="supervisor",
        application_agent_id=None,
        enterprise_knowledge_host_path=None,
        runtime_user_uid=10000,
        runtime_shared_gid=1000,
    )

    asyncio.run(LocalRuntimeDirectoryProvisioner().ensure_directory(spec))

    user = (memories / "USER.md").read_text(encoding="utf-8")
    assert user == "# User Profile\n\n## User Preferences\n\n喜欢简洁回答。\n"
    assert (memories / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Long-term Memory\n"
    )
