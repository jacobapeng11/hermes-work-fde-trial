from __future__ import annotations

import pytest

from app.services.chat_files import ChatFileRecord
from app.services.office_preview import (
    OfficePreviewError,
    build_office_config,
    create_office_file_token,
    create_personal_knowledge_office_token,
    decode_office_file_token,
    decode_personal_knowledge_office_token,
    office_file_type,
)
from app.services.runtime_workspace import Principal


def _record(display_name: str = "quarterly-report.xlsx") -> ChatFileRecord:
    return ChatFileRecord(
        file_id=17,
        runtime_workspace_id=4,
        runtime_session_id="rs-4-1",
        runtime_run_id=None,
        relative_path=f"workspace/sessions/rs-4-1/{display_name}",
        display_name=display_name,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=4096,
        source_type="upload",
        created_at="2026-07-15T10:00:00Z",
    )


def test_office_preview_config_is_signed_and_strictly_read_only() -> None:
    config = build_office_config(
        record=_record(),
        content_url="http://platform.internal/api/v1/user/chat-files/17/office-content?token=file-token",
        secret="office-secret",
        user_id=8,
        ttl_seconds=300,
        now=1000,
    )

    assert config["documentType"] == "cell"
    assert config["document"]["fileType"] == "xlsx"
    assert config["document"]["permissions"]["edit"] is False
    assert config["editorConfig"]["mode"] == "view"
    assert config["editorConfig"]["coEditing"] == {"mode": "strict", "change": False}
    assert config["token"].count(".") == 2


def test_office_file_token_binds_principal_and_file_and_expires() -> None:
    principal = Principal(org_id=3, user_id=8)
    token = create_office_file_token(
        principal=principal,
        file_id=17,
        secret="office-secret",
        ttl_seconds=300,
        now=1000,
    )

    decoded_principal, file_id = decode_office_file_token(token, secret="office-secret", now=1200)
    assert decoded_principal == principal
    assert file_id == 17
    with pytest.raises(OfficePreviewError, match="expired"):
        decode_office_file_token(token, secret="office-secret", now=1301)


def test_office_preview_rejects_non_office_extensions() -> None:
    assert office_file_type("slides.pptx") == "pptx"
    assert office_file_type("legacy.doc") == "doc"
    with pytest.raises(OfficePreviewError, match="not supported"):
        office_file_type("archive.zip")


def test_personal_knowledge_office_token_is_resource_scoped() -> None:
    principal = Principal(org_id=3, user_id=8)
    token = create_personal_knowledge_office_token(
        principal=principal,
        knowledge_base_id=9,
        file_id=18,
        secret="office-secret",
        ttl_seconds=300,
        now=1000,
    )

    assert decode_personal_knowledge_office_token(
        token,
        secret="office-secret",
        now=1200,
    ) == (principal, 9, 18)
    chat_token = create_office_file_token(
        principal=principal,
        file_id=18,
        secret="office-secret",
        ttl_seconds=300,
        now=1000,
    )
    with pytest.raises(OfficePreviewError, match="invalid"):
        decode_personal_knowledge_office_token(chat_token, secret="office-secret", now=1200)


def test_office_preview_can_disable_download_copy_and_print() -> None:
    config = build_office_config(
        record=_record(),
        content_url="http://platform.internal/content",
        secret="office-secret",
        user_id=8,
        ttl_seconds=300,
        allow_download=False,
        now=1000,
    )

    permissions = config["document"]["permissions"]
    assert permissions["copy"] is False
    assert permissions["download"] is False
    assert permissions["print"] is False
