from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.models import Base


ROOT = Path(__file__).resolve().parents[4]


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_conversation_file_table_uses_conversation_identity() -> None:
    table = Base.metadata.tables["application_agent_conversation_file_artifact"]

    assert set(table.c.keys()) == {
        "id",
        "org_id",
        "application_agent_id",
        "conversation_id",
        "application_agent_run_id",
        "relative_path",
        "relative_path_sha256",
        "mime_type",
        "size_bytes",
        "source_type",
        "status",
        "created_at",
        "updated_at",
    }
    assert {fk.target_fullname for fk in table.c.application_agent_id.foreign_keys} == {
        "application_agent.id"
    }
    assert {fk.target_fullname for fk in table.c.conversation_id.foreign_keys} == {
        "application_agent_conversation.id"
    }
    assert {
        fk.target_fullname
        for fk in table.c.application_agent_run_id.foreign_keys
    } == {"application_agent_run.id"}
    assert "runtime_workspace_id" not in table.c
    assert "user_id" not in table.c

    constraint_columns = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.name
    }
    assert constraint_columns["uq_application_agent_conversation_file_path"] == (
        "conversation_id",
        "relative_path_sha256",
    )


def test_conversation_file_schema_is_registered_in_current_schema() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS application_agent_conversation_file_artifact" in content
    assert "uq_application_agent_conversation_file_path" in content
    assert "ix_application_agent_conversation_file_list" in content
    assert "ix_application_agent_conversation_file_run" in content


def test_file_download_settings_are_registered_and_production_safe() -> None:
    local = _settings()

    assert local.FILE_DOWNLOAD_URL_TTL_SECONDS == 86_400
    assert local.FILE_DOWNLOAD_SIGNING_SECRET == ""

    production = {
        "APP_ENV": "production",
        "HERMES_AUTH_MODE": "enterprise_idpAES",
        "ENTERPRISE_IDP_AES_KEY": "0123456789abcdef",
        "ENTERPRISE_IDP_AES_IV": "fedcba9876543210",
        "JWT_SECRET_KEY": "production-secret",
        "AES_ENCRYPTION_KEY": "production-managed-aes-key",
        "RUNTIME_CHAT_MODE": "real",
    }
    with pytest.raises(ValueError, match="FILE_DOWNLOAD_SIGNING_SECRET"):
        _settings(**production)

    configured = _settings(
        **production,
        FILE_DOWNLOAD_SIGNING_SECRET="managed-openapi-file-signing-secret",
    )
    assert configured.FILE_DOWNLOAD_SIGNING_SECRET == (
        "managed-openapi-file-signing-secret"
    )
