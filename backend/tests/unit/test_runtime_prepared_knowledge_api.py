from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.api.v1.application_agent_runtime_knowledge as app_enterprise_api
import app.api.v1.application_agent_runtime_personal_knowledge as app_personal_api
import app.api.v1.runtime_knowledge as workspace_enterprise_api
import app.api.v1.runtime_personal_knowledge as workspace_personal_api
from app.schemas.personal_knowledge import RuntimePersonalKnowledgeState


class _PreparedCache:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def load_or_prepare(self, **kwargs: object):
        self.calls.append(kwargs)
        if kwargs["kind"] == "enterprise":
            return {"enabled": True, "mode": "full"}
        return {
            "enabled": False,
            "knowledge_bases": [],
            "revision": "prepared",
        }


class _EnterpriseService:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None, str]] = []

    async def state(
        self,
        target_id: int,
        profile_id: int | None,
        token: str,
    ) -> dict[str, object]:
        self.calls.append((target_id, profile_id, token))
        return {"enabled": False, "mode": "full"}


class _PersonalService:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None, str]] = []

    async def state(
        self,
        target_id: int,
        profile_id: int | None,
        token: str,
    ) -> RuntimePersonalKnowledgeState:
        self.calls.append((target_id, profile_id, token))
        return RuntimePersonalKnowledgeState(
            enabled=False,
            knowledge_bases=[],
            revision="live",
        )


@pytest.mark.asyncio
async def test_workspace_enterprise_cache_is_hook_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _PreparedCache()
    service = _EnterpriseService()
    monkeypatch.setattr(
        workspace_enterprise_api,
        "shared_runtime_prepared_knowledge_service",
        lambda: cache,
    )

    live = await workspace_enterprise_api.runtime_knowledge_state(
        runtime_workspace_id=43,
        runtime_agent_profile_id=7,
        authorization="Bearer runtime-token",
        runtime_session_id="rs-43-a",
        prepared_context=None,
        service=service,  # type: ignore[arg-type]
    )
    prepared = await workspace_enterprise_api.runtime_knowledge_state(
        runtime_workspace_id=43,
        runtime_agent_profile_id=7,
        authorization="Bearer runtime-token",
        runtime_session_id="rs-43-a",
        prepared_context="1",
        service=service,  # type: ignore[arg-type]
    )

    assert live == {"enabled": False, "mode": "full"}
    assert prepared == {"enabled": True, "mode": "full"}
    assert service.calls == [(43, 7, "runtime-token")]
    assert [call["kind"] for call in cache.calls] == ["enterprise"]


@pytest.mark.asyncio
async def test_workspace_personal_cache_is_hook_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _PreparedCache()
    service = _PersonalService()
    monkeypatch.setattr(
        workspace_personal_api,
        "shared_runtime_prepared_knowledge_service",
        lambda: cache,
    )

    live = await workspace_personal_api.runtime_personal_knowledge_state(
        runtime_workspace_id=43,
        runtime_agent_profile_id=7,
        authorization="Bearer runtime-token",
        runtime_session_id="rs-43-a",
        prepared_context=None,
        service=service,  # type: ignore[arg-type]
    )
    prepared = await workspace_personal_api.runtime_personal_knowledge_state(
        runtime_workspace_id=43,
        runtime_agent_profile_id=7,
        authorization="Bearer runtime-token",
        runtime_session_id="rs-43-a",
        prepared_context="1",
        service=service,  # type: ignore[arg-type]
    )

    assert live.revision == "live"
    assert prepared.revision == "prepared"
    assert service.calls == [(43, 7, "runtime-token")]
    assert [call["kind"] for call in cache.calls] == ["personal"]


@pytest.mark.asyncio
async def test_application_enterprise_cache_is_hook_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _PreparedCache()
    service = _EnterpriseService()
    monkeypatch.setattr(
        app_enterprise_api,
        "shared_runtime_prepared_knowledge_service",
        lambda: cache,
    )
    monkeypatch.setattr(
        app_enterprise_api,
        "_service",
        lambda *_args, **_kwargs: service,
    )

    live = await app_enterprise_api.application_agent_runtime_knowledge_state(
        application_agent_id=31,
        authorization="Bearer runtime-token",
        runtime_session_id="studio-31-7-session",
        turn_id="turn-1",
        tool_call_id="tool-1",
        prepared_context=None,
        db=SimpleNamespace(),  # type: ignore[arg-type]
    )
    prepared = (
        await app_enterprise_api.application_agent_runtime_knowledge_state(
            application_agent_id=31,
            authorization="Bearer runtime-token",
            runtime_session_id="studio-31-7-session",
            turn_id="turn-1",
            tool_call_id=None,
            prepared_context="1",
            db=SimpleNamespace(),  # type: ignore[arg-type]
        )
    )

    assert live == {"enabled": False, "mode": "full"}
    assert prepared == {"enabled": True, "mode": "full"}
    assert service.calls == [(31, None, "runtime-token")]
    assert [call["kind"] for call in cache.calls] == ["enterprise"]


@pytest.mark.asyncio
async def test_application_personal_cache_is_hook_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _PreparedCache()
    service = _PersonalService()
    monkeypatch.setattr(
        app_personal_api,
        "shared_runtime_prepared_knowledge_service",
        lambda: cache,
    )
    monkeypatch.setattr(
        app_personal_api,
        "_service",
        lambda *_args, **_kwargs: service,
    )

    live = (
        await app_personal_api.application_agent_runtime_personal_knowledge_state(
            application_agent_id=31,
            authorization="Bearer runtime-token",
            runtime_session_id="studio-31-7-session",
            turn_id="turn-1",
            tool_call_id="tool-1",
            prepared_context=None,
            db=SimpleNamespace(),  # type: ignore[arg-type]
        )
    )
    prepared = (
        await app_personal_api.application_agent_runtime_personal_knowledge_state(
            application_agent_id=31,
            authorization="Bearer runtime-token",
            runtime_session_id="studio-31-7-session",
            turn_id="turn-1",
            tool_call_id=None,
            prepared_context="1",
            db=SimpleNamespace(),  # type: ignore[arg-type]
        )
    )

    assert live.revision == "live"
    assert prepared.revision == "prepared"
    assert service.calls == [(31, None, "runtime-token")]
    assert [call["kind"] for call in cache.calls] == ["personal"]
