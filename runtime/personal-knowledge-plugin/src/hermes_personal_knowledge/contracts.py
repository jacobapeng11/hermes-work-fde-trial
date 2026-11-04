from __future__ import annotations

from typing import Any, Protocol


class PersonalKnowledgeClientError(RuntimeError):
    """Stable adapter failure used by tool implementations."""


class PersonalKnowledgeStateReader(Protocol):
    def state(
        self,
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        ...


class PersonalKnowledgePreparedStateReader(
    PersonalKnowledgeStateReader,
    Protocol,
):
    def prepared_state(
        self,
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        ...


class PersonalKnowledgeSearchClient(Protocol):
    def search(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        ...


class PersonalKnowledgeWikiClient(Protocol):
    def wiki_search(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        ...

    def wiki_read_page(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        ...


class PersonalKnowledgeHybridSearchClient(Protocol):
    def hybrid_search(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        ...


class PersonalKnowledgeToolClient(
    PersonalKnowledgeSearchClient,
    PersonalKnowledgeWikiClient,
    PersonalKnowledgeHybridSearchClient,
    Protocol,
):
    """Transport-independent contract consumed by plugin tools."""
