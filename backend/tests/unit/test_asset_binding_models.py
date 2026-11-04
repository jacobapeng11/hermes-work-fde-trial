from app.models import (
    Base,
    KnowledgeBase,
    Mcp,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    Skill,
    SkillVersion,
)


def test_asset_and_binding_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {
        "skill",
        "skill_version",
        "mcp",
        "knowledge_base",
        "profile_skill_binding",
        "profile_mcp_binding",
        "profile_knowledge_binding",
    }.issubset(table_names)


def test_runtime_asset_bindings_target_workspace_and_agent_profile() -> None:
    for table in (
        ProfileSkillBinding.__table__,
        ProfileMcpBinding.__table__,
        ProfileKnowledgeBinding.__table__,
    ):
        targets = {fk.target_fullname for fk in table.c.runtime_workspace_id.foreign_keys}
        assert targets == {"cloud_hermes_profile.id"}
        profile_targets = {
            fk.target_fullname
            for fk in table.c.runtime_agent_profile_id.foreign_keys
        }
        assert profile_targets == {"runtime_agent_profile.id"}
        assert table.c.runtime_agent_profile_id.nullable is False


def test_skill_mcp_kb_are_assets_not_runtimes() -> None:
    for table in (Skill.__table__, Mcp.__table__, KnowledgeBase.__table__):
        columns = set(table.c.keys())
        assert "runtime_workspace_id" not in columns
        assert "runtime_id" not in columns
        assert "copaw_agent_id" not in columns


def test_binding_uniqueness_constraints_exist() -> None:
    constraint_names = {
        constraint.name
        for table in (
            ProfileSkillBinding.__table__,
            ProfileMcpBinding.__table__,
            ProfileKnowledgeBinding.__table__,
        )
        for constraint in table.constraints
    }

    assert "uq_profile_skill_binding_asset" in constraint_names
    assert "uq_profile_mcp_binding_asset" in constraint_names
    assert "uq_profile_knowledge_binding_asset" in constraint_names


def test_skill_version_is_versioned_asset() -> None:
    constraints = {constraint.name for constraint in SkillVersion.__table__.constraints}

    assert "uq_skill_version" in constraints
    assert "manifest" in SkillVersion.__table__.c


def test_legacy_asset_review_table_is_not_registered() -> None:
    assert "asset_review" not in Base.metadata.tables
