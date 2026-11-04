from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.im_gateway.messages import NormalizedWeComMessage
from app.im_gateway.queue import ImInboundQueue
from app.models import (
    CloudHermesProfile,
    HermesRuntimeBinding,
    ImChannel,
    ImChannelLease,
    Organization,
    RuntimeAgentProfile,
    RuntimeRun,
    RuntimeSession,
    User,
)
from app.models.base import Base
from app.services.runtime_chat import (
    PreparedRuntimeRun,
    RuntimeChatIdempotencyConflictError,
    SqlAlchemyRuntimeChatStore,
)
from scripts.rotate_personal_wecom_session import (
    PersonalWeComSessionRotationRejected,
    rotate_personal_wecom_session,
)


pytestmark = pytest.mark.skipif(
    os.getenv("HERMES_WECOM_ROTATION_MYSQL_TEST") != "1",
    reason=(
        "set HERMES_WECOM_ROTATION_MYSQL_TEST=1 with an isolated "
        "MySQL 5.7 database"
    ),
)


class _NoopSecretCodec:
    def encrypt(self, value: str) -> str:
        return value

    def decrypt(self, value: str) -> str:
        return value


class _PauseAfterFirstCall:
    def __init__(
        self,
        session: AsyncSession,
        *,
        method_name: str,
        locked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self._session = session
        self._method_name = method_name
        self._locked = locked
        self._release = release
        self._paused = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def execute(self, statement: object):
        result = await self._session.execute(statement)
        await self._pause("execute")
        return result

    async def scalar(self, statement: object):
        result = await self._session.scalar(statement)
        await self._pause("scalar")
        return result

    async def _pause(self, method_name: str) -> None:
        if self._paused or method_name != self._method_name:
            return
        self._paused = True
        self._locked.set()
        await self._release.wait()


class _SignalBeforeFirstCall:
    def __init__(
        self,
        session: AsyncSession,
        *,
        method_name: str,
        attempted: asyncio.Event,
    ) -> None:
        self._session = session
        self._method_name = method_name
        self._attempted = attempted
        self._signaled = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def execute(self, statement: object):
        self._signal("execute")
        return await self._session.execute(statement)

    async def scalar(self, statement: object):
        self._signal("scalar")
        return await self._session.scalar(statement)

    def _signal(self, method_name: str) -> None:
        if self._signaled or method_name != self._method_name:
            return
        self._signaled = True
        self._attempted.set()


@dataclass(frozen=True)
class _SeededChannel:
    channel_id: int
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_agent_profile_id: int
    old_runtime_session_row_id: int
    old_runtime_session_id: str
    owner_instance_id: str
    lease_epoch: int


async def _seed_channel(
    sessions: async_sessionmaker[AsyncSession],
) -> _SeededChannel:
    suffix = uuid4().hex[:12]
    async with sessions() as db:
        organization = Organization(
            name=f"WeCom rotation {suffix}",
            code=f"wecom-rotation-{suffix}",
            status="active",
        )
        db.add(organization)
        await db.flush()
        user = User(
            org_id=int(organization.id),
            user_code=f"u-{suffix}",
            name="WeCom rotation",
            status="active",
        )
        db.add(user)
        await db.flush()
        workspace = CloudHermesProfile(
            org_id=int(organization.id),
            user_id=int(user.id),
            profile_type="default_chat_agent",
            name=f"WeCom rotation {suffix}",
            status="active",
        )
        db.add(workspace)
        await db.flush()
        profile = RuntimeAgentProfile(
            org_id=int(organization.id),
            user_id=int(user.id),
            runtime_workspace_id=int(workspace.id),
            hermes_profile_name="default",
            profile_kind="default",
            display_name="个人 Agent",
            default_workspace_key=int(workspace.id),
            status="ready",
        )
        db.add(profile)
        await db.flush()
        db.add(
            HermesRuntimeBinding(
                org_id=int(organization.id),
                user_id=int(user.id),
                runtime_workspace_id=int(workspace.id),
                provider="knative",
                runtime_id=f"rt-{workspace.id}",
                config_generation=1,
                runtime_config_source="pvc",
                image="test/hermes-agent:integration",
                k8s_namespace="integration-test",
                knative_service=f"runtime-{workspace.id}",
                endpoint_base_url="http://runtime.test",
                endpoint_host_header="runtime.test",
                hermes_home_sub_path="hermes-home",
                workspace_sub_path="workspace",
                hermes_home_mount_path="/root/.hermes",
                workspace_mount_path="/workspace",
                api_key_encrypted="test-only",
                status="ready",
            )
        )
        old_session = RuntimeSession(
            org_id=int(organization.id),
            user_id=int(user.id),
            runtime_workspace_id=int(workspace.id),
            runtime_agent_profile_id=int(profile.id),
            runtime_session_id=f"rs-{workspace.id}-old-{suffix}",
            source_type="wecom",
            title_cache="企微会话",
            message_count_cache=0,
            last_active_at=datetime.now(timezone.utc),
            chat_log_backfilled_at=datetime.now(timezone.utc),
            status="active",
            session_model_entry_id=None,
        )
        db.add(old_session)
        await db.flush()
        channel = ImChannel(
            org_id=int(organization.id),
            target_type="personal_agent",
            created_by_user_id=int(user.id),
            user_id=int(user.id),
            runtime_workspace_id=int(workspace.id),
            provider="wecom",
            connection_mode="websocket",
            bot_id=f"bot-{suffix}",
            secret_encrypted="test-only",
            enabled=True,
            status="connected",
            config_generation=1,
            runtime_session_row_id=int(old_session.id),
        )
        db.add(channel)
        await db.flush()
        owner_instance_id = f"mysql-test-{suffix}"
        lease_epoch = 1
        db.add(
            ImChannelLease(
                channel_id=int(channel.id),
                owner_instance_id=owner_instance_id,
                lease_epoch=lease_epoch,
                expires_at=(
                    datetime.now(timezone.utc)
                    + timedelta(minutes=10)
                ),
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        return _SeededChannel(
            channel_id=int(channel.id),
            org_id=int(organization.id),
            user_id=int(user.id),
            runtime_workspace_id=int(workspace.id),
            runtime_agent_profile_id=int(profile.id),
            old_runtime_session_row_id=int(old_session.id),
            old_runtime_session_id=str(
                old_session.runtime_session_id
            ),
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
        )


def _message(external_message_id: str) -> NormalizedWeComMessage:
    return NormalizedWeComMessage(
        external_message_id=external_message_id,
        provider_request_id=f"request-{external_message_id}",
        external_conversation_id="wecom-user",
        message_type="text",
        text="你好",
        quote_payload=None,
    )


def _prepared_run(
    seeded: _SeededChannel,
    *,
    runtime_session_id: str,
    runtime_run_id: str,
    client_request_digest: str,
) -> PreparedRuntimeRun:
    return PreparedRuntimeRun(
        runtime_workspace_id=seeded.runtime_workspace_id,
        runtime_session_id=runtime_session_id,
        model_entry_id=None,
        message="你好",
        source_type="wecom",
        enabled_expert_ids=[],
        enabled_skill_ids=[],
        enabled_mcp_ids=[],
        enabled_knowledge_base_ids=[],
        allow_new_run=True,
        channel="wecom",
        runtime_agent_profile_id=(
            seeded.runtime_agent_profile_id
        ),
        hermes_profile_name="default",
        runtime_run_id=runtime_run_id,
        client_request_digest=client_request_digest,
    )


async def _assert_waiting(task: asyncio.Task[Any]) -> None:
    done, _pending = await asyncio.wait({task}, timeout=0.2)
    assert not done


@pytest.fixture
async def mysql_sessions():
    source_url = make_url(settings.database_url)
    database_name = f"hermes_wecom_rotation_{uuid4().hex}"
    assert database_name != source_url.database
    test_url = source_url.set(database=database_name)
    admin_engine = create_async_engine(
        source_url,
        pool_pre_ping=True,
    )
    test_engine = None
    database_created = False
    try:
        async with admin_engine.connect() as connection:
            version = str(
                await connection.scalar(text("SELECT VERSION()"))
            )
            assert version.startswith("5.7.")
            await connection.execute(
                text(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 "
                    "COLLATE utf8mb4_unicode_ci"
                )
            )
            database_created = True

        test_engine = create_async_engine(
            test_url,
            pool_pre_ping=True,
        )
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        sessions = async_sessionmaker(
            test_engine,
            expire_on_commit=False,
        )
        yield sessions
    finally:
        if test_engine is not None:
            await test_engine.dispose()
        if database_created:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(f"DROP DATABASE `{database_name}`")
                )
            async with admin_engine.connect() as connection:
                remaining = await connection.scalar(
                    text(
                        "SELECT SCHEMA_NAME "
                        "FROM INFORMATION_SCHEMA.SCHEMATA "
                        "WHERE SCHEMA_NAME = :database_name"
                    ),
                    {"database_name": database_name},
                )
                assert remaining is None
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_ingest_first_blocks_rotation_on_real_mysql(
    mysql_sessions,
) -> None:
    seeded = await _seed_channel(mysql_sessions)
    ingress_locked = asyncio.Event()
    release_ingress = asyncio.Event()
    rotation_attempted = asyncio.Event()

    async with (
        mysql_sessions() as ingress_db,
        mysql_sessions() as rotation_db,
    ):
        ingress_task = asyncio.create_task(
            ImInboundQueue(
                _PauseAfterFirstCall(
                    ingress_db,
                    method_name="scalar",
                    locked=ingress_locked,
                    release=release_ingress,
                ),
                secret_codec=_NoopSecretCodec(),
            ).ingest(
                channel_id=seeded.channel_id,
                message=_message(f"ingress-first-{uuid4().hex}"),
            )
        )
        await asyncio.wait_for(ingress_locked.wait(), timeout=5)
        rotation_task = asyncio.create_task(
            rotate_personal_wecom_session(
                _SignalBeforeFirstCall(
                    rotation_db,
                    method_name="execute",
                    attempted=rotation_attempted,
                ),
                channel_id=seeded.channel_id,
                expected_runtime_session_id=(
                    seeded.old_runtime_session_id
                ),
            )
        )
        await asyncio.wait_for(rotation_attempted.wait(), timeout=5)
        await _assert_waiting(rotation_task)

        release_ingress.set()
        receipt = await asyncio.wait_for(ingress_task, timeout=5)
        assert receipt.status == "queued"
        with pytest.raises(
            PersonalWeComSessionRotationRejected,
            match="unfinished inbound",
        ):
            await asyncio.wait_for(rotation_task, timeout=5)

    async with mysql_sessions() as db:
        current_row_id = await db.scalar(
            select(ImChannel.runtime_session_row_id).where(
                ImChannel.id == seeded.channel_id
            )
        )
    assert current_row_id == seeded.old_runtime_session_row_id


@pytest.mark.asyncio
async def test_rotation_first_ingest_claims_new_session_on_real_mysql(
    mysql_sessions,
) -> None:
    seeded = await _seed_channel(mysql_sessions)
    rotation_locked = asyncio.Event()
    release_rotation = asyncio.Event()
    ingress_attempted = asyncio.Event()

    async with (
        mysql_sessions() as rotation_db,
        mysql_sessions() as ingress_db,
    ):
        rotation_task = asyncio.create_task(
            rotate_personal_wecom_session(
                _PauseAfterFirstCall(
                    rotation_db,
                    method_name="execute",
                    locked=rotation_locked,
                    release=release_rotation,
                ),
                channel_id=seeded.channel_id,
                expected_runtime_session_id=(
                    seeded.old_runtime_session_id
                ),
            )
        )
        await asyncio.wait_for(rotation_locked.wait(), timeout=5)
        ingress_task = asyncio.create_task(
            ImInboundQueue(
                _SignalBeforeFirstCall(
                    ingress_db,
                    method_name="scalar",
                    attempted=ingress_attempted,
                ),
                secret_codec=_NoopSecretCodec(),
            ).ingest(
                channel_id=seeded.channel_id,
                message=_message(f"rotation-first-{uuid4().hex}"),
            )
        )
        await asyncio.wait_for(ingress_attempted.wait(), timeout=5)
        await _assert_waiting(ingress_task)

        release_rotation.set()
        rotation = await asyncio.wait_for(rotation_task, timeout=5)
        receipt = await asyncio.wait_for(ingress_task, timeout=5)
        assert receipt.status == "queued"

    async with mysql_sessions() as claim_db:
        queued = await ImInboundQueue(
            claim_db,
            secret_codec=_NoopSecretCodec(),
        ).claim_next(
            channel_id=seeded.channel_id,
            owner_instance_id=seeded.owner_instance_id,
            lease_epoch=seeded.lease_epoch,
        )

    assert queued is not None
    assert (
        queued.runtime_session_row_id
        == rotation.new_runtime_session_row_id
    )
    assert (
        queued.runtime_session_row_id
        != seeded.old_runtime_session_row_id
    )


@pytest.mark.asyncio
async def test_same_session_concurrent_runtime_run_id_is_created_once(
    mysql_sessions,
) -> None:
    seeded = await _seed_channel(mysql_sessions)
    runtime_run_id = f"rr-im-wecom-{uuid4().hex}"
    prepared = _prepared_run(
        seeded,
        runtime_session_id=seeded.old_runtime_session_id,
        runtime_run_id=runtime_run_id,
        client_request_digest="digest-same-session",
    )

    async def create():
        async with mysql_sessions() as db:
            return await SqlAlchemyRuntimeChatStore(
                db
            ).create_runtime_run(
                org_id=seeded.org_id,
                user_id=seeded.user_id,
                prepared=prepared,
            )

    first, second = await asyncio.gather(create(), create())

    assert {first.runtime_run_id, second.runtime_run_id} == {
        runtime_run_id
    }
    assert sorted((first.reused, second.reused)) == [False, True]
    async with mysql_sessions() as db:
        rows = (
            await db.execute(
                select(RuntimeRun).where(
                    RuntimeRun.runtime_run_id == runtime_run_id
                )
            )
        ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_runtime_run_unique_race_uses_current_mysql_read(
    mysql_sessions,
) -> None:
    seeded = await _seed_channel(mysql_sessions)
    suffix = uuid4().hex[:12]
    async with mysql_sessions() as db:
        second_session = RuntimeSession(
            org_id=seeded.org_id,
            user_id=seeded.user_id,
            runtime_workspace_id=seeded.runtime_workspace_id,
            runtime_agent_profile_id=(
                seeded.runtime_agent_profile_id
            ),
            runtime_session_id=(
                f"rs-{seeded.runtime_workspace_id}-second-{suffix}"
            ),
            source_type="wecom",
            title_cache="第二个企微会话",
            message_count_cache=0,
            status="active",
        )
        db.add(second_session)
        await db.commit()
        second_session_id = str(
            second_session.runtime_session_id
        )

    runtime_run_id = f"rr-im-wecom-{uuid4().hex}"
    first_precheck = asyncio.Event()
    second_precheck = asyncio.Event()
    release = asyncio.Event()

    async def create(
        *,
        runtime_session_id: str,
        prechecked: asyncio.Event,
    ):
        async with mysql_sessions() as db:
            paused_db = _PauseAfterFirstCall(
                db,
                method_name="execute",
                locked=prechecked,
                release=release,
            )
            return await SqlAlchemyRuntimeChatStore(
                paused_db  # type: ignore[arg-type]
            ).create_runtime_run(
                org_id=seeded.org_id,
                user_id=seeded.user_id,
                prepared=_prepared_run(
                    seeded,
                    runtime_session_id=runtime_session_id,
                    runtime_run_id=runtime_run_id,
                    client_request_digest="digest-cross-session",
                ),
            )

    first_task = asyncio.create_task(
        create(
            runtime_session_id=seeded.old_runtime_session_id,
            prechecked=first_precheck,
        )
    )
    second_task = asyncio.create_task(
        create(
            runtime_session_id=second_session_id,
            prechecked=second_precheck,
        )
    )
    await asyncio.gather(
        first_precheck.wait(),
        second_precheck.wait(),
    )
    release.set()
    results = await asyncio.gather(
        first_task,
        second_task,
        return_exceptions=True,
    )

    assert sum(
        not isinstance(result, BaseException)
        for result in results
    ) == 1
    assert sum(
        isinstance(
            result,
            RuntimeChatIdempotencyConflictError,
        )
        for result in results
    ) == 1
    async with mysql_sessions() as db:
        rows = (
            await db.execute(
                select(RuntimeRun).where(
                    RuntimeRun.runtime_run_id == runtime_run_id
                )
            )
        ).scalars().all()
    assert len(rows) == 1
