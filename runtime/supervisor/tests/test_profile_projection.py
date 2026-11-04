from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
from typing import Any
import zipfile

import pytest

from platform_supervisor.asset_contracts import (
    ProfileCapabilityTarget,
    ProfileProjectionApplyRequest,
)
from platform_supervisor.execution import PrepareIdentity
from platform_supervisor.profile_projection import ProfileProjectionController
from platform_supervisor.profile_projection_state import ProfileProjectionStateStore


def _skill_archive(files: dict[str, bytes]) -> tuple[bytes, str]:
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

    async def download(self, **_kwargs) -> bytes:
        self.calls += 1
        return self.content


class FailingOnceDownloader(FakeDownloader):
    async def download(self, **kwargs) -> bytes:
        if self.calls == 0:
            self.calls += 1
            from platform_supervisor.profile_projection import ProfileProjectionError

            raise ProfileProjectionError("PROFILE_ARTIFACT_DOWNLOAD_FAILED")
        return await super().download(**kwargs)


class FakeDashboard:
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
                "skills": {"external_dirs": ["/opt/hermes/platform-default-skills"]},
                "plugins": {"enabled": ["kanban-lifecycle"]},
                "platform": {},
            }
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
            {
                "model": "1",
                "providers": {"platform": {"default_model": "1"}},
                "skills": {"external_dirs": ["/opt/hermes/platform-default-skills"]},
                "plugins": {"enabled": ["kanban-lifecycle"]},
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
        self.writes += 1
        self.documents[profile_name] = document


class HermesNormalizingDashboard(FakeDashboard):
    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None:
        self.writes += 1
        self.documents[profile_name] = _without_none_mapping_values(document)


class FreshProfileWithoutModelDashboard(FakeDashboard):
    """Mirror Hermes ``clone_all=False`` bootstrap for a new named Profile."""

    def __init__(self) -> None:
        super().__init__()
        self.documents["default"]["model"] = {
            "provider": "custom:platform",
            "default": "1",
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
                "plugins": {"enabled": ["kanban-lifecycle"]},
                "platform": {},
            },
        )


def _without_none_mapping_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_none_mapping_values(child)
            for key, child in value.items()
            if child is not None
        }
    if isinstance(value, list):
        return [_without_none_mapping_values(child) for child in value]
    return value


def _request(sha256: str, *, revision: int = 1, expected: int = 0):
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
            "description": "Alpha",
            "soul_content": "Alpha soul",
            "plugin_ids": ["personal-knowledge"],
            "platform_toolsets": {"cli": ["hermes-cli", "mcp-crm"]},
            "mcp_servers": {
                "crm": {
                    "transport": "streamable_http",
                    "url": "https://crm.invalid/mcp",
                    "enabled": True,
                }
            },
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


@pytest.mark.asyncio
async def test_complete_projection_publishes_immutable_release_and_pointer(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n", "refs/a.txt": b"A"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FakeDashboard()
    downloader = FakeDownloader(content)
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=downloader,
    )
    request = _request(digest)

    result = await controller.apply(request)
    assert result.status == "applied"
    release = tmp_path / "home/platform/profile-assets/skills/releases/11/current"
    assert (release / "SKILL.md").read_text() == "# Alpha\n"
    assert (
        tmp_path / "home/platform/profile-assets/profiles/173/current.json"
    ).is_file()
    projected = dashboard.documents["expert-173"]
    assert str(release) in projected["skills"]["external_dirs"]
    assert projected["plugins"]["enabled"] == [
        "kanban-lifecycle",
        "clarify-lifecycle",
        "platform-main-conversation-boundary-v1",
        "personal-knowledge",
    ]
    assert projected["mcp_servers"] == request.profile_config["mcp_servers"]
    assert projected["mcp_discovery_timeout"] == 30
    assert projected["platform"]["asset_projection"]["desired_revision"] == 1

    repeated = await controller.apply(request)
    assert repeated.status == "applied"
    assert downloader.calls == 1
    assert dashboard.writes == 1

    dashboard.documents["expert-173"].pop("platform_toolsets", None)
    healed = await controller.apply(request)
    assert healed.status == "applied"
    assert downloader.calls == 1
    assert dashboard.writes == 2
    assert dashboard.documents["expert-173"]["platform_toolsets"]["cli"][0] == "hermes-cli"
    await state.close()


@pytest.mark.asyncio
async def test_new_named_profile_inherits_default_model_route(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FreshProfileWithoutModelDashboard()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(content),
    )

    result = await controller.apply(_request(digest))

    assert result.status == "applied"
    assert dashboard.documents["expert-173"]["model"] == dashboard.documents["default"]["model"]
    assert dashboard.documents["expert-173"]["providers"]["platform"] == {
        "default_model": "1"
    }
    await state.close()


@pytest.mark.asyncio
async def test_projection_canonicalizes_optional_nulls_before_hermes_readback(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = HermesNormalizingDashboard()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(content),
    )
    request = _request(digest)
    request.profile_config.update(
        {
            "avatar_url": None,
            "system_prompt": None,
            "optional": {"missing": None, "disabled": False, "count": 0},
        }
    )

    result = await controller.apply(request)

    assert result.status == "applied"
    definition = dashboard.documents["expert-173"]["platform"][
        "asset_projection"
    ]["definition"]
    assert "avatar_url" not in definition
    assert "system_prompt" not in definition
    assert definition["optional"] == {"disabled": False, "count": 0}
    await state.close()


@pytest.mark.asyncio
async def test_projection_does_not_add_empty_default_skill_config(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Unused\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FakeDashboard()
    dashboard.documents["expert-173"] = {}
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(content),
    )
    request = _request(digest)
    request.artifacts = []

    result = await controller.apply(request)

    assert result.status == "applied"
    assert "skills" not in dashboard.documents["expert-173"]
    await state.close()


@pytest.mark.asyncio
async def test_default_profile_projection_keeps_registry_owned_full_server_map(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Default\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FakeDashboard()
    dashboard.documents["default"]["mcp_servers"] = {
        "crm": {"url": "https://crm.invalid/mcp"},
        "ticket": {"url": "https://ticket.invalid/mcp"},
    }
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=FakeDownloader(content),
    )
    request = _request(digest).model_copy(
        update={"hermes_profile_name": "default"}
    )

    result = await controller.apply(request)

    assert result.status == "applied"
    assert dashboard.documents["default"]["mcp_servers"] == {
        "crm": {"url": "https://crm.invalid/mcp"},
        "ticket": {"url": "https://ticket.invalid/mcp"},
    }
    assert dashboard.documents["default"]["platform_toolsets"] == {
        "cli": ["hermes-cli", "mcp-crm"]
    }
    await state.close()


@pytest.mark.asyncio
async def test_empty_default_profile_without_projection_state_is_executable(
    tmp_path: Path,
) -> None:
    """Fresh runtimes may have an empty default Profile before first projection."""
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-237",
        binding_id="227",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=FakeDownloader(b""),
    )
    identity = PrepareIdentity(
        runtime_id="rt-237",
        session_id="rs-237-test",
        profile_id="default",
        model_id="1",
        config_revision="generation-1",
        admission_generation=1,
        user_id=990237,
        org_id=1,
        runtime_agent_profile_id=753,
        profile_desired_revision=0,
        profile_applied_revision=0,
        profile_digest="sha256:" + "0" * 64,
        skill_digest="sha256:" + "0" * 64,
        mcp_digest="sha256:" + "0" * 64,
        knowledge_digest="sha256:" + "0" * 64,
    )

    resolved = await controller.resolve_identity(identity, allow_pending=True)

    assert resolved == identity
    await state.close()


@pytest.mark.asyncio
async def test_digest_failure_keeps_pending_target_failed_without_pointer(
    tmp_path: Path,
) -> None:
    content, _digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=FakeDownloader(content),
    )
    result = await controller.apply(_request("f" * 64))
    assert result.status == "failed"
    assert result.applied_revision == 0
    assert result.error_code == "PROFILE_ARTIFACT_DIGEST_MISMATCH"
    assert not (
        tmp_path / "home/platform/profile-assets/profiles/173/current.json"
    ).exists()
    await state.close()


@pytest.mark.asyncio
async def test_optional_skill_failure_applies_degraded_and_recovers_generation(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    downloader = FailingOnceDownloader(content)
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=downloader,
    )
    request = _request(digest)
    request.capabilities = [
        ProfileCapabilityTarget(
            capability_type="skill",
            capability_id=11,
            target_version_id=12,
            desired_digest=f"sha256:{digest}",
            required=False,
        )
    ]

    degraded = await controller.apply(request)
    assert degraded.status == "applied_degraded"
    assert degraded.applied_revision == 1
    assert degraded.effective_generation == 1
    assert degraded.capabilities[0].status == "unavailable"

    recovered = await controller.apply(request)
    assert recovered.status == "applied"
    assert recovered.applied_revision == 1
    assert recovered.effective_generation == 2
    assert recovered.capabilities[0].status == "applied"
    await state.close()


@pytest.mark.asyncio
async def test_failed_next_revision_preserves_old_effective_manifest(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=FakeDownloader(content),
    )
    first = _request(digest)
    applied = await controller.apply(first)
    assert applied.status == "applied"

    second = _request("f" * 64, revision=2, expected=1)
    failed = await controller.apply(second)
    assert failed.status == "failed"
    assert failed.desired_revision == 2
    assert failed.applied_revision == 1
    assert failed.effective_generation == 1
    assert failed.digests == first.digests
    await state.close()


@pytest.mark.asyncio
async def test_next_revision_reuses_release_and_removes_old_managed_plugins(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    dashboard = FakeDashboard()
    downloader = FakeDownloader(content)
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=dashboard,
        downloader=downloader,
    )
    first = _request(digest)
    assert (await controller.apply(first)).status == "applied"
    second = _request(digest, revision=2, expected=1)
    second.profile_config["plugin_ids"] = []
    assert (await controller.apply(second)).status == "applied"
    assert dashboard.documents["expert-173"]["plugins"]["enabled"] == [
        "kanban-lifecycle",
        "clarify-lifecycle",
        "platform-main-conversation-boundary-v1",
    ]
    assert downloader.calls == 1
    await state.close()


@pytest.mark.asyncio
async def test_execution_identity_waits_for_applied_projection_then_uses_it(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=FakeDownloader(content),
    )
    request = _request(digest)
    await state.begin(request)
    stale = PrepareIdentity(
        runtime_id="rt-119",
        session_id="session-alpha",
        profile_id="expert-173",
        runtime_agent_profile_id=173,
        model_id="7",
        config_revision="runtime-1",
        admission_generation=11,
    )

    assert await controller.resolve_identity(stale, allow_pending=True) is None

    assert (await controller.apply(request)).status == "applied"
    resolved = await controller.resolve_identity(stale, allow_pending=True)
    assert resolved is not None
    assert resolved.profile_desired_revision == 1
    assert resolved.profile_applied_revision == 1
    assert resolved.profile_digest == request.digests.profile
    assert resolved.skill_digest == request.digests.skill
    assert resolved.mcp_digest == request.digests.mcp
    assert resolved.knowledge_digest == request.digests.knowledge
    await state.close()


@pytest.mark.asyncio
async def test_applied_profile_state_never_leaks_to_another_profile(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=FakeDownloader(content),
    )
    assert (await controller.apply(_request(digest))).status == "applied"

    beta = PrepareIdentity(
        runtime_id="rt-119",
        session_id="session-beta",
        profile_id="expert-174",
        runtime_agent_profile_id=174,
        model_id="7",
        config_revision="runtime-1",
        admission_generation=11,
    )
    assert await controller.resolve_identity(beta, allow_pending=True) is None
    await state.close()


@pytest.mark.asyncio
async def test_grant_ahead_of_local_projection_cannot_start_old_assets(
    tmp_path: Path,
) -> None:
    content, digest = _skill_archive({"SKILL.md": b"# Alpha\n"})
    state = ProfileProjectionStateStore(tmp_path / "state.db")
    await state.start()
    controller = ProfileProjectionController(
        runtime_id="rt-119",
        binding_id="119",
        home=tmp_path / "home",
        state=state,
        dashboard=FakeDashboard(),
        downloader=FakeDownloader(content),
    )
    assert (await controller.apply(_request(digest))).status == "applied"
    future = PrepareIdentity(
        runtime_id="rt-119",
        session_id="session-alpha",
        profile_id="expert-173",
        runtime_agent_profile_id=173,
        model_id="7",
        config_revision="runtime-1",
        admission_generation=11,
        profile_desired_revision=2,
        profile_applied_revision=1,
    )

    assert await controller.resolve_identity(future, allow_pending=True) is None
    await state.close()
