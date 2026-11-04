from app.models import Base
from scripts.migrate_application_agent_platform import (
    APPLICATION_AGENT_TABLES,
    schema_errors,
)


def test_application_agent_migration_is_scoped_and_detects_missing_tables() -> None:
    assert set(APPLICATION_AGENT_TABLES).issubset(Base.metadata.tables)
    assert {
        "im_channel",
        "organization_wecom_identity",
        "application_agent_wecom_user_access",
        "wecom_identity_binding_ticket",
    }.issubset(APPLICATION_AGENT_TABLES)
    errors = schema_errors({})
    assert len(errors) == len(APPLICATION_AGENT_TABLES)
    assert errors[0].startswith("missing table:")


def test_application_agent_migration_accepts_matching_columns() -> None:
    actual = {
        table_name: {
            column.name
            for column in Base.metadata.tables[table_name].columns
        }
        for table_name in APPLICATION_AGENT_TABLES
    }
    assert schema_errors(actual) == []


def test_application_agent_table_has_no_description_column() -> None:
    columns = Base.metadata.tables["application_agent"].columns

    assert "description" not in columns
