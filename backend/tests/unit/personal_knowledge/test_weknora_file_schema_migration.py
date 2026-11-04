from scripts.migrate_personal_knowledge_weknora_files import (
    PROVIDER_DOCUMENT_INDEX,
    planned_statements,
)


def test_schema_migration_plans_columns_nullability_and_unique_index() -> None:
    statements = planned_statements(columns={"oss_url": False}, indexes=set())

    assert len(statements) == 6
    assert any("provider_document_id VARCHAR(64) NULL" in value for value in statements)
    assert any("provider_status VARCHAR(32) NULL" in value for value in statements)
    assert any("provider_error TEXT NULL" in value for value in statements)
    assert any("provider_synced_at DATETIME NULL" in value for value in statements)
    assert any("MODIFY COLUMN oss_url VARCHAR(1000) NULL" in value for value in statements)
    assert any(PROVIDER_DOCUMENT_INDEX in value for value in statements)


def test_schema_migration_is_idempotent_when_contract_exists() -> None:
    statements = planned_statements(
        columns={
            "oss_url": True,
            "provider_document_id": True,
            "provider_status": True,
            "provider_error": True,
            "provider_synced_at": True,
        },
        indexes={PROVIDER_DOCUMENT_INDEX},
    )

    assert statements == []
