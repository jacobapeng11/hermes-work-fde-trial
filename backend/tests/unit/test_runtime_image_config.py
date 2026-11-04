from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.runtime_image_contract import (
    RuntimeImageContractError,
    resolve_runtime_image_contract,
)
from app.services.runtime_image_config import (
    RuntimeImageConfig,
    RuntimeImageConfigConflict,
    RuntimeImageConfigError,
    RuntimeImageConfigStore,
)


CURRENT_IMAGE = "kind.local/hermes-agent:v0.19.1-cc4cab2f59-32285038ed-df7c7313-candidate"


def _store(tmp_path: Path, contents: str) -> RuntimeImageConfigStore:
    env_path = tmp_path / ".env"
    env_path.write_text(contents, encoding="utf-8")
    return RuntimeImageConfigStore(
        env_path,
        lock_path=tmp_path / "runtime-image.lock",
    )


def test_runtime_image_config_reads_only_authoritative_keys(
    tmp_path: Path,
) -> None:
    store = _store(
        tmp_path,
        (
            "OTHER=value\n"
            f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n"
            "SHELL_TEXT=$(touch /tmp/must-not-run)\n"
        ),
    )

    assert store.read() == RuntimeImageConfig(
        image=CURRENT_IMAGE,
    )
    assert not Path("/tmp/must-not-run").exists()


def test_runtime_image_config_reads_closed_published_identity(tmp_path: Path) -> None:
    manifest_digest = "sha256:" + "a" * 64
    config_id = "sha256:" + "b" * 64
    store = _store(
        tmp_path,
        (
            f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n"
            f"HERMES_RUNTIME_IMAGE_MANIFEST_DIGEST={manifest_digest}\n"
            f"HERMES_RUNTIME_IMAGE_CONFIG_ID={config_id}\n"
        ),
    )

    assert store.read() == RuntimeImageConfig(
        image=CURRENT_IMAGE,
        manifest_digest=manifest_digest,
        config_id=config_id,
    )


def test_runtime_image_config_rejects_partial_published_identity(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        (
            f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n"
            f"HERMES_RUNTIME_IMAGE_CONFIG_ID=sha256:{'b' * 64}\n"
        ),
    )

    with pytest.raises(RuntimeImageConfigError, match="必须同时配置"):
        store.read()


@pytest.mark.parametrize(
    "contents,match",
    [
        ("OTHER=value\n", "HERMES_RUNTIME_IMAGE"),
        (
            f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\nHERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n",
            "重复定义 HERMES_RUNTIME_IMAGE",
        ),
        (
            "HERMES_RUNTIME_IMAGE=nginx:latest\n",
            "hermes-agent",
        ),
    ],
)
def test_runtime_image_config_rejects_invalid_sources(
    tmp_path: Path,
    contents: str,
    match: str,
) -> None:
    store = _store(tmp_path, contents)

    with pytest.raises(RuntimeImageConfigError, match=match):
        store.read()


def test_runtime_image_config_replace_is_atomic_and_preserves_file(
    tmp_path: Path,
) -> None:
    retired_key = "HERMES_RUNTIME_" + "GENERATION"
    store = _store(
        tmp_path,
        (
            "# managed backend config\n"
            f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n"
            f"{retired_key}=17\n"
            "\n"
            "UNRELATED='kept'\n"
        ),
    )
    os.chmod(store.env_path, 0o640)
    replacement = RuntimeImageConfig(
        image="kind.local/hermes-agent:v0.19.2-abcdef0123-platform-base-candidate",
    )

    assert (
        store.replace(
            expected=store.read(),
            replacement=replacement,
        )
        == replacement
    )

    assert store.read() == replacement
    rendered = store.env_path.read_text(encoding="utf-8")
    assert "# managed backend config" in rendered
    assert retired_key not in rendered
    assert f"HERMES_RUNTIME_IMAGE={replacement.image}\n\nUNRELATED" in rendered
    assert "UNRELATED='kept'" in rendered
    assert (store.env_path.stat().st_mode & 0o777) == 0o640
    assert not list(tmp_path.glob("..env.*.tmp"))


def test_runtime_image_config_replace_adds_published_identity_atomically(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n")
    replacement = RuntimeImageConfig(
        image="kind.local/hermes-agent:build-20260820-120000-deadbeef",
        manifest_digest="sha256:" + "a" * 64,
        config_id="sha256:" + "b" * 64,
    )

    assert store.replace(expected=store.read(), replacement=replacement) == replacement
    assert store.read() == replacement


def test_runtime_image_config_replace_detects_conflict(
    tmp_path: Path,
) -> None:
    store = _store(
        tmp_path,
        (f"HERMES_RUNTIME_IMAGE={CURRENT_IMAGE}\n"),
    )
    expected = store.read()
    competing_image = "kind.local/hermes-agent:v0.19.2-abcdef0123-platform-base-competing"
    store.env_path.write_text(
        f"HERMES_RUNTIME_IMAGE={competing_image}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeImageConfigConflict):
        store.replace(
            expected=expected,
            replacement=RuntimeImageConfig(
                image="kind.local/hermes-agent:v0.19.2-abcdef0123-platform-base-candidate",
            ),
        )


def test_runtime_image_contract_is_fixed_across_code_only_tags() -> None:
    repository_root = Path(__file__).resolve().parents[3]

    contract = resolve_runtime_image_contract(
        repository_root=repository_root,
        image_reference="kind.local/hermes-agent:code-20260820-deadbeef",
    )

    assert contract.upstream_version == "v0.20.1"
    assert contract.upstream_commit == "f80f453ae0679347e38abc917c7f94f717bf96c5"
    assert contract.state_schema == 25
    assert contract.config_schema == 34


def test_runtime_image_contract_rejects_non_hermes_reference() -> None:
    repository_root = Path(__file__).resolve().parents[3]

    with pytest.raises(RuntimeImageContractError, match="invalid"):
        resolve_runtime_image_contract(
            repository_root=repository_root,
            image_reference="kind.local/other:code",
        )
