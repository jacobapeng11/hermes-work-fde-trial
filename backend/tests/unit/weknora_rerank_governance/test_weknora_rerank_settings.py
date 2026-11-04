from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable

from app.models import WeKnoraRerankConfig
from app.services.weknora_rerank_settings import WeKnoraRerankSettingService


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


@pytest.mark.asyncio
async def test_setting_service_defaults_to_model_and_persists_mode() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as connection:
        await connection.execute(CreateTable(WeKnoraRerankConfig.__table__))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        service = WeKnoraRerankSettingService(session)
        assert await service.get_mode() == "model"
        assert await service.is_rerank_enabled() is True

        await service.set_mode(mode="disabled", updated_by_user_id=99)
        assert await service.get_mode() == "disabled"
        assert await service.is_rerank_enabled() is False

        await service.set_mode(mode="model", updated_by_user_id=100)
        assert await service.get_mode() == "model"

    async with session_factory() as session:
        rows = (await session.execute(select(WeKnoraRerankConfig))).scalars().all()
        assert len(rows) == 1
        assert rows[0].provider == "weknora"
        assert rows[0].rerank_enabled is True
        assert rows[0].updated_by_user_id == 100

    await engine.dispose()
