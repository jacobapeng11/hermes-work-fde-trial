from app.hermes_runtime.public_tool_payload import (
    build_public_tool_payload,
    sanitize_public_tool_history_message,
    sanitize_public_tool_text,
)


def test_public_tool_payload_whitelists_and_redacts_fields() -> None:
    payload = build_public_tool_payload(
        name="read_file",
        status="completed",
        call_id="call-1",
        has_stable_call_id=True,
        preview={"path": "/root/workspace/report.xlsx", "api_key": "sk-secret"},
        result={"rows": 3, "authorization": "Bearer abc.def.ghi"},
    )

    assert payload == {
        "name": "read_file",
        "status": "completed",
        "callId": "call-1",
        "hasStableCallId": True,
        "preview": '{\n  "path": "<path>/report.xlsx",\n  "api_key": "••••"\n}',
        "result": '{\n  "rows": 3,\n  "authorization": "••••"\n}',
    }


def test_public_tool_text_redacts_tokens_urls_paths_depth_and_length() -> None:
    nested = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "secret"}}}}}}}

    assert "[内容层级过深]" in (sanitize_public_tool_text(nested) or "")
    assert sanitize_public_tool_text("Bearer abc.def.ghi") == "••••"
    assert (
        sanitize_public_tool_text("http://127.0.0.1:8000/api?q=1&token=secret")
        == "<endpoint>/api?q=1&token=••••"
    )
    assert sanitize_public_tool_text(r"C:\Users\alice\report.xlsx") == "<path>/report.xlsx"
    assert sanitize_public_tool_text("/root/work/report.xlsx") == "<path>/report.xlsx"
    assert (
        sanitize_public_tool_text("https://user:pass@example.com/api?password=secret&q=1")
        == "https://example.com/api?password=••••&q=1"
    )
    assert sanitize_public_tool_text("http://localhost:bad/path") == "<endpoint>/path"
    assert sanitize_public_tool_text("http://example.com:bad/path") == "<endpoint>"
    value = sanitize_public_tool_text("x" * 12_100)
    assert value is not None
    assert value.endswith("[内容已截断]")
    assert len(value) <= 12_010


def test_public_tool_payload_normalizes_status_and_omits_empty_details() -> None:
    assert build_public_tool_payload(
        name="",
        status="queued",
        call_id="fallback",
        has_stable_call_id=False,
        preview="",
        result=None,
    ) == {
        "name": "tool",
        "status": "running",
        "callId": "fallback",
        "hasStableCallId": False,
    }


def test_public_tool_payload_keeps_failure_aliases_red() -> None:
    assert build_public_tool_payload(
        name="search_files",
        status="error",
        call_id="call-error",
        has_stable_call_id=True,
    )["status"] == "failed"
    assert sanitize_public_tool_history_message(
        {
            "role": "tool",
            "name": "search_files",
            "result": {"error": "permission denied"},
        },
        0,
    )["status"] == "failed"


def test_history_sanitizer_only_rewrites_tool_messages() -> None:
    user = {"role": "user", "content": "/root/user-visible-text"}
    tool = sanitize_public_tool_history_message(
        {
            "role": "tool",
            "name": "read_file",
            "content": {"path": "/root/a.txt"},
            "token": "drop-me",
        },
        4,
    )

    assert sanitize_public_tool_history_message(user, 0) == user
    assert tool == {
        "role": "tool",
        "name": "read_file",
        "status": "completed",
        "callId": "history-4",
        "hasStableCallId": False,
        "result": '{\n  "path": "<path>/a.txt"\n}',
    }
    assert "token" not in tool


def test_history_sanitizer_preserves_explicit_stable_call_metadata() -> None:
    tool = sanitize_public_tool_history_message(
        {
            "role": "tool",
            "tool_name": "search_files",
            "tool_call_id": "call-search",
            "has_stable_call_id": True,
            "status": "failed",
            "preview": "querying",
            "result": {"error": "denied"},
        },
        2,
    )

    assert tool == {
        "role": "tool",
        "name": "search_files",
        "status": "failed",
        "callId": "call-search",
        "hasStableCallId": True,
        "preview": "querying",
        "result": '{\n  "error": "denied"\n}',
    }
