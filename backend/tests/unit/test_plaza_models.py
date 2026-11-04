from app.models import Base, PlazaTemplate, TemplateReview, TemplateVersion


def test_plaza_review_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {"plaza_template", "template_version", "template_review"}.issubset(table_names)


def test_template_version_is_config_snapshot_not_runtime_instance() -> None:
    columns = set(TemplateVersion.__table__.c.keys())

    assert "config_snapshot" in columns
    assert "skill_snapshots" in columns
    assert "mcp_snapshots" in columns
    assert "knowledge_snapshots" in columns
    assert "runtime_id" not in columns
    assert "runtime_token" not in columns
    assert "api_key" not in columns
    assert "session_id" not in columns


def test_template_review_links_to_expert_package() -> None:
    table = TemplateReview.__table__

    assert {fk.target_fullname for fk in table.c.expert_package_id.foreign_keys} == {
        "expert_package.id"
    }
    assert {fk.target_fullname for fk in table.c.submitter_user_id.foreign_keys} == {
        "user.id"
    }


def test_template_uniqueness_is_org_scoped() -> None:
    constraints = {constraint.name for constraint in PlazaTemplate.__table__.constraints}
    version_constraints = {constraint.name for constraint in TemplateVersion.__table__.constraints}

    assert "uq_plaza_template_org_slug" in constraints
    assert "uq_template_version" in version_constraints
