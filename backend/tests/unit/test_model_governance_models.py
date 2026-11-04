from app.models import Base, ModelEntry, ModelProvider, ModelUpgradeLog


def test_model_governance_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {"model_provider", "model_entry", "model_upgrade_log"}.issubset(table_names)


def test_provider_and_entry_uniqueness_is_org_scoped() -> None:
    provider_constraints = {constraint.name for constraint in ModelProvider.__table__.constraints}
    entry_constraints = {constraint.name for constraint in ModelEntry.__table__.constraints}

    assert "uq_provider_org_key" in provider_constraints
    assert "uq_model_entry_provider_key" in entry_constraints
    assert "org_id" in ModelEntry.__table__.c


def test_provider_key_stays_platform_side() -> None:
    provider_columns = set(ModelProvider.__table__.c.keys())
    entry_columns = set(ModelEntry.__table__.c.keys())

    assert "api_key_encrypted" in provider_columns
    assert "api_key_encrypted" not in entry_columns
    assert "upstream_api_key" not in entry_columns


def test_model_upgrade_log_is_separate_from_plaza_upgrade_log() -> None:
    columns = ModelUpgradeLog.__table__.c

    assert "source_model_entry_id" in columns
    assert "target_model_entry_id" in columns
    assert "affected_runtime_workspace_count" in columns


def test_model_catalog_has_publish_version_and_hash_fields() -> None:
    assert "catalog_version" in ModelProvider.__table__.c
    assert "catalog_version" in ModelEntry.__table__.c
    assert "catalog_version" in ModelUpgradeLog.__table__.c
    assert "previous_catalog_version" in ModelUpgradeLog.__table__.c
    assert "config_hash" in ModelUpgradeLog.__table__.c
