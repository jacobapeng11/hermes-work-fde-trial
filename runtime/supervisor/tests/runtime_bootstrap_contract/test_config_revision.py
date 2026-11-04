from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from platform_supervisor.configuration import (
    RuntimeConfigController,
    config_bundle_digest,
)


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def apply_config(self, *, config_revision: str, **_kwargs) -> dict[str, object]:
        self.calls.append(config_revision)
        return {}


class _Sessions:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    async def invalidate_idle_for_config(self, revision: str) -> None:
        self.invalidated.append(revision)


@pytest.mark.asyncio
async def test_bootstrap_revision_replaces_newer_legacy_generation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model: old\n", encoding="utf-8")
    (home / "SOUL.md").write_text("# Old\n", encoding="utf-8")
    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-2",
                "config_digest": "b" * 64,
                "managed_files": ["SOUL.md", "config.yaml"],
            }
        ),
        encoding="utf-8",
    )
    files = {
        "config.yaml": "model: session\nproviders: {}\n",
        "SOUL.md": "# Bootstrap\n",
    }
    digest = config_bundle_digest(files)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "runtime_id": "rt-bootstrap-41",
                "binding_id": "191",
                "config_revision": "bootstrap-generation-3",
                "config_digest": digest,
                "files": files,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="bootstrap-generation-3",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="rt-bootstrap-41",
        binding_id="191",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        initial_download_url="/internal/v1/config-bundles/personal/41/3",
        initial_digest=digest,
        client=client,
    )

    try:
        await controller.start()
    finally:
        await client.aclose()

    assert requests[0].headers["X-Hermes-Config-Revision"] == (
        "bootstrap-generation-3"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["config_revision"] == "bootstrap-generation-3"
    assert (home / "config.yaml").read_text(encoding="utf-8") == files["config.yaml"]
