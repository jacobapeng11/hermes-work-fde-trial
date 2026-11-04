from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.im_gateway import leases as lease_module
from app.im_gateway.leases import SqlAlchemyImChannelLeaseStore
from app.models import (
    ApplicationAgent,
    ImChannel,
    ImChannelLease,
    RuntimeSession,
)
from app.models.base import Base
from app.services.managed_secret import ManagedSecretCodec


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
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                ApplicationAgent.__table__,
                RuntimeSession.__table__,
                ImChannel.__table__,
                ImChannelLease.__table__,
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _agent(*, status: str) -> ApplicationAgent:
    return ApplicationAgent(
        id=50,
        org_id=10,
        slug="wecom-agent",
        name="企微应用 Agent",
        status=status,
        created_by_user_id=20,
        updated_by_user_id=20,
        config={"max_inflight_runs": 17},
        config_generation=1,
        applied_generation=1 if status == "published" else None,
        apply_status="applied" if status == "published" else "not_applied",
    )


def _channel() -> ImChannel:
    return ImChannel(
        id=1,
        org_id=10,
        target_type="application_agent",
        created_by_user_id=20,
        application_agent_id=50,
        application_client_id=60,
        provider="wecom",
        connection_mode="websocket",
        bot_id="application-bot",
        secret_encrypted=ManagedSecretCodec().encrypt("secret"),
        enabled=True,
        status="connected",
        config_generation=1,
    )


def _personal_channel() -> ImChannel:
    return ImChannel(
        id=2,
        org_id=10,
        target_type="personal_agent",
        created_by_user_id=20,
        user_id=20,
        runtime_workspace_id=30,
        runtime_session_row_id=40,
        provider="wecom",
        connection_mode="websocket",
        bot_id="personal-bot",
        secret_encrypted=ManagedSecretCodec().encrypt("secret"),
        enabled=True,
        status="connected",
        config_generation=1,
    )


def _mysql_operational_error(code: int) -> OperationalError:
    return OperationalError(
        "SELECT im_channel FOR UPDATE",
        {},
        Exception(code, "lease claim failure"),
    )


class _EmptyLeaseRows:
    def all(self) -> list[tuple[Any, Any]]:
        return []


class _LeaseClaimDb:
    def __init__(self, errors: list[OperationalError]) -> None:
        self.errors = list(errors)
        self.events: list[str] = []

    async def execute(self, _statement: object) -> _EmptyLeaseRows:
        self.events.append("execute")
        if self.errors:
            raise self.errors.pop(0)
        return _EmptyLeaseRows()

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def commit(self) -> None:
        self.events.append("commit")


@pytest.mark.asyncio
async def test_claim_available_retries_mysql_1213_once_after_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LeaseClaimDb([_mysql_operational_error(1213)])

    async def sleep(delay: float) -> None:
        db.events.append(f"sleep:{delay}")

    monkeypatch.setattr(lease_module.asyncio, "sleep", sleep)
    store = SqlAlchemyImChannelLeaseStore(
        db,  # type: ignore[arg-type]
        owner_instance_id="backend-a",
        lease_ttl_seconds=30,
    )

    assert await store.claim_available(limit=1) == []
    assert db.events == [
        "execute",
        "rollback",
        "sleep:0.05",
        "execute",
        "commit",
    ]


@pytest.mark.asyncio
async def test_claim_available_raises_second_mysql_1213(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_error = _mysql_operational_error(1213)
    db = _LeaseClaimDb(
        [_mysql_operational_error(1213), second_error]
    )

    async def sleep(delay: float) -> None:
        db.events.append(f"sleep:{delay}")

    monkeypatch.setattr(lease_module.asyncio, "sleep", sleep)
    store = SqlAlchemyImChannelLeaseStore(
        db,  # type: ignore[arg-type]
        owner_instance_id="backend-a",
        lease_ttl_seconds=30,
    )

    with pytest.raises(OperationalError) as raised:
        await store.claim_available(limit=1)

    assert raised.value is second_error
    assert db.events == [
        "execute",
        "rollback",
        "sleep:0.05",
        "execute",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_claim_available_does_not_retry_non_1213_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _mysql_operational_error(1205)
    db = _LeaseClaimDb([error])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(lease_module.asyncio, "sleep", sleep)
    store = SqlAlchemyImChannelLeaseStore(
        db,  # type: ignore[arg-type]
        owner_instance_id="backend-a",
        lease_ttl_seconds=30,
    )

    with pytest.raises(OperationalError) as raised:
        await store.claim_available(limit=1)

    assert raised.value is error
    assert db.events == ["execute"]
    assert sleeps == []


@pytest.mark.parametrize("status", ["unpublished", "disabled"])
@pytest.mark.asyncio
async def test_application_channel_lease_requires_published_agent(
    status: str,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([_agent(status=status), _channel()])
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=10)

            assert grants == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_available_skips_unexpired_lease_owned_by_instance(
) -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    original_heartbeat = now - timedelta(seconds=5)
    original_expiry = now + timedelta(seconds=25)
    try:
        async with sessions() as db:
            lease = ImChannelLease(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=7,
                heartbeat_at=original_heartbeat,
                expires_at=original_expiry,
            )
            db.add_all([_agent(status="published"), _channel(), lease])
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=10, now=now)

            assert grants == []
            await db.refresh(lease)
            assert lease.owner_instance_id == "backend-a"
            assert lease.lease_epoch == 7
            assert lease.heartbeat_at == original_heartbeat
            assert lease.expires_at == original_expiry
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_lease_does_not_consume_capacity_for_new_channel() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    try:
        async with sessions() as db:
            db.add_all(
                [
                    _agent(status="published"),
                    _channel(),
                    ImChannelLease(
                        channel_id=1,
                        owner_instance_id="backend-a",
                        lease_epoch=7,
                        heartbeat_at=now,
                        expires_at=now + timedelta(seconds=30),
                    ),
                    _personal_channel(),
                ]
            )
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=1, now=now)

            assert len(grants) == 1
            assert grants[0].channel_id == 2
            assert grants[0].lease_epoch == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_available_skips_unexpired_foreign_lease() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    try:
        async with sessions() as db:
            lease = ImChannelLease(
                channel_id=1,
                owner_instance_id="backend-b",
                lease_epoch=7,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=30),
            )
            db.add_all([_agent(status="published"), _channel(), lease])
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=10, now=now)

            assert grants == []
            await db.refresh(lease)
            assert lease.owner_instance_id == "backend-b"
            assert lease.lease_epoch == 7
            assert lease.expires_at == now + timedelta(seconds=30)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_still_renews_matching_owned_lease() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    heartbeat_at = now + timedelta(seconds=10)
    try:
        async with sessions() as db:
            lease = ImChannelLease(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=7,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=20),
            )
            db.add_all([_agent(status="published"), _channel(), lease])
            await db.commit()
            store = SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            )

            retained = await store.heartbeat(
                channel_epochs={1: 7},
                now=heartbeat_at,
            )

            assert retained == {1}
            await db.refresh(lease)
            assert lease.heartbeat_at == heartbeat_at
            assert lease.expires_at == heartbeat_at + timedelta(seconds=30)
            assert lease.lease_epoch == 7
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_owned_lease_can_be_reclaimed_without_new_epoch(
) -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    try:
        async with sessions() as db:
            lease = ImChannelLease(
                channel_id=1,
                owner_instance_id="backend-a",
                lease_epoch=7,
                heartbeat_at=now - timedelta(seconds=40),
                expires_at=now - timedelta(seconds=10),
            )
            db.add_all([_agent(status="published"), _channel(), lease])
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=10, now=now)

            assert len(grants) == 1
            assert grants[0].lease_epoch == 7
            await db.refresh(lease)
            assert lease.owner_instance_id == "backend-a"
            assert lease.lease_epoch == 7
            assert lease.heartbeat_at == now
            assert lease.expires_at == now + timedelta(seconds=30)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_foreign_lease_is_taken_over_with_new_epoch(
) -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    try:
        async with sessions() as db:
            lease = ImChannelLease(
                channel_id=1,
                owner_instance_id="backend-b",
                lease_epoch=7,
                heartbeat_at=now - timedelta(seconds=40),
                expires_at=now - timedelta(seconds=10),
            )
            db.add_all([_agent(status="published"), _channel(), lease])
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=10, now=now)

            assert len(grants) == 1
            assert grants[0].lease_epoch == 8
            await db.refresh(lease)
            assert lease.owner_instance_id == "backend-a"
            assert lease.lease_epoch == 8
            assert lease.heartbeat_at == now
            assert lease.expires_at == now + timedelta(seconds=30)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_channel_without_lease_can_be_claimed() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 8, 1, 12, 0, 0)
    try:
        async with sessions() as db:
            db.add(_personal_channel())
            await db.commit()

            grants = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            ).claim_available(limit=10, now=now)

            assert len(grants) == 1
            assert grants[0].channel_id == 2
            assert grants[0].owner_instance_id == "backend-a"
            assert grants[0].lease_epoch == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_application_channel_is_dropped_while_agent_disabled(
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            agent = _agent(status="published")
            db.add_all([agent, _channel()])
            await db.commit()
            store = SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id="backend-a",
                lease_ttl_seconds=30,
            )

            assert len(await store.claim_available(limit=10)) == 1
            owned = await store.owned_channels()
            assert len(owned) == 1
            assert owned[0].max_inflight_runs == 500

            current = await db.scalar(
                select(ApplicationAgent).where(
                    ApplicationAgent.id == 50
                )
            )
            assert current is not None
            current.status = "disabled"
            await db.commit()

            assert await store.owned_channels() == []
    finally:
        await engine.dispose()
