from __future__ import annotations

import json
from typing import Any, Callable

from .contracts import (
    PersonalKnowledgeClientError,
    PersonalKnowledgeToolClient,
)


def _result(call: Callable[[], dict[str, Any]]) -> str:
    try:
        return json.dumps(call(), ensure_ascii=False)
    except (PersonalKnowledgeClientError, TypeError, ValueError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _search_payload(params: dict[str, Any]) -> dict[str, Any]:
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    payload: dict[str, Any] = {"query": query}
    knowledge_base_ids = params.get("knowledge_base_ids")
    if knowledge_base_ids is not None:
        if not isinstance(knowledge_base_ids, list) or not all(
            isinstance(value, int) for value in knowledge_base_ids
        ):
            raise ValueError("knowledge_base_ids must be an integer array")
        payload["knowledge_base_ids"] = knowledge_base_ids
    return payload


def _tool_context(context: dict[str, Any]) -> dict[str, str | None]:
    return {
        "runtime_session_id": context.get("session_id"),
        "turn_id": context.get("turn_id"),
        "tool_call_id": context.get("tool_call_id"),
    }


def handlers(client: PersonalKnowledgeToolClient) -> dict[str, Any]:
    def search(params: dict[str, Any], **context: Any) -> dict[str, Any]:
        return client.search(
            _search_payload(params),
            **_tool_context(context),
        )

    def wiki_search(
        params: dict[str, Any],
        **context: Any,
    ) -> dict[str, Any]:
        return client.wiki_search(
            _search_payload(params),
            **_tool_context(context),
        )

    def hybrid_search(
        params: dict[str, Any],
        **context: Any,
    ) -> dict[str, Any]:
        return client.hybrid_search(
            _search_payload(params),
            **_tool_context(context),
        )

    def wiki_read_page(
        params: dict[str, Any],
        **context: Any,
    ) -> dict[str, Any]:
        knowledge_base_id = params.get("knowledge_base_id")
        if not isinstance(knowledge_base_id, int):
            raise ValueError("knowledge_base_id must be an integer")
        slug = str(params.get("slug") or "").strip()
        if not slug:
            raise ValueError("slug is required")
        return client.wiki_read_page(
            {"knowledge_base_id": knowledge_base_id, "slug": slug},
            **_tool_context(context),
        )

    return {
        "personal_knowledge_search": lambda params, **context: _result(
            lambda: search(params, **context)
        ),
        "personal_knowledge_wiki_search": lambda params, **context: _result(
            lambda: wiki_search(params, **context)
        ),
        "personal_knowledge_hybrid_search": lambda params, **context: _result(
            lambda: hybrid_search(params, **context)
        ),
        "personal_knowledge_wiki_read_page": (
            lambda params, **context: _result(
                lambda: wiki_read_page(params, **context)
            )
        ),
    }


TOOL_SCHEMAS = {
    "personal_knowledge_search": {
        "description": (
            "Search the personal knowledge bases currently selected for this Runtime."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["query"],
        },
    },
    "personal_knowledge_wiki_search": {
        "description": (
            "Search Wiki pages in the selected personal knowledge bases for "
            "concepts, relationships and overviews. Read a matching page for full text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["query"],
        },
    },
    "personal_knowledge_hybrid_search": {
        "description": (
            "Search RAG chunks and Wiki pages in parallel. Prefer this for broad "
            "questions that may need both exact document evidence and conceptual "
            "overview; results are separated as rag_results and wiki_results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "knowledge_base_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["query"],
        },
    },
    "personal_knowledge_wiki_read_page": {
        "description": (
            "Read the full content and platform source files of a Wiki page "
            "returned by personal_knowledge_wiki_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "knowledge_base_id": {"type": "integer"},
                "slug": {"type": "string"},
            },
            "required": ["knowledge_base_id", "slug"],
        },
    },
}
