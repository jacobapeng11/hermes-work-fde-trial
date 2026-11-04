import hashlib
import logging
from io import BytesIO
import json
from pathlib import Path
import sys
import types
from urllib.error import HTTPError

import pytest

from hermes_enterprise_knowledge import context as context_module
from hermes_enterprise_knowledge.context import build_context
from hermes_enterprise_knowledge import platform_client
from hermes_enterprise_knowledge.platform_client import (
    PlatformClient,
    PlatformClientError,
    current_runtime_agent_profile_id,
)
from hermes_enterprise_knowledge.plugin import register
from hermes_enterprise_knowledge.tools import read_bundle_file
from hermes_enterprise_knowledge.tools import handlers


class FakeClient:
    def __init__(self, enabled):
        self.enabled = enabled

    def state(self, **_kwargs):
        return {"enabled": self.enabled, "mode": "full"}

    def post(self, _path, _payload, **_kwargs):
        return {"ok": True}

    def sql_query(self, _payload, **_kwargs):
        return {"ok": True}


class FailingClient:
    def state(self, **_kwargs):
        raise PlatformClientError("sensitive-platform-error")


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.tools = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_register_adds_hook_and_four_tools() -> None:
    context = FakeContext()
    register(context)
    assert "pre_llm_call" in context.hooks
    assert set(context.tools) == {"enterprise_knowledge_read", "enterprise_sql_context", "enterprise_sql_query", "enterprise_document_search"}


def _history_message(
    suffix: str,
    *,
    content="question",
    role="user",
    api_content=None,
):
    return {
        "role": role,
        "content": content,
        "api_content": (
            api_content
            if api_content is not None
            else f"{content}\n\n{suffix}"
        ),
    }


def test_context_is_fail_closed_and_injects_full_primer(tmp_path: Path) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    disabled = build_context(FakeClient(False), tmp_path)
    assert disabled == {"context": context_module.DISABLED_MARKER}

    result = build_context(FakeClient(True), tmp_path)
    context = result["context"]
    expected_sha = hashlib.sha256(b"complete primer").hexdigest()
    assert "complete primer" in context
    assert "enterprise_sql_query" in context
    assert f'primer_sha256="{expected_sha}"' in context
    assert context.endswith("\n</enterprise-knowledge>")


def test_context_keeps_enterprise_tool_calls_in_current_agent(tmp_path: Path) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")

    context = build_context(FakeClient(True), tmp_path)["context"]

    assert "execute these tools directly in the current agent" in context
    assert "must not delegate" in context
    assert "must not use terminal to read /knowledge" in context


def test_context_logs_enabled_stage_timings_without_primer_content(
    tmp_path: Path,
    caplog,
    monkeypatch,
) -> None:
    (tmp_path / "PRIMER.md").write_text(
        "sensitive-enterprise-primer",
        encoding="utf-8",
    )
    caplog.set_level(logging.INFO, logger="hermes_agent.runtime_timing")
    clock = iter((20.0, 20.01, 20.014, 20.016))
    monkeypatch.setattr(
        context_module.time,
        "perf_counter",
        lambda: next(clock),
    )

    result = build_context(
        FakeClient(True),
        tmp_path,
        session_id="session-1",
        turn_id="turn-1",
        sender_id="sensitive-wecom-identity",
        conversation_history=[
            _history_message(
                "sensitive-other-hook-context",
                content="sensitive-user-message",
            )
        ],
    )

    assert result is not None
    records = [
        record
        for record in caplog.records
        if record.name == "hermes_agent.runtime_timing"
    ]
    assert len(records) == 1
    payload = records[0].args
    assert payload["component"] == "enterprise_knowledge"
    assert payload["stage"] == "context_build_done"
    assert payload["status"] == "enabled"
    assert payload["context_action"] == "primer_injected"
    assert payload["history_state"] == "none"
    assert payload["history_format"] == "none"
    assert len(payload["primer_sha256_prefix"]) == 12
    assert payload["runtime_session_id"] == "session-1"
    assert payload["turn_id"] == "turn-1"
    assert payload["duration_ms"] == 2.0
    assert payload["durations_ms"] == {
        "total": 16.0,
        "state_http": 10.0,
        "primer_read": 4.0,
        "context_build": 2.0,
    }
    assert "sensitive-enterprise-primer" not in caplog.text
    assert "sensitive-other-hook-context" not in caplog.text
    assert "sensitive-user-message" not in caplog.text
    assert "sensitive-wecom-identity" not in caplog.text


@pytest.mark.parametrize(
    ("client", "status", "stage", "marker", "context_action"),
    [
        (
            FakeClient(False),
            "disabled",
            "state_http_done",
            context_module.DISABLED_MARKER,
            "disabled_marker",
        ),
        (
            FailingClient(),
            "error",
            "state_http",
            context_module.UNAVAILABLE_MARKER,
            "unavailable_marker",
        ),
    ],
)
def test_context_logs_disabled_and_state_error_outcomes(
    tmp_path: Path,
    client,
    status,
    stage,
    marker,
    context_action,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_agent.runtime_timing")

    assert build_context(
        client,
        tmp_path,
        session_id="session-1",
        turn_id="turn-1",
    ) == {"context": marker}

    records = [
        record
        for record in caplog.records
        if record.name == "hermes_agent.runtime_timing"
    ]
    assert len(records) == 1
    payload = records[0].args
    assert payload["status"] == status
    assert payload["stage"] == stage
    assert payload["context_action"] == context_action
    assert payload["history_state"] == "none"
    assert payload["history_format"] == "none"
    if status == "error":
        assert payload["error_type"] == "PlatformClientError"


def test_context_logs_primer_read_error_without_changing_fallback(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_agent.runtime_timing")

    assert build_context(
        FakeClient(True),
        tmp_path,
        session_id="session-1",
        turn_id="turn-1",
    ) == {"context": context_module.UNAVAILABLE_MARKER}

    records = [
        record
        for record in caplog.records
        if record.name == "hermes_agent.runtime_timing"
    ]
    assert len(records) == 1
    payload = records[0].args
    assert payload["status"] == "error"
    assert payload["stage"] == "primer_read"
    assert payload["context_action"] == "unavailable_marker"
    assert payload["error_type"] == "FileNotFoundError"


def test_context_accepts_hermes_pre_llm_hook_metadata(tmp_path: Path) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")

    result = build_context(
        FakeClient(True),
        tmp_path,
        session_id="runtime-session",
        task_id="default",
        turn_id="turn-1",
        user_message="深圳区本周有多少工作量？",
    )

    assert "complete primer" in result["context"]


def test_context_deduplicates_same_primer_from_user_api_suffix(
    tmp_path: Path,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    first = build_context(FakeClient(True), tmp_path)["context"]

    assert build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(first)],
    ) is None


def test_context_finds_marker_before_newer_user_without_enterprise_suffix(
    tmp_path: Path,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    first = build_context(FakeClient(True), tmp_path)["context"]
    history = [
        _history_message(first, content="first question"),
        _history_message(
            "other hook context",
            content="follow-up question",
        ),
    ]

    assert build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=history,
    ) is None


def test_context_injects_new_primer_when_sha_changes(tmp_path: Path) -> None:
    primer = tmp_path / "PRIMER.md"
    primer.write_text("old primer", encoding="utf-8")
    old_context = build_context(FakeClient(True), tmp_path)["context"]
    primer.write_text("new primer", encoding="utf-8")

    result = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(old_context)],
    )["context"]

    assert "new primer" in result
    assert result.count("<enterprise-knowledge>") == 1
    assert hashlib.sha256(b"new primer").hexdigest() in result


def test_context_emits_state_markers_only_on_transitions(
    tmp_path: Path,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    enabled = build_context(FakeClient(True), tmp_path)["context"]

    disabled = build_context(
        FakeClient(False),
        tmp_path,
        conversation_history=[_history_message(enabled)],
    )
    assert disabled == {"context": context_module.DISABLED_MARKER}
    assert build_context(
        FakeClient(False),
        tmp_path,
        conversation_history=[_history_message(disabled["context"])],
    ) is None

    reenabled = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(disabled["context"])],
    )
    assert "complete primer" in reenabled["context"]


def test_state_failure_emits_unavailable_once_and_recovers(
    tmp_path: Path,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    unavailable = build_context(FailingClient(), tmp_path)
    assert unavailable == {"context": context_module.UNAVAILABLE_MARKER}

    history = [_history_message(unavailable["context"])]
    assert build_context(
        FailingClient(),
        tmp_path,
        conversation_history=history,
    ) is None

    recovered = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=history,
    )
    assert "complete primer" in recovered["context"]


def test_primer_failure_emits_unavailable_once(tmp_path: Path) -> None:
    unavailable = build_context(FakeClient(True), tmp_path)
    assert unavailable == {"context": context_module.UNAVAILABLE_MARKER}

    assert build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(unavailable["context"])],
    ) is None


@pytest.mark.parametrize(
    "history_factory",
    [
        lambda block: [
            {
                "role": "user",
                "content": block,
                "api_content": block,
            }
        ],
        lambda block: [_history_message(block, role="assistant")],
        lambda block: [
            _history_message(
                block,
                content="question",
                api_content=f"wrong-prefix\n\n{block}",
            )
        ],
        lambda block: [
            {
                "role": "user",
                "content": ["multimodal"],
                "api_content": f"question\n\n{block}",
            }
        ],
        lambda block: [
            {
                "role": "user",
                "content": "question",
                "api_content": ["multimodal", block],
            }
        ],
    ],
)
def test_context_ignores_untrusted_or_non_string_history(
    tmp_path: Path,
    history_factory,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    block = build_context(FakeClient(True), tmp_path)["context"]

    result = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=history_factory(block),
    )

    assert result is not None
    assert "complete primer" in result["context"]


def test_context_recognizes_only_exact_current_legacy_block(
    tmp_path: Path,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    legacy = (
        f"{context_module.CONTEXT_HEADER}complete primer"
        "\n</enterprise-knowledge>"
    )

    assert build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(legacy)],
    ) is None

    stale_legacy = legacy.replace("complete primer", "stale primer")
    result = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(stale_legacy)],
    )
    assert result is not None
    assert "complete primer" in result["context"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda block: block.replace('schema="1"', 'schema="2"', 1),
        lambda block: block.replace(
            'status="enabled"',
            'status="illegal"',
            1,
        ),
        lambda block: block.replace(
            hashlib.sha256(b"complete primer").hexdigest(),
            "g" * 64,
            1,
        ),
        lambda block: block.replace("complete primer", "tampered primer", 1),
        lambda block: block.removesuffix("</enterprise-knowledge>"),
    ],
)
def test_context_ignores_malformed_or_incomplete_enabled_blocks(
    tmp_path: Path,
    mutation,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    valid = build_context(FakeClient(True), tmp_path)["context"]

    result = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(mutation(valid))],
    )

    assert result is not None
    assert "complete primer" in result["context"]


@pytest.mark.parametrize(
    ("suffix_factory", "expected_deduplicated"),
    [
        (
            lambda enabled: (
                f"{enabled}\n\n{context_module.DISABLED_MARKER}"
            ),
            False,
        ),
        (
            lambda enabled: (
                f"{context_module.DISABLED_MARKER}\n\n{enabled}"
            ),
            True,
        ),
    ],
)
def test_context_uses_latest_valid_state_within_suffix(
    tmp_path: Path,
    suffix_factory,
    expected_deduplicated,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    enabled = build_context(FakeClient(True), tmp_path)["context"]

    result = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[
            _history_message(suffix_factory(enabled)),
        ],
    )

    assert (result is None) is expected_deduplicated


def test_context_uses_latest_valid_state_across_messages(
    tmp_path: Path,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    enabled = build_context(FakeClient(True), tmp_path)["context"]
    history = [
        _history_message(enabled, content="first"),
        _history_message(
            context_module.DISABLED_MARKER,
            content="second",
        ),
    ]

    result = build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=history,
    )

    assert result is not None
    assert "complete primer" in result["context"]


def test_context_spill_limit_fails_closed_once(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    monkeypatch.setattr(
        context_module,
        "_hook_output_spill_config",
        lambda: {
            "enabled": True,
            "max_chars": len(context_module.UNAVAILABLE_MARKER),
        },
    )
    caplog.set_level(logging.INFO, logger="hermes_agent.runtime_timing")

    first = build_context(FakeClient(True), tmp_path)
    assert first == {"context": context_module.UNAVAILABLE_MARKER}
    assert "spill_blocked" in caplog.text

    assert build_context(
        FakeClient(True),
        tmp_path,
        conversation_history=[_history_message(first["context"])],
    ) is None


def test_context_ignores_spill_limit_when_spill_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "PRIMER.md").write_text("complete primer", encoding="utf-8")
    monkeypatch.setattr(
        context_module,
        "_hook_output_spill_config",
        lambda: {"enabled": False, "max_chars": 1},
    )

    result = build_context(FakeClient(True), tmp_path)

    assert "complete primer" in result["context"]


def test_context_retains_controlled_header_and_exact_primer_text(
    tmp_path: Path,
) -> None:
    primer = "first line\n中文内容\n"
    (tmp_path / "PRIMER.md").write_text(primer, encoding="utf-8")

    context = build_context(FakeClient(True), tmp_path)["context"]

    assert (
        "The enterprise knowledge capability is enabled for this Runtime."
        in context
    )
    assert "execute these tools directly in the current agent" in context
    assert "must not use terminal to read /knowledge" in context
    assert context.endswith(f"{primer}\n</enterprise-knowledge>")
    assert hashlib.sha256(primer.encode("utf-8")).hexdigest() in context


def test_bundle_read_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "allowed.md").write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (bundle / "escape.md").symlink_to(outside)
    assert read_bundle_file("allowed.md", root=bundle) == "ok"
    with pytest.raises(ValueError):
        read_bundle_file("../outside.md", root=bundle)
    with pytest.raises(ValueError):
        read_bundle_file("escape.md", root=bundle)


def test_registered_bundle_tool_is_denied_when_disabled() -> None:
    result = handlers(FakeClient(False))["enterprise_knowledge_read"]({"relative_path": "PRIMER.md"})
    assert "disabled" in result


def _install_profile_modules(monkeypatch, *, profile_name: str) -> None:
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"platform": {}}
    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.get_active_profile_name = lambda: profile_name
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles)


def test_default_profile_without_identity_uses_backend_compatibility(monkeypatch) -> None:
    _install_profile_modules(monkeypatch, profile_name="default")

    assert current_runtime_agent_profile_id() is None


def test_named_profile_without_identity_fails_closed(monkeypatch) -> None:
    _install_profile_modules(monkeypatch, profile_name="expert-7")

    with pytest.raises(PlatformClientError, match="identity is missing"):
        current_runtime_agent_profile_id()


def test_explicit_profile_identity_is_sent_to_platform(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"enabled": true}'

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    result = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=10,
        token="token",
        runtime_agent_profile_id=7,
    ).state()

    assert result == {"enabled": True}
    assert captured["url"].endswith(
        "/runtime-knowledge/10/state?runtime_agent_profile_id=7"
    )


def test_sql_query_uses_independent_timeout(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout):
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    client = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=10,
        token="token",
    )

    assert client.sql_query({"sql": "SELECT 1"}) == {"ok": True}
    assert captured["timeout"] == platform_client._SQL_QUERY_TIMEOUT_SECONDS


def test_sql_tool_bounds_page_size_and_large_results() -> None:
    captured = {}

    class SqlResultClient:
        def sql_query(self, payload, **_kwargs):
            captured.update(payload)
            return {
                "row_count": 100,
                "total": 200,
                "page": 1,
                "page_size": 100,
                "total_pages": 2,
                "data": [
                    {"value": f"row-{index}-" + "x" * 500}
                    for index in range(100)
                ],
            }

    result = json.loads(
        handlers(SqlResultClient())["enterprise_sql_query"](
            {"sql": "SELECT 1", "page": 1, "page_size": 500},
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="tool-1",
        )
    )

    assert captured["page_size"] == 100
    assert result["truncated"] is True
    assert result["original_row_count"] == 100
    assert 0 < result["row_count"] < 100
    assert result["next_page"] == 2
    assert len(json.dumps(result, ensure_ascii=False)) <= 16_500


def test_application_agent_identity_uses_active_runtime_session_header(
    monkeypatch,
) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"enabled": true}'

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["session_id"] = request.headers.get(
            "X-hermes-runtime-session-id"
        )
        captured["turn_id"] = request.headers.get("X-hermes-turn-id")
        captured["tool_call_id"] = request.headers.get(
            "X-hermes-tool-call-id"
        )
        captured["knowledge_request_id"] = request.headers.get(
            "X-hermes-knowledge-request-id"
        )
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    result = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=31,
        application_agent_id=31,
        token="token",
    ).state(
        runtime_session_id="app-31-session",
        turn_id="turn-7",
        tool_call_id="tool-9",
    )

    assert result == {"enabled": True}
    assert captured["url"].endswith(
        "/application-agent-runtime-knowledge/31/state"
    )
    assert captured["session_id"] == "app-31-session"
    assert captured["turn_id"] == "turn-7"
    assert captured["tool_call_id"] == "tool-9"
    assert captured["knowledge_request_id"] == "tool-9"


def test_platform_client_preserves_structured_sql_error(monkeypatch) -> None:
    def fake_urlopen(request, *, timeout):
        raise HTTPError(
            request.full_url,
            400,
            "bad request",
            None,
            BytesIO(
                json.dumps(
                    {
                        "error": {
                            "code": "sql_subquery_not_supported",
                            "message": "当前权限网关不支持子查询。",
                            "retryable": True,
                            "hint": "改为单层 SELECT。",
                        }
                    }
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    client = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=10,
        token="token",
        runtime_agent_profile_id=7,
    )

    with pytest.raises(PlatformClientError) as raised:
        client.post("/sql/query", {"sql": "SELECT 1"})

    assert raised.value.as_error() == {
        "code": "sql_subquery_not_supported",
        "message": "当前权限网关不支持子查询。",
        "retryable": True,
        "hint": "改为单层 SELECT。",
    }


def test_tool_handler_returns_structured_platform_error() -> None:
    class StructuredFailureClient(FakeClient):
        def sql_query(self, _payload, **_kwargs):
            raise PlatformClientError(
                "必须查询已配置业务表。",
                code="sql_requires_configured_table",
                retryable=True,
                hint="先调用 enterprise_sql_context。",
            )

    payload = json.loads(
        handlers(StructuredFailureClient(True))["enterprise_sql_query"](
            {"sql": "SELECT 1"}
        )
    )

    assert payload == {
        "error": {
            "code": "sql_requires_configured_table",
            "message": "必须查询已配置业务表。",
            "retryable": True,
            "hint": "先调用 enterprise_sql_context。",
        }
    }


def test_prepared_state_caches_only_hook_reads(monkeypatch) -> None:
    client = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=9123,
        token="unique-enterprise-cache-token",
        runtime_agent_profile_id=77,
    )
    calls = []

    def request(method, path, payload=None, **context):
        calls.append({
            "method": method,
            "path": path,
            "payload": payload,
            **context,
        })
        return {"enabled": True, "mode": "full"}

    monkeypatch.setattr(client, "_request", request)

    first = client.prepared_state(runtime_session_id="cache-session")
    second = client.prepared_state(runtime_session_id="cache-session")
    live_tool_check = client.state(runtime_session_id="cache-session")

    assert first == second == live_tool_check
    assert len(calls) == 2
    assert calls[0]["prepared_context"] is True
    assert "prepared_context" not in calls[1]
