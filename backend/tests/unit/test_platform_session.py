from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, Response
from redis.exceptions import RedisError
from starlette.requests import Request

from app.api.v1 import auth as auth_api
from app.api.v1.auth import exchange_enterprise_idp_token, refresh_platform_session
from app.config import settings
from app.schemas.auth import EnterpriseIdpTokenExchangeRequest
from app.services.enterprise_idp import EnterpriseIdpAssertion
from app.services.platform_session import (
    DATA_APP_LAUNCH_KEY_PREFIX,
    ENTERPRISE_IDP_REPLAY_KEY_PREFIX,
    PLATFORM_SESSION_COOKIE_NAME,
    InMemoryPlatformSessionStore,
    PlatformSessionUnavailable,
    RedisPlatformSessionStore,
)
from app.services.auth import AuthTokenService, InMemoryAuthUserStore
from app.services.runtime_workspace import Principal
from app.services.sso_identity import SsoIdentity


class FakeApplicationAgentAuthorization:
    async def can_manage(self, _principal: Principal) -> bool:
        return False


class FakeAdminAuthorization:
    async def is_admin(self, _principal: Principal) -> bool:
        return False


class FakeEnterpriseIdpAssertionService:
    def decrypt_and_validate(self, _token: str) -> EnterpriseIdpAssertion:
        now = int(time.time())
        return EnterpriseIdpAssertion(
            work_no="employee-7",
            issued_at=datetime.fromtimestamp(now, tz=timezone.utc),
            expires_at=datetime.fromtimestamp(now + 300, tz=timezone.utc),
            token_fingerprint="e" * 64,
        )


class FakeSsoIdentityService:
    def __init__(self, _repository: object) -> None:
        pass

    async def resolve_or_create(self, *, org_id: int, work_no: str) -> SsoIdentity:
        return SsoIdentity(
            org_id=org_id,
            user_id=7,
            user_code=work_no,
            name="测试用户",
            email=None,
        )


class FakeApplicationAgentAuthorizationService:
    def __init__(self, _store: object) -> None:
        pass

    async def can_manage(self, _principal: Principal) -> bool:
        return False


class FakeAdminWorkNoAuthorizationService:
    def __init__(self, _store: object) -> None:
        pass

    async def is_admin_work_no(self, *, org_id: int, work_no: str | None) -> bool:
        return False


def request_with_cookie(session_id: str | None) -> Request:
    headers = [(b"origin", settings.PUBLIC_WEB_BASE.encode())]
    if session_id:
        headers.append(
            (
                b"cookie",
                f"{PLATFORM_SESSION_COOKIE_NAME}={session_id}".encode(),
            )
        )
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/refresh",
        "headers": headers,
    })


async def test_refresh_issues_short_access_token_from_server_session() -> None:
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=180)
    platform_session = await store.create(
        Principal(
            org_id=settings.DEFAULT_ORG_ID,
            user_id=settings.DEV_PRINCIPAL_USER_ID,
        )
    )
    response = Response()

    payload = await refresh_platform_session(
        request=request_with_cookie(platform_session.session_id),
        response=response,
        service=AuthTokenService(InMemoryAuthUserStore()),
        application_agent_authorization=FakeApplicationAgentAuthorization(),
        admin_authorization=FakeAdminAuthorization(),
        session_store=store,
    )

    assert payload.expires_in == settings.PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert payload.principal.user_id == settings.DEV_PRINCIPAL_USER_ID
    assert payload.access_token
    assert response.headers["cache-control"] == "no-store"


async def test_refresh_rejects_an_expired_server_session() -> None:
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=1)
    with pytest.raises(HTTPException) as captured:
        await refresh_platform_session(
            request=request_with_cookie(None),
            response=Response(),
            service=AuthTokenService(InMemoryAuthUserStore()),
            application_agent_authorization=FakeApplicationAgentAuthorization(),
            admin_authorization=FakeAdminAuthorization(),
            session_store=store,
        )

    assert captured.value.status_code == 401
    assert captured.value.detail["code"] == "session_expired"


async def test_in_memory_session_touch_and_revoke() -> None:
    now = 1_000.0
    store = InMemoryPlatformSessionStore(
        idle_ttl_seconds=10,
        now=lambda: now,
    )
    session = await store.create(Principal(org_id=3, user_id=7))

    now = 1_009.0
    assert await store.touch(session.session_id) is True
    now = 1_018.0
    assert await store.get(session.session_id) == session
    await store.revoke(session.session_id)
    assert await store.get(session.session_id) is None


async def test_enterprise_idp_assertion_creates_only_one_session_under_concurrency() -> None:
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=180)
    principal = Principal(org_id=3, user_id=7)

    first, second = await asyncio.gather(
        store.create_from_enterprise_idp(
            principal,
            token_fingerprint="a" * 64,
            assertion_ttl_seconds=300,
        ),
        store.create_from_enterprise_idp(
            principal,
            token_fingerprint="a" * 64,
            assertion_ttl_seconds=300,
        ),
    )

    assert sum(session is not None for session in (first, second)) == 1
    assert len(store.sessions) == 1


async def test_enterprise_idp_assertion_can_be_used_after_replay_ttl_expires() -> None:
    now = 1_000.0
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=180, now=lambda: now)
    principal = Principal(org_id=3, user_id=7)

    first = await store.create_from_enterprise_idp(
        principal,
        token_fingerprint="b" * 64,
        assertion_ttl_seconds=2,
    )
    now = 1_003.0
    second = await store.create_from_enterprise_idp(
        principal,
        token_fingerprint="b" * 64,
        assertion_ttl_seconds=2,
    )

    assert first is not None
    assert second is not None


class FakeAtomicRedis:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.values: dict[str, tuple[str, int]] = {}

    async def eval(self, _script: str, key_count: int, *args: object) -> int:
        if self.unavailable:
            raise RedisError("unavailable")
        assert key_count == 2
        session_key, replay_key, session_json, session_ttl, replay_ttl = args
        assert isinstance(session_key, str)
        assert isinstance(replay_key, str)
        assert isinstance(session_json, str)
        if replay_key in self.values:
            return 0
        self.values[session_key] = (session_json, int(session_ttl))
        self.values[replay_key] = ("used", int(replay_ttl))
        return 1

    async def set(self, key: str, value: str, *, ex: int) -> None:
        if self.unavailable:
            raise RedisError("unavailable")
        self.values[key] = (value, ex)

    async def getdel(self, key: str) -> str | None:
        if self.unavailable:
            raise RedisError("unavailable")
        item = self.values.pop(key, None)
        return item[0] if item is not None else None


async def test_redis_enterprise_idp_creation_writes_session_and_replay_marker_together() -> None:
    redis = FakeAtomicRedis()
    store = RedisPlatformSessionStore(redis, idle_ttl_seconds=180)  # type: ignore[arg-type]
    principal = Principal(org_id=3, user_id=7)

    session = await store.create_from_enterprise_idp(
        principal,
        token_fingerprint="c" * 64,
        assertion_ttl_seconds=300,
    )
    duplicate = await store.create_from_enterprise_idp(
        principal,
        token_fingerprint="c" * 64,
        assertion_ttl_seconds=300,
    )

    assert session is not None
    assert duplicate is None
    replay_key = f"{ENTERPRISE_IDP_REPLAY_KEY_PREFIX}{'c' * 64}"
    assert redis.values[replay_key] == ("used", 300)
    session_payload = json.loads(redis.values[store._key(session.session_id)][0])
    assert session_payload["user_id"] == 7


async def test_redis_enterprise_idp_creation_fails_closed_when_redis_is_unavailable() -> None:
    store = RedisPlatformSessionStore(
        FakeAtomicRedis(unavailable=True),  # type: ignore[arg-type]
        idle_ttl_seconds=180,
    )

    with pytest.raises(PlatformSessionUnavailable):
        await store.create_from_enterprise_idp(
            Principal(org_id=3, user_id=7),
            token_fingerprint="d" * 64,
            assertion_ttl_seconds=300,
        )


async def test_enterprise_idp_exchange_keeps_response_contract_and_rejects_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_api, "EnterpriseIdpAssertionService", FakeEnterpriseIdpAssertionService)
    monkeypatch.setattr(auth_api, "SsoIdentityService", FakeSsoIdentityService)
    monkeypatch.setattr(
        auth_api,
        "ApplicationAgentAuthorizationService",
        FakeApplicationAgentAuthorizationService,
    )
    monkeypatch.setattr(
        auth_api,
        "AdminWorkNoAuthorizationService",
        FakeAdminWorkNoAuthorizationService,
    )
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "enterprise_idpAES")
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=180)
    payload = EnterpriseIdpTokenExchangeRequest(token="opaque-token")
    response = Response()

    result = await exchange_enterprise_idp_token(
        payload=payload,
        response=response,
        db=object(),  # type: ignore[arg-type]
        session_store=store,
    )

    assert result.principal.user_id == 7
    assert result.access_token
    assert PLATFORM_SESSION_COOKIE_NAME in response.headers["set-cookie"]
    with pytest.raises(HTTPException) as captured:
        await exchange_enterprise_idp_token(
            payload=payload,
            response=Response(),
            db=object(),  # type: ignore[arg-type]
            session_store=store,
        )
    assert captured.value.status_code == 401
    assert captured.value.detail == "invalid EnterpriseIdp assertion"


async def test_data_app_launch_expires_and_cannot_be_replayed() -> None:
    now = 1_000.0
    store = InMemoryPlatformSessionStore(now=lambda: now)
    ticket = await store.create_data_app_launch(
        Principal(org_id=3, user_id=7),
        app_id=11,
        ttl_seconds=60,
    )

    assert ticket not in next(iter(store.data_app_launches))
    assert next(iter(store.data_app_launches)).startswith(DATA_APP_LAUNCH_KEY_PREFIX)
    now = 1_061.0
    assert await store.consume_data_app_launch(ticket) is None
    assert await store.consume_data_app_launch(ticket) is None


async def test_data_app_launch_has_only_one_concurrent_consumer() -> None:
    store = InMemoryPlatformSessionStore()
    ticket = await store.create_data_app_launch(
        Principal(org_id=3, user_id=7),
        app_id=11,
        ttl_seconds=60,
    )

    launches = await asyncio.gather(
        store.consume_data_app_launch(ticket),
        store.consume_data_app_launch(ticket),
    )

    assert sum(launch is not None for launch in launches) == 1
    launch = next(item for item in launches if item is not None)
    assert launch.principal == Principal(org_id=3, user_id=7)
    assert launch.app_id == 11


async def test_redis_data_app_launch_is_hashed_and_atomically_consumed() -> None:
    redis = FakeAtomicRedis()
    store = RedisPlatformSessionStore(redis, idle_ttl_seconds=180)  # type: ignore[arg-type]
    ticket = await store.create_data_app_launch(
        Principal(org_id=3, user_id=7),
        app_id=11,
        ttl_seconds=60,
    )

    key = next(key for key in redis.values if key.startswith(DATA_APP_LAUNCH_KEY_PREFIX))
    assert ticket not in key
    assert redis.values[key][1] == 60
    launch = await store.consume_data_app_launch(ticket)
    assert launch is not None
    assert launch.principal == Principal(org_id=3, user_id=7)
    assert launch.app_id == 11
    assert await store.consume_data_app_launch(ticket) is None


async def test_redis_data_app_launch_fails_closed_when_unavailable() -> None:
    store = RedisPlatformSessionStore(
        FakeAtomicRedis(unavailable=True),  # type: ignore[arg-type]
        idle_ttl_seconds=180,
    )

    with pytest.raises(PlatformSessionUnavailable):
        await store.create_data_app_launch(
            Principal(org_id=3, user_id=7),
            app_id=11,
            ttl_seconds=60,
        )
    with pytest.raises(PlatformSessionUnavailable):
        await store.consume_data_app_launch("ticket")
