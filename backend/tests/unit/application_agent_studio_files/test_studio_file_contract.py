from pathlib import Path

from app.models import Base


ROOT = Path(__file__).resolve().parents[4]


def test_studio_file_table_uses_application_agent_identities() -> None:
    table = Base.metadata.tables["application_agent_studio_file_artifact"]

    assert set(table.c.keys()) == {
        "id",
        "studio_session_id",
        "application_agent_run_id",
        "relative_path",
        "mime_type",
        "size_bytes",
        "sha256_digest",
        "source_type",
        "status",
        "created_at",
        "updated_at",
    }
    assert {fk.target_fullname for fk in table.c.studio_session_id.foreign_keys} == {
        "application_agent_studio_session.id"
    }
    assert {fk.target_fullname for fk in table.c.application_agent_run_id.foreign_keys} == {
        "application_agent_run.id"
    }
    assert {"org_id", "user_id", "application_agent_id", "visibility"}.isdisjoint(table.c.keys())
    path_index = next(
        index
        for index in table.indexes
        if index.name == "ix_application_agent_studio_file_path"
    )
    assert path_index.dialect_options["mysql"]["length"]["relative_path"] == 766


def test_studio_file_contract_is_registered_in_current_schema() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS application_agent_studio_file_artifact" in content
    assert "fk_application_agent_studio_file_session" in content
    assert "fk_application_agent_studio_file_run" in content
    assert "ck_application_agent_studio_file_status" in content
    assert "ck_application_agent_studio_file_source" in content
    assert "relative_path(766)" in content
    assert "ADD INDEX ix_application_agent_studio_file_session" in content
