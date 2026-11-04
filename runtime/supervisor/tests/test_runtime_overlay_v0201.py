from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import sysconfig

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_ROOT = REPOSITORY_ROOT / "runtime" / "hermes-runtime-overlay"
PLUGIN_ROOT = OVERLAY_ROOT / "platform_main_conversation_boundary"
MANIFEST_PATH = (
    REPOSITORY_ROOT / "runtime" / "hermes" / "compatibility" / "v0.20.1.yaml"
)
sys.path.insert(0, str(OVERLAY_ROOT))
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from overlay_contract import (  # noqa: E402
    OverlayContractError,
    load_overlay_contract,
    sha256_jcs,
)
from platform_main_conversation_boundary.marker import (  # noqa: E402
    HEADER_NAME,
    BoundaryBootstrapError,
    BoundaryMarkerSigner,
    route_token_from_base_url,
)
from platform_main_conversation_boundary import plugin as boundary_plugin  # noqa: E402
from upstream_component_probe import (  # noqa: E402
    distribution_entry,
    distribution_inventory,
)
from verify_platform_boundary_plugin import verify as verify_boundary_plugin  # noqa: E402
from verify_skill_visibility import (  # noqa: E402
    OFFICIAL_SOURCE_COUNT,
    SkillVisibilityError,
    _inventory,
)


def _bootstrap_bytes(manifest: dict) -> bytes:
    golden = manifest["runtime_overlay"]["platform_main_conversation_boundary"][
        "golden_vector"
    ]
    return json.dumps(
        {
            "contract_version": 1,
            "algorithm": "hmac-sha256",
            "key_base64url": golden["key_base64url"],
            "boundary_epoch_nonce": golden["payload"]["boundary_epoch_nonce"],
        },
        separators=(",", ":"),
    ).encode()


def test_v0201_manifest_pins_identity_dependencies_and_vector_digests() -> None:
    manifest = load_overlay_contract(MANIFEST_PATH)

    assert manifest["release_tag"] == "v2026.8.13"
    assert manifest["upstream_commit"] == "f80f453ae0679347e38abc917c7f94f717bf96c5"
    assert manifest["source_archive_sha256"] == (
        "570900ec79d2c292a9bf04a015032b417412fb962f329c2c7a13381beda9d154"
    )
    assert manifest["state_schema"] == 25
    assert manifest["config_schema"] == 34
    locked = manifest["runtime_overlay"]["dependency_lock"]["required_distributions"]
    assert locked == {
        "Pillow": "12.3.0",
        "edge-tts": "7.2.7",
        "fal-client": "0.13.1",
        "simple-term-menu": "1.6.6",
        "setuptools": "83.0.0",
        "watchfiles": "1.1.1",
    }
    cjk = manifest["runtime_overlay"]["fts5_cjk_extension"]
    assert cjk["immutable_path"] == "/opt/hermes-platform/lib/libfts5_cjk.so"
    assert cjk["init_symbol"] == "sqlite3_fts5_cjk_init"
    state_probe = manifest["runtime_overlay"]["state_schema_probe"]
    assert state_probe["contract"] == "hermes-state-schema-probe-f80f453-v1"
    assert state_probe["supported_source_schemas"] == [23, 25]
    assert state_probe["core_tables_by_schema"][25] == [
        "schema_version",
        "system_prompts",
        "sessions",
        "messages",
        "state_meta",
    ]
    assert state_probe["fts_objects"]["cjk_triggers"] == [
        "messages_fts_cjk_insert",
        "messages_fts_cjk_delete",
        "messages_fts_cjk_update",
    ]
    assert cjk["tokenizer"] == "cjk_unicode61"
    assert cjk["source_files"]["fts5_cjk.c"] == (
        "sha256:814e7dd9ab1b4c4f69673715ed147a2f8203e3443661c0152d36ecfabcb4768f"
    )
    for component in manifest["upstream_components"]["components"].values():
        for vector_id, vector in component["vectors"].items():
            assert vector["expected_digest"] == sha256_jcs(
                {
                    "vector_id": vector_id,
                    "actual_projection": vector["expected_projection"],
                }
            )


def test_v0201_manifest_fails_closed_on_identity_drift(tmp_path: Path) -> None:
    document = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["upstream_commit"] = "0" * 40
    drifted = tmp_path / "v0.20.1.yaml"
    drifted.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(OverlayContractError, match="fixed upstream identity"):
        load_overlay_contract(drifted)


def test_boundary_marker_matches_golden_and_rejects_noncanonical_urls() -> None:
    manifest = load_overlay_contract(MANIFEST_PATH)
    golden = manifest["runtime_overlay"]["platform_main_conversation_boundary"][
        "golden_vector"
    ]
    payload = golden["payload"]
    signer = BoundaryMarkerSigner.from_bootstrap_bytes(
        _bootstrap_bytes(manifest),
        clock=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
        random_bytes=lambda size: bytes(range(size)),
    )

    result = signer.apply(
        {"messages": [], "extra_headers": {"X-Existing": "ok"}},
        base_url=golden["canonical_base_url"],
        session_id=payload["session_id"],
        task_id=payload["task_id"],
        turn_id=payload["turn_id"],
        api_request_id=payload["api_request_id"],
        api_call_count=payload["api_call_count"],
    )

    assert result is not None
    assert result["request"]["messages"] == []
    assert result["request"]["extra_headers"] == {
        "X-Existing": "ok",
        HEADER_NAME: golden["header"],
    }
    assert all(route_token_from_base_url(value) is None for value in golden["invalid_base_urls"])
    assert signer.apply({}, base_url="http://localhost:9121") is None


def test_boundary_bootstrap_descriptor_is_one_shot_and_closed() -> None:
    manifest = load_overlay_contract(MANIFEST_PATH)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, _bootstrap_bytes(manifest))
    os.close(write_fd)

    signer = BoundaryMarkerSigner.from_fixed_fd(read_fd)

    assert isinstance(signer, BoundaryMarkerSigner)
    with pytest.raises(OSError):
        os.read(read_fd, 1)


def test_boundary_bootstrap_rejects_unknown_fields() -> None:
    manifest = load_overlay_contract(MANIFEST_PATH)
    payload = json.loads(_bootstrap_bytes(manifest))
    payload["secret_from_env"] = "forbidden"

    with pytest.raises(BoundaryBootstrapError, match="fields"):
        BoundaryMarkerSigner.from_bootstrap_bytes(json.dumps(payload).encode())


def test_boundary_plugin_registers_only_llm_request(monkeypatch: pytest.MonkeyPatch) -> None:
    class Signer:
        def apply(self, request, **_context):
            return {"request": request}

    class Context:
        def __init__(self) -> None:
            self.registrations: list[tuple[str, object]] = []

        def register_middleware(self, kind: str, callback: object) -> None:
            self.registrations.append((kind, callback))

    signer = Signer()
    monkeypatch.setattr(boundary_plugin, "_load_signer", lambda: signer)
    context = Context()

    boundary_plugin.register(context)

    assert context.registrations == [("llm_request", signer.apply)]


def test_boundary_plugin_integrity_probe_checks_sources_and_registration() -> None:
    report = verify_boundary_plugin(manifest_path=MANIFEST_PATH, plugin_root=PLUGIN_ROOT)

    assert report["status"] == "passed"
    assert report["registration_surface"] == {
        "middleware": ["llm_request"],
        "tools": [],
        "hooks": [],
        "listeners": [],
    }
    assert report["golden_vector_passed"] is True


def test_distribution_entry_hashes_raw_record_and_direct_url(tmp_path: Path) -> None:
    dist_info = tmp_path / "Example_Pkg-1.2.3.dist-info"
    dist_info.mkdir()
    record = dist_info / "RECORD"
    direct_url = dist_info / "direct_url.json"
    record.write_bytes(b"raw,record,bytes\n")
    direct_url.write_bytes(b'{"url":"file:///immutable"}\n')

    class Distribution:
        metadata = {"Name": "Example_Pkg"}
        version = "1.2.3"
        files = [
            importlib.metadata.PackagePath("Example_Pkg-1.2.3.dist-info/RECORD"),
            importlib.metadata.PackagePath(
                "Example_Pkg-1.2.3.dist-info/direct_url.json"
            ),
        ]

        def locate_file(self, path: object) -> Path:
            return tmp_path / str(path)

    entry = distribution_entry(Distribution())

    assert entry == {
        "name": "example-pkg",
        "version": "1.2.3",
        "record_digest": "sha256:" + hashlib.sha256(record.read_bytes()).hexdigest(),
        "direct_url_digest": "sha256:"
        + hashlib.sha256(direct_url.read_bytes()).hexdigest(),
    }


def test_distribution_entry_ignores_vendored_dist_info_records(tmp_path: Path) -> None:
    dist_info = tmp_path / "setuptools-83.0.0.dist-info"
    dist_info.mkdir()
    record = dist_info / "RECORD"
    record.write_bytes(b"setuptools,record\n")
    vendored = (
        tmp_path
        / "setuptools"
        / "_vendor"
        / "packaging-26.0.dist-info"
        / "RECORD"
    )
    vendored.parent.mkdir(parents=True)
    vendored.write_bytes(b"vendored,record\n")

    class Distribution:
        metadata = {"Name": "setuptools"}
        version = "83.0.0"
        files = [
            importlib.metadata.PackagePath("setuptools-83.0.0.dist-info/RECORD"),
            importlib.metadata.PackagePath(
                "setuptools/_vendor/packaging-26.0.dist-info/RECORD"
            ),
        ]

        def locate_file(self, path: object) -> Path:
            return tmp_path / str(path)

    entry = distribution_entry(Distribution())

    assert entry["record_digest"] == "sha256:" + hashlib.sha256(
        record.read_bytes()
    ).hexdigest()


def test_distribution_inventory_scans_only_interpreter_site_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def distributions(*, path: list[str]) -> list[object]:
        observed["path"] = path
        return []

    monkeypatch.setattr(importlib.metadata, "distributions", distributions)

    assert distribution_inventory() == []
    assert observed["path"] == sorted(
        {
            sysconfig.get_path("purelib"),
            sysconfig.get_path("platlib"),
        }
    )


def test_official_skill_inventory_requires_81_unique_regular_manifests(
    tmp_path: Path,
) -> None:
    for index in range(OFFICIAL_SOURCE_COUNT):
        skill = tmp_path / f"category-{index}" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text(
            f"---\nname: official-{index}\ndescription: fixture\n---\n",
            encoding="utf-8",
        )

    inventory = _inventory(tmp_path, expected_count=OFFICIAL_SOURCE_COUNT)

    assert len(inventory["names"]) == OFFICIAL_SOURCE_COUNT
    assert inventory["digest"].startswith("sha256:")


def test_official_skill_inventory_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: source\ndescription: fixture\n---\n",
        encoding="utf-8",
    )
    (tmp_path / "escaped").symlink_to(source, target_is_directory=True)

    with pytest.raises(SkillVisibilityError, match="symlink"):
        _inventory(tmp_path)


def test_overlay_dockerfile_uses_locked_0201_dependencies_and_probes() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay").read_text(
        encoding="utf-8"
    )

    assert "--extra edge-tts" in dockerfile
    assert "--extra fal" in dockerfile
    assert "--no-install-project" not in dockerfile
    assert "platform-build-tools.requirements.lock" in dockerfile
    assert "legacy-runtime-dependencies.requirements.lock" in dockerfile
    assert "/usr/local/bin/uv --no-config pip install" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "setuptools') == '83.0.0'" in dockerfile
    assert "watchfiles') == '1.1.1'" in dockerfile
    assert "Pillow') == '12.3.0'" in dockerfile
    assert "edge-tts') == '7.2.7'" in dockerfile
    assert "fal-client') == '0.13.1'" in dockerfile
    assert "importlib.import_module('audioop')" in dockerfile
    assert "version('audioop-lts') == '0.2.2'" in dockerfile
    assert "simple-term-menu') == '1.6.6'" in dockerfile
    assert "relocate_venv_python.py" in dockerfile
    assert "test ! -L /opt/hermes/.venv/bin/python" in dockerfile
    assert "sys.base_prefix == '/opt/hermes-platform/python'" in dockerfile
    assert "verify_platform_boundary_plugin.py" in dockerfile
    assert "verify_lazy_install_policy.py" in dockerfile
    assert "upstream_component_probe.py" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "libatomic1" in dockerfile
    assert "AS cjk-extension-builder" in dockerfile
    assert "native/fts5_cjk/build.sh /opt/hermes-platform/lib" in dockerfile
    assert "HERMES_EXPECTED_CONFIG_SCHEMA=34" in dockerfile
    assert "HERMES_FTS5_CJK_SO=/opt/hermes-platform/lib/libfts5_cjk.so" in dockerfile
    assert 'HERMES_LAZY_INSTALL_TARGET=""' in dockerfile
    assert "watchfiles==1.1.0" not in dockerfile
    assert "setuptools==81.0.0" not in dockerfile
    supervisor_project = (
        REPOSITORY_ROOT / "runtime/supervisor/pyproject.toml"
    ).read_text(encoding="utf-8")
    assert "platform-hermes-child-exec" in supervisor_project
    assert "hermes-posix-access-probe.py" in dockerfile
    assert (
        "7abda9c68c9916300f3b310a61eb86f074ea19bcce29fc0c54e97143afa19187"
        in dockerfile
    )
