from pathlib import Path

import pytest

from app.services.asset_unification_preflight import (
    AssetUnificationPreflightSnapshot,
    LegacyAssetBindingFact,
    LegacyAssetVersionFact,
    LegacyDraftFact,
    LegacyProfileFact,
    analyze_asset_unification_snapshot,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "asset_unification"


def _snapshot_fixture(name: str) -> AssetUnificationPreflightSnapshot:
    return AssetUnificationPreflightSnapshot.model_validate_json(
        (FIXTURE_ROOT / name).read_text(encoding="utf-8")
    )


def _profile(*, profile_id: int = 10, kind: str = "default") -> LegacyProfileFact:
    return LegacyProfileFact(
        profile_id=profile_id,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        profile_kind=kind,
    )


def _binding(
    *,
    source_table: str,
    source_id: int,
    version: str = "v1",
    digest: str = "sha256:one",
    artifact_uri: str | None = None,
) -> LegacyAssetBindingFact:
    return LegacyAssetBindingFact(
        source_table=source_table,
        source_id=source_id,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        profile_id=10,
        asset_type="skill",
        logical_asset_key="public-skill:7",
        version_key=version,
        content_digest=digest,
        artifact_uri=artifact_uri,
    )


def test_preflight_accepts_normal_and_mergeable_duplicate_facts(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "skill.zip"
    artifact.write_bytes(b"skill")
    snapshot = AssetUnificationPreflightSnapshot(
        profiles=[_profile()],
        bindings=[
            _binding(
                source_table="user_runtime_skill",
                source_id=1,
                artifact_uri=str(artifact),
            ),
            _binding(
                source_table="runtime_skill_binding",
                source_id=2,
                artifact_uri=str(artifact),
            ),
        ],
    )

    report = analyze_asset_unification_snapshot(snapshot)

    assert report.ready is True
    assert report.blocking_issue_count == 0
    assert report.mergeable_duplicate_count == 1
    assert report.affected_user_count == 1
    assert report.affected_profile_count == 1


def test_preflight_accepts_directory_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "deployment"
    artifact.mkdir()
    (artifact / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")
    snapshot = AssetUnificationPreflightSnapshot(
        profiles=[_profile()],
        bindings=[
            _binding(
                source_table="user_runtime_skill",
                source_id=1,
                artifact_uri=str(artifact),
            )
        ],
        versions=[
            LegacyAssetVersionFact(
                source_table="skill_version",
                source_id=5,
                parent_table="skill",
                parent_id=7,
                parent_exists=True,
                artifact_required=True,
                artifact_uri=str(artifact),
            )
        ],
    )

    report = analyze_asset_unification_snapshot(snapshot)

    assert report.ready is True
    assert report.issues == []


def test_preflight_blocks_conflicting_duplicates_without_guessing() -> None:
    snapshot = AssetUnificationPreflightSnapshot(
        profiles=[_profile()],
        bindings=[
            _binding(source_table="user_runtime_skill", source_id=1),
            _binding(
                source_table="runtime_skill_binding",
                source_id=2,
                version="v2",
                digest="sha256:two",
            ),
        ],
    )

    report = analyze_asset_unification_snapshot(snapshot)

    assert report.ready is False
    assert [issue.code for issue in report.issues] == [
        "DUPLICATE_BINDING_CONFLICT"
    ]
    assert report.issues[0].source_refs == [
        "runtime_skill_binding:2",
        "user_runtime_skill:1",
    ]


def test_preflight_reports_orphans_missing_files_and_unsaved_drafts(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.zip"
    snapshot = AssetUnificationPreflightSnapshot(
        profiles=[_profile()],
        bindings=[
            _binding(
                source_table="user_runtime_skill",
                source_id=1,
                artifact_uri=str(missing),
            ),
            _binding(source_table="runtime_skill_binding", source_id=2),
        ],
        versions=[
            LegacyAssetVersionFact(
                source_table="skill_version",
                source_id=5,
                parent_table="skill",
                parent_id=99,
                parent_exists=False,
                artifact_required=True,
                artifact_uri=f"file://{missing}",
            )
        ],
        drafts=[
            LegacyDraftFact(
                source_id=8,
                runtime_workspace_id=3,
                status="staged",
            )
        ],
    )

    report = analyze_asset_unification_snapshot(snapshot)

    assert report.ready is False
    assert {issue.code for issue in report.issues} == {
        "ASSET_ARTIFACT_MISSING",
        "DUPLICATE_BINDING_MERGEABLE",
        "ORPHAN_ASSET_VERSION",
        "UNSAVED_PROFILE_DRAFT",
        "VERSION_ARTIFACT_MISSING",
    }
    assert report.blocking_issue_count == 4


def test_preflight_report_is_deterministic_for_idempotent_reruns() -> None:
    snapshot = AssetUnificationPreflightSnapshot(
        profiles=[_profile()],
        bindings=[_binding(source_table="user_runtime_skill", source_id=1)],
    )

    first = analyze_asset_unification_snapshot(snapshot)
    second = analyze_asset_unification_snapshot(snapshot)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_preflight_blocks_required_version_without_artifact_location() -> None:
    snapshot = AssetUnificationPreflightSnapshot(
        profiles=[_profile()],
        versions=[
            LegacyAssetVersionFact(
                source_table="skill_version",
                source_id=5,
                parent_table="skill",
                parent_id=7,
                parent_exists=True,
                artifact_required=True,
            )
        ],
    )

    report = analyze_asset_unification_snapshot(snapshot)

    assert report.ready is False
    assert [issue.code for issue in report.issues] == [
        "VERSION_ARTIFACT_URI_MISSING"
    ]


@pytest.mark.parametrize(
    ("fixture_name", "ready", "issue_code"),
    [
        ("normal.json", True, None),
        ("consistent-duplicate.json", True, "DUPLICATE_BINDING_MERGEABLE"),
        ("conflicting-duplicate.json", False, "DUPLICATE_BINDING_CONFLICT"),
    ],
)
def test_preflight_desensitized_cutover_fixtures(
    fixture_name: str,
    ready: bool,
    issue_code: str | None,
) -> None:
    snapshot = _snapshot_fixture(fixture_name)

    first = analyze_asset_unification_snapshot(snapshot)
    second = analyze_asset_unification_snapshot(snapshot)

    assert first.ready is ready
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    codes = [issue.code for issue in first.issues]
    if issue_code is None:
        assert codes == []
    else:
        assert issue_code in codes
