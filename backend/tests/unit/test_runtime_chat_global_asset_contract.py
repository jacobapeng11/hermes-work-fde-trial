from app.schemas.runtime_chat import RuntimeChatRequest


def test_chat_request_has_no_run_level_asset_selection_fields() -> None:
    assert "enabled_skill_ids" not in RuntimeChatRequest.model_fields
    assert "enabled_mcp_ids" not in RuntimeChatRequest.model_fields
    assert "enabled_knowledge_base_ids" not in RuntimeChatRequest.model_fields


def test_legacy_asset_selection_fields_are_not_retained() -> None:
    request = RuntimeChatRequest.model_validate(
        {
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-1-1",
            "message": "hello",
            "enabled_skill_ids": [99],
            "enabled_mcp_ids": [99],
            "enabled_knowledge_base_ids": [99],
        }
    )
    dumped = request.model_dump()
    assert "enabled_skill_ids" not in dumped
    assert "enabled_mcp_ids" not in dumped
    assert "enabled_knowledge_base_ids" not in dumped
