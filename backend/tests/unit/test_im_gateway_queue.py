from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import BigInteger, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.im_gateway.messages import NormalizedWeComMessage
from app.im_gateway import queue as queue_module
from app.im_gateway.queue import (
    ImInboundQueue,
    _wecom_provider_stream_id,
    im_timing_payload,
)
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


async def _database() -> tuple[
    Any, async_sessionmaker[AsyncSession]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE application_agent_run (
                    id INTEGER PRIMARY KEY,
                    runtime_run_id VARCHAR(255) NOT NULL UNIQUE,
                    status VARCHAR(32) NOT NULL,
                    error_code VARCHAR(100)
                )
                """
            )
        )
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


def test_wecom_provider_stream_id_is_deterministic_per_runtime_run() -> None:
    personal = _wecom_provider_stream_id(
        target_type="personal_agent",
        channel_id=7,
        inbound_message_id=101,
    )
    assert personal == _wecom_provider_stream_id(
        target_type="personal_agent",
        channel_id=7,
        inbound_message_id=101,
    )
    assert personal != _wecom_provider_stream_id(
        target_type="personal_agent",
        channel_id=7,
        inbound_message_id=102,
    )
    assert personal != _wecom_provider_stream_id(
        target_type="application_agent",
        channel_id=7,
        inbound_message_id=101,
    )


def _channel() -> ImChannel:
    return ImChannel(
        id=1,
        org_id=10,
        target_type="personal_agent",
        created_by_user_id=20,
        user_id=20,
        runtime_workspace_id=30,
        provider="wecom",
        connection_mode="websocket",
        bot_id="bot-1",
        secret_encrypted="encrypted",
        enabled=True,
        status="connected",
        config_generation=1,
        runtime_session_row_id=40,
    )


def _application_channel() -> ImChannel:
    return ImChannel(
        id=1,
        org_id=10,
        target_type="application_agent",
        created_by_user_id=20,
        user_id=None,
        runtime_workspace_id=None,
        runtime_session_row_id=None,
        application_agent_id=50,
        application_client_id=60,
        provider="wecom",
        connection_mode="websocket",
        bot_id="app-bot-1",
        secret_encrypted="encrypted",
        enabled=True,
        status="connected",
        config_generation=1,
    )


def _lease(*, owner: str = "backend-a", epoch: int = 1):
    now = datetime.now(timezone.utc)
    return ImChannelLease(
        channel_id=1,
        owner_instance_id=owner,
        lease_epoch=epoch,
        expires_at=now + timedelta(minutes=5),
        heartbeat_at=now,
    )


def _message(
    message_id: int,
    *,
    status: str,
    received_at: datetime,
) -> ImInboundMessage:
    return ImInboundMessage(
        id=message_id,
        channel_id=1,
        provider="wecom",
        external_message_id=f"provider-{message_id}",
        external_conversation_id="zhangsan",
        provider_request_id=f"request-{message_id}",
        message_type="image" if status == "staging_media" else "text",
        normalized_content={"text": "hello"},
        status=status,
        attempt_count=0,
        received_at=received_at,
    )


def _checkpoint(message_id: int) -> ImDeliveryCheckpoint:
    return ImDeliveryCheckpoint(
        id=message_id,
        inbound_message_id=message_id,
        provider_request_id=f"request-{message_id}",
        provider_stream_id=f"stream-{message_id}",
        delivery_mode="passive_stream",
        status="pending",
    )


def _execution_batch(
    batch_id: int,
    *,
    state: str,
    ordering_key: str,
    execution_slot_key: str | None = None,
    collection_slot_key: str | None = None,
) -> ImExecutionBatch:
    return ImExecutionBatch(
        id=batch_id,
        channel_id=1,
        target_type="personal_agent",
        ordering_key=ordering_key,
        execution_slot_key=execution_slot_key,
        collection_slot_key=collection_slot_key,
        state=state,
        collected_at=datetime.now(timezone.utc),
    )


def _normalized_text(
    message_id: int,
    *,
    sender_member_id: str | None = None,
) -> NormalizedWeComMessage:
    return NormalizedWeComMessage(
        external_message_id=f"inbound-{message_id}",
        provider_request_id=f"request-inbound-{message_id}",
        external_conversation_id="zhangsan",
        sender_member_id=sender_member_id,
        message_type="text",
        text=f"message-{message_id}",
        quote_payload=None,
    )


def _mysql_operational_error(code: int) -> OperationalError:
    return OperationalError(
        "SELECT 1",
        {},
        Exception(code, "database error"),
    )


@pytest.mark.asyncio
async def test_execution_and_collection_slots_can_coexist_for_one_lane() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            db.add(_channel())
            db.add_all(
                [
                    _execution_batch(
                        1,
                        state="running",
                        ordering_key="personal-session:40",
                        execution_slot_key=lane,
                    ),
                    _execution_batch(
                        2,
                        state="interrupting",
                        ordering_key="personal-session:40",
                        collection_slot_key=lane,
                    ),
                ]
            )
            await db.commit()

            batches = (
                await db.execute(
                    select(ImExecutionBatch).order_by(ImExecutionBatch.id)
                )
            ).scalars().all()
            assert [batch.state for batch in batches] == [
                "running",
                "interrupting",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_execution_slot_is_unique_per_wecom_lane() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            db.add(_channel())
            db.add_all(
                [
                    _execution_batch(
                        1,
                        state="running",
                        ordering_key="personal-session:40",
                        execution_slot_key=lane,
                    ),
                    _execution_batch(
                        2,
                        state="submitting",
                        ordering_key="personal-session:40",
                        execution_slot_key=lane,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_followups_reserve_fast_lane_tickets_in_order() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            db.add(_channel())
            db.add(
                _execution_batch(
                    1,
                    state="running",
                    ordering_key="personal-session:40",
                    execution_slot_key=lane,
                )
            )
            await db.commit()

            second = await ImInboundQueue(db).ingest(
                channel_id=1,
                message=_normalized_text(2),
            )
            third = await ImInboundQueue(db).ingest(
                channel_id=1,
                message=_normalized_text(3),
            )

            second_row = await db.get(
                ImInboundMessage, second.inbound_message_id
            )
            third_row = await db.get(
                ImInboundMessage, third.inbound_message_id
            )
            assert second_row is not None
            assert third_row is not None
            assert second_row.execution_batch_id is None
            assert third_row.execution_batch_id is None
            assert (second_row.ingress_ticket, third_row.ingress_ticket) == (1, 2)
            assert second_row.dispatch_lane == third_row.dispatch_lane == (
                "fast_lane_v2"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_direct_personal_message_confirms_proactive_target_without_retargeting() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add(_channel())
            await db.commit()
            queue = ImInboundQueue(db)

            await queue.ingest(
                channel_id=1,
                message=NormalizedWeComMessage(
                    external_message_id="target-1",
                    provider_request_id="request-target-1",
                    external_conversation_id="zhangsan",
                    message_type="text",
                    text="hello",
                    quote_payload=None,
                    sender_member_id="zhangsan",
                    chat_type="direct",
                ),
            )
            channel = await db.get(ImChannel, 1)
            assert channel is not None
            first_digest = channel.proactive_target_digest
            first_ciphertext = channel.proactive_target_encrypted
            assert first_digest
            assert queue.secret_codec.decrypt(str(first_ciphertext)) == "zhangsan"
            assert channel.proactive_target_confirmed_at is not None

            await queue.ingest(
                channel_id=1,
                message=NormalizedWeComMessage(
                    external_message_id="target-2",
                    provider_request_id="request-target-2",
                    external_conversation_id="lisi",
                    message_type="text",
                    text="hello",
                    quote_payload=None,
                    sender_member_id="lisi",
                    chat_type="direct",
                ),
            )
            await db.refresh(channel)
            assert channel.proactive_target_digest == first_digest
            assert channel.proactive_target_encrypted == first_ciphertext
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ingress_ticket_and_delivery_identity_are_durable_and_idempotent(
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add(_channel())
            await db.commit()
            queue = ImInboundQueue(db)

            first = await queue.ingest(
                channel_id=1,
                message=_normalized_text(1),
            )
            retry = await queue.ingest(
                channel_id=1,
                message=_normalized_text(1),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_text(2),
            )

            assert first.ingress_ticket == retry.ingress_ticket == 1
            assert first.delivery_id == retry.delivery_id
            assert first.client_message_id == retry.client_message_id
            assert retry.duplicate is True
            assert second.ingress_ticket == 2
            assert first.dispatch_lane == second.dispatch_lane == "fast_lane_v2"
            assert first.ingress_ticket_state == "pending"

            first_row = await db.get(
                ImInboundMessage,
                first.inbound_message_id,
            )
            counter = await db.get(
                ImIngressTicketCounter,
                (1, "personal-session:40"),
            )
            assert first_row is not None
            assert first_row.ingress_ordering_key == "personal-session:40"
            assert first_row.ingress_payload_digest is not None
            assert counter is not None
            assert counter.last_ticket == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_provider_id_with_different_payload_fails_closed() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add(_channel())
            await db.commit()
            queue = ImInboundQueue(db)
            original = _normalized_text(1)
            await queue.ingest(channel_id=1, message=original)
            changed = NormalizedWeComMessage(
                external_message_id=original.external_message_id,
                provider_request_id="retry-request",
                external_conversation_id=original.external_conversation_id,
                message_type=original.message_type,
                text="different payload",
                quote_payload=original.quote_payload,
                sender_member_id=original.sender_member_id,
                chat_type=original.chat_type,
            )

            with pytest.raises(
                ValueError,
                match="idempotency payload conflict",
            ):
                await queue.ingest(channel_id=1, message=changed)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fast_lane_ingress_does_not_mutate_legacy_batch_slot() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            stale = _execution_batch(
                1,
                state="terminal",
                ordering_key="personal-session:40",
                collection_slot_key=lane,
            )
            stale.terminal_at = datetime.now(timezone.utc)
            stale.terminal_status = "completed"
            db.add_all([_channel(), stale])
            await db.commit()

            receipt = await ImInboundQueue(db).ingest(
                channel_id=1,
                message=_normalized_text(2),
            )

            row = await db.get(ImInboundMessage, receipt.inbound_message_id)
            await db.refresh(stale)
            assert row is not None
            assert row.execution_batch_id is None
            assert row.batch_ordinal is None
            assert row.dispatch_lane == "fast_lane_v2"
            assert stale.collection_slot_key == lane
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminalize_batch_repairs_stale_collection_slot() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            terminal_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            batch = _execution_batch(
                1,
                state="terminal",
                ordering_key="personal-session:40",
                collection_slot_key=lane,
            )
            batch.terminal_at = terminal_at
            batch.terminal_status = "completed"
            message = _message(
                1,
                status="running",
                received_at=datetime.now(timezone.utc),
            )
            message.execution_batch_id = 1
            db.add_all([_channel(), batch, message])
            await db.commit()

            await ImInboundQueue(db)._terminalize_batch(
                row=message,
                terminal_status="failed",
                terminal_reason="must-not-overwrite",
            )
            await db.commit()

            await db.refresh(batch)
            assert batch.collection_slot_key is None
            assert batch.execution_slot_key is None
            assert batch.terminal_at is not None
            assert batch.terminal_at.replace(tzinfo=timezone.utc) == terminal_at
            assert batch.terminal_status == "completed"
            assert batch.terminal_reason is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_runtime_failure_releases_batch_and_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            message = _message(
                1,
                status="running",
                received_at=datetime.now(timezone.utc),
            )
            message.ordering_key = "personal-session:40"
            message.execution_batch_id = 1
            message.batch_ordinal = 1
            message.processing_owner = "backend-a"
            message.processing_lease_epoch = 1
            message.attempt_count = 3
            predecessor = _execution_batch(
                1,
                state="submitting",
                ordering_key="personal-session:40",
                execution_slot_key=lane,
            )
            predecessor.response_inbound_message_id = 1
            successor = _execution_batch(
                2,
                state="interrupting",
                ordering_key="personal-session:40",
                collection_slot_key=lane,
            )
            successor.predecessor_batch_id = 1
            db.add_all(
                [
                    _channel(),
                    _lease(),
                    predecessor,
                    successor,
                    message,
                ]
            )
            await db.commit()

            status = await ImInboundQueue(db).mark_failed(
                inbound_message_id=1,
                error_code="ImRuntimeBridgeError",
                error_message="terminal",
                max_attempts=3,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            await db.refresh(predecessor)
            await db.refresh(successor)
            assert status == "dead_letter"
            assert message.status == "dead_letter"
            assert predecessor.state == "terminal"
            assert predecessor.execution_slot_key is None
            assert predecessor.collection_slot_key is None
            assert predecessor.terminal_status == "dead_letter"
            assert predecessor.terminal_reason == "ImRuntimeBridgeError"
            assert successor.state == "ready"
            assert successor.predecessor_released_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_compat_claim_does_not_run_interrupting_batch() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            batch = _execution_batch(
                1,
                state="interrupting",
                ordering_key="personal-session:40",
                collection_slot_key=lane,
            )
            batch.response_inbound_message_id = 1
            message = _message(
                1,
                status="queued",
                received_at=datetime.now(timezone.utc),
            )
            message.ordering_key = "personal-session:40"
            message.execution_batch_id = 1
            message.batch_ordinal = 1
            db.add_all(
                [_channel(), _lease(), batch, message, _checkpoint(1)]
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            await db.refresh(message)
            assert claimed is None
            assert message.status == "queued"
            assert message.processing_owner is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_compat_claim_does_not_run_interrupting_batch() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:application-direct:zhangsan"
            batch = _execution_batch(
                1,
                state="interrupting",
                ordering_key="application-direct:zhangsan",
                collection_slot_key=lane,
            )
            batch.target_type = "application_agent"
            batch.response_inbound_message_id = 1
            message = _message(
                1,
                status="queued",
                received_at=datetime.now(timezone.utc),
            )
            message.target_type = "application_agent"
            message.application_agent_id = 50
            message.application_client_id = 60
            message.sender_platform_user_id = 101
            message.ordering_key = "application-direct:zhangsan"
            message.execution_batch_id = 1
            message.batch_ordinal = 1
            db.add_all(
                [
                    _application_channel(),
                    _lease(),
                    batch,
                    message,
                    _checkpoint(1),
                ]
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            await db.refresh(message)
            assert claimed is None
            assert message.status == "queued"
            assert message.processing_owner is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_state", ["submitting", "running"])
async def test_personal_compat_claim_recovers_sealed_batch_retry(
    batch_state: str,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:personal-session:40"
            batch = _execution_batch(
                1,
                state=batch_state,
                ordering_key="personal-session:40",
                execution_slot_key=lane,
            )
            batch.response_inbound_message_id = 1
            message = _message(
                1,
                status="queued",
                received_at=datetime.now(timezone.utc),
            )
            message.ordering_key = "personal-session:40"
            message.execution_batch_id = 1
            message.batch_ordinal = 1
            db.add_all(
                [_channel(), _lease(), batch, message, _checkpoint(1)]
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            assert claimed is not None
            assert claimed.inbound_message_id == 1
            assert message.status == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_state", ["submitting", "running"])
async def test_application_compat_claim_recovers_sealed_batch_retry(
    batch_state: str,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            lane = "1:application-direct:zhangsan"
            batch = _execution_batch(
                1,
                state=batch_state,
                ordering_key="application-direct:zhangsan",
                execution_slot_key=lane,
            )
            batch.target_type = "application_agent"
            batch.response_inbound_message_id = 1
            message = _message(
                1,
                status="queued",
                received_at=datetime.now(timezone.utc),
            )
            message.target_type = "application_agent"
            message.application_agent_id = 50
            message.application_client_id = 60
            message.sender_platform_user_id = 101
            message.ordering_key = "application-direct:zhangsan"
            message.execution_batch_id = 1
            message.batch_ordinal = 1
            db.add_all(
                [
                    _application_channel(),
                    _lease(),
                    batch,
                    message,
                    _checkpoint(1),
                ]
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            assert claimed is not None
            assert claimed.inbound_message_id == 1
            assert message.status == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_durable_fast_lane_ticket_releases_its_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_text(2),
            )
            third = await queue.ingest(
                channel_id=1,
                message=_normalized_text(3),
            )
            claimed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            assert claimed.inbound_message_id == second.inbound_message_id
            assert await queue.mark_fast_lane_runtime_durable(
                inbound_message_id=claimed.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                disposition_seq=1,
                disposition_state="ordinary_run_accepted",
                canonical_run_id="run-fast-lane-second",
                parent_run_id=None,
                start_after_seq=0,
            )
            successor = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert successor is not None
            assert successor.inbound_message_id == third.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_application_failure_settles_ticket_and_releases_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    41,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    42,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            for inbound_message_id in (
                first.inbound_message_id,
                second.inbound_message_id,
            ):
                assert await queue.mark_identity_resolved(
                    inbound_message_id=inbound_message_id,
                    platform_user_id=101,
                ) == "queued"

            first_row = await db.get(
                ImInboundMessage,
                first.inbound_message_id,
            )
            assert first_row is not None
            first_row.dispatch_lane = "ordinary_text"
            await db.commit()
            ordinary = await queue.claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert ordinary is not None
            assert ordinary.inbound_message_id == first.inbound_message_id

            status = await queue.mark_failed(
                inbound_message_id=first.inbound_message_id,
                error_code="ApplicationAgentInvocationError",
                error_message="application_channel_unauthorized",
                max_attempts=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            first_row = await db.get(
                ImInboundMessage, first.inbound_message_id
            )
            assert first_row is not None
            assert status == "dead_letter"
            assert first_row.status == "dead_letter"
            assert first_row.ingress_ticket_state == "dead_letter"
            assert first_row.terminal_outcome_digest is not None
            successor = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert successor is not None
            assert successor.inbound_message_id == second.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retryable_application_failure_keeps_successor_blocked() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    51,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    52,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            for inbound_message_id in (
                first.inbound_message_id,
                second.inbound_message_id,
            ):
                assert await queue.mark_identity_resolved(
                    inbound_message_id=inbound_message_id,
                    platform_user_id=101,
                ) == "queued"
            first_row = await db.get(
                ImInboundMessage,
                first.inbound_message_id,
            )
            assert first_row is not None
            first_row.dispatch_lane = "ordinary_text"
            await db.commit()
            assert await queue.claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            ) is not None

            status = await queue.mark_failed(
                inbound_message_id=first.inbound_message_id,
                error_code="ApplicationAgentInvocationError",
                error_message="temporary",
                max_attempts=3,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            first_row = await db.get(
                ImInboundMessage, first.inbound_message_id
            )
            assert first_row is not None
            assert status == "queued"
            assert first_row.ingress_ticket_state == "pending"
            assert await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            ) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_failure_preserves_runtime_durable_ticket() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            message = _message(
                1,
                status="running",
                received_at=datetime.now(timezone.utc),
            )
            message.target_type = "application_agent"
            message.application_agent_id = 50
            message.ordering_key = "direct:1:101"
            message.ingress_ordering_key = "direct:1:member:abc"
            message.ingress_ticket = 1
            message.ingress_ticket_state = "runtime_durable"
            message.dispatch_lane = "fast_lane_v2"
            message.processing_owner = "backend-a"
            message.processing_lease_epoch = 1
            message.attempt_count = 1
            db.add_all(
                [
                    _application_channel(),
                    _lease(),
                    message,
                    _checkpoint(1),
                ]
            )
            await db.commit()

            status = await ImInboundQueue(db).mark_failed(
                inbound_message_id=1,
                error_code="RuntimeProjectionError",
                error_message="terminal after durable acceptance",
                max_attempts=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            await db.refresh(message)
            assert status == "dead_letter"
            assert message.ingress_ticket_state == "runtime_durable"
            assert message.terminal_outcome_digest is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delivered_application_receipt_cannot_block_newer_message() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            now = datetime.now(timezone.utc)
            old = _message(
                1,
                status="queued",
                received_at=now - timedelta(minutes=1),
            )
            old.target_type = "application_agent"
            old.application_agent_id = 50
            old.application_agent_run_id = 900
            old.ordering_key = "direct:1:101"
            old.identity_status = "resolved"
            old.sender_platform_user_id = 101
            delivered = _checkpoint(1)
            delivered.status = "delivered"
            current = _message(2, status="queued", received_at=now)
            current.target_type = "application_agent"
            current.application_agent_id = 50
            current.ordering_key = "direct:1:101"
            current.identity_status = "resolved"
            current.sender_platform_user_id = 101
            current.dispatch_lane = "ordinary_text"
            current.ingress_ordering_key = "direct:1:member:abc"
            current.ingress_ticket = 1
            current.ingress_ticket_state = "pending"
            db.add_all(
                [
                    _application_channel(),
                    _lease(),
                    old,
                    delivered,
                    current,
                    _checkpoint(2),
                ]
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            assert claimed is not None
            assert claimed.inbound_message_id == 2
            assert old.status == "completed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_promoted_application_run_releases_ingress_fifo() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            now = datetime.now(timezone.utc)
            old = _message(
                1,
                status="running",
                received_at=now - timedelta(minutes=1),
            )
            old.target_type = "application_agent"
            old.application_agent_id = 50
            old.ordering_key = "direct:1:101"
            old.identity_status = "resolved"
            old.sender_platform_user_id = 101
            old.dispatch_lane = "fast_lane_v2"
            old.ingress_ordering_key = "direct:1:member:abc"
            old.ingress_ticket = 1
            old.ingress_ticket_state = "runtime_durable"
            old.runtime_disposition_state = "promoted"
            old.runtime_canonical_run_id = "promoted-run-1"
            old.processing_owner = "backend-a"
            old.processing_lease_epoch = 1
            current = _message(2, status="queued", received_at=now)
            current.target_type = "application_agent"
            current.application_agent_id = 50
            current.ordering_key = "direct:1:101"
            current.identity_status = "resolved"
            current.sender_platform_user_id = 101
            current.dispatch_lane = "ordinary_text"
            current.ingress_ordering_key = "direct:1:member:abc"
            current.ingress_ticket = 2
            current.ingress_ticket_state = "pending"
            db.add_all(
                [
                    _application_channel(),
                    _lease(),
                    old,
                    _checkpoint(1),
                    current,
                    _checkpoint(2),
                ]
            )
            await db.execute(
                text(
                    """
                    INSERT INTO application_agent_run(
                        id, runtime_run_id, status, error_code
                    ) VALUES (
                        901, 'promoted-run-1', 'failed',
                        'RUNTIME_ADMISSION_HISTORY_CHECKPOINT_FAILED'
                    )
                    """
                )
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            assert claimed is not None
            assert claimed.inbound_message_id == 2
            await db.refresh(old)
            checkpoint = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id == 1
                )
            )
            assert old.status == "failed"
            assert old.application_agent_run_id == 901
            assert (
                old.error_code
                == "RUNTIME_ADMISSION_HISTORY_CHECKPOINT_FAILED"
            )
            assert checkpoint is not None
            assert checkpoint.status == "delivery_failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fast_lane_alias_does_not_replace_earliest_delivery_owner() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_text(20),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_text(21),
            )
            first_claim = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert first_claim is not None
            assert await queue.mark_fast_lane_runtime_durable(
                inbound_message_id=first.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                disposition_seq=2,
                disposition_state="redirect_consumed",
                canonical_run_id="run-parent-shared",
                parent_run_id="run-parent-shared",
                start_after_seq=10,
            )
            second_claim = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert second_claim is not None
            assert await queue.mark_fast_lane_runtime_durable(
                inbound_message_id=second.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                disposition_seq=2,
                disposition_state="redirect_consumed",
                canonical_run_id="run-parent-shared",
                parent_run_id="run-parent-shared",
                start_after_seq=20,
            )

            assert await queue.is_fast_lane_delivery_owner(
                inbound_message_id=first.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert not await queue.is_fast_lane_delivery_owner(
                inbound_message_id=second.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_durable_fast_lane_row_remains_resume_claimable() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            receipt = await queue.ingest(
                channel_id=1,
                message=_normalized_text(22),
            )
            claimed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            assert await queue.mark_fast_lane_runtime_durable(
                inbound_message_id=receipt.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                disposition_seq=1,
                disposition_state="ordinary_run_accepted",
                canonical_run_id="run-resumable",
                parent_run_id=None,
                start_after_seq=0,
            )
            assert await queue.mark_fast_lane_retry_wait(
                inbound_message_id=receipt.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="CONNECTION_LOST",
                error_message="resume the same delivery",
            )
            row = await db.get(ImInboundMessage, receipt.inbound_message_id)
            assert row is not None
            row.next_attempt_at = None
            await db.commit()

            resumed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert resumed is not None
            assert resumed.delivery_id == receipt.delivery_id
            assert resumed.runtime_disposition_seq == 1
            assert resumed.runtime_canonical_run_id == "run-resumable"
            assert resumed.runtime_start_after_seq == 0
            durable = await db.get(
                ImInboundMessage,
                receipt.inbound_message_id,
            )
            assert durable is not None
            assert durable.ingress_ticket_state == "runtime_durable"
    finally:
        await engine.dispose()


async def _mark_fast_lane_checkpoint_terminal(
    db: AsyncSession,
    queue: ImInboundQueue,
    *,
    inbound_message_id: int,
    canonical_run_id: str,
    checkpoint_status: str = "completed",
) -> ImDeliveryCheckpoint:
    claimed = await queue.claim_next_fast_lane(
        channel_id=1,
        owner_instance_id="backend-a",
        lease_epoch=1,
    )
    assert claimed is not None
    assert claimed.inbound_message_id == inbound_message_id
    assert await queue.mark_fast_lane_runtime_durable(
        inbound_message_id=inbound_message_id,
        owner_instance_id="backend-a",
        lease_epoch=1,
        disposition_seq=1,
        disposition_state="ordinary_run_accepted",
        canonical_run_id=canonical_run_id,
        parent_run_id=None,
        start_after_seq=0,
    )
    checkpoint = await db.scalar(
        select(ImDeliveryCheckpoint).where(
            ImDeliveryCheckpoint.inbound_message_id
            == inbound_message_id
        )
    )
    assert checkpoint is not None
    checkpoint.status = checkpoint_status
    await db.commit()
    return checkpoint


@pytest.mark.asyncio
async def test_terminal_fast_lane_checkpoint_releases_next_delivery() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_text(26),
            )
            checkpoint = await _mark_fast_lane_checkpoint_terminal(
                db,
                queue,
                inbound_message_id=first.inbound_message_id,
                canonical_run_id="run-already-delivered",
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_text(27),
            )

            successor = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
                active_delivery_ids=set(),
            )

            first_row = await db.get(
                ImInboundMessage, first.inbound_message_id
            )
            await db.refresh(checkpoint)
            assert first_row is not None
            assert first_row.status == "completed"
            assert first_row.processing_owner is None
            assert first_row.processing_lease_epoch is None
            assert first_row.error_code is None
            assert first_row.error_message is None
            assert checkpoint.status == "delivered"
            assert successor is not None
            assert successor.inbound_message_id == second.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_application_fast_lane_checkpoint_releases_next_delivery(
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    61,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            assert await queue.mark_identity_resolved(
                inbound_message_id=first.inbound_message_id,
                platform_user_id=101,
            ) == "queued"
            await _mark_fast_lane_checkpoint_terminal(
                db,
                queue,
                inbound_message_id=first.inbound_message_id,
                canonical_run_id="app-run-already-delivered",
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    62,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            assert await queue.mark_identity_resolved(
                inbound_message_id=second.inbound_message_id,
                platform_user_id=101,
            ) == "queued"

            successor = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
                active_delivery_ids=set(),
            )

            first_row = await db.get(
                ImInboundMessage, first.inbound_message_id
            )
            assert first_row is not None
            assert first_row.status == "completed"
            assert successor is not None
            assert successor.inbound_message_id == second.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.parametrize("checkpoint_status", ["completed", "delivered"])
@pytest.mark.asyncio
async def test_retry_does_not_revive_terminal_fast_lane_checkpoint(
    checkpoint_status: str,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            receipt = await queue.ingest(
                channel_id=1,
                message=_normalized_text(28),
            )
            checkpoint = await _mark_fast_lane_checkpoint_terminal(
                db,
                queue,
                inbound_message_id=receipt.inbound_message_id,
                canonical_run_id="run-terminal-before-retry",
                checkpoint_status=checkpoint_status,
            )
            row = await db.get(
                ImInboundMessage, receipt.inbound_message_id
            )
            assert row is not None
            row.error_code = "OLD_RETRY_ERROR"
            row.error_message = "must be cleared"
            checkpoint.error_code = "OLD_DELIVERY_ERROR"
            checkpoint.error_message = "must be cleared"
            await db.commit()

            assert await queue.mark_fast_lane_retry_wait(
                inbound_message_id=receipt.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="FAST_LANE_TASK_CANCELLED",
                error_message="must not revive terminal delivery",
            )

            await db.refresh(row)
            await db.refresh(checkpoint)
            assert row.status == "completed"
            assert row.next_attempt_at is None
            assert row.processing_owner is None
            assert row.processing_lease_epoch is None
            assert row.error_code is None
            assert row.error_message is None
            assert checkpoint.status == "delivered"
            assert checkpoint.error_code is None
            assert checkpoint.error_message is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_current_owner_fast_lane_claim_uses_local_activity_fact() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            receipt = await queue.ingest(
                channel_id=1,
                message=_normalized_text(23),
            )
            claimed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
                active_delivery_ids=set(),
            )
            assert claimed is not None

            assert await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
                active_delivery_ids={receipt.delivery_id},
            ) is None
            recovered = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
                active_delivery_ids=set(),
            )

            assert recovered is not None
            assert recovered.inbound_message_id == receipt.inbound_message_id
            assert recovered.delivery_id == receipt.delivery_id
            assert recovered.client_message_id == receipt.client_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fast_lane_validation_rejection_releases_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_text(24),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_text(25),
            )
            claimed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None

            assert await queue.mark_fast_lane_validation_rejected(
                inbound_message_id=first.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="MODEL_NOT_AVAILABLE",
                error_message="no visible model",
            )
            assert not await queue.mark_fast_lane_validation_rejected(
                inbound_message_id=first.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="MODEL_NOT_AVAILABLE",
                error_message="no visible model",
            )

            row = await db.get(
                ImInboundMessage,
                first.inbound_message_id,
            )
            assert row is not None
            assert row.status == "failed"
            assert row.ingress_ticket_state == "validation_rejected"
            assert row.terminal_outcome_digest is not None
            successor = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert successor is not None
            assert successor.inbound_message_id == second.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_route_exhaustion_releases_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_text(26, sender_member_id="member-1"),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_text(27, sender_member_id="member-1"),
            )
            assert await queue.mark_identity_resolved(
                inbound_message_id=first.inbound_message_id,
                platform_user_id=101,
            ) == "queued"
            assert await queue.mark_identity_resolved(
                inbound_message_id=second.inbound_message_id,
                platform_user_id=101,
            ) == "queued"
            claimed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            row = await db.get(ImInboundMessage, first.inbound_message_id)
            assert row is not None
            row.attempt_count = 5
            await db.commit()

            assert await queue.mark_fast_lane_retry_wait(
                inbound_message_id=first.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="FAST_LANE_ROUTE_UNAVAILABLE",
                error_message="application route unavailable",
            ) == "failed"
            await db.refresh(row)
            assert row.status == "failed"
            assert row.ingress_ticket_state == "dead_letter"

            successor = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert successor is not None
            assert successor.inbound_message_id == second.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_message_locks_lease_before_message() -> None:
    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object:
            return self.value

    class Session:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.scalar_values = [
                1,
                SimpleNamespace(channel_id=1),
            ]

        async def scalar(self, statement: object) -> object:
            self.statements.append(str(statement))
            return self.scalar_values.pop(0)

        async def execute(self, statement: object) -> Result:
            self.statements.append(str(statement))
            return Result(SimpleNamespace(id=9))

        async def rollback(self) -> None:
            raise AssertionError("valid ownership must not roll back")

    session = Session()
    row = await ImInboundQueue(  # type: ignore[arg-type]
        session
    )._owned_message(
        inbound_message_id=9,
        owner_instance_id="backend-a",
        lease_epoch=1,
        expected_status="running",
    )

    assert row is not None
    assert "im_inbound_message.channel_id" in session.statements[0]
    assert "FROM im_channel_lease" in session.statements[1]
    assert "FROM im_inbound_message" in session.statements[2]
    assert "JOIN im_channel_lease" not in session.statements[2]


@pytest.mark.asyncio
async def test_delivery_progress_uses_lease_inbound_checkpoint_lock_order() -> None:
    inbound = SimpleNamespace(
        id=9,
        first_provider_dispatch_at=None,
        first_provider_confirmed_at=None,
    )
    checkpoint = SimpleNamespace(
        last_runtime_event_seq=2,
        reasoning_text="",
        activity_text="",
        message_text="",
        final_text="",
        provider_delivered_text="",
        delivery_mode="passive_stream",
        status="pending",
        error_code=None,
        error_message=None,
        last_delivered_at=None,
    )

    class Result:
        def scalar_one_or_none(self) -> object:
            return inbound

    class Session:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.scalar_values = [
                1,
                SimpleNamespace(channel_id=1),
                checkpoint,
            ]
            self.committed = False

        async def scalar(self, statement: object) -> object:
            self.statements.append(str(statement))
            return self.scalar_values.pop(0)

        async def execute(self, statement: object) -> Result:
            self.statements.append(str(statement))
            return Result()

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            raise AssertionError("valid progress must not roll back")

    session = Session()
    result = await ImInboundQueue(  # type: ignore[arg-type]
        session
    ).persist_delivery_progress(
        inbound_message_id=9,
        owner_instance_id="backend-a",
        lease_epoch=1,
        last_runtime_event_seq=3,
        reasoning_text="思考",
        activity_text="活动",
        message_text="回答",
        final_text="最终",
        provider_delivered_text="最终",
        delivery_mode="passive_stream",
        status="streaming",
        error_code=None,
        error_message=None,
        first_provider_dispatch_at=datetime.now(timezone.utc),
        first_provider_confirmed_at=datetime.now(timezone.utc),
    )

    assert result.persisted is True
    assert result.first_provider_confirmation_recorded is True
    assert session.committed is True
    assert "im_inbound_message.channel_id" in session.statements[0]
    assert "FROM im_channel_lease" in session.statements[1]
    assert "FROM im_inbound_message" in session.statements[2]
    assert "FROM im_delivery_checkpoint" in session.statements[3]
    assert "FOR UPDATE" in session.statements[1]
    assert "FOR UPDATE" in session.statements[2]
    assert "FOR UPDATE" in session.statements[3]
    assert checkpoint.last_runtime_event_seq == 3


@pytest.mark.asyncio
async def test_application_completion_retries_one_mysql_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SimpleNamespace(rollback=AsyncMock())
    queue = ImInboundQueue(db)  # type: ignore[arg-type]
    complete_once = AsyncMock(
        side_effect=[
            _mysql_operational_error(1213),
            True,
        ]
    )
    monkeypatch.setattr(
        queue,
        "_mark_application_completed_once",
        complete_once,
    )
    sleep = AsyncMock()
    monkeypatch.setattr(queue_module.asyncio, "sleep", sleep)

    result = await queue.mark_application_completed(
        inbound_message_id=9,
        application_agent_run_id=19,
        owner_instance_id="backend-a",
        lease_epoch=1,
    )

    assert result is True
    assert complete_once.await_count == 2
    db.rollback.assert_awaited_once()
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_application_failure_retries_one_mysql_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SimpleNamespace(rollback=AsyncMock())
    queue = ImInboundQueue(db)  # type: ignore[arg-type]
    fail_once = AsyncMock(
        side_effect=[
            _mysql_operational_error(1213),
            "queued",
        ]
    )
    monkeypatch.setattr(queue, "_mark_failed_once", fail_once)
    sleep = AsyncMock()
    monkeypatch.setattr(queue_module.asyncio, "sleep", sleep)

    result = await queue.mark_failed(
        inbound_message_id=9,
        error_code="HermesProtocolError",
        error_message="Runtime control failed with status 422",
        max_attempts=3,
        owner_instance_id="backend-a",
        lease_epoch=1,
    )

    assert result == "queued"
    assert fail_once.await_count == 2
    db.rollback.assert_awaited_once()
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_delivered_application_receipt_releases_successor() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            now = datetime.now(timezone.utc)
            db.add_all([_application_channel(), _lease()])
            await db.execute(
                text(
                    "INSERT INTO application_agent_run "
                    "(id, runtime_run_id, status, error_code) "
                    "VALUES (445, 'run-445', 'completed', NULL)"
                )
            )
            stuck = _message(1, status="running", received_at=now)
            stuck.target_type = "application_agent"
            stuck.application_agent_id = 50
            stuck.application_client_id = 60
            stuck.sender_platform_user_id = 101
            stuck.ordering_key = "application-direct:zhangsan"
            stuck.ingress_ordering_key = "application-direct:zhangsan"
            stuck.ingress_ticket = 37
            stuck.ingress_ticket_state = "runtime_durable"
            stuck.dispatch_lane = "ordinary_text"
            stuck.application_agent_run_id = 445
            stuck.processing_owner = "backend-a"
            stuck.processing_lease_epoch = 1
            stuck_checkpoint = _checkpoint(1)
            stuck_checkpoint.status = "delivered"

            successor = _message(
                2,
                status="queued",
                received_at=now + timedelta(seconds=1),
            )
            successor.target_type = "application_agent"
            successor.application_agent_id = 50
            successor.application_client_id = 60
            successor.sender_platform_user_id = 101
            successor.ordering_key = "application-direct:zhangsan"
            successor.ingress_ordering_key = "application-direct:zhangsan"
            successor.ingress_ticket = 38
            successor.ingress_ticket_state = "pending"
            successor.dispatch_lane = "ordinary_text"
            db.add_all(
                [
                    stuck,
                    stuck_checkpoint,
                    successor,
                    _checkpoint(2),
                ]
            )
            await db.commit()

            claimed = await ImInboundQueue(db).claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            await db.refresh(stuck)
            assert stuck.status == "completed"
            assert stuck.processing_owner is None
            assert stuck.processing_lease_epoch is None
            assert claimed is not None
            assert claimed.inbound_message_id == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_completion_clears_stale_retry_error() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            message = _message(
                9,
                status="running",
                received_at=datetime.now(timezone.utc),
            )
            message.target_type = "application_agent"
            message.processing_owner = "backend-a"
            message.processing_lease_epoch = 1
            message.error_code = "HermesProtocolError"
            message.error_message = "stale retry failure"
            checkpoint = _checkpoint(9)
            checkpoint.status = "streaming"
            checkpoint.error_code = "OLD_DELIVERY_ERROR"
            checkpoint.error_message = "stale delivery failure"
            db.add_all(
                [
                    _application_channel(),
                    _lease(),
                    message,
                    checkpoint,
                ]
            )
            await db.commit()

            assert await ImInboundQueue(db).mark_application_completed(
                inbound_message_id=9,
                application_agent_run_id=None,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            await db.refresh(message)
            await db.refresh(checkpoint)
            assert message.status == "completed"
            assert message.error_code is None
            assert message.error_message is None
            assert checkpoint.status == "delivered"
            assert checkpoint.error_code is None
            assert checkpoint.error_message is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_completion_does_not_retry_other_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SimpleNamespace(rollback=AsyncMock())
    queue = ImInboundQueue(db)  # type: ignore[arg-type]
    complete_once = AsyncMock(
        side_effect=_mysql_operational_error(1205)
    )
    monkeypatch.setattr(
        queue,
        "_mark_application_completed_once",
        complete_once,
    )

    with pytest.raises(OperationalError):
        await queue.mark_application_completed(
            inbound_message_id=9,
            application_agent_run_id=19,
            owner_instance_id="backend-a",
            lease_epoch=1,
        )

    assert complete_once.await_count == 1
    db.rollback.assert_not_awaited()


def _normalized_application_group(
    message_id: int,
    *,
    group_id: str,
    member_id: str,
) -> NormalizedWeComMessage:
    return NormalizedWeComMessage(
        external_message_id=f"app-inbound-{message_id}",
        provider_request_id=f"app-request-{message_id}",
        external_conversation_id=group_id,
        sender_member_id=member_id,
        chat_type="group",
        bot_mentioned=True,
        message_type="text",
        text=f"message-{message_id}",
        quote_payload=None,
    )


def test_im_timing_payload_uses_only_ordered_safe_boundaries() -> None:
    started = datetime(2026, 7, 30, tzinfo=timezone.utc)
    row = _message(
        101,
        status="running",
        received_at=started,
    )
    row.target_type = "application_agent"
    row.db_committed_at = started + timedelta(milliseconds=4)
    row.identity_resolved_at = started + timedelta(milliseconds=9)
    row.scheduler_claimed_at = started + timedelta(milliseconds=15)
    row.run_created_at = started + timedelta(milliseconds=20)
    row.runtime_submitted_at = started + timedelta(milliseconds=30)
    row.first_runtime_public_event_at = (
        started + timedelta(milliseconds=50)
    )
    row.first_provider_dispatch_at = (
        started + timedelta(milliseconds=52)
    )
    row.first_provider_confirmed_at = (
        started + timedelta(milliseconds=56)
    )

    payload = im_timing_payload(
        row,
        stage="first_provider_confirmed",
        runtime_run_id="app_run_safe",
    )

    assert payload["request_id"] == "im-wecom-101"
    assert payload["runtime_run_id"] == "app_run_safe"
    assert payload["durations_ms"] == {
        "provider_received_to_db_committed": 4.0,
        "db_committed_to_identity_resolved": 5.0,
        "db_committed_to_scheduler_claimed": 11.0,
        "identity_resolved_to_scheduler_claimed": 6.0,
        "scheduler_claimed_to_run_created": 5.0,
        "run_created_to_runtime_submitted": 10.0,
        "runtime_submitted_to_first_runtime_public_event": 20.0,
        "first_runtime_public_event_to_first_provider_dispatch": 2.0,
        "first_provider_dispatch_to_first_provider_confirmed": 4.0,
        "provider_received_to_first_provider_confirmed": 56.0,
    }
    assert "provider_request_id" not in payload
    assert "runtime_run_id" not in im_timing_payload(
        row,
        stage="first_provider_confirmed",
        runtime_run_id="unsafe run id",
    )


@pytest.mark.parametrize(
    "terminal_status", ["completed", "failed", "dead_letter"]
)
@pytest.mark.asyncio
async def test_ingest_marks_first_and_terminal_followup_as_immediate(
    terminal_status: str,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add(_channel())
            await db.commit()

            first = await ImInboundQueue(db).ingest(
                channel_id=1,
                message=_normalized_text(1),
            )
            assert first.has_unfinished_predecessor is False

            first_row = await db.get(
                ImInboundMessage, first.inbound_message_id
            )
            assert first_row is not None
            first_row.status = terminal_status
            await db.commit()

            followup = await ImInboundQueue(db).ingest(
                channel_id=1,
                message=_normalized_text(2),
            )
            assert followup.has_unfinished_predecessor is False
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "predecessor_status",
    ["received", "staging_media", "queued", "running"],
)
@pytest.mark.asyncio
async def test_ingest_marks_message_waiting_for_unfinished_predecessor(
    predecessor_status: str,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            now = datetime.now(timezone.utc)
            db.add_all(
                [
                    _channel(),
                    _message(
                        100,
                        status=predecessor_status,
                        received_at=now,
                    ),
                ]
            )
            await db.commit()

            receipt = await ImInboundQueue(db).ingest(
                channel_id=1,
                message=_normalized_text(101),
            )

            assert receipt.has_unfinished_predecessor is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_ingest_rejects_above_derived_queue_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    monkeypatch.setattr(
        queue_module.settings,
        "APPLICATION_AGENT_MAX_INFLIGHT_RUNS",
        1,
    )
    try:
        async with sessions() as db:
            db.add(_application_channel())
            await db.commit()
            queue = ImInboundQueue(db)
            for index in range(4):
                receipt = await queue.ingest(
                    channel_id=1,
                    message=_normalized_application_group(
                        index,
                        group_id=f"group-{index}",
                        member_id=f"member-{index}",
                    ),
                )
                assert receipt.status == "awaiting_identity"

            rejected = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    99,
                    group_id="group-overflow",
                    member_id="member-overflow",
                ),
            )
            assert rejected.status == "dead_letter"
            row = await db.get(
                ImInboundMessage,
                rejected.inbound_message_id,
            )
            assert row is not None
            assert row.error_code == "APPLICATION_CAPACITY_QUEUE_FULL"
            assert row.finished_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_wait_does_not_spend_failure_attempt_and_times_out() -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            message = _message(101, status="running", received_at=now)
            message.target_type = "application_agent"
            message.application_agent_id = 50
            message.processing_owner = "backend-a"
            message.processing_lease_epoch = 1
            message.attempt_count = 1
            db.add_all([_application_channel(), _lease(), message])
            await db.commit()
            queue = ImInboundQueue(db)

            assert await queue.mark_capacity_wait(
                inbound_message_id=101,
                owner_instance_id="backend-a",
                lease_epoch=1,
            ) == "capacity_wait"
            assert message.status == "queued"
            assert message.attempt_count == 0
            assert message.capacity_wait_attempt_count == 1
            assert message.next_attempt_at is not None

            message.status = "running"
            message.processing_owner = "backend-a"
            message.processing_lease_epoch = 1
            message.attempt_count = 1
            message.capacity_wait_started_at = now - timedelta(seconds=61)
            await db.commit()

            assert await queue.mark_capacity_wait(
                inbound_message_id=101,
                owner_instance_id="backend-a",
                lease_epoch=1,
            ) == "capacity_timeout"
            assert message.status == "dead_letter"
            assert message.attempt_count == 0
            assert message.error_code == "CAPACITY_TIMEOUT"
            assert message.finished_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_media_staging_retries_with_backoff_and_preserves_fifo() -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            first = _message(
                101, status="staging_media", received_at=now
            )
            second = _message(
                102,
                status="queued",
                received_at=now + timedelta(milliseconds=1),
            )
            db.add_all(
                [
                    _channel(),
                    _lease(),
                    first,
                    second,
                    _checkpoint(101),
                    _checkpoint(102),
                    ImMessageAttachment(
                        id=201,
                        inbound_message_id=101,
                        ordinal=0,
                        media_type="image",
                        provider_media_metadata_encrypted="temporary-secret",
                        staging_status="pending",
                    ),
                ]
            )
            await db.commit()

            queue = ImInboundQueue(db)
            assert (
                await queue.claim_staging(
                    channel_id=1,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                == 101
            )
            assert first.attempt_count == 1

            status = await queue.mark_staging_failed(
                101,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="TimeoutError",
                error_message="temporary",
                max_attempts=2,
            )
            assert status == "staging_media"
            assert first.next_attempt_at is not None
            attachment = await db.get(ImMessageAttachment, 201)
            assert attachment is not None
            assert attachment.staging_status == "pending"
            assert (
                attachment.provider_media_metadata_encrypted
                == "temporary-secret"
            )

            # The backed-off media message remains the head of line, so the
            # later text cannot overtake it.
            assert (
                await queue.claim_staging(
                    channel_id=1,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                is None
            )
            assert (
                await queue.claim_next(
                    channel_id=1,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                is None
            )

            first.next_attempt_at = now - timedelta(seconds=1)
            await db.commit()
            assert (
                await queue.claim_staging(
                    channel_id=1,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                == 101
            )
            status = await queue.mark_staging_failed(
                101,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="TimeoutError",
                error_message="terminal",
                max_attempts=2,
            )
            assert status == "dead_letter"
            assert first.finished_at is not None
            assert attachment.staging_status == "failed"
            assert attachment.provider_media_metadata_encrypted is None

            claimed = await queue.claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            assert claimed.inbound_message_id == 102
            assert claimed.owner_instance_id == "backend-a"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_old_epoch_cannot_finish_media_or_runtime_queue_state() -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            staging = _message(
                101, status="staging_media", received_at=now
            )
            db.add_all(
                [
                    _channel(),
                    _lease(),
                    staging,
                    _checkpoint(101),
                ]
            )
            await db.commit()
            queue = ImInboundQueue(db)
            assert (
                await queue.claim_staging(
                    channel_id=1,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                == 101
            )

            lease = await db.get(ImChannelLease, 1)
            assert lease is not None
            lease.owner_instance_id = "backend-b"
            lease.lease_epoch = 2
            await db.commit()

            assert (
                await queue.finish_staging(
                    101,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                is False
            )
            await db.refresh(staging)
            assert staging.status == "staging_media"
            assert staging.processing_owner == "backend-a"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_media_staging_resets_runtime_retry_budget() -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            staging = _message(
                101, status="staging_media", received_at=now
            )
            db.add_all(
                [
                    _channel(),
                    _lease(),
                    staging,
                    _checkpoint(101),
                ]
            )
            await db.commit()
            queue = ImInboundQueue(db)
            assert (
                await queue.claim_staging(
                    channel_id=1,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                )
                == 101
            )
            staging.attempt_count = 2
            await db.commit()

            assert await queue.finish_staging(
                101,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert staging.status == "queued"
            assert staging.attempt_count == 0

            claimed = await queue.claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            assert claimed.attempt_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_busy_requeues_without_spending_failure_budget() -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            message = _message(101, status="queued", received_at=now)
            db.add_all(
                [_channel(), _lease(), message, _checkpoint(101)]
            )
            await db.commit()
            queue = ImInboundQueue(db)
            claimed = await queue.claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            assert message.attempt_count == 1

            assert await queue.mark_session_busy_wait(
                inbound_message_id=101,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert message.status == "queued"
            assert message.attempt_count == 0
            assert message.next_attempt_at is not None
            assert message.error_code == "SESSION_BUSY"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_artifact_delivery_checkpoint_is_retryable_and_fenced() -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            message = _message(101, status="queued", received_at=now)
            checkpoint = _checkpoint(101)
            db.add_all(
                [_channel(), _lease(), message, checkpoint]
            )
            await db.commit()
            queue = ImInboundQueue(db)
            claimed = await queue.claim_next(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None

            assert await queue.mark_artifact_delivery_failed(
                inbound_message_id=101,
                owner_instance_id="backend-a",
                lease_epoch=1,
                error_code="BRIDGE_TIMEOUT",
                error_message="retry",
            )
            assert checkpoint.attempt_count == 1
            assert checkpoint.error_code == (
                "WECOM_MEDIA_BRIDGE_TIMEOUT"
            )

            assert await queue.mark_artifact_delivered(
                inbound_message_id=101,
                artifact_id=501,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert checkpoint.delivered_artifact_ids == [501]
            assert await queue.clear_artifact_delivery_failure(
                inbound_message_id=101,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert checkpoint.error_code is None
            assert checkpoint.error_message is None

            lease = await db.get(ImChannelLease, 1)
            assert lease is not None
            lease.owner_instance_id = "backend-b"
            lease.lease_epoch = 2
            await db.commit()
            assert not await queue.mark_artifact_delivered(
                inbound_message_id=101,
                artifact_id=502,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            await db.refresh(checkpoint)
            assert checkpoint.delivered_artifact_ids == [501]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_queue_serializes_one_sender_but_runs_other_senders() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    1,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    2,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            other_sender = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    3,
                    group_id="group-a",
                    member_id="lisi",
                ),
            )
            assert first.status == second.status == other_sender.status == (
                "awaiting_identity"
            )
            assert await queue.mark_identity_resolved(
                inbound_message_id=first.inbound_message_id,
                platform_user_id=101,
            ) == "queued"
            assert await queue.mark_identity_resolved(
                inbound_message_id=second.inbound_message_id,
                platform_user_id=101,
            ) == "queued"
            assert await queue.mark_identity_resolved(
                inbound_message_id=other_sender.inbound_message_id,
                platform_user_id=102,
            ) == "queued"

            claimed_first = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            claimed_parallel = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )

            assert claimed_first is not None
            assert claimed_parallel is not None
            assert claimed_first.inbound_message_id == first.inbound_message_id
            assert claimed_parallel.inbound_message_id == (
                other_sender.inbound_message_id
            )
            assert claimed_first.ingress_ordering_key != (
                claimed_parallel.ingress_ordering_key
            )

            assert await queue.mark_fast_lane_runtime_durable(
                inbound_message_id=claimed_first.inbound_message_id,
                owner_instance_id="backend-a",
                lease_epoch=1,
                disposition_seq=1,
                disposition_state="ordinary_run_accepted",
                canonical_run_id="run-fast-lane-first",
                parent_run_id=None,
                start_after_seq=0,
            )
            same_sender_followup = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert same_sender_followup is not None
            assert same_sender_followup.inbound_message_id == (
                second.inbound_message_id
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_ticket_order_ignores_identity_resolution_latency() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            first = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    11,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )
            second = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    12,
                    group_id="group-a",
                    member_id="zhangsan",
                ),
            )

            assert await queue.mark_identity_resolved(
                inbound_message_id=second.inbound_message_id,
                platform_user_id=101,
            ) == "queued"
            assert await queue.mark_identity_resolved(
                inbound_message_id=first.inbound_message_id,
                platform_user_id=101,
            ) == "queued"

            first_row = await db.get(
                ImInboundMessage, first.inbound_message_id
            )
            second_row = await db.get(
                ImInboundMessage, second.inbound_message_id
            )
            assert first_row is not None
            assert second_row is not None
            assert first_row.execution_batch_id is None
            assert second_row.execution_batch_id is None
            assert (first_row.ingress_ticket, second_row.ingress_ticket) == (1, 2)
            claimed = await queue.claim_next_fast_lane(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            )
            assert claimed is not None
            assert claimed.inbound_message_id == first.inbound_message_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unmapped_application_message_is_terminal_until_user_retries() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_application_channel(), _lease()])
            await db.commit()
            queue = ImInboundQueue(db)
            receipt = await queue.ingest(
                channel_id=1,
                message=_normalized_application_group(
                    4,
                    group_id="group-a",
                    member_id="unmapped",
                ),
            )

            assert await queue.mark_identity_required(
                inbound_message_id=receipt.inbound_message_id
            )
            row = await db.get(
                ImInboundMessage,
                receipt.inbound_message_id,
            )
            assert row is not None
            assert row.status == "identity_required"
            assert row.identity_status == "binding_required"
            assert await queue.claim_next_application(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=1,
            ) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_runtime_timing_updates_preserve_observed_order() -> None:
    engine, sessions = await _database()
    started = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            message = _message(
                901,
                status="running",
                received_at=started,
            )
            message.db_committed_at = started + timedelta(milliseconds=2)
            message.scheduler_claimed_at = (
                started + timedelta(milliseconds=4)
            )
            message.run_created_at = (
                started + timedelta(milliseconds=6)
            )
            db.add_all([_channel(), message, _checkpoint(901)])
            await db.commit()
            queue = ImInboundQueue(db)

            # The background tasks may commit in either order. Their captured
            # instants, rather than commit order, define the TTFT boundaries.
            assert await queue.mark_first_runtime_public_event(
                inbound_message_id=901,
                occurred_at=started + timedelta(milliseconds=30),
            )
            assert await queue.mark_runtime_submitted(
                inbound_message_id=901,
                occurred_at=started + timedelta(milliseconds=10),
            )

            row = await db.get(ImInboundMessage, 901)
            assert row is not None
            assert row.runtime_submitted_at == (
                started + timedelta(milliseconds=10)
            )
            assert row.first_runtime_public_event_at == (
                started + timedelta(milliseconds=30)
            )
    finally:
        await engine.dispose()
