import pytest
from fastapi import HTTPException

from app.api.v1.user.runtime_sessions import read_runtime_session
from app.services.runtime_sessions import RuntimeSessionRecord
from app.services.runtime_workspace import Principal


@pytest.mark.asyncio
async def test_reads_one_owned_runtime_session() -> None:
    calls: list[tuple[int, int, str]] = []

    class SessionService:
        async def get_session(self, *, runtime_workspace_id, runtime_session_id, **_kwargs):
            calls.append((runtime_workspace_id, runtime_session_id, "get"))
            return RuntimeSessionRecord(
                session_id=91,
                runtime_workspace_id=runtime_workspace_id,
                org_id=1,
                user_id=2,
                runtime_session_id=runtime_session_id,
                title="旧 HTML 任务",
                source_type="web",
                message_count=8,
                session_model_entry_id=19,
                status="active",
            )

    result = await read_runtime_session(
        runtime_workspace_id=7,
        runtime_session_id="rs-7-old",
        principal=Principal(org_id=1, user_id=2),
        service=SessionService(),
    )

    assert calls == [(7, "rs-7-old", "get")]
    assert result.runtime_session_id == "rs-7-old"
    assert result.title == "旧 HTML 任务"
    assert result.status == "active"


@pytest.mark.asyncio
async def test_missing_runtime_session_returns_404() -> None:
    class SessionService:
        async def get_session(self, **_kwargs):
            raise PermissionError("runtime session not found")

    with pytest.raises(HTTPException) as exc_info:
        await read_runtime_session(
            runtime_workspace_id=7,
            runtime_session_id="rs-7-missing",
            principal=Principal(org_id=1, user_id=2),
            service=SessionService(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "runtime session not found"
