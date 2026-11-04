from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_supervisor_overlay_does_not_patch_hermes_source() -> None:
    legacy_dockerfile = REPOSITORY_ROOT / "Dockerfile.hermes-compute-host-model-sync"
    legacy_overlay = REPOSITORY_ROOT / "runtime" / "hermes-compute-host-model-sync"
    supervisor_dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay").read_text(
        encoding="utf-8"
    )

    assert not legacy_dockerfile.exists()
    assert not legacy_overlay.exists()
    assert "apply_overlay" not in supervisor_dockerfile
    assert "tui_gateway/compute_host.py" not in supervisor_dockerfile
    assert "delete-old-source" not in supervisor_dockerfile
    assert "COPY upstream/ /opt/hermes" not in supervisor_dockerfile
    assert 'hermes.platform.build-strategy="complete-runtime-overlay"' in (supervisor_dockerfile)


def test_code_only_pipeline_has_no_source_rebase_dependency() -> None:
    formal_sources = (
        REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay",
        REPOSITORY_ROOT / "scripts" / "build-publish-runtime-code-image-dev.py",
        REPOSITORY_ROOT / "backend" / "app" / "services" / "runtime_code_image_contract.py",
    )

    for source in formal_sources:
        content = source.read_text(encoding="utf-8")
        assert "hermes-source-rebase" not in content
        assert "rebase_source.py" not in content
        assert "delete-old-source" not in content
        assert "apply-adaptation" not in content


def test_code_only_overlay_refreshes_supervisor_child_helpers() -> None:
    dockerfile = (
        REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay"
    ).read_text(encoding="utf-8")

    for helper in (
        "hermes-sessiondb-helper.py",
        "hermes-session-metadata-helper.py",
        "hermes-posix-access-probe.py",
    ):
        assert f"/opt/hermes-platform/bin/{helper}" in dockerfile


def test_complete_runtime_overlay_installs_the_full_platform_inventory() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay").read_text(
        encoding="utf-8"
    )

    assert dockerfile.splitlines().count("ARG HERMES_BASE_IMAGE") == 1
    assert "AS browser-deps" in dockerfile
    assert "AS office-python-deps" in dockerfile
    assert "AS office-node-deps" in dockerfile
    assert "AS docx-builder" in dockerfile
    assert "AS skill-assets" in dockerfile
    assert "AS runtime-final" in dockerfile
    for package in (
        "supervisor",
        "enterprise-knowledge-plugin",
        "personal-knowledge-plugin",
        "clarify-lifecycle-plugin",
        "kanban-lifecycle-plugin",
        "expert-roster-plugin",
        "kanban-dispatcher",
    ):
        assert f"runtime/{package}" in dockerfile
    assert "skill_inventory.py" in dockerfile
    assert "skill-inventory.json" in dockerfile
    assert "verify_runtime_overlay.py" in dockerfile
    assert "verify_skill_visibility.py" in dockerfile
    assert "source-normalization-f80f453-v1.yaml" in dockerfile
    assert "/opt/hermes-platform/runtime-overlay/verify_runtime_overlay.py" in dockerfile
    assert "HERMES_BUNDLED_SKILLS=/opt/hermes/platform-default-skills" in dockerfile
    assert "COPY runtime/hermes-platform-skills/" in dockerfile
    assert "COPY runtime/hermes-readonly-skills/" in dockerfile
    assert "--root platform-default-skills" in dockerfile
    assert "--root platform-readonly-skills" in dockerfile
    assert "COPY runtime/hermes-default-skills-zh-v2/" not in dockerfile
    assert "-type f -name SKILL.md | wc -l)\" -eq 5" in dockerfile
    assert "-type f -name SKILL.md | wc -l)\" -eq 1" in dockerfile
    assert "ARG HERMES_RUNTIME_USER=root" in dockerfile
    assert "USER ${HERMES_RUNTIME_USER}" in dockerfile
    assert "&& /usr/local/bin/hermes-office-verify" in dockerfile


def test_complete_runtime_overlay_uses_locked_offline_capabilities() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay").read_text(
        encoding="utf-8"
    )

    assert "runtime/hermes-browser-runtime/node/package-lock.json" in dockerfile
    assert "runtime/hermes-office-runtime-zh-v2/node/package-lock.json" in dockerfile
    assert "runtime/hermes-office-runtime-zh-v2/python/requirements.lock" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in dockerfile
    assert "dotnet restore --locked-mode" in dockerfile
    assert "packages.lock.json" in dockerfile
    assert "dotnet-sdk-8.0" not in dockerfile
    assert "HERMES_DISABLE_LAZY_INSTALLS=1" in dockerfile
    assert "font-manifest.json" in dockerfile
    assert "verify_fonts.py" in dockerfile
    assert "fc-cache" in dockerfile
    assert "fc-match" in dockerfile


def test_docx_projects_commit_locked_dependency_graphs() -> None:
    dotnet = REPOSITORY_ROOT / "runtime" / "hermes-office-runtime-zh-v2" / "dotnet"

    assert (dotnet / "MiniMaxAIDocx.Cli" / "packages.lock.json").is_file()
    assert (dotnet / "MiniMaxAIDocx.Core" / "packages.lock.json").is_file()
