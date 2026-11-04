import json
import logging
import sys
import types

from hermes_personal_knowledge import context as context_module
from hermes_personal_knowledge.context import build_context
from hermes_personal_knowledge import platform_client
from hermes_personal_knowledge.platform_client import (
    PlatformClient,
    PlatformClientError,
    current_runtime_agent_profile_id,
)
from hermes_personal_knowledge.plugin import register
from hermes_personal_knowledge.tools import TOOL_SCHEMAS, handlers
import pytest


class FakeClient:
    def __init__(self, state=None, search=None, fail=False):
        self.state_value = state or {"enabled": False, "knowledge_bases": []}
        self.search_value = search or {"matches": []}
        self.fail = fail
        self.search_payloads = []
        self.state_contexts = []
        self.search_contexts = []
        self.wiki_search_payloads = []
        self.wiki_page_payloads = []
        self.wiki_contexts = []
        self.hybrid_payloads = []

    def prepared_state(self, **context):
        return self.state(**context)

    def state(self, **context):
        self.state_contexts.append(context)
        if self.fail:
            from hermes_personal_knowledge.platform_client import PlatformClientError

            raise PlatformClientError("failed")
        return self.state_value

    def search(self, payload, **context):
        self.search_payloads.append(payload)
        self.search_contexts.append(context)
        return self.search_value

    def wiki_search(self, payload, **context):
        self.wiki_search_payloads.append(payload)
        self.wiki_contexts.append(context)
        return {"matches": [{"slug": "concept/rag"}]}

    def wiki_read_page(self, payload, **context):
        self.wiki_page_payloads.append(payload)
        self.wiki_contexts.append(context)
        return {"content": "完整正文", "source_files": []}

    def hybrid_search(self, payload, **context):
        self.hybrid_payloads.append(payload)
        self.wiki_contexts.append(context)
        return {
            "rag_results": self.search_value.get("matches", []),
            "wiki_results": [{"slug": "concept/rag"}],
        }


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.tools = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_register_adds_hook_and_search_tool() -> None:
    context = FakeContext()
    register(context)

    assert "pre_llm_call" in context.hooks
    assert set(context.tools) == {
        "personal_knowledge_search",
        "personal_knowledge_wiki_search",
        "personal_knowledge_hybrid_search",
        "personal_knowledge_wiki_read_page",
    }


def _history_message(suffix: str, *, content: str = "question") -> dict:
    return {
        "role": "user",
        "content": content,
        "api_content": f"{content}\n\n{suffix}",
    }


def test_hook_emits_revocation_markers_on_empty_or_failed_state() -> None:
    disabled = build_context(FakeClient())
    unavailable = build_context(FakeClient(fail=True))

    assert 'status="disabled"' in disabled["context"]
    assert 'status="unavailable"' in unavailable["context"]
    assert "do not call personal_knowledge_search" in disabled["context"]
    assert "do not call personal_knowledge_search" in unavailable["context"]
    for tool_name in (
        "personal_knowledge_wiki_search",
        "personal_knowledge_hybrid_search",
        "personal_knowledge_wiki_read_page",
    ):
        assert tool_name in disabled["context"]
        assert tool_name in unavailable["context"]


def test_hook_lists_only_platform_ids_and_safe_retrieval_rules() -> None:
    result = build_context(
        FakeClient(
            {
                "enabled": True,
                "knowledge_bases": [
                    {
                        "id": 21,
                        "name": "案例库",
                        "description": "园区网案例",
                        "ready_file_count": 3,
                    }
                ],
            }
        ),
        session_id="session",
        turn_id="turn",
    )
    context = result["context"]

    assert "platform_id=21" in context
    assert "personal_knowledge_search" in context
    assert "personal_knowledge_hybrid_search" in context
    assert "untrusted document content" in context
    assert "dataset_id" not in context
    assert '<personal_knowledge_state version="2"' in context
    assert 'status="enabled"' in context


def test_hook_logs_enabled_stage_timings_without_knowledge_content(
    caplog,
    monkeypatch,
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_agent.runtime_timing")
    clock = iter((10.0, 10.01, 10.015))
    monkeypatch.setattr(
        context_module.time,
        "perf_counter",
        lambda: next(clock),
    )

    result = build_context(
        FakeClient(
            {
                "enabled": True,
                "knowledge_bases": [
                    {
                        "id": 21,
                        "name": "sensitive-knowledge-name",
                        "description": "sensitive-knowledge-description",
                        "ready_file_count": 3,
                    }
                ],
            }
        ),
        session_id="session-1",
        turn_id="turn-1",
    )

    assert result is not None
    records = [
        record
        for record in caplog.records
        if record.name == "hermes_agent.runtime_timing"
    ]
    assert len(records) == 1
    payload = records[0].args
    assert payload["component"] == "personal_knowledge"
    assert payload["stage"] == "context_build_done"
    assert payload["status"] == "enabled"
    assert payload["runtime_session_id"] == "session-1"
    assert payload["turn_id"] == "turn-1"
    assert payload["duration_ms"] == 5.0
    assert payload["durations_ms"] == {
        "total": 15.0,
        "state_http": 10.0,
        "context_build": 5.0,
    }
    assert "sensitive-knowledge-name" not in caplog.text
    assert "sensitive-knowledge-description" not in caplog.text


@pytest.mark.parametrize(
    ("client", "status", "stage"),
    [
        (FakeClient(), "disabled", "state_http_done"),
        (FakeClient(fail=True), "error", "state_http"),
    ],
)
def test_hook_logs_disabled_and_error_outcomes(
    client,
    status,
    stage,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="hermes_agent.runtime_timing")

    assert build_context(
        client,
        session_id="session-1",
        turn_id="turn-1",
    ) is not None

    records = [
        record
        for record in caplog.records
        if record.name == "hermes_agent.runtime_timing"
    ]
    assert len(records) == 1
    payload = records[0].args
    assert payload["status"] == status
    assert payload["stage"] == stage
    if status == "error":
        assert payload["error_type"] == "PlatformClientError"


def test_hook_deduplicates_same_state_digest_from_history() -> None:
    client = FakeClient(
        {
            "enabled": True,
            "revision": "pkb:31:none:7",
            "knowledge_bases": [
                {
                    "id": 22,
                    "name": "运维库",
                    "description": "",
                    "ready_file_count": 1,
                },
                {
                    "id": 21,
                    "name": "案例库",
                    "description": "园区网案例",
                    "ready_file_count": 3,
                },
            ],
        }
    )
    first = build_context(client)["context"]

    assert first.index("platform_id=21") < first.index("platform_id=22")
    assert build_context(
        client,
        conversation_history=[_history_message(first)],
    ) is None


def test_hook_reinjects_when_revision_changes() -> None:
    first_client = FakeClient(
        {
            "enabled": True,
            "revision": "pkb:31:none:7",
            "knowledge_bases": [
                {
                    "id": 21,
                    "name": "案例库",
                    "description": "园区网案例",
                    "ready_file_count": 3,
                }
            ],
        }
    )
    next_client = FakeClient(
        {
            **first_client.state_value,
            "revision": "pkb:31:none:8",
        }
    )
    first = build_context(first_client)["context"]

    changed = build_context(
        next_client,
        conversation_history=[_history_message(first)],
    )

    assert changed is not None
    assert changed["context"] != first


def test_disabled_and_unavailable_markers_are_deduplicated() -> None:
    disabled = build_context(FakeClient())["context"]
    unavailable = build_context(FakeClient(fail=True))["context"]

    assert build_context(
        FakeClient(),
        conversation_history=[_history_message(disabled)],
    ) is None
    assert build_context(
        FakeClient(fail=True),
        conversation_history=[_history_message(unavailable)],
    ) is None


def test_search_tool_only_forwards_query_and_platform_ids() -> None:
    client = FakeClient(search={"matches": [{"content": "命中"}]})
    result = json.loads(
        handlers(client)["personal_knowledge_search"](
            {"query": "园区网", "knowledge_base_ids": [21]}
        )
    )

    assert result["matches"][0]["content"] == "命中"
    assert client.search_payloads == [
        {"query": "园区网", "knowledge_base_ids": [21]}
    ]


def test_search_tool_rejects_dataset_or_non_integer_ids() -> None:
    client = FakeClient()
    result = json.loads(
        handlers(client)["personal_knowledge_search"](
            {"query": "园区网", "knowledge_base_ids": ["ds-1"]}
        )
    )

    assert "integer array" in result["error"]
    assert client.search_payloads == []


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
        "/runtime-personal-knowledge/10/state?runtime_agent_profile_id=7"
    )


def test_application_agent_uses_active_run_headers(monkeypatch) -> None:
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
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    result = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=31,
        application_agent_id=31,
        token="token",
    ).state(
        runtime_session_id="studio-31-7-session",
        turn_id="turn-7",
        tool_call_id="tool-9",
    )

    assert result == {"enabled": True}
    assert captured["url"].endswith(
        "/application-agent-runtime-personal-knowledge/31/state"
    )
    assert captured["session_id"] == "studio-31-7-session"
    assert captured["turn_id"] == "turn-7"
    assert captured["tool_call_id"] == "tool-9"


def test_application_search_forwards_tool_context() -> None:
    client = FakeClient(search={"matches": []})

    handlers(client)["personal_knowledge_search"](
        {"query": "园区网"},
        session_id="studio-31-7-session",
        turn_id="turn-7",
        tool_call_id="tool-9",
    )

    assert client.search_contexts == [{
        "runtime_session_id": "studio-31-7-session",
        "turn_id": "turn-7",
        "tool_call_id": "tool-9",
    }]


def test_prepared_state_caches_only_hook_reads(monkeypatch) -> None:
    client = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=9124,
        token="unique-personal-cache-token",
        runtime_agent_profile_id=78,
    )
    calls = []

    def request(method, path, payload=None, **context):
        calls.append({
            "method": method,
            "path": path,
            "payload": payload,
            **context,
        })
        return {
            "enabled": False,
            "knowledge_bases": [],
            "revision": "pkb:v2:test",
        }

    monkeypatch.setattr(client, "_request", request)

    first = client.prepared_state(runtime_session_id="cache-session")
    second = client.prepared_state(runtime_session_id="cache-session")
    live_tool_check = client.state(runtime_session_id="cache-session")

    assert first == second == live_tool_check
    assert len(calls) == 2
    assert calls[0]["prepared_context"] is True
    assert "prepared_context" not in calls[1]


def test_hook_describes_rag_wiki_capabilities_without_auto_search() -> None:
    client = FakeClient(
        {
            "enabled": True,
            "knowledge_bases": [
                {
                    "id": 1,
                    "name": "RAG 库",
                    "ready_file_count": 1,
                    "capabilities": {"rag": True, "wiki": False},
                },
                {
                    "id": 2,
                    "name": "Wiki 库",
                    "ready_file_count": 1,
                    "capabilities": {"rag": False, "wiki": True},
                },
                {
                    "id": 3,
                    "name": "混合库",
                    "ready_file_count": 1,
                    "capabilities": {"rag": True, "wiki": True},
                },
                {
                    "id": 4,
                    "name": "旧缓存库",
                    "ready_file_count": 1,
                },
            ],
        }
    )

    context = build_context(client)["context"]
    lines = {
        knowledge_base_id: next(
            line
            for line in context.splitlines()
            if f"platform_id={knowledge_base_id}" in line
        )
        for knowledge_base_id in (1, 2, 3, 4)
    }

    assert "personal_knowledge_wiki_search" in context
    assert "personal_knowledge_hybrid_search" in context
    assert "personal_knowledge_wiki_read_page" in context
    assert "rag=true; wiki=false" in lines[1]
    assert "personal_knowledge_wiki_search" not in lines[1]
    assert "rag=false; wiki=true" in lines[2]
    assert "personal_knowledge_search" not in lines[2]
    assert "rag=true; wiki=true" in lines[3]
    assert "rag=true; wiki=false" in lines[4]
    assert client.search_payloads == []
    assert client.wiki_search_payloads == []
    assert client.hybrid_payloads == []


def test_wiki_tools_forward_platform_ids_and_tool_context() -> None:
    client = FakeClient()
    tool_handlers = handlers(client)

    search_result = json.loads(
        tool_handlers["personal_knowledge_wiki_search"](
            {"query": "概念", "knowledge_base_ids": [2]},
            session_id="studio-session",
            turn_id="turn-1",
            tool_call_id="tool-1",
        )
    )
    page_result = json.loads(
        tool_handlers["personal_knowledge_wiki_read_page"](
            {"knowledge_base_id": 2, "slug": "concept/rag"},
            session_id="studio-session",
            turn_id="turn-2",
            tool_call_id="tool-2",
        )
    )

    assert search_result["matches"][0]["slug"] == "concept/rag"
    assert page_result["content"] == "完整正文"
    assert client.wiki_search_payloads == [
        {"query": "概念", "knowledge_base_ids": [2]}
    ]
    assert client.wiki_page_payloads == [
        {"knowledge_base_id": 2, "slug": "concept/rag"}
    ]
    assert client.wiki_contexts == [
        {
            "runtime_session_id": "studio-session",
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
        },
        {
            "runtime_session_id": "studio-session",
            "turn_id": "turn-2",
            "tool_call_id": "tool-2",
        },
    ]
    assert TOOL_SCHEMAS["personal_knowledge_wiki_read_page"]["parameters"][
        "required"
    ] == ["knowledge_base_id", "slug"]


def test_hybrid_search_forwards_scope_and_tool_context() -> None:
    client = FakeClient()
    result = json.loads(
        handlers(client)["personal_knowledge_hybrid_search"](
            {"query": "部署", "knowledge_base_ids": [1, 3]},
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="tool-1",
        )
    )

    assert result["wiki_results"][0]["slug"] == "concept/rag"
    assert client.hybrid_payloads == [
        {"query": "部署", "knowledge_base_ids": [1, 3]}
    ]
    assert client.wiki_contexts[-1] == {
        "runtime_session_id": "session-1",
        "turn_id": "turn-1",
        "tool_call_id": "tool-1",
    }


def test_hybrid_search_requires_query_and_integer_kb_ids() -> None:
    client = FakeClient()
    handler = handlers(client)["personal_knowledge_hybrid_search"]

    missing_query = json.loads(handler({"knowledge_base_ids": [1]}))
    invalid_ids = json.loads(
        handler({"query": "部署", "knowledge_base_ids": ["1"]})
    )

    assert "query" in missing_query["error"]
    assert "knowledge_base_ids" in invalid_ids["error"]
    assert client.hybrid_payloads == []


def test_wiki_read_page_requires_integer_kb_id_and_slug() -> None:
    client = FakeClient()
    result = json.loads(
        handlers(client)["personal_knowledge_wiki_read_page"](
            {"knowledge_base_id": "2", "slug": "concept/rag"}
        )
    )

    assert "knowledge_base_id" in result["error"]
    assert client.wiki_page_payloads == []


def test_wiki_platform_endpoints_include_profile_and_application_identity(
    monkeypatch,
) -> None:
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"success": true, "data": []}'

    def fake_urlopen(request, *, timeout):
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "payload": json.loads(request.data),
                "session_id": request.headers.get(
                    "X-hermes-runtime-session-id"
                ),
            }
        )
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    personal = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=10,
        token="token",
        runtime_agent_profile_id=7,
    )
    personal.wiki_search({"query": "概念", "knowledge_base_ids": [2]})
    personal.hybrid_search({"query": "混合"})
    application = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=31,
        application_agent_id=31,
        token="token",
    )
    application.wiki_read_page(
        {"knowledge_base_id": 2, "slug": "concept/rag"},
        runtime_session_id="studio-session",
    )

    assert calls[0]["url"].endswith(
        "/runtime-personal-knowledge/10/wiki-search?runtime_agent_profile_id=7"
    )
    assert calls[0]["method"] == "POST"
    assert calls[0]["payload"] == {"query": "概念", "knowledge_base_ids": [2]}
    assert calls[1]["url"].endswith(
        "/runtime-personal-knowledge/10/hybrid-search?runtime_agent_profile_id=7"
    )
    assert calls[1]["payload"] == {"query": "混合"}
    assert calls[2]["url"].endswith(
        "/application-agent-runtime-personal-knowledge/31/wiki-read-page"
    )
    assert calls[2]["method"] == "POST"
    assert calls[2]["payload"] == {
        "knowledge_base_id": 2,
        "slug": "concept/rag",
    }
    assert calls[2]["session_id"] == "studio-session"
