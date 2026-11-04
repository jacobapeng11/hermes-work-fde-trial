from app.models import Base, KnowledgeBase, KnowledgeBaseFile


def test_personal_knowledge_models_keep_legacy_external_fields() -> None:
    assert KnowledgeBase.__table__.c.ragflow_id.type.length == 255
    assert KnowledgeBase.__table__.c.ragflow_id.name == "ragflow_id"
    assert "dataset_id" in KnowledgeBase.__mapper__.attrs
    assert KnowledgeBase.__table__.c.kind.default.arg == "personal"
    assert KnowledgeBase.__table__.c.provider.default.arg == "filebase"
    assert KnowledgeBase.__table__.c.provider_capabilities.nullable is True
    assert KnowledgeBase.__table__.c.provider_capabilities_synced_at.nullable is True
    assert "provider_capabilities" in KnowledgeBase.__mapper__.attrs
    assert "provider_capabilities_synced_at" in KnowledgeBase.__mapper__.attrs
    assert KnowledgeBaseFile.__table__.c.file_status.default.arg == 0
    assert KnowledgeBaseFile.__table__.c.is_deleted.default.arg == 0


def test_personal_knowledge_file_table_is_registered_with_compatibility_indexes() -> None:
    table = Base.metadata.tables["knowledge_base_file"]
    index_names = {index.name for index in table.indexes}
    foreign_keys = {
        foreign_key.target_fullname
        for foreign_key in table.c.kb_id.foreign_keys
    }

    assert {
        "filename",
        "oss_url",
        "oss_key",
        "provider_document_id",
        "provider_status",
        "provider_error",
        "provider_synced_at",
        "size_bytes",
        "mime_type",
        "file_status",
        "uploader_id",
        "updater_id",
        "is_deleted",
    }.issubset(table.c.keys())
    assert foreign_keys == {"knowledge_base.id"}
    assert table.c.kb_id.name == "kb_id"
    assert "knowledge_base_id" in KnowledgeBaseFile.__mapper__.attrs
    assert "ix_kb_file_org_kb_deleted" in index_names
    assert "ix_kb_file_kb_status" in index_names
    assert "uq_kb_file_provider_document" in index_names
    assert table.c.oss_url.nullable is True
    provider_document_index = next(
        index
        for index in table.indexes
        if index.name == "uq_kb_file_provider_document"
    )
    assert provider_document_index.unique is True
    assert [column.name for column in provider_document_index.columns] == [
        "provider_document_id"
    ]
