from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import mysql

from app.models import (
    ImChannel,
    RuntimeAgentProfile,
    RuntimeAuditLog,
    RuntimeSession,
)
from app.services.runtime_activity import ACTIVE_RUN_STATUSES
from scripts.rotate_personal_wecom_session import (
    PersonalWeComSessionRotationRejected,
    rotate_personal_wecom_session,
)


class _LockedResult:
    def __init__(
        self,
        value: tuple[ImChannel, RuntimeSession] | None,
    ) -> None:
        self.value = value

    def first(self) -> tuple[ImChannel, RuntimeSession] | None:
        return self.value


class _RotationDb:
    def __init__(
        self,
        *,
        channel: ImChannel,
        session: RuntimeSession,
        scalar_results: list[object | None],
        commit_error: Exception | None = None,
    ) -> None:
        self.channel = channel
        self.session = session
        self.scalar_results = deque(scalar_results)
        self.commit_error = commit_error
        self.statements: list[object] = []
        self.added: list[object] = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, statement: object) -> _LockedResult:
        self.statements.append(statement)
        return _LockedResult((self.channel, self.session))

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_results.popleft()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1
        new_session = next(
            (
                value
                for value in self.added
                if isinstance(value, RuntimeSession)
            ),
            None,
        )
        if new_session is not None and new_session.id is None:
            new_session.id = 202

    async def commit(self) -> None:
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollback_count += 1


def _rows() -> tuple[ImChannel, RuntimeSession, RuntimeAgentProfile]:
    channel = ImChannel(
        id=101,
        org_id=11,
        target_type="personal_agent",
        created_by_user_id=12,
        user_id=12,
        runtime_workspace_id=13,
        provider="wecom",
        connection_mode="websocket",
        bot_id="bot-id",
        secret_encrypted="encrypted-secret",
        enabled=True,
        status="connected",
        config_generation=7,
        runtime_session_row_id=201,
        direct_enabled=True,
        group_enabled=True,
    )
    old_session = RuntimeSession(
        id=201,
        org_id=11,
        user_id=12,
        runtime_workspace_id=13,
        runtime_agent_profile_id=14,
        runtime_session_id="rs-13-old",
        source_type="wecom",
        title_cache="旧企微会话",
        message_count_cache=36,
        hermes_materialized_at=datetime(
            2026, 8, 1, tzinfo=timezone.utc
        ),
        last_synced_at=datetime(
            2026, 8, 2, tzinfo=timezone.utc
        ),
        model_switch_events=[{"from": 15, "to": 16}],
        status="active",
        session_model_entry_id=16,
    )
    profile = RuntimeAgentProfile(
        id=14,
        org_id=11,
        user_id=12,
        runtime_workspace_id=13,
        hermes_profile_name="default",
        profile_kind="default",
        display_name="个人 Agent",
        status="ready",
    )
    return channel, old_session, profile


@pytest.mark.asyncio
async def test_idle_personal_wecom_rotation_is_atomic_and_clean() -> None:
    channel, old_session, profile = _rows()
    original_channel_state = {
        "bot_id": channel.bot_id,
        "secret_encrypted": channel.secret_encrypted,
        "enabled": channel.enabled,
        "status": channel.status,
        "config_generation": channel.config_generation,
    }
    current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[profile, None, None],
    )

    result = await rotate_personal_wecom_session(
        db,
        channel_id=101,
        expected_runtime_session_id="rs-13-old",
        now=current,
    )

    assert db.commit_count == 1
    assert db.rollback_count == 0
    assert db.flush_count == 1
    assert channel.runtime_session_row_id == 202
    assert {
        "bot_id": channel.bot_id,
        "secret_encrypted": channel.secret_encrypted,
        "enabled": channel.enabled,
        "status": channel.status,
        "config_generation": channel.config_generation,
    } == original_channel_state
    assert old_session.runtime_session_id == "rs-13-old"
    assert old_session.status == "active"
    assert old_session.message_count_cache == 36
    assert old_session.model_switch_events == [{"from": 15, "to": 16}]

    new_session = next(
        value
        for value in db.added
        if isinstance(value, RuntimeSession)
    )
    assert new_session.org_id == old_session.org_id
    assert new_session.user_id == old_session.user_id
    assert (
        new_session.runtime_workspace_id
        == old_session.runtime_workspace_id
    )
    assert (
        new_session.runtime_agent_profile_id
        == old_session.runtime_agent_profile_id
    )
    assert (
        new_session.session_model_entry_id
        == old_session.session_model_entry_id
    )
    assert new_session.runtime_session_id != "rs-13-old"
    assert new_session.source_type == "wecom"
    assert new_session.title_cache == "企微会话"
    assert new_session.message_count_cache == 0
    assert new_session.status == "active"
    assert new_session.last_active_at == current
    assert new_session.chat_log_backfilled_at == current
    assert new_session.hermes_materialized_at is None
    assert new_session.last_synced_at is None
    assert new_session.model_switch_events is None

    audit = next(
        value
        for value in db.added
        if isinstance(value, RuntimeAuditLog)
    )
    assert audit.action == "im.wecom.runtime_session_rotated"
    assert audit.target_type == "im_channel"
    assert audit.target_id == "101"
    assert audit.detail == {
        "provider": "wecom",
        "old_runtime_session_row_id": 201,
        "old_runtime_session_id": "rs-13-old",
        "new_runtime_session_row_id": 202,
        "new_runtime_session_id": new_session.runtime_session_id,
    }
    assert result.old_runtime_session_row_id == 201
    assert result.new_runtime_session_row_id == 202

    lock_sql = str(
        db.statements[0].compile(dialect=mysql.dialect())
    ).lower()
    assert "for update" in lock_sql
    assert "im_channel" in lock_sql
    assert "runtime_session" in lock_sql


@pytest.mark.parametrize(
    "status",
    [
        "received",
        "awaiting_identity",
        "staging_media",
        "queued",
        "running",
    ],
)
@pytest.mark.asyncio
async def test_unfinished_inbound_work_blocks_rotation(
    status: str,
) -> None:
    channel, old_session, profile = _rows()
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[profile, 301],
    )

    with pytest.raises(
        PersonalWeComSessionRotationRejected,
        match="unfinished inbound",
    ):
        await rotate_personal_wecom_session(
            db,
            channel_id=101,
            expected_runtime_session_id="rs-13-old",
        )

    inbound_sql = str(
        db.statements[2].compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert f"'{status}'" in inbound_sql
    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert channel.runtime_session_row_id == 201
    assert not any(
        isinstance(value, RuntimeSession) for value in db.added
    )


@pytest.mark.parametrize("status", sorted(ACTIVE_RUN_STATUSES))
@pytest.mark.asyncio
async def test_every_authoritative_active_run_status_blocks_rotation(
    status: str,
) -> None:
    channel, old_session, profile = _rows()
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[profile, None, 401],
    )

    with pytest.raises(
        PersonalWeComSessionRotationRejected,
        match="active run",
    ):
        await rotate_personal_wecom_session(
            db,
            channel_id=101,
            expected_runtime_session_id="rs-13-old",
        )

    active_sql = str(
        db.statements[3].compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert f"'{status}'" in active_sql
    assert "'canceling'" in active_sql
    assert "'cancel_failed'" in active_sql
    assert "runtime_run.session_id = 201" in active_sql
    assert "runtime_run.session_id is null" in active_sql
    assert (
        "runtime_run.runtime_session_id = 'rs-13-old'"
        in active_sql
    )
    assert "runtime_run.org_id = 11" in active_sql
    assert "runtime_run.user_id = 12" in active_sql
    assert "runtime_run.runtime_workspace_id = 13" in active_sql
    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert channel.runtime_session_row_id == 201


@pytest.mark.asyncio
async def test_cas_mismatch_rolls_back_without_creating_session() -> None:
    channel, old_session, profile = _rows()
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[profile],
    )

    with pytest.raises(
        PersonalWeComSessionRotationRejected,
        match="no longer matches",
    ):
        await rotate_personal_wecom_session(
            db,
            channel_id=101,
            expected_runtime_session_id="rs-13-stale",
        )

    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert db.flush_count == 0
    assert channel.runtime_session_row_id == 201
    assert db.added == []


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("channel", "target_type", "application_agent"),
        ("channel", "provider", "feishu"),
        ("channel", "user_id", 99),
        ("channel", "runtime_workspace_id", 99),
        ("session", "source_type", "web"),
        ("session", "status", "archived"),
    ],
)
@pytest.mark.asyncio
async def test_channel_session_invariant_mismatch_is_rejected(
    target: str,
    field: str,
    value: object,
) -> None:
    channel, old_session, _profile = _rows()
    setattr(
        channel if target == "channel" else old_session,
        field,
        value,
    )
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[],
    )

    with pytest.raises(
        PersonalWeComSessionRotationRejected,
        match="invariant is broken",
    ):
        await rotate_personal_wecom_session(
            db,
            channel_id=101,
            expected_runtime_session_id="rs-13-old",
        )

    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert db.added == []


@pytest.mark.asyncio
async def test_profile_invariant_mismatch_is_rejected() -> None:
    channel, old_session, _profile = _rows()
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[None],
    )

    with pytest.raises(
        PersonalWeComSessionRotationRejected,
        match="profile invariant",
    ):
        await rotate_personal_wecom_session(
            db,
            channel_id=101,
            expected_runtime_session_id="rs-13-old",
        )

    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert db.added == []


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_rotation() -> None:
    channel, old_session, profile = _rows()
    db = _RotationDb(
        channel=channel,
        session=old_session,
        scalar_results=[profile, None, None],
        commit_error=RuntimeError("commit failed"),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await rotate_personal_wecom_session(
            db,
            channel_id=101,
            expected_runtime_session_id="rs-13-old",
        )

    assert db.commit_count == 1
    assert db.rollback_count == 1
