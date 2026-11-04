from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.im_gateway.identity import opaque_digest, wecom_member_digest
from app.im_gateway.outbound import ImOutboundDeliveryStore
from app.models import (
    ImChannel,
    ImChannelLease,
    ImOutboundDelivery,
    RuntimeRun,
    ScheduledTask,
)
from app.models.base import Base
from app.services.managed_secret import ManagedSecretCodec


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger, _compiler: object, **_kwargs: object
) -> str:
    return "INTEGER"


@pytest.mark.asyncio
async def test_outbound_delivery_is_claimed_by_channel_lease_and_resumes_text() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                ImChannel.__table__,
                ImChannelLease.__table__,
                ScheduledTask.__table__,
                RuntimeRun.__table__,
                ImOutboundDelivery.__table__,
            ],
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    codec = ManagedSecretCodec("outbound-test-key")
    now = datetime.now(timezone.utc)
    target = "zhangsan"
    try:
        async with sessions() as db:
            db.add_all(
                [
                    ImChannel(
                        id=1,
                        org_id=3,
                        target_type="personal_agent",
                        created_by_user_id=7,
                        user_id=7,
                        runtime_workspace_id=11,
                        provider="wecom",
                        bot_id="bot-1",
                        secret_encrypted="secret",
                        enabled=True,
                        status="connected",
                        runtime_session_row_id=23,
                        proactive_target_encrypted=codec.encrypt(target),
                        proactive_target_digest=wecom_member_digest(target),
                        proactive_target_confirmed_at=now,
                    ),
                    ImChannelLease(
                        channel_id=1,
                        owner_instance_id="backend-a",
                        lease_epoch=4,
                        expires_at=now + timedelta(minutes=5),
                        heartbeat_at=now,
                    ),
                    ScheduledTask(
                        id=7,
                        org_id=3,
                        user_id=7,
                        runtime_workspace_id=11,
                        target_session_id=23,
                        task_name="daily",
                        prompt="report",
                        schedule_type="once",
                        schedule_config={"type": "once"},
                        timezone="Asia/Shanghai",
                        status="completed",
                    ),
                    RuntimeRun(
                        id=41,
                        org_id=3,
                        user_id=7,
                        runtime_workspace_id=11,
                        session_id=23,
                        runtime_session_id="rs-23",
                        runtime_run_id="run-41",
                        status="completed",
                        source_type="scheduled",
                    ),
                    ImOutboundDelivery(
                        id=51,
                        delivery_key="scheduled:7:run-41",
                        channel_id=1,
                        scheduled_task_id=7,
                        runtime_run_row_id=41,
                        runtime_run_id="run-41",
                        bot_id_digest=opaque_digest("bot-1"),
                        target_encrypted=codec.encrypt(target),
                        target_digest=wecom_member_digest(target),
                        content="done",
                        artifact_ids=[],
                        delivered_artifact_ids=[],
                        status="pending",
                        next_attempt_at=now,
                    ),
                ]
            )
            await db.commit()

            store = ImOutboundDeliveryStore(db, secret_codec=codec)
            assert await store.claim_next(
                channel_id=1,
                owner_instance_id="backend-b",
                lease_epoch=4,
                now=now,
            ) is None
            claimed = await store.claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=4,
                now=now,
            )
            assert claimed is not None
            assert claimed.target == target
            assert claimed.content == "done"

            await store.mark_text_delivered(claimed)
            lease = await db.get(ImChannelLease, 1)
            assert lease is not None
            lease.owner_instance_id = "backend-b"
            lease.lease_epoch = 5
            await db.commit()
            with pytest.raises(
                RuntimeError, match="outbound channel lease was lost"
            ):
                await store.mark_delivered(claimed)
            lease.owner_instance_id = "backend-a"
            lease.lease_epoch = 4
            lease.expires_at = now + timedelta(minutes=5)
            await db.commit()
            await store.mark_delivered(claimed)
            row = await db.scalar(
                select(ImOutboundDelivery).where(ImOutboundDelivery.id == 51)
            )
            assert row is not None
            assert row.status == "delivered"
            assert row.provider_delivered_text == "done"
            assert row.delivered_at is not None
    finally:
        await engine.dispose()
