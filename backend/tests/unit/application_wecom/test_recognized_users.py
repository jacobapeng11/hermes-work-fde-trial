"""Storage contract for recognized application WeCom users; no live services."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.im_gateway.application_channel_service import ApplicationAgentImChannelService
from app.models import (
    ApplicationAgentWeComUserAccess,
    ImChannel,
    ImInboundMessage,
    OrganizationWeComIdentity,
    User,
)
from app.models.base import Base
from app.services.runtime_workspace import Principal


NOW = datetime(2026, 9, 13, 10)
PRINCIPAL = Principal(org_id=1, user_id=1)


@pytest_asyncio.fixture
async def service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[model.__table__ for model in (
                User, OrganizationWeComIdentity, ImChannel,
                ApplicationAgentWeComUserAccess, ImInboundMessage,
            )],
        )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add_all([
                ImChannel(
                    id=agent_id, org_id=org_id, created_by_user_id=1,
                    target_type="application_agent", application_agent_id=agent_id,
                    application_client_id=agent_id,
                )
                for agent_id, org_id in ((31, 1), (32, 1), (33, 2))
            ])
            await db.commit()
            result = ApplicationAgentImChannelService(
                db, secret_codec=SimpleNamespace(decrypt=lambda value: value),
            )
            result._agent = AsyncMock()
            yield result
    finally:
        await engine.dispose()


def user(user_id, *, org_id=1, name=None, code=None, bound=True):
    return User(
        id=user_id, org_id=org_id, name=name or f"用户{user_id}",
        user_code=code or f"E{user_id:04}",
        qw_token=f"member-{user_id}" if bound else None,
    )


def message(message_id, user_id, *, agent_id=31, status="resolved", hours=0, chat="direct"):
    return ImInboundMessage(
        id=message_id, channel_id=agent_id, provider="wecom",
        target_type="application_agent", application_agent_id=agent_id,
        external_message_id=f"message-{message_id}",
        sender_platform_user_id=user_id, identity_status=status,
        sender_member_id_encrypted=f"member-{user_id}",
        chat_type=chat, message_type="text", normalized_content={},
        received_at=NOW + timedelta(hours=hours),
    )


def legacy(user_id, *, revoked=False):
    return [
        OrganizationWeComIdentity(
            id=user_id, org_id=1, platform_user_id=user_id,
            member_id_digest=f"digest-{user_id}",
            member_id_encrypted=f"member-{user_id}",
            status="revoked" if revoked else "active", bound_at=NOW,
            bound_user_name_snapshot=f"用户{user_id}",
        ),
        ApplicationAgentWeComUserAccess(
            id=user_id, application_agent_id=31, channel_id=31,
            organization_wecom_identity_id=user_id,
            first_seen_at=NOW - timedelta(days=1), last_seen_at=NOW,
            last_chat_type="direct",
        ),
    ]


@pytest.mark.asyncio
async def test_qw_token_user_is_visible_without_legacy_identity(service):
    service.db.add_all([user(10), message(1, 10), message(2, 10, hours=1, chat="group")])
    await service.db.commit()

    result = await service.users(principal=PRINCIPAL, agent_id=31)

    assert result.total == 1
    assert len(result.users) == 1
    recognized = result.users[0]
    assert recognized.platform_user_id == 10
    assert recognized.identity_id is None
    assert recognized.status == "active"
    assert recognized.first_seen_at == NOW
    assert recognized.last_seen_at == NOW + timedelta(hours=1)
    assert recognized.last_chat_type == "group"
    assert "member-10" not in recognized.model_dump_json()
    service._agent.assert_awaited_once_with(principal=PRINCIPAL, agent_id=31)


@pytest.mark.asyncio
async def test_merges_legacy_and_current_users_without_duplicates(service):
    service.db.add_all([
        user(10), user(11, bound=False), user(12, bound=False),
        *legacy(10), *legacy(11), *legacy(12, revoked=True),
        message(1, 10, hours=1, chat="group"),
    ])
    await service.db.commit()

    result = await service.users(principal=PRINCIPAL, agent_id=31)

    assert result.total == 3
    rows = {row.platform_user_id: row for row in result.users}
    assert rows[10].first_seen_at == NOW - timedelta(days=1)
    assert rows[10].last_chat_type == "group"
    assert rows[10].identity_id is None  # Revoking a legacy mapping cannot unbind qw_token.
    assert rows[11].identity_id == 11
    assert rows[12].status == "revoked"


@pytest.mark.asyncio
async def test_total_exceeds_page_and_counts_each_user_once(service):
    for user_id in range(1, 126):
        service.db.add_all([
            user(user_id, bound=False), *legacy(user_id),
            message(user_id, user_id),
        ])
    await service.db.commit()

    result = await service.users(principal=PRINCIPAL, agent_id=31, limit=100)

    assert result.total == 125
    assert len(result.users) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("query, expected", [(None, 1), ("E0010", 1), ("50%_", 1), ("missing", 0)])
async def test_scopes_and_search_apply_to_both_count_and_list(service, query, expected):
    service.db.add_all([
        user(10, name="50%_用户"), message(1, 10),
        user(11), message(2, 11, agent_id=32),
        user(12, org_id=2), message(3, 12, agent_id=33),
        user(13), message(4, 13, status="binding_required"),
        user(14),  # Bound, but has never used this application.
        user(15, org_id=2), message(5, 15),  # Malformed cross-org projection.
    ])
    await service.db.commit()

    result = await service.users(principal=PRINCIPAL, agent_id=31, query=query, limit=1)

    assert result.total == expected
    assert [row.platform_user_id for row in result.users] == ([10] if expected else [])
