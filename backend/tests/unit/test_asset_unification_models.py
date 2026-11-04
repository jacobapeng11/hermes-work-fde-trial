from app.models import Base


TARGET_TABLES = {
    "personal_skill_category",
    "runtime_agent_profile_revision",
    "profile_skill_binding",
    "profile_mcp_binding",
    "profile_knowledge_binding",
    "profile_convergence_job",
    "profile_convergence_attempt",
    "mcp_version",
    "mcp_slug_lease",
    "runtime_mcp_registry_state",
    "runtime_mcp_registry_revision",
    "runtime_mcp_registry_entry",
    "runtime_mcp_registry_job",
    "asset_rollout_batch",
    "asset_rollout_target",
}


def _columns(table_name: str) -> set[str]:
    return {column.name for column in Base.metadata.tables[table_name].columns}


def test_asset_unification_target_tables_are_registered() -> None:
    assert TARGET_TABLES.issubset(Base.metadata.tables)


def test_runtime_agent_profile_has_one_desired_and_applied_head() -> None:
    assert {
        "desired_revision",
        "applied_revision",
        "desired_digest",
        "applied_digest",
    }.issubset(_columns("runtime_agent_profile"))

    revision = _columns("runtime_agent_profile_revision")
    assert {
        "runtime_agent_profile_id",
        "revision",
        "profile_digest",
        "skill_digest",
        "mcp_digest",
        "knowledge_digest",
        "snapshot",
        "source_type",
        "source_id",
    }.issubset(revision)


def test_profile_bindings_use_stable_assets_and_explicit_version_policy() -> None:
    assert {
        "runtime_agent_profile_id",
        "skill_id",
        "skill_version_id",
        "version_policy",
        "status",
    }.issubset(_columns("profile_skill_binding"))
    assert {
        "runtime_agent_profile_id",
        "mcp_id",
        "mcp_version_id",
        "toolset_key",
        "status",
    }.issubset(_columns("profile_mcp_binding"))
    assert {
        "runtime_agent_profile_id",
        "knowledge_base_id",
        "status",
    }.issubset(_columns("profile_knowledge_binding"))


def test_skill_catalog_uses_stable_public_and_personal_assets() -> None:
    assert {
        "asset_scope",
        "visibility",
        "current_version_id",
        "archived_at",
        "revoked_at",
    }.issubset(_columns("skill"))
    assert {
        "package_uri",
        "package_sha256",
        "published_at",
    }.issubset(_columns("skill_version"))


def test_mcp_credentials_are_confined_to_explicit_secret_refs_field() -> None:
    for table_name in (
        "mcp_version",
        "runtime_mcp_registry_revision",
        "runtime_mcp_registry_entry",
        "runtime_mcp_registry_job",
    ):
        columns = _columns(table_name)
        assert "secret" not in columns
        assert "credentials" not in columns
        assert "headers" not in columns
        assert "token" not in columns

    assert {
        "connection_config",
        "secret_refs",
        "tools_schema",
        "tools_schema_digest",
    }.issubset(_columns("mcp_version"))
    assert {
        "last_reload_at",
        "last_reload_affected_run_ids",
    }.issubset(_columns("runtime_mcp_registry_state"))


def test_mcp_slug_lease_separates_active_name_from_historical_asset() -> None:
    assert {"org_id", "namespace_key", "slug", "mcp_id"}.issubset(
        _columns("mcp_slug_lease")
    )
    mcp_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in Base.metadata.tables["mcp"].constraints
        if hasattr(constraint, "columns") and constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("org_id", "owner_user_id", "slug") not in mcp_unique_columns


def test_rollout_targets_are_profile_scoped_and_durable() -> None:
    assert {
        "asset_type",
        "asset_id",
        "action",
        "source_version_id",
        "target_version_id",
        "affected_user_count",
        "affected_profile_count",
        "status",
    }.issubset(_columns("asset_rollout_batch"))
    assert {
        "asset_rollout_batch_id",
        "user_id",
        "runtime_workspace_id",
        "runtime_agent_profile_id",
        "target_revision",
        "status",
        "last_error_code",
    }.issubset(_columns("asset_rollout_target"))


def test_asset_unification_tables_have_unique_index_names() -> None:
    for table_name in TARGET_TABLES:
        index_names = [index.name for index in Base.metadata.tables[table_name].indexes]
        assert len(index_names) == len(set(index_names)), table_name
