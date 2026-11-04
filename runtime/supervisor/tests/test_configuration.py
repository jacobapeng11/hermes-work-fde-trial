from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import httpx
import pytest

from platform_supervisor.configuration import (
    RuntimeConfigController,
    RuntimeConfigError,
    config_bundle_digest,
    config_bundle_signature,
    digest_managed_runtime_config,
)
from platform_supervisor.contracts import ConfigBundleReference


def test_managed_config_write_uses_workspace_parent_group(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    os.chown(home, os.getuid(), 1234)
    path = home / "config.yaml"

    RuntimeConfigController._write_text_atomic(path, "model: test\n")

    assert path.stat().st_gid == 1234
    assert path.stat().st_mode & 0o777 == 0o660


class _Adapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, bool, bool]] = []

    async def apply_config(
        self,
        *,
        config_revision: str,
        config_digest: str,
        reload_skills: bool,
        reload_mcp: bool,
    ) -> dict[str, object]:
        self.calls.append(
            (
                config_revision,
                config_digest,
                reload_skills,
                reload_mcp,
            )
        )
        if self.fail:
            raise RuntimeError("reload failed")
        return {}


class _Sessions:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    async def invalidate_idle_for_config(self, revision: str) -> None:
        self.invalidated.append(revision)


def _write_initial(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: old\n", encoding="utf-8")
    (home / "SOUL.md").write_text("# Old\n", encoding="utf-8")


def test_reload_plan_keeps_settings_lightweight_and_detects_capabilities(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state/current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
    )

    assert controller._reload_plan({
        "config.yaml": "model: old\n",
        "SOUL.md": "# New prompt\n",
    }) == (False, False)
    assert controller._reload_plan({
        "config.yaml": "model: old\n",
        "SOUL.md": "# Old\n",
        "skills/ticket/SKILL.md": "# Ticket\n",
    }) == (True, False)
    assert controller._reload_plan({
        "config.yaml": "mcp_servers:\n  ticket: {}\n",
        "SOUL.md": "# Old\n",
    }) == (False, True)


def test_runtime_skill_cache_is_not_config_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    cache = home / "skills/application/private-5/cache/actuals_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{\"runtime\": true}\n", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_cache_read(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == cache:
            raise PermissionError("cache is owned by Hermes")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_cache_read)

    assert digest_managed_runtime_config(home) == config_bundle_digest(
        {
            "config.yaml": "model: old\n",
            "SOUL.md": "# Old\n",
        }
    )


def test_unreadable_real_skill_config_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    skill = home / "skills/application/private-5/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Skill\n", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_real_config_read(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == skill:
            raise PermissionError("real config is unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_real_config_read)

    with pytest.raises(RuntimeConfigError, match="CONFIG_BUNDLE_FILE_INVALID"):
        digest_managed_runtime_config(home)


@pytest.mark.asyncio
async def test_unreadable_skill_cache_does_not_block_bundle_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    old_skill = home / "skills/application/private-5/SKILL.md"
    old_skill.parent.mkdir(parents=True)
    old_skill.write_text("# Old skill\n", encoding="utf-8")
    cache = home / "skills/application/private-5/cache/actuals_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{\"runtime\": true}\n", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_cache_read(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == cache:
            raise PermissionError("cache is owned by Hermes")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_cache_read)
    files = {
        "config.yaml": "model: new\n",
        "SOUL.md": "# New\n",
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-2",
                "config_digest": bundle.config_digest,
                "files": files,
            },
        )

    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "config_revision": "generation-1",
                "config_digest": config_bundle_digest(
                    {
                        "config.yaml": "model: old\n",
                        "SOUL.md": "# Old\n",
                    }
                ),
                "managed_files": [
                    "config.yaml",
                    "SOUL.md",
                    "skills/application/private-5/SKILL.md",
                    "skills/application/private-5/cache/actuals_cache.json",
                ],
            }
        ),
        encoding="utf-8",
    )
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    adapter = _Adapter()
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-1",
        adapter=adapter,
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        client=http_client,
    )

    await controller.start()
    assert "skills/application/private-5/cache/actuals_cache.json" not in controller._managed_files
    await controller.apply(bundle)

    assert not old_skill.exists()
    assert cache.exists()
    assert "skills/application/private-5/cache/actuals_cache.json" not in controller._managed_files
    assert adapter.calls == [("generation-2", bundle.config_digest, True, False)]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_unreadable_skill_cache_is_not_touched_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    old_skill = home / "skills/application/private-5/SKILL.md"
    old_skill.parent.mkdir(parents=True)
    old_skill.write_text("# Old skill\n", encoding="utf-8")
    cache = home / "skills/application/private-5/cache/actuals_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{\"runtime\": true}\n", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_cache_read(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path == cache:
            raise PermissionError("cache is owned by Hermes")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_cache_read)
    files = {
        "config.yaml": "model: new\n",
        "SOUL.md": "# New\n",
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-2",
                "config_digest": bundle.config_digest,
                "files": files,
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state/current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(fail=True),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        client=http_client,
    )

    await controller.start()
    with pytest.raises(RuntimeError, match="reload failed"):
        await controller.apply(bundle)

    assert (home / "config.yaml").read_text() == "model: old\n"
    assert old_skill.read_text() == "# Old skill\n"
    assert cache.exists()
    assert not (tmp_path / "state/config-apply-journal.json").exists()
    await http_client.aclose()


def _bundle(
    *,
    files: dict[str, str],
    secret: str,
    generation: int = 2,
) -> ConfigBundleReference:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    unsigned = ConfigBundleReference(
        runtime_id="runtime-1",
        binding_id="17",
        config_revision=f"generation-{generation}",
        config_digest=config_bundle_digest(files),
        download_url=(
            f"/internal/v1/config-bundles/personal/1/{generation}"
        ),
        expires_at=expires_at,
        signature="0" * 64,
    )
    return unsigned.model_copy(
        update={
            "signature": config_bundle_signature(
                secret=secret,
                bundle=unsigned,
            )
        }
    )


@pytest.mark.asyncio
async def test_signed_bundle_is_downloaded_published_and_reloaded(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    files = {
        "config.yaml": "model: new\n",
        "SOUL.md": "# New\n",
        "platform/revision.json": '{"revision":2}\n',
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-2",
                "config_digest": bundle.config_digest,
                "files": files,
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    adapter = _Adapter()
    sessions = _Sessions()
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=adapter,
        sessions=sessions,  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        client=http_client,
    )
    await controller.start()

    applied = await controller.apply(bundle)
    repeated = await controller.apply(bundle)

    assert applied.config_revision == "generation-2"
    assert repeated == applied
    assert (home / "config.yaml").read_text() == "model: new\n"
    assert (home / "platform/revision.json").is_file()
    assert sessions.invalidated == ["generation-2"]
    assert adapter.calls == [
        ("generation-2", bundle.config_digest, False, False)
    ]
    assert not (tmp_path / "state/config-apply-journal.json").exists()

    stale = _bundle(
        files={
            "config.yaml": "model: old\n",
            "SOUL.md": "# Old\n",
        },
        secret=secret,
        generation=1,
    )
    with pytest.raises(RuntimeConfigError, match="CONFIG_REVISION_STALE"):
        await controller.apply(stale)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_reload_failure_restores_previous_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    files = {
        "config.yaml": "model: new\n",
        "SOUL.md": "# New\n",
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "runtime_id": "runtime-1",
                    "binding_id": "17",
                    "config_revision": "generation-2",
                    "config_digest": bundle.config_digest,
                    "files": files,
                },
            )
        )
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(fail=True),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        client=http_client,
    )
    await controller.start()

    with pytest.raises(RuntimeError, match="reload failed"):
        await controller.apply(bundle)

    assert controller.current_revision == "generation-1"
    assert (home / "config.yaml").read_text() == "model: old\n"
    assert (home / "SOUL.md").read_text() == "# Old\n"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_invalid_bundle_signature_is_rejected_before_download(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    files = {"config.yaml": "model: new\n", "SOUL.md": "# New\n"}
    bundle = _bundle(files=files, secret="s" * 32).model_copy(
        update={"signature": "f" * 64}
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
    )
    await controller.start()

    with pytest.raises(
        RuntimeConfigError,
        match="CONFIG_BUNDLE_SIGNATURE_INVALID",
    ):
        await controller.apply(bundle)

    await controller.close()


@pytest.mark.asyncio
async def test_empty_pvc_bootstraps_bundle_before_dashboard_start(
    tmp_path: Path,
) -> None:
    files = {
        "config.yaml": "model: bootstrap\n",
        "SOUL.md": "# Bootstrap\n",
        "platform/user-identity.json": (
            '{"name":"测试用户","schema_version":1,'
            '"user_code":"employee-7"}\n'
        ),
    }
    digest = config_bundle_digest(files)
    secret = "s" * 32

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-1",
                "config_digest": digest,
                "files": files,
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    home = tmp_path / "empty-home"
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        initial_download_url=(
            "/internal/v1/config-bundles/personal/1/1"
        ),
        initial_digest=digest,
        client=http_client,
    )

    await controller.start()

    assert (home / "config.yaml").read_text() == "model: bootstrap\n"
    assert not (home / "memories" / "USER.md").exists()
    assert not (home / "memories" / "MEMORY.md").exists()
    assert controller.current_revision == "generation-1"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_existing_state_refreshes_stale_config_from_initial_bundle(
    tmp_path: Path,
) -> None:
    files = {
        "config.yaml": "plugins:\n  enabled:\n    - clarify-lifecycle\n",
        "SOUL.md": "# Current\n",
    }
    digest = config_bundle_digest(files)
    secret = "s" * 32

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-1",
                "config_digest": digest,
                "files": files,
            },
        )

    home = tmp_path / "home"
    state_path = tmp_path / "state" / "current-config.json"
    first_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/personal/1/1",
        initial_digest=digest,
        client=first_client,
    )
    await first.start()
    await first_client.aclose()

    # Simulates an old config file surviving while its state metadata was
    # already updated by an earlier Pod/image transition.
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    second_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    second = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/personal/1/1",
        initial_digest=digest,
        client=second_client,
    )
    await second.start()

    assert (home / "config.yaml").read_text() == files["config.yaml"]
    assert second.current_digest == digest
    await second_client.aclose()


@pytest.mark.asyncio
async def test_older_initial_bundle_never_downgrades_newer_persisted_state(
    tmp_path: Path,
) -> None:
    old_files = {
        "config.yaml": "model: generation-13\n",
        "SOUL.md": "# Generation 13\n",
    }
    new_files = {
        "config.yaml": "model: generation-14\n",
        "SOUL.md": "# Generation 14\n",
    }
    secret = "s" * 32
    old_digest = config_bundle_digest(old_files)
    new_digest = config_bundle_digest(new_files)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Hermes-Config-Revision"] == "generation-13"
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-13",
                "config_digest": old_digest,
                "files": old_files,
            },
        )

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(new_files["config.yaml"], encoding="utf-8")
    (home / "SOUL.md").write_text(new_files["SOUL.md"], encoding="utf-8")
    state_path = tmp_path / "state" / "current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "config_revision": "generation-14",
                "config_digest": new_digest,
                "managed_files": ["config.yaml", "SOUL.md"],
            }
        ),
        encoding="utf-8",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-13",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/personal/1/13",
        initial_digest=old_digest,
        client=client,
    )

    await controller.start()

    assert controller.current_revision == "generation-14"
    assert controller.current_digest == new_digest
    assert (home / "config.yaml").read_text() == new_files["config.yaml"]
    assert (home / "SOUL.md").read_text() == new_files["SOUL.md"]
    await client.aclose()


@pytest.mark.asyncio
async def test_legacy_pvc_bootstraps_authoritative_bundle_without_reading_binary_assets(
    tmp_path: Path,
) -> None:
    home = tmp_path / "legacy-home"
    _write_initial(home)
    binary_asset = home / "skills" / "legacy" / "asset.bin"
    binary_asset.parent.mkdir(parents=True)
    binary_asset.write_bytes(b"\xd2\x00\xfflegacy-user-data")
    files = {
        "config.yaml": "model: supervisor\n",
        "SOUL.md": "# Supervisor\n",
        "platform/revision.json": '{"revision":1}\n',
    }
    digest = config_bundle_digest(files)
    secret = "s" * 32

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-1",
                "config_digest": digest,
                "files": files,
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        initial_download_url=(
            "/internal/v1/config-bundles/personal/1/1"
        ),
        initial_digest=digest,
        client=http_client,
    )

    await controller.start()

    assert (home / "config.yaml").read_text() == "model: supervisor\n"
    assert (home / "SOUL.md").read_text() == "# Supervisor\n"
    assert binary_asset.read_bytes() == b"\xd2\x00\xfflegacy-user-data"
    assert controller.current_digest == digest
    await http_client.aclose()


@pytest.mark.asyncio
async def test_migrated_local_bundle_excludes_binary_skill_assets(
    tmp_path: Path,
) -> None:
    home = tmp_path / "migrated-home"
    _write_initial(home)
    binary_asset = home / "skills" / "research" / "template.pdf"
    binary_asset.parent.mkdir(parents=True)
    binary_asset.write_bytes(b"%PDF-1.7\n\xbf\x00\xff")
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
    )

    await controller.start(preserve_migrated_local=True)

    assert binary_asset.read_bytes() == b"%PDF-1.7\n\xbf\x00\xff"
    state = json.loads(
        (tmp_path / "state" / "current-config.json").read_text()
    )
    assert "skills/research/template.pdf" not in state["managed_files"]


@pytest.mark.asyncio
async def test_migrated_local_bundle_manages_only_signed_bundle_paths(
    tmp_path: Path,
) -> None:
    home = tmp_path / "migrated-home"
    _write_initial(home)
    builtin_skill = home / "skills/autonomous-ai-agents/claude-code/SKILL.md"
    builtin_skill.parent.mkdir(parents=True)
    builtin_skill.write_text("# Builtin\n", encoding="utf-8")
    old_application_skill = home / "skills/application/old/SKILL.md"
    old_application_skill.parent.mkdir(parents=True)
    old_application_skill.write_text("# Old application skill\n", encoding="utf-8")
    files = {
        "config.yaml": "model: qwen\n",
        "SOUL.md": "# Application\n",
        "platform/application-agent.json": '{"generation":14}\n',
    }
    digest = config_bundle_digest(files)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "runtime_id": "runtime-1",
                    "binding_id": "17",
                    "config_revision": "generation-14",
                    "config_digest": digest,
                    "files": files,
                },
            )
        )
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=tmp_path / "state/current-config.json",
        initial_revision="generation-14",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/application/25/14",
        initial_digest=digest,
        client=client,
    )

    await controller.start(preserve_migrated_local=True)

    state = json.loads(
        (tmp_path / "state/current-config.json").read_text()
    )
    assert builtin_skill.read_text(encoding="utf-8") == "# Builtin\n"
    assert state["managed_files"] == sorted(
        {*files, "skills/application/old/SKILL.md"}
    )
    assert (home / "config.yaml").read_text() == "model: old\n"
    await client.aclose()


@pytest.mark.asyncio
async def test_personal_steady_state_drops_overbroad_legacy_skill_ownership(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    legacy_skill = home / "skills/autonomous-ai-agents/claude-code/SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_text("# Legacy builtin\n", encoding="utf-8")
    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-13",
                "config_digest": "a" * 64,
                "managed_files": [
                    "SOUL.md",
                    "config.yaml",
                    "skills/autonomous-ai-agents/claude-code/SKILL.md",
                ],
            }
        ),
        encoding="utf-8",
    )
    files = {
        "config.yaml": "model: qwen\n",
        "SOUL.md": "# Personal\n",
    }
    digest = config_bundle_digest(files)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "runtime_id": "runtime-1",
                    "binding_id": "17",
                    "config_revision": "generation-14",
                    "config_digest": digest,
                    "files": files,
                },
            )
        )
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-14",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/personal/1/14",
        initial_digest=digest,
        client=client,
    )

    await controller.start()

    state = json.loads(state_path.read_text())
    assert legacy_skill.read_text(encoding="utf-8") == "# Legacy builtin\n"
    assert state["managed_files"] == sorted(files)
    assert (home / "config.yaml").read_text(encoding="utf-8") == "model: qwen\n"
    await client.aclose()


def test_config_recovery_skips_equal_backup_without_replacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    state_path = tmp_path / "state/current-config.json"
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
    )
    target = home / "skills/legacy/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Unchanged\n", encoding="utf-8")
    backup = controller._backup_root / "skills/legacy/SKILL.md"
    backup.parent.mkdir(parents=True)
    backup.write_text("# Unchanged\n", encoding="utf-8")
    controller._journal_path.parent.mkdir(parents=True)
    controller._journal_path.write_text(
        json.dumps(
            {
                "old_files": ["skills/legacy/SKILL.md"],
                "new_files": ["skills/legacy/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )

    def must_not_replace(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("equal backup must not be replaced")

    monkeypatch.setattr(os, "replace", must_not_replace)

    controller._recover_incomplete_apply()

    assert target.read_text(encoding="utf-8") == "# Unchanged\n"
    assert not controller._journal_path.exists()
    assert not controller._backup_root.exists()


@pytest.mark.asyncio
async def test_steady_state_repairs_overbroad_migrated_managed_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    builtin_skill = home / "skills/autonomous-ai-agents/claude-code/SKILL.md"
    builtin_skill.parent.mkdir(parents=True)
    builtin_skill.write_text("# Builtin\n", encoding="utf-8")
    old_application_skill = home / "skills/application/old/SKILL.md"
    old_application_skill.parent.mkdir(parents=True)
    old_application_skill.write_text("# Old application skill\n", encoding="utf-8")
    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-13",
                "config_digest": "a" * 64,
                "managed_files": [
                    "SOUL.md",
                    "config.yaml",
                    "skills/application/old/SKILL.md",
                    "skills/autonomous-ai-agents/claude-code/SKILL.md",
                ],
            }
        ),
        encoding="utf-8",
    )
    files = {
        "config.yaml": "model: qwen\n",
        "SOUL.md": "# Application\n",
        "platform/application-agent.json": '{"generation":14}\n',
    }
    digest = config_bundle_digest(files)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "runtime_id": "runtime-1",
                    "binding_id": "17",
                    "config_revision": "generation-14",
                    "config_digest": digest,
                    "files": files,
                },
            )
        )
    )
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-14",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/application/25/14",
        initial_digest=digest,
        client=client,
    )

    await controller.start()

    state = json.loads(state_path.read_text())
    assert builtin_skill.read_text(encoding="utf-8") == "# Builtin\n"
    assert not old_application_skill.exists()
    assert state["managed_files"] == sorted(files)
    assert (home / "config.yaml").read_text() == "model: qwen\n"
    await client.aclose()


@pytest.mark.parametrize(
    "relative",
    ["skills/.bundled_manifest", "skills/.usage.json"],
)
def test_runtime_generated_skill_state_is_not_config_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    home = tmp_path / "home"
    _write_initial(home)
    generated = home / relative
    generated.parent.mkdir(parents=True)
    generated.write_text("{}", encoding="utf-8")
    original = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs) -> str:
        if path == generated:
            raise PermissionError("child-owned generated skill state")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert len(digest_managed_runtime_config(home)) == 64
