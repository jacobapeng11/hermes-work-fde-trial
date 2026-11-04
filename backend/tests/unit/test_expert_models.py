from app.models import (
    Base,
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    ExpertVersionKnowledgeSnapshot,
    InstalledExpertBinding,
    RuntimeAgentProfile,
)


def test_expert_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {
        "expert_package",
        "expert_package_version",
        "expert_version_artifact",
        "expert_version_knowledge_snapshot",
        "installed_expert_binding",
    }.issubset(table_names)


def test_expert_package_has_no_runtime_identity() -> None:
    columns = set(ExpertPackage.__table__.c.keys())

    assert "runtime_workspace_id" not in columns
    assert "runtime_id" not in columns
    assert "copaw_agent_id" not in columns


def test_expert_package_has_workspace_scoped_active_name_key() -> None:
    table = ExpertPackage.__table__
    constraints = {constraint.name for constraint in table.constraints}

    assert {"origin_runtime_workspace_id", "active_name_key"}.issubset(table.c.keys())
    assert "uq_expert_owner_workspace_active_name" in constraints


def test_expert_package_has_market_name_lease_and_orthogonal_execution_gate() -> None:
    table = ExpertPackage.__table__
    constraints = {constraint.name for constraint in table.constraints}

    assert {
        "market_name_key",
        "execution_status",
        "execution_status_changed_at",
        "execution_status_reason",
    }.issubset(table.c.keys())
    assert "uq_expert_market_name_lease" in constraints


def test_runtime_profile_has_shared_active_display_name_lease() -> None:
    table = RuntimeAgentProfile.__table__
    constraints = {constraint.name for constraint in table.constraints}

    assert "active_display_name_key" in table.c
    assert "uq_runtime_agent_profile_workspace_active_display_name" in constraints


def test_expert_version_contains_projection_inputs() -> None:
    columns = set(ExpertPackageVersion.__table__.c.keys())

    assert {"manifest", "skill_refs", "mcp_refs", "knowledge_refs", "model_preferences", "change_summary"}.issubset(
        columns
    )


def test_expert_release_artifacts_are_version_scoped() -> None:
    assert {
        "expert_package_version_id",
        "artifact_type",
        "payload",
        "storage_uri",
        "sha256",
    }.issubset(ExpertVersionArtifact.__table__.c.keys())
    assert {
        "expert_package_version_id",
        "source_knowledge_base_id",
        "snapshot_knowledge_base_id",
        "index_status",
    }.issubset(ExpertVersionKnowledgeSnapshot.__table__.c.keys())


def test_installed_expert_binds_to_current_runtime_workspace() -> None:
    table = InstalledExpertBinding.__table__

    assert {fk.target_fullname for fk in table.c.runtime_workspace_id.foreign_keys} == {
        "cloud_hermes_profile.id"
    }
    assert {fk.target_fullname for fk in table.c.expert_package_version_id.foreign_keys} == {
        "expert_package_version.id"
    }


def test_installed_expert_unique_per_workspace_package() -> None:
    constraints = {constraint.name for constraint in InstalledExpertBinding.__table__.constraints}

    assert "uq_installed_expert_workspace_package" in constraints
    assert "installation_epoch" in InstalledExpertBinding.__table__.c
