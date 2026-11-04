from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import RuntimeBootstrapIntent
from app.services.runtime_bootstrap_intent import (
    RuntimeBootstrapIntentError,
    RuntimeBootstrapIntentService,
)


async def test_bootstrap_authorization_is_idempotent_and_identity_bound() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeBootstrapIntent.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            service = RuntimeBootstrapIntentService(db)
            values = {
                "binding_type": "personal_runtime",
                "binding_id": 17,
                "runtime_id": "rt-17",
                "pvc_storage_identity_hash": "sha256:" + "a" * 64,
                "target_image": "kind.local/runtime:code-1",
                "target_image_digest": "sha256:" + "b" * 64,
            }
            first = await service.authorize(**values)
            repeated = await service.authorize(**values)

            assert repeated == first
            assert first.status == "authorized"
            assert "release_id" not in first.document
            assert "upgrade_job_id" not in first.document

            with pytest.raises(
                RuntimeBootstrapIntentError,
                match="different bootstrap authorization",
            ):
                await service.authorize(
                    **{
                        **values,
                        "target_image": "kind.local/runtime:code-2",
                        "target_image_digest": "sha256:" + "c" * 64,
                    }
                )
    finally:
        await engine.dispose()


async def test_completed_bootstrap_cannot_be_marked_failed() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeBootstrapIntent.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            service = RuntimeBootstrapIntentService(db)
            intent = await service.authorize(
                binding_type="application_runtime",
                binding_id=23,
                runtime_id="app-23",
                pvc_storage_identity_hash="sha256:" + "d" * 64,
                target_image="kind.local/runtime:code-1",
                target_image_digest="sha256:" + "e" * 64,
            )
            completed = await service.mark_completed(intent_id=intent.intent_id)
            assert completed.status == "completed"
            with pytest.raises(
                RuntimeBootstrapIntentError,
                match="completed bootstrap intent cannot fail",
            ):
                await service.mark_failed(
                    intent_id=intent.intent_id,
                    error_summary="late failure",
                )
    finally:
        await engine.dispose()
