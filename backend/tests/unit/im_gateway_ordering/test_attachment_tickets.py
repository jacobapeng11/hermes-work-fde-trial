from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles

from app.im_gateway.queue import ImInboundQueue, utc_now
from app.models import (
    ImChannel,
    ImChannelLease,
    ImDeliveryCheckpoint,
    ImExecutionBatch,
    ImIngressTicketCounter,
    ImInboundMessage,
    ImMessageAttachment,
)
from app.models.base import Base


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger, _compiler: object, **_kwargs: object
) -> str:
    return "INTEGER"


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                ImChannel.__table__,
                ImChannelLease.__table__,
                ImExecutionBatch.__table__,
                ImIngressTicketCounter.__table__,
                ImInboundMessage.__table__,
                ImMessageAttachment.__table__,
                ImDeliveryCheckpoint.__table__,
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _channel(
    *, target_type: str = "personal_agent"
) -> ImChannel:
    personal = target_type == "personal_agent"
    return ImChannel(
        id=1,
        org_id=10,
        target_type=target_type,
        created_by_user_id=20,
        user_id=20 if personal else None,
        runtime_workspace_id=30 if personal else None,
        runtime_session_row_id=40 if personal else None,
        application_agent_id=50 if not personal else None,
        application_client_id=60 if not personal else None,
        provider="wecom",
        connection_mode="websocket",
        bot_id="bot-1",
        secret_encrypted="encrypted",
        enabled=True,
        status="connected",
        config_generation=1,
    )


def _lease() -> ImChannelLease:
    now = utc_now()
    return ImChannelLease(
        channel_id=1,
        owner_instance_id="backend-a",
        lease_epoch=2,
        expires_at=now + timedelta(minutes=5),
        heartbeat_at=now,
    )


def _message(
    message_id: int,
    *,
    status: str,
    ingress_ticket: int,
    ingress_ticket_state: str,
    processing_owner: str | None = None,
    processing_lease_epoch: int | None = None,
    target_type: str = "personal_agent",
) -> ImInboundMessage:
    ordering_key = (
        "personal-session:40"
        if target_type == "personal_agent"
        else "application-direct:101"
    )
    return ImInboundMessage(
        id=message_id,
        channel_id=1,
        provider="wecom",
        external_message_id=f"provider-{message_id}",
        external_conversation_id="conversation",
        provider_request_id=f"request-{message_id}",
        message_type="image",
        normalized_content={"text": "[企微图片附件]"},
        dispatch_lane="ordinary_attachment",
        target_type=target_type,
        status=status,
        ingress_ordering_key=ordering_key,
        ingress_ticket=ingress_ticket,
        ingress_ticket_state=ingress_ticket_state,
        ordering_key=ordering_key,
        processing_owner=processing_owner,
        processing_lease_epoch=processing_lease_epoch,
        attempt_count=0,
        received_at=datetime.now(timezone.utc)
        + timedelta(seconds=message_id),
    )


def _checkpoint(message_id: int) -> ImDeliveryCheckpoint:
    return ImDeliveryCheckpoint(
        inbound_message_id=message_id,
        provider_request_id=f"request-{message_id}",
        provider_stream_id=f"stream-{message_id}",
        delivery_mode="passive_stream",
        status="pending",
    )


@pytest.mark.asyncio
async def test_completed_legacy_attachment_does_not_block_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            predecessor = _message(
                1,
                status="completed",
                ingress_ticket=137,
                ingress_ticket_state="pending",
            )
            predecessor.runtime_run_row_id = 9945
            successor = _message(
                2,
                status="staging_media",
                ingress_ticket=138,
                ingress_ticket_state="pending",
            )
            db.add_all([_channel(), _lease(), predecessor, successor])
            await db.commit()

            claimed = await ImInboundQueue(db).claim_staging(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=2,
            )

            assert claimed == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_acceptance_marks_attachment_ticket_durable() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            batch = ImExecutionBatch(
                id=131,
                channel_id=1,
                target_type="personal_agent",
                ordering_key="personal-session:40",
                state="submitting",
                execution_run_kind="personal",
                execution_run_row_id=9946,
                execution_runtime_run_id="runtime-run-9946",
                collected_at=utc_now(),
            )
            message = _message(
                1,
                status="running",
                ingress_ticket=137,
                ingress_ticket_state="pending",
                processing_owner="backend-a",
                processing_lease_epoch=2,
            )
            message.execution_batch_id = batch.id
            db.add_all(
                [_channel(), _lease(), batch, message, _checkpoint(1)]
            )
            await db.commit()

            accepted = await ImInboundQueue(db).mark_batch_accepted(
                inbound_message_id=1,
                owner_instance_id="backend-a",
                lease_epoch=2,
            )

            assert accepted is True
            assert message.ingress_ticket_state == "runtime_durable"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_application_attachment_releases_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            predecessor = _message(
                1,
                status="failed",
                ingress_ticket=142,
                ingress_ticket_state="pending",
                target_type="application_agent",
            )
            predecessor.application_agent_run_id = 11245
            successor = _message(
                2,
                status="staging_media",
                ingress_ticket=143,
                ingress_ticket_state="pending",
                target_type="application_agent",
            )
            db.add_all(
                [
                    _channel(target_type="application_agent"),
                    _lease(),
                    predecessor,
                    successor,
                ]
            )
            await db.commit()

            claimed = await ImInboundQueue(
                db
            ).claim_staging_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=2,
            )

            assert claimed == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_attachment_run_binding_releases_ticket() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            message = _message(
                1,
                status="running",
                ingress_ticket=142,
                ingress_ticket_state="pending",
                processing_owner="backend-a",
                processing_lease_epoch=2,
                target_type="application_agent",
            )
            db.add_all(
                [
                    _channel(target_type="application_agent"),
                    _lease(),
                    message,
                    _checkpoint(1),
                ]
            )
            await db.commit()

            bound = await ImInboundQueue(db).bind_application_run(
                inbound_message_id=1,
                application_agent_run_id=11245,
                runtime_run_id="wecom-run-11245",
                owner_instance_id="backend-a",
                lease_epoch=2,
            )

            assert bound is True
            assert message.ingress_ticket_state == "runtime_durable"
    finally:
        await engine.dispose()
