from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

from platform_supervisor.configuration import (
    RuntimeConfigController,
    RuntimeConfigError,
    _is_runtime_cache_path,
    config_bundle_digest,
    config_bundle_signature,
)
from platform_supervisor.contracts import ConfigBundleReference


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
            (config_revision, config_digest, reload_skills, reload_mcp)
        )
        if self.fail:
            raise RuntimeError("reload failed")
        return {}


class _Sessions:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    async def invalidate_idle_for_config(self, revision: str) -> None:
        self.invalidated.append(revision)


def _bundle(
    *,
    files: dict[str, str],
    secret: str,
) -> ConfigBundleReference:
    unsigned = ConfigBundleReference(
        runtime_id="runtime-1",
        binding_id="17",
        config_revision="generation-2",
        config_digest=config_bundle_digest(files),
        download_url="/internal/v1/config-bundles/application/59/2",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
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


def _controller(
    tmp_path: Path,
    *,
    adapter: _Adapter,
    client: httpx.AsyncClient,
) -> RuntimeConfigController:
    return RuntimeConfigController(
        home=tmp_path / "home",
        state_path=tmp_path / "state" / "current-config.json",
        initial_revision="generation-1",
        adapter=adapter,
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        client=client,
    )


def _http_client(
    *,
    files: dict[str, str],
    bundle: ConfigBundleReference,
    secret: str,
) -> httpx.AsyncClient:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": bundle.config_revision,
                "config_digest": bundle.config_digest,
                "files": files,
            },
            headers={"authorization": f"Bearer {secret}"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _write_generation_one(home: Path, state_path: Path) -> None:
    home.mkdir(parents=True)
    files = {
        "config.yaml": "model: old\n",
        "SOUL.md": "# Old\n",
        "skills/application/private-1903/SKILL.md": "# OKR\n",
    }
    for relative, content in files.items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-1",
                "config_digest": config_bundle_digest(files),
                "managed_files": [
                    *files,
                    "skills/application/private-1903/.pytest_cache/README.md",
                    "skills/application/private-1903/scripts/__pycache__/helper.pyc",
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_runtime_caches(home: Path) -> tuple[Path, Path]:
    pytest_cache = (
        home
        / "skills"
        / "application"
        / "private-1903"
        / ".pytest_cache"
        / "README.md"
    )
    pytest_cache.parent.mkdir(parents=True, exist_ok=True)
    pytest_cache.write_text("# Hermes runtime cache\n", encoding="utf-8")

    compiled = (
        home
        / "skills"
        / "application"
        / "private-1903"
        / "scripts"
        / "__pycache__"
        / "helper.pyc"
    )
    compiled.parent.mkdir(parents=True, exist_ok=True)
    compiled.write_bytes(b"\x00compiled")
    return pytest_cache, compiled


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("skills/application/private/cache/value.json", True),
        ("skills/application/private/__pycache__/helper.pyc", True),
        ("skills/application/private/.pytest_cache/README.md", True),
        ("skills/application/private/.mypy_cache/state.json", True),
        ("skills/application/private/.ruff_cache/state.json", True),
        ("skills/application/private/compiled.pyc", True),
        ("skills/application/private/SKILL.md", False),
        ("skills/application/private/scripts/helper.py", False),
        ("platform/application-agent.json", False),
    ],
)
def test_runtime_cache_paths_are_classified_by_contract(
    relative: str,
    expected: bool,
) -> None:
    assert _is_runtime_cache_path(relative) is expected


@pytest.mark.asyncio
async def test_unreadable_runtime_caches_do_not_block_skill_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state_path = tmp_path / "state" / "current-config.json"
    _write_generation_one(home, state_path)
    pytest_cache, compiled = _write_runtime_caches(home)
    original_read_text = Path.read_text

    def deny_runtime_cache_read(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        if path in {pytest_cache, compiled}:
            raise PermissionError("runtime cache is owned by Hermes")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_runtime_cache_read)
    files = {
        "config.yaml": "model: new\n",
        "SOUL.md": "# New\n",
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)
    client = _http_client(files=files, bundle=bundle, secret=secret)
    adapter = _Adapter()
    controller = _controller(tmp_path, adapter=adapter, client=client)

    await controller.start()
    await controller.apply(bundle)

    assert adapter.calls == [(bundle.config_revision, bundle.config_digest, True, False)]
    assert not (home / "skills/application/private-1903/SKILL.md").exists()
    assert pytest_cache.is_file()
    assert compiled.is_file()
    assert "skills/application/private-1903/.pytest_cache/README.md" not in controller._managed_files
    assert "skills/application/private-1903/scripts/__pycache__/helper.pyc" not in controller._managed_files
    await client.aclose()


@pytest.mark.asyncio
async def test_unreadable_real_skill_config_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state_path = tmp_path / "state" / "current-config.json"
    _write_generation_one(home, state_path)
    skill = home / "skills/application/private-1903/SKILL.md"
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
    files = {"config.yaml": "model: new\n", "SOUL.md": "# New\n"}
    bundle = _bundle(files=files, secret="s" * 32)
    client = _http_client(
        files=files,
        bundle=bundle,
        secret="s" * 32,
    )
    controller = _controller(tmp_path, adapter=_Adapter(), client=client)

    await controller.start()
    with pytest.raises(RuntimeConfigError, match="CONFIG_BUNDLE_FILE_INVALID"):
        await controller.apply(bundle)
    await client.aclose()


@pytest.mark.asyncio
async def test_runtime_caches_are_not_touched_during_rollback(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state_path = tmp_path / "state" / "current-config.json"
    _write_generation_one(home, state_path)
    pytest_cache, compiled = _write_runtime_caches(home)
    files = {
        "config.yaml": "model: new\n",
        "SOUL.md": "# New\n",
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)
    client = _http_client(files=files, bundle=bundle, secret=secret)
    adapter = _Adapter(fail=True)
    controller = _controller(tmp_path, adapter=adapter, client=client)

    await controller.start()
    with pytest.raises(RuntimeError, match="reload failed"):
        await controller.apply(bundle)

    assert (home / "skills/application/private-1903/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# OKR\n"
    assert pytest_cache.is_file()
    assert compiled.is_file()
    assert not controller._journal_path.exists()
    await client.aclose()
