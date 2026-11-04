from platform_supervisor.hermes.capability_errors import (
    capability_error_from_tool_payload,
)


def test_knowledge_permission_failure_becomes_safe_recoverable_event() -> None:
    event = capability_error_from_tool_payload(
        {
            "name": "personal_knowledge_search",
            "tool_id": "call-1",
            "result": {
                "error": "KNOWLEDGE_BASE_NO_PERMISSION",
                "knowledge_base_id": 991,
                "path": "/home/hermes/private/index",
            },
        },
        run_id="run-1",
    )

    assert event is not None
    assert event.type == "capability.error"
    assert event.data.error_code == "KNOWLEDGE_BASE_NO_PERMISSION"
    assert event.data.capability_type == "knowledge_base"
    assert event.data.display_message == "资产访问权限不足"
    assert event.data.recoverable is True
    public = event.model_dump(mode="json")
    assert "991" not in str(public)
    assert "/home/hermes" not in str(public)


def test_unrelated_tool_failure_stays_an_ordinary_tool_error() -> None:
    assert capability_error_from_tool_payload(
        {"name": "write_file", "error": "permission denied"},
        run_id="run-1",
    ) is None


def test_user_mcp_authorization_failure_is_recoverable_and_redacted() -> None:
    event = capability_error_from_tool_payload(
        {
            "tool_call_id": "call-mcp-1",
            "error": {
                "code": "MCP_USER_AUTH_REQUIRED",
                "server_url": "https://private.example/mcp",
                "authorization": "Bearer secret",
            },
        },
        run_id="run-2",
    )

    assert event is not None
    assert event.data.error_code == "MCP_USER_AUTH_REQUIRED"
    assert event.data.capability_type == "mcp"
    assert event.data.recoverable is True
    public = str(event.model_dump(mode="json"))
    assert "private.example" not in public
    assert "Bearer secret" not in public
