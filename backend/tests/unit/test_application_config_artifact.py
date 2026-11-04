from pathlib import Path

import pytest

from app.services.application_agent_runtime import (
    ApplicationAgentConfigArtifactStore,
    ApplicationAgentRuntimeError,
    SqlAlchemyApplicationAgentRuntimeManager,
)
from app.services.runtime_config_bundle import (
    config_bundle_digest,
    read_config_bundle_files,
)
from app.services.runtime_commands import RuntimeCommandError


def _files(prompt: str) -> dict[str, str]:
    return {
        "config.yaml": "model: platform\n",
        "SOUL.md": prompt,
    }


def test_config_bundle_reader_skips_binary_and_invalid_text_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "skills/demo/__pycache__").mkdir(parents=True)
    (home / "skills/demo/scripts").mkdir(parents=True)
    (home / "config.yaml").write_text("model: platform\n", encoding="utf-8")
    (home / "SOUL.md").write_text("# Demo\n", encoding="utf-8")
    (home / "skills/demo/SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
    (home / "skills/demo/scripts/helper.py").write_text(
        "print('ok')\n",
        encoding="utf-8",
    )
    (home / "skills/demo/__pycache__/helper.cpython-313.pyc").write_bytes(
        b"\x00\xffcompiled"
    )
    (home / "skills/demo/generated.bin").write_bytes(b"\x00\xffbinary")
    (home / "skills/demo/valid-utf8.bin").write_bytes(b"binary\x00text")

    observed = read_config_bundle_files(home)

    assert observed["config.yaml"] == "model: platform\n"
    assert observed["skills/demo/SKILL.md"] == "# Demo skill\n"
    assert observed["skills/demo/scripts/helper.py"] == "print('ok')\n"
    assert "skills/demo/generated.bin" not in observed
    assert "skills/demo/valid-utf8.bin" not in observed
    assert not any("__pycache__" in path for path in observed)


def test_config_bundle_reader_skips_unreadable_skill_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "skills/demo/cache").mkdir(parents=True)
    (home / "config.yaml").write_text("model: platform\n", encoding="utf-8")
    (home / "SOUL.md").write_text("# Demo\n", encoding="utf-8")
    cache = home / "skills/demo/cache/actuals_cache.json"
    cache.write_text("{}\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def deny_cache_read(path: Path, *args: object, **kwargs: object) -> bytes:
        if path == cache:
            raise PermissionError("cache is owned by Hermes")
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", deny_cache_read)

    observed = read_config_bundle_files(home)

    assert set(observed) == {"config.yaml", "SOUL.md"}


def test_runtime_command_error_code_is_preserved_only_for_stable_codes() -> None:
    manager = SqlAlchemyApplicationAgentRuntimeManager(object())  # type: ignore[arg-type]

    assert manager._runtime_command_error_code(
        RuntimeCommandError("CONFIG_BUNDLE_FILE_INVALID")
    ) == "CONFIG_BUNDLE_FILE_INVALID"
    assert manager._runtime_command_error_code(
        RuntimeCommandError("Runtime command delivery failed")
    ) is None


def test_application_config_artifact_is_immutable_per_generation(
    tmp_path: Path,
) -> None:
    files = _files("# First\n")
    digest = config_bundle_digest(files)

    ApplicationAgentConfigArtifactStore.write(
        runtime_root_host_path=str(tmp_path),
        generation=4,
        files=files,
        digest=digest,
    )
    ApplicationAgentConfigArtifactStore.write(
        runtime_root_host_path=str(tmp_path),
        generation=4,
        files=files,
        digest=digest,
    )

    assert ApplicationAgentConfigArtifactStore.read(
        runtime_root_host_path=str(tmp_path),
        generation=4,
    ) == (files, digest)
    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_config_artifact_conflict",
    ):
        changed = _files("# Changed\n")
        ApplicationAgentConfigArtifactStore.write(
            runtime_root_host_path=str(tmp_path),
            generation=4,
            files=changed,
            digest=config_bundle_digest(changed),
        )


def test_application_config_artifact_rejects_tampering(tmp_path: Path) -> None:
    files = _files("# First\n")
    ApplicationAgentConfigArtifactStore.write(
        runtime_root_host_path=str(tmp_path),
        generation=7,
        files=files,
        digest=config_bundle_digest(files),
    )
    artifact = (
        tmp_path
        / ".platform-config-bundles/application/generation-7.json"
    )
    artifact.write_text("{}", encoding="utf-8")

    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_config_artifact_invalid",
    ):
        ApplicationAgentConfigArtifactStore.read(
            runtime_root_host_path=str(tmp_path),
            generation=7,
        )


@pytest.mark.asyncio
async def test_runtime_recovery_reuses_exact_generation_artifact(
    tmp_path: Path,
) -> None:
    files = _files("# Original generation\n")
    digest = config_bundle_digest(files)
    ApplicationAgentConfigArtifactStore.write(
        runtime_root_host_path=str(tmp_path),
        generation=23,
        files=files,
        digest=digest,
    )
    manager = SqlAlchemyApplicationAgentRuntimeManager(object())  # type: ignore[arg-type]

    observed = await manager._load_or_create_config_artifact(
        runtime_root_host_path=str(tmp_path),
        generation=23,
        baseline_epoch=1,
        spec=object(),  # type: ignore[arg-type]
        agent=object(),  # type: ignore[arg-type]
    )

    assert observed == (files, digest)


def test_config_artifact_baseline_epoch_isolates_same_generation(
    tmp_path: Path,
) -> None:
    legacy_files = _files("# Legacy baseline\n")
    legacy_digest = config_bundle_digest(legacy_files)
    ApplicationAgentConfigArtifactStore.write(
        runtime_root_host_path=str(tmp_path),
        generation=9,
        files=legacy_files,
        digest=legacy_digest,
    )
    baseline_files = _files("# New platform baseline\n")
    baseline_digest = config_bundle_digest(baseline_files)
    ApplicationAgentConfigArtifactStore.write(
        runtime_root_host_path=str(tmp_path),
        generation=9,
        baseline_epoch=2,
        files=baseline_files,
        digest=baseline_digest,
    )

    assert ApplicationAgentConfigArtifactStore.read(
        runtime_root_host_path=str(tmp_path),
        generation=9,
    ) == (legacy_files, legacy_digest)
    assert ApplicationAgentConfigArtifactStore.read(
        runtime_root_host_path=str(tmp_path),
        generation=9,
        baseline_epoch=2,
    ) == (baseline_files, baseline_digest)
    assert (
        tmp_path / ".platform-config-bundles/application/generation-9.json"
    ).is_file()
    assert (
        tmp_path
        / ".platform-config-bundles/application/generation-9-baseline-2.json"
    ).is_file()


def test_config_artifact_rejects_invalid_baseline_epoch(
    tmp_path: Path,
) -> None:
    files = _files("# Epoch guard\n")
    with pytest.raises(
        ApplicationAgentRuntimeError,
        match="application_agent_config_artifact_baseline_invalid",
    ):
        ApplicationAgentConfigArtifactStore.write(
            runtime_root_host_path=str(tmp_path),
            generation=3,
            baseline_epoch=0,
            files=files,
            digest=config_bundle_digest(files),
        )
