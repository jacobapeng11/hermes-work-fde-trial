import json
from pathlib import Path
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "scripts/build-publish-runtime-code-image-dev.py"


def test_print_plan_is_read_only_and_uses_unique_code_overlay(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "HERMES_RUNTIME_IMAGE=kind.local/hermes-agent:current-build\nOTHER=kept\n"
    env_file.write_text(original, encoding="utf-8")

    completed = subprocess.run(
        [
            str(SCRIPT),
            "--env-file",
            str(env_file),
            "--publisher",
            "kind",
            "--print-plan",
        ],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["base_image"] == "kind.local/hermes-agent:current-build"
    assert plan["target_image"].startswith("kind.local/hermes-agent:code-")
    assert plan["dockerfile"] == "Dockerfile.runtime-code-overlay"
    assert env_file.read_text(encoding="utf-8") == original


def test_print_plan_marks_active_mcp_reload_test_image(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "HERMES_RUNTIME_IMAGE=kind.local/hermes-agent:current-build\n"
    env_file.write_text(original, encoding="utf-8")

    completed = subprocess.run(
        [
            str(SCRIPT),
            "--env-file",
            str(env_file),
            "--publisher",
            "containerd",
            "--active-mcp-reload-test",
            "--print-plan",
        ],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["target_image"].startswith(
        "kind.local/hermes-agent:active-mcp-reload-test-"
    )
    assert plan["active_mcp_reload_test"] is True
    assert env_file.read_text(encoding="utf-8") == original


def test_code_overlay_reinstalls_supervisor_without_copying_hermes_source() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay").read_text(
        encoding="utf-8"
    )

    assert "COPY runtime/supervisor" in dockerfile
    assert "runtime-code-contract-v1" in dockerfile
    assert "HERMES_IMMUTABLE_RUNTIME_SHA256" in dockerfile
    assert "COPY runtime/hermes" not in dockerfile
    assert "schema_migration" not in dockerfile


def test_code_overlay_only_adds_active_mcp_reload_marker_when_explicitly_enabled() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay").read_text(
        encoding="utf-8"
    )

    assert "HERMES_ACTIVE_MCP_RELOAD_TEST=0" in dockerfile
    assert 'if [ "${HERMES_ACTIVE_MCP_RELOAD_TEST}" = "1" ]' in dockerfile
    assert "/opt/hermes-platform/allow-active-mcp-reload-test" in dockerfile
    assert 'hermes.platform.active-mcp-reload-test="${HERMES_ACTIVE_MCP_RELOAD_TEST}"' in dockerfile


def test_code_overlay_does_not_require_remote_buildkit_frontend() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay").read_text(
        encoding="utf-8"
    )

    assert "docker/dockerfile:" not in dockerfile


def test_build_compares_parent_and_target_immutable_runtime_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "inspect_runtime_code_invariant" in source
    assert "target_invariant.payload != base_invariant.payload" in source
    assert "target_invariant.oci_config != base_invariant.oci_config" in source
    assert "--no-deps" in (REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay").read_text(
        encoding="utf-8"
    )


def test_build_receipt_is_release_identifiable_and_checks_tracked_tree() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'git", "rev-parse", "HEAD"' in source
    assert '"platform_revision": revision' in source
    assert '"supervisor_tree_sha256": supervisor_digest' in source
    assert '"config_id": identity.config_id' in source
    assert "tracked_tree_before" in source
    assert "tracked_tree_after" in source


def test_build_accepts_explicit_image_repository_for_local_registry() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--image-repository"' in source
    assert 'default="kind.local/hermes-agent"' in source


def test_immutable_code_contract_does_not_probe_runtime_pvc_mountpoints() -> None:
    source = (
        REPOSITORY_ROOT / "backend/app/services/runtime_code_image_contract.py"
    ).read_text(encoding="utf-8")

    assert '"/home/hermes"' not in source


def test_preflight_allows_docker_index_id_when_config_identity_is_published() -> None:
    source = (REPOSITORY_ROOT / "backend/scripts/runtime_image_preflight.py").read_text(
        encoding="utf-8"
    )

    assert 'image.get("Id") != target.config_id' not in source
    assert ".inspect_published(" in source
