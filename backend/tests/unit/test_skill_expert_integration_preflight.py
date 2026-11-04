from app.services.skill_expert_integration_preflight import (
    ExpertPackageFact,
    ExpertVersionFact,
    InstallationFact,
    ProfileFact,
    SkillBindingFact,
    SkillExpertPreflightSnapshot,
    StableAssetFact,
    analyze_skill_expert_snapshot,
)


def _ready_snapshot() -> SkillExpertPreflightSnapshot:
    return SkillExpertPreflightSnapshot(
        profiles=[
            ProfileFact(
                profile_id=11,
                runtime_workspace_id=7,
                profile_kind="expert",
                ownership_type="market_installed",
                status="active",
                display_name_key="写作专家",
                desired_revision=3,
                applied_revision=3,
            )
        ],
        packages=[
            ExpertPackageFact(
                package_id=21,
                org_id=1,
                status="published",
                execution_status="enabled",
                market_name_key="写作专家",
                current_version_id=31,
            )
        ],
        versions=[
            ExpertVersionFact(
                version_id=31,
                package_id=21,
                status="approved",
                skill_ids=[41],
            )
        ],
        installations=[
            InstallationFact(
                binding_id=51,
                runtime_workspace_id=7,
                package_id=21,
                version_id=31,
                profile_id=11,
                status="enabled",
                installation_epoch=1,
            )
        ],
        skill_bindings=[
            SkillBindingFact(
                binding_id=61,
                profile_id=11,
                skill_id=41,
                skill_version_id=None,
                version_policy="follow_latest",
                status="enabled",
            )
        ],
        skills=[StableAssetFact(asset_id=41, current_version_id=71)],
        skill_versions=[71],
    )


def test_preflight_ready_snapshot_has_no_issues() -> None:
    report = analyze_skill_expert_snapshot(_ready_snapshot())

    assert report.status == "ready"
    assert report.counts["blocking"] == 0
    assert report.issues == []


def test_preflight_reports_exact_conflicting_and_missing_ids() -> None:
    snapshot = _ready_snapshot()
    snapshot.profiles.append(
        ProfileFact(
            profile_id=12,
            runtime_workspace_id=7,
            profile_kind="expert",
            ownership_type="self_created",
            status="ready",
            display_name_key=" 写作专家 ",
            desired_revision=1,
            applied_revision=2,
        )
    )
    snapshot.skill_bindings[0].version_policy = "pinned"
    snapshot.skill_bindings[0].skill_version_id = 999
    snapshot.versions[0].unresolved_skill_refs = ["ref:2"]

    report = analyze_skill_expert_snapshot(snapshot)

    assert report.status == "blocked"
    by_code = {item.code: item for item in report.issues}
    assert by_code["WORKSPACE_RUNNABLE_EXPERT_NAME_CONFLICT"].entity_id in {
        "11",
        "12",
    }
    assert by_code["PROFILE_APPLIED_REVISION_AHEAD_OF_DESIRED"].entity_id == "12"
    assert by_code["PROFILE_SKILL_VERSION_MISSING"].related_ids == ["999"]
    assert by_code["EXPERT_SKILL_REFERENCE_UNRESOLVED"].related_ids == ["ref:2"]
    assert "PROFILE_SKILL_POLICY_REQUIRES_UNIFICATION" in by_code
