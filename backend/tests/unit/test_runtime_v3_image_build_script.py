from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "scripts/build-publish-runtime-v3-image-dev.py"


def test_v3_builder_keeps_hermes_source_out_of_the_overlay_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay").read_text(
        encoding="utf-8"
    )

    assert '"hermes_source_unchanged": True' in source
    assert "HERMES_BASE_IMAGE_IMMUTABLE_REFERENCE" in source
    assert '"--network", "host"' in source
    assert "_docker_build_proxy" in source
    assert "runtime/hermes-runtime-overlay" in source
    assert "COPY runtime/hermes/compatibility/v0.20.1.yaml" in dockerfile
    assert "COPY runtime/hermes/ " not in dockerfile
    assert "docker/dockerfile:" not in dockerfile
    assert "complete-runtime-overlay" in dockerfile


def test_v3_builder_carries_skill_and_default_dependency_manifests() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay").read_text(
        encoding="utf-8"
    )

    assert "skill-inventory.json" in source
    assert "font-manifest.json" in source
    for marker in (
        "libreoffice-calc",
        "libreoffice-impress",
        "libreoffice-writer",
        "poppler-utils",
        "pygount",
        "designmd",
        "fetch-retries=5",
    ):
        assert marker in dockerfile


def test_v3_builder_has_read_only_plan_and_atomic_identity_update() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "args.print_plan" in source
    assert "config_store.replace(expected=current, replacement=replacement)" in source
    assert "runtime-v3-build-receipt-v1" in source
    assert "tracked_tree_before" in source
