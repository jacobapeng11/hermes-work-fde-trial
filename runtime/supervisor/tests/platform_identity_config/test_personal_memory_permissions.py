from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from platform_supervisor.configuration import (
    RuntimeConfigController,
    config_bundle_digest,
    digest_managed_runtime_config,
)


class _Adapter:
    async def apply_config(
        self,
        **kwargs: object,
    ) -> dict[str, object]:
        return {}


class _Sessions:
    async def invalidate_idle_for_config(self, revision: str) -> None:
        del revision


def _state(path: Path, *, revision: str, digest: str, managed: list[str]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": revision,
                "config_digest": digest,
                "managed_files": managed,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_personal_config_refresh_never_reads_private_user_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_files = {"config.yaml": "model: old\n", "SOUL.md": "# Old\n"}
    new_files = {
        "config.yaml": "model: new\n",
        "SOUL.md": (
            "# Agent\n\n"
            "<!-- PLATFORM_IDENTITY_BEGIN -->\n"
            "姓名：测试用户\n工号：employee-7\n"
            "<!-- PLATFORM_IDENTITY_END -->\n"
        ),
        "platform/user-identity.json": (
            '{"name":"测试用户","schema_version":1,"user_code":"employee-7"}\n'
        ),
    }
    digest = config_bundle_digest(new_files)
    home = tmp_path / "home"
    memories = home / "memories"
    memories.mkdir(parents=True)
    (home / "config.yaml").write_text(old_files["config.yaml"], encoding="utf-8")
    (home / "SOUL.md").write_text(old_files["SOUL.md"], encoding="utf-8")
    user_path = memories / "USER.md"
    user_path.write_bytes(b"Hermes-owned private profile\n")
    user_path.chmod(0o600)
    state_path = tmp_path / "supervisor" / "current-config.json"
    _state(
        state_path,
        revision="generation-1",
        digest=config_bundle_digest(old_files),
        managed=[
            "SOUL.md",
            "config.yaml",
            "memories/USER.md",
            "platform/user-identity.json",
        ],
    )

    original_read_text = Path.read_text

    def deny_user_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == user_path:
            raise PermissionError("USER.md is private to Hermes")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_user_read)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "runtime-1",
                "binding_id": "17",
                "config_revision": "generation-2",
                "config_digest": digest,
                "files": new_files,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-2",
        adapter=_Adapter(),
        sessions=_Sessions(),
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/personal/1/2",
        initial_digest=digest,
        client=client,
    )

    try:
        await controller.start()
    finally:
        await client.aclose()

    assert user_path.read_bytes() == b"Hermes-owned private profile\n"
    assert "PLATFORM_IDENTITY_BEGIN" in (home / "SOUL.md").read_text()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert "memories/USER.md" not in payload["managed_files"]


def test_application_user_memory_stays_config_managed(tmp_path: Path) -> None:
    baseline = {"config.yaml": "model: app\n", "SOUL.md": "# App\n"}
    home = tmp_path / "home"
    (home / "platform").mkdir(parents=True)
    (home / "memories").mkdir()
    (home / "config.yaml").write_text(baseline["config.yaml"], encoding="utf-8")
    (home / "SOUL.md").write_text(baseline["SOUL.md"], encoding="utf-8")
    (home / "platform" / "application-agent.json").write_text("{}\n", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("# Application prompt\n", encoding="utf-8")

    application_digest = digest_managed_runtime_config(home)
    (home / "platform" / "application-agent.json").unlink()

    assert application_digest != digest_managed_runtime_config(home)
    assert digest_managed_runtime_config(home) == config_bundle_digest(baseline)
