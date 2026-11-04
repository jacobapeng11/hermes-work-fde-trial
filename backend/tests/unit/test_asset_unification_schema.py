from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from app.models.legacy_assets import LegacyAssetBase
from app.services.asset_unification_schema import inspect_asset_unification_schema


async def _schema_state(*, legacy: bool):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if legacy:
            await connection.run_sync(LegacyAssetBase.metadata.create_all)
    try:
        return await inspect_asset_unification_schema(engine)
    finally:
        await engine.dispose()


async def test_fresh_stable_schema_is_already_finalized() -> None:
    state = await _schema_state(legacy=False)

    assert state.status == "finalized"
    assert state.retired_tables == ()


async def test_complete_legacy_schema_is_ready_for_one_time_migration() -> None:
    state = await _schema_state(legacy=True)

    assert state.status == "ready"
    assert state.missing_source_tables == ()
    assert state.missing_stable_tables == ()
    assert state.missing_skillhub_columns == ()
