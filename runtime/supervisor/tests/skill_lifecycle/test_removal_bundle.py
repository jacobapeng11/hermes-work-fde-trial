from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat

import httpx
import pytest

from platform_supervisor.configuration import (
    RuntimeConfigController,
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
        runtime_id="application-agent-runtime-29",
        binding_id="29",
        config_revision="generation-2",
        config_digest=config_bundle_digest(files),
        download_url="/internal/v1/config-bundles/application/29/2",
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
                "runtime_id": bundle.runtime_id,
                "binding_id": bundle.binding_id,
                "config_revision": bundle.config_revision,
                "config_digest": bundle.config_digest,
                "files": files,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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
        runtime_id="application-agent-runtime-29",
        binding_id="29",
        api_server_key="s" * 32,
        backend_base_url="http://backend:8011",
        client=client,
    )


def _write_runtime_mutation_state(
    tmp_path: Path,
) -> tuple[Path, dict[str, str], Path, Path]:
    home = tmp_path / "home"
    files = {
        "config.yaml": "model: old\n",
        "SOUL.md": "# Application\n",
        "platform/application-agent.json": '{"generation":1}\n',
        "skills/application/private-99/SKILL.md": "# Channel analysis\n",
    }
    for relative, content in files.items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    outside = tmp_path / "outside-python"
    outside.write_text("managed interpreter\n", encoding="utf-8")
    venv_python = home / "skills/application/private-99/.venv/bin/python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.symlink_to(outside)
    work_report = home / "skills/application/private-99/work/report.json"
    work_report.parent.mkdir(parents=True, exist_ok=True)
    work_report.write_text('{"runtime":"output"}\n', encoding="utf-8")

    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-1",
                "config_digest": config_bundle_digest(files),
                "managed_files": sorted(files),
            }
        ),
        encoding="utf-8",
    )
    return home, files, outside, work_report


def _removal_bundle_files() -> dict[str, str]:
    return {
        "config.yaml": "model: new\n",
        "SOUL.md": "# Application\n",
        "platform/application-agent.json": '{"generation":2}\n',
    }


@pytest.mark.asyncio
async def test_application_skill_removal_moves_runtime_mutation_without_reading_it(
    tmp_path: Path,
) -> None:
    home, _files, outside, work_report = _write_runtime_mutation_state(tmp_path)
    files = _removal_bundle_files()
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)
    client = _http_client(files=files, bundle=bundle, secret=secret)
    adapter = _Adapter()
    controller = _controller(tmp_path, adapter=adapter, client=client)

    await controller.start()
    await controller.apply(bundle)

    assert adapter.calls == [
        (bundle.config_revision, bundle.config_digest, True, False)
    ]
    assert not (home / "skills/application/private-99").exists()
    assert outside.read_text(encoding="utf-8") == "managed interpreter\n"
    assert not work_report.exists()
    assert controller._managed_files == tuple(sorted(files))
    await client.aclose()


@pytest.mark.asyncio
async def test_application_skill_removal_rollback_restores_mutation_evidence(
    tmp_path: Path,
) -> None:
    home, files, outside, work_report = _write_runtime_mutation_state(tmp_path)
    desired = _removal_bundle_files()
    secret = "s" * 32
    bundle = _bundle(files=desired, secret=secret)
    client = _http_client(files=desired, bundle=bundle, secret=secret)
    adapter = _Adapter(fail=True)
    controller = _controller(tmp_path, adapter=adapter, client=client)

    await controller.start()
    with pytest.raises(RuntimeError, match="reload failed"):
        await controller.apply(bundle)

    skill = home / "skills/application/private-99/SKILL.md"
    venv_python = home / "skills/application/private-99/.venv/bin/python"
    assert skill.read_text(encoding="utf-8") == files[
        "skills/application/private-99/SKILL.md"
    ]
    assert venv_python.is_symlink()
    assert venv_python.readlink() == outside
    assert work_report.is_file()
    assert controller.current_revision == "generation-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_application_skill_config_is_not_group_writable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    old_files = {
        "config.yaml": "model: old\n",
        "SOUL.md": "# Application\n",
        "platform/application-agent.json": '{"generation":1}\n',
        "skills/application/private-99/SKILL.md": "# Old\n",
    }
    for relative, content in old_files.items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o660)

    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-1",
                "config_digest": config_bundle_digest(old_files),
                "managed_files": sorted(old_files),
            }
        ),
        encoding="utf-8",
    )
    files = {
        **old_files,
        "platform/application-agent.json": '{"generation":2}\n',
        "skills/application/private-99/SKILL.md": "# New\n",
        "skills/application/private-99/scripts/run.py": "print('run')\n",
    }
    secret = "s" * 32
    bundle = _bundle(files=files, secret=secret)
    client = _http_client(files=files, bundle=bundle, secret=secret)
    adapter = _Adapter()
    controller = _controller(tmp_path, adapter=adapter, client=client)

    await controller.start()
    await controller.apply(bundle)

    for relative in (
        "skills/application/private-99/SKILL.md",
        "skills/application/private-99/scripts/run.py",
    ):
        assert stat.S_IMODE((home / relative).stat().st_mode) & 0o020 == 0
    for directory in (
        home / "skills/application/private-99",
        home / "skills/application/private-99/scripts",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) & 0o020 == 0
        assert stat.S_IMODE(directory.stat().st_mode) & 0o004 == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_matching_application_bundle_startup_still_reclaims_mutations(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    files = {
        "config.yaml": "model: old\n",
        "SOUL.md": "# Application\n",
        "platform/application-agent.json": '{"generation":1}\n',
        "skills/application/private-99/SKILL.md": "# OKR\n",
    }
    for relative, content in files.items():
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o660)
    application_root = home / "skills/application"
    skill_root = application_root / "private-99"
    application_root.chmod(0o2770)
    skill_root.chmod(0o2770)

    outside = tmp_path / "outside-python"
    outside.write_text("managed interpreter\n", encoding="utf-8")
    venv_python = skill_root / ".venv/bin/python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.symlink_to(outside)
    work_report = skill_root / "work/report.json"
    work_report.parent.mkdir(parents=True, exist_ok=True)
    work_report.write_text('{"runtime":"output"}\n', encoding="utf-8")
    stale_skill = application_root / "private-old"
    stale_skill.mkdir()
    (stale_skill / "SKILL.md").write_text("# Stale\n", encoding="utf-8")

    state_path = tmp_path / "state/current-config.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_revision": "generation-1",
                "config_digest": config_bundle_digest(files),
                "managed_files": sorted(files),
            }
        ),
        encoding="utf-8",
    )
    secret = "s" * 32
    unsigned = _bundle(files=files, secret=secret).model_copy(
        update={"config_revision": "generation-1"}
    )
    bundle = unsigned.model_copy(
        update={
            "signature": config_bundle_signature(
                secret=secret,
                bundle=unsigned,
            )
        }
    )
    client = _http_client(files=files, bundle=bundle, secret=secret)
    controller = RuntimeConfigController(
        home=home,
        state_path=state_path,
        initial_revision="generation-1",
        adapter=_Adapter(),
        sessions=_Sessions(),  # type: ignore[arg-type]
        runtime_id="application-agent-runtime-29",
        binding_id="29",
        api_server_key=secret,
        backend_base_url="http://backend:8011",
        initial_download_url=bundle.download_url,
        initial_digest=bundle.config_digest,
        client=client,
    )

    await controller.start()

    assert not (skill_root / ".venv").exists()
    assert not (skill_root / "work").exists()
    assert not stale_skill.exists()
    quarantine = home / ".platform-runtime-quarantine"
    assert any(
        path.is_symlink() and path.name == "python"
        for path in quarantine.rglob("*")
    )
    assert any(path.name == "report.json" for path in quarantine.rglob("*"))
    assert any(
        path.name == "SKILL.md" and "private-old" in path.parts
        for path in quarantine.rglob("*")
    )
    assert stat.S_IMODE(application_root.stat().st_mode) & 0o020 == 0
    assert stat.S_IMODE(skill_root.stat().st_mode) & 0o020 == 0
    assert (
        stat.S_IMODE((skill_root / "SKILL.md").stat().st_mode) & 0o020
        == 0
    )
    await client.aclose()
