from app.models import Base


FORBIDDEN_COLUMNS = {
    "copaw_agent_id",
    "copaw_session_id",
    "copaw_chat_id",
    "copaw_instance_url",
    "workspace_id",
    "worker_id",
    "pool_id",
    "container_id",
    "host",
    "port",
}


def test_initial_schema_contains_required_tables() -> None:
    table_names = set(Base.metadata.tables)

    assert {
        "organization",
        "user",
        "role",
        "permission",
        "cloud_hermes_profile",
        "hermes_runtime_binding",
        "runtime_session",
        "runtime_run",
        "expert_package",
        "expert_package_version",
        "installed_expert_binding",
        "skill",
        "skill_version",
        "mcp",
        "mcp_version",
        "profile_skill_binding",
        "profile_mcp_binding",
        "profile_knowledge_binding",
        "runtime_agent_profile_revision",
        "chat_file_artifact",
        "model_upgrade_log",
        "runtime_audit_log",
        "model_usage_log",
        "runtime_projection_receipt",
        "runtime_admission_audit",
    }.issubset(table_names)

    assert {
        "runtime_skill_binding",
        "runtime_mcp_binding",
        "runtime_kb_binding",
        "user_runtime_skill",
        "user_runtime_mcp",
        "public_skill_asset",
        "public_skill_version",
        "public_mcp_asset",
        "profile_asset_draft",
    }.isdisjoint(table_names)


def test_initial_schema_excludes_forbidden_runtime_columns() -> None:
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        forbidden = FORBIDDEN_COLUMNS.intersection(table.c.keys())
        offenders.extend(f"{table.name}.{column}" for column in sorted(forbidden))

    assert offenders == []


def test_key_unique_constraints_are_declared() -> None:
    constraints = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name
    }

    assert {
        "uq_user_org_code",
        "uq_provider_org_key",
        "uq_runtime_binding_workspace",
        "uq_runtime_run_runtime_id",
        "uq_installed_expert_workspace_package",
        "uq_profile_skill_binding_asset",
        "uq_profile_mcp_binding_asset",
        "uq_profile_knowledge_binding_asset",
        "uq_runtime_agent_profile_revision",
    }.issubset(constraints)

def test_stable_skill_schema_uses_one_catalog_identity() -> None:
    table = Base.metadata.tables["skill"]
    assert {"slug", "asset_scope", "visibility", "current_version_id"}.issubset(
        table.c.keys()
    )
    assert "public_skill_asset_id" not in table.c
