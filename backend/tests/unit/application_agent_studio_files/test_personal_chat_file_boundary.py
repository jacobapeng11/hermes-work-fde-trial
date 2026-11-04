from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentStudioSession,
    Base,
    CloudHermesProfile,
)
from app.services.chat_files import (
    ChatFilePermissionError,
    SqlAlchemyChatFileStore,
)


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger,
    _compiler: object,
    **_kwargs: object,
) -> str:
    return "INTEGER"


@pytest.mark.asyncio
async def test_personal_chat_file_store_does_not_resolve_application_identities() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                CloudHermesProfile.__table__,
                ApplicationAgent.__table__,
                ApplicationAgentStudioSession.__table__,
                ApplicationAgentRun.__table__,
            ],
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        session.add(
            ApplicationAgent(
                id=55,
                org_id=1,
                slug="crc-1",
                name="crc-1",
                status="published",
                created_by_user_id=990095,
                updated_by_user_id=990095,
            )
        )
        session.add(
            ApplicationAgentStudioSession(
                id=193,
                org_id=1,
                application_agent_id=55,
                user_id=990095,
                runtime_session_id="studio-55-990095-RmgdLsLHkjD8AhjO",
                status="active",
            )
        )
        await session.commit()

        with pytest.raises(ChatFilePermissionError, match="runtime workspace not found"):
            await SqlAlchemyChatFileStore(session)._resolve_context(
                org_id=1,
                user_id=990095,
                runtime_workspace_id=55,
                runtime_session_id="studio-55-990095-RmgdLsLHkjD8AhjO",
                runtime_run_id=None,
            )
    finally:
        await session.close()
        await engine.dispose()
