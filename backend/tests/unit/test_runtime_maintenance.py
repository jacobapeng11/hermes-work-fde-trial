from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import RuntimeMaintenanceControl
from app.services.runtime_maintenance import (
    MAINTENANCE_STATE_FROZEN,
    MAINTENANCE_STATE_OPEN,
    RuntimeMaintenanceError,
    RuntimeMaintenanceService,
)


async def _database(**overrides: object):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeMaintenanceControl.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        values: dict[str, object] = {"id": 1}
        values.update(overrides)
        db.add(RuntimeMaintenanceControl(**values))
        await db.commit()
    return engine, factory


async def test_freeze_survives_restart_and_does_not_rotate_generation() -> None:
    engine, factory = await _database(admission_generation=7)
    started = datetime(2026, 8, 20, 9, 0, 0)
    completed = datetime(2026, 8, 20, 9, 30, 0)
    try:
        async with factory() as db:
            frozen = await RuntimeMaintenanceService(db).freeze(
                maintenance_epoch="cutover-20260820",
                reason="runtime code image cutover",
                now=started,
            )
            assert frozen.maintenance_state == MAINTENANCE_STATE_FROZEN
            assert frozen.admission_generation == 7
            assert frozen.grant_issuance_enabled is False

        async with factory() as restarted_db:
            service = RuntimeMaintenanceService(restarted_db)
            with pytest.raises(
                RuntimeMaintenanceError,
                match="MAINTENANCE_ADMISSION_FROZEN",
            ):
                await service.require_grant_issuance()
            opened = await service.open(
                maintenance_epoch="cutover-20260820",
                now=completed,
            )
            assert opened.maintenance_state == MAINTENANCE_STATE_OPEN
            assert opened.admission_generation == 7
            assert opened.completed_at == completed
            assert opened.credential_admission_generation == 7
    finally:
        await engine.dispose()


async def test_active_epoch_is_idempotent_and_different_epoch_fails_closed() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            service = RuntimeMaintenanceService(db)
            first = await service.freeze(
                maintenance_epoch="cutover-a",
                reason="fleet reload",
            )
            repeated = await service.freeze(
                maintenance_epoch="cutover-a",
                reason="ignored retry text",
            )
            assert repeated == first
            with pytest.raises(
                RuntimeMaintenanceError,
                match="RUNTIME_MAINTENANCE_IDENTITY_CONFLICT",
            ):
                await service.freeze(
                    maintenance_epoch="cutover-b",
                    reason="different fleet reload",
                )
    finally:
        await engine.dispose()


async def test_missing_or_inconsistent_control_fails_closed() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeMaintenanceControl.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            with pytest.raises(
                RuntimeMaintenanceError,
                match="RUNTIME_MAINTENANCE_CONTROL_UNINITIALIZED",
            ):
                await RuntimeMaintenanceService(db).require_admission_open()
            db.add(
                RuntimeMaintenanceControl(
                    id=1,
                    maintenance_state="open",
                    grant_issuance_enabled=False,
                )
            )
            await db.commit()
            with pytest.raises(
                RuntimeMaintenanceError,
                match="RUNTIME_MAINTENANCE_CONTROL_INVALID",
            ):
                await RuntimeMaintenanceService(db).require_admission_open()
    finally:
        await engine.dispose()
