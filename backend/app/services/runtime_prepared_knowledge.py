from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, TypeVar

from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    ApplicationAgent,
    ApplicationAgentPersonalKnowledgeBinding,
    ApplicationAgentClientGrant,
    ApplicationAgentConversation,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioSession,
    ApplicationClient,
    User,
)
from app.services.knowledge_assertion import KnowledgeAssertionIssuer
from app.services.runtime_knowledge import (
    RuntimeKnowledgeService,
    SqlAlchemyRuntimeKnowledgeStore,
)
from app.services.runtime_personal_knowledge import (
    RuntimePersonalKnowledgeService,
    SqlAlchemyRuntimePersonalKnowledgeStore,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


logger = logging.getLogger(__name__)
T = TypeVar("T")
PreparedKnowledgeKind = Literal["enterprise", "personal"]


class PreparedKnowledgeStoreUnavailable(RuntimeError):
    """The canonical prepared-context store is unavailable."""


@dataclass(frozen=True)
class RuntimePreparedKnowledgeKey:
    surface: Literal["workspace", "application_agent"]
    target_id: int
    runtime_session_id: str
    runtime_agent_profile_id: int | None
    runtime_token_digest: str

    @classmethod
    def workspace(
        cls,
        *,
        runtime_workspace_id: int,
        runtime_session_id: str,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimePreparedKnowledgeKey:
        return cls(
            surface="workspace",
            target_id=int(runtime_workspace_id),
            runtime_session_id=str(runtime_session_id),
            runtime_agent_profile_id=(
                int(runtime_agent_profile_id)
                if runtime_agent_profile_id is not None
                else None
            ),
            runtime_token_digest=hashlib.sha256(
                str(runtime_token).encode("utf-8")
            ).hexdigest(),
        )

    @classmethod
    def application_agent(
        cls,
        *,
        application_agent_id: int,
        runtime_session_id: str,
        runtime_token: str,
    ) -> RuntimePreparedKnowledgeKey:
        return cls(
            surface="application_agent",
            target_id=int(application_agent_id),
            runtime_session_id=str(runtime_session_id),
            runtime_agent_profile_id=None,
            runtime_token_digest=hashlib.sha256(
                str(runtime_token).encode("utf-8")
            ).hexdigest(),
        )

    def digest(self) -> str:
        identity = "\n".join(
            (
                self.surface,
                str(self.target_id),
                self.runtime_session_id,
                str(self.runtime_agent_profile_id or 0),
                self.runtime_token_digest,
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class RedisRuntimePreparedKnowledgeStore:
    """Session-scoped prepared knowledge state shared by Backend workers."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = 180,
        key_prefix: str = "hermes:prepared-knowledge:v1:",
    ) -> None:
        self.redis = redis
        self.ttl_seconds = max(90, int(ttl_seconds))
        self.key_prefix = key_prefix

    async def get(
        self,
        key: RuntimePreparedKnowledgeKey,
        kind: PreparedKnowledgeKind,
    ) -> dict[str, Any] | None:
        try:
            encoded = await self.redis.hget(self._key(key), kind)
        except RedisError as exc:
            raise PreparedKnowledgeStoreUnavailable(
                "prepared knowledge store is unavailable"
            ) from exc
        if encoded is None:
            return None
        try:
            value = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise PreparedKnowledgeStoreUnavailable(
                "prepared knowledge entry is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise PreparedKnowledgeStoreUnavailable(
                "prepared knowledge entry is invalid"
            )
        return value

    async def put(
        self,
        key: RuntimePreparedKnowledgeKey,
        kind: PreparedKnowledgeKind,
        value: dict[str, Any],
    ) -> None:
        await self.put_bundle(key, {kind: value})

    async def put_bundle(
        self,
        key: RuntimePreparedKnowledgeKey,
        values: dict[PreparedKnowledgeKind, dict[str, Any]],
    ) -> None:
        encoded = {
            kind: json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for kind, value in values.items()
        }
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.hset(self._key(key), mapping=encoded)
            pipeline.expire(self._key(key), self.ttl_seconds)
            await pipeline.execute()
        except RedisError as exc:
            raise PreparedKnowledgeStoreUnavailable(
                "prepared knowledge store is unavailable"
            ) from exc

    def _key(self, key: RuntimePreparedKnowledgeKey) -> str:
        return f"{self.key_prefix}{key.digest()}"


class RuntimePreparedKnowledgeService:
    def __init__(
        self,
        store: RedisRuntimePreparedKnowledgeStore,
    ) -> None:
        self.store = store

    async def prepare_workspace(
        self,
        *,
        runtime_workspace_id: int,
        runtime_session_id: str,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> None:
        if not str(runtime_token):
            raise PreparedKnowledgeStoreUnavailable(
                "runtime credential is unavailable during context preparation"
            )
        key = RuntimePreparedKnowledgeKey.workspace(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            runtime_token=runtime_token,
        )
        started = time.perf_counter()

        async def enterprise() -> dict[str, Any]:
            async with AsyncSessionLocal() as db:
                service = RuntimeKnowledgeService(
                    SqlAlchemyRuntimeKnowledgeStore(db),
                    issuer=KnowledgeAssertionIssuer(
                        issuer=settings.KNOWLEDGE_ASSERTION_ISSUER,
                        ttl_seconds=settings.KNOWLEDGE_ASSERTION_TTL_SECONDS,
                    ),
                )
                return await service.state(
                    runtime_workspace_id,
                    runtime_agent_profile_id,
                    runtime_token,
                )

        async def personal() -> dict[str, Any]:
            async with AsyncSessionLocal() as db:
                service = RuntimePersonalKnowledgeService(
                    SqlAlchemyRuntimePersonalKnowledgeStore(db)
                )
                state = await service.state(
                    runtime_workspace_id,
                    runtime_agent_profile_id,
                    runtime_token,
                )
                return state.model_dump(mode="json")

        enterprise_state, personal_state = await asyncio.gather(
            enterprise(),
            personal(),
        )
        await self.store.put_bundle(
            key,
            {
                "enterprise": enterprise_state,
                "personal": personal_state,
            },
        )
        logger.info(
            "runtime prepared knowledge ready workspace_id=%s session_id=%s "
            "profile_id=%s duration_ms=%.1f",
            runtime_workspace_id,
            runtime_session_id,
            runtime_agent_profile_id,
            (time.perf_counter() - started) * 1000.0,
        )

    async def load_or_prepare(
        self,
        *,
        key: RuntimePreparedKnowledgeKey,
        kind: PreparedKnowledgeKind,
        loader: Callable[[], Awaitable[T]],
        serializer: Callable[[T], dict[str, Any]],
    ) -> T | dict[str, Any]:
        cached = await self.store.get(key, kind)
        if cached is not None:
            return cached
        loaded = await loader()
        await self.store.put(key, kind, serializer(loaded))
        return loaded

    async def prepare_application_agent(
        self,
        *,
        application_agent_id: int,
        runtime_session_id: str,
        runtime_run_id: str,
        runtime_token: str,
    ) -> None:
        if not str(runtime_token):
            raise PreparedKnowledgeStoreUnavailable(
                "application runtime credential is unavailable"
            )
        key = RuntimePreparedKnowledgeKey.application_agent(
            application_agent_id=application_agent_id,
            runtime_session_id=runtime_session_id,
            runtime_token=runtime_token,
        )
        started = time.perf_counter()

        enterprise_cached, personal_cached = await asyncio.gather(
            self.store.get(key, "enterprise"),
            self.store.get(key, "personal"),
        )
        if enterprise_cached is not None and personal_cached is not None:
            return

        async def application_states() -> tuple[
            dict[str, Any],
            dict[str, Any],
        ]:
            async with AsyncSessionLocal() as db:
                agent = (
                    await db.execute(
                        select(ApplicationAgent).where(
                            ApplicationAgent.id
                            == application_agent_id
                        )
                    )
                ).scalar_one_or_none()
                if agent is None:
                    raise PreparedKnowledgeStoreUnavailable(
                        "application agent knowledge configuration unavailable"
                    )
                return (
                    {
                        "enabled": _application_enterprise_enabled(
                            agent
                        ),
                        "mode": "full",
                    },
                    await _application_personal_prepared_state(
                        db=db,
                        agent=agent,
                    ),
                )

        enterprise_state, personal_state = await application_states()
        missing: dict[PreparedKnowledgeKind, dict[str, Any]] = {}
        if enterprise_cached is None:
            missing["enterprise"] = enterprise_state
        if personal_cached is None:
            missing["personal"] = personal_state
        await self.store.put_bundle(
            key,
            missing,
        )
        logger.info(
            "application prepared knowledge agent_id=%s session_id=%s "
            "runtime_run_id=%s duration_ms=%.1f",
            application_agent_id,
            runtime_session_id,
            runtime_run_id,
            (time.perf_counter() - started) * 1000.0,
        )

    async def prepare_application_agent_channel(
        self,
        *,
        application_agent_id: int,
        application_client_id: int,
        subject_user_id: int,
        runtime_session_id: str,
        runtime_run_id: str,
        runtime_token: str,
    ) -> None:
        """Prepare a cold Channel route without requiring a Backend Run."""

        if not str(runtime_token):
            raise PreparedKnowledgeStoreUnavailable(
                "application runtime credential is unavailable"
            )
        key = RuntimePreparedKnowledgeKey.application_agent(
            application_agent_id=application_agent_id,
            runtime_session_id=runtime_session_id,
            runtime_token=runtime_token,
        )
        started = time.perf_counter()
        enterprise_cached, personal_cached = await asyncio.gather(
            self.store.get(key, "enterprise"),
            self.store.get(key, "personal"),
        )
        if enterprise_cached is not None and personal_cached is not None:
            return

        async with AsyncSessionLocal() as db:
            current = (
                await db.execute(
                    select(
                        ApplicationAgent,
                        ApplicationAgentRuntimeBinding,
                        ApplicationClient,
                        ApplicationAgentClientGrant,
                        ApplicationAgentConversation,
                        User,
                    )
                    .join(
                        ApplicationAgentRuntimeBinding,
                        ApplicationAgentRuntimeBinding.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .join(
                        ApplicationAgentClientGrant,
                        ApplicationAgentClientGrant.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .join(
                        ApplicationClient,
                        ApplicationClient.id
                        == ApplicationAgentClientGrant.application_client_id,
                    )
                    .join(
                        ApplicationAgentConversation,
                        ApplicationAgentConversation.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .join(
                        User,
                        User.id
                        == ApplicationAgentConversation.subject_user_id,
                    )
                    .where(
                        ApplicationAgent.id == application_agent_id,
                        ApplicationAgent.status == "published",
                        ApplicationAgentRuntimeBinding.status == "ready",
                        ApplicationAgentRuntimeBinding.org_id
                        == ApplicationAgent.org_id,
                        ApplicationClient.id == application_client_id,
                        ApplicationClient.org_id == ApplicationAgent.org_id,
                        ApplicationClient.status == "active",
                        ApplicationClient.auth_method == "internal_channel",
                        ApplicationAgentClientGrant.application_client_id
                        == application_client_id,
                        ApplicationAgentClientGrant.status == "active",
                        ApplicationAgentClientGrant.allow_user_delegation.is_(
                            True
                        ),
                        ApplicationAgentConversation.application_client_id
                        == application_client_id,
                        ApplicationAgentConversation.runtime_session_id
                        == runtime_session_id,
                        ApplicationAgentConversation.subject_type == "user",
                        ApplicationAgentConversation.subject_user_id
                        == subject_user_id,
                        ApplicationAgentConversation.status == "active",
                        ApplicationAgentConversation.org_id
                        == ApplicationAgent.org_id,
                        User.id == subject_user_id,
                        User.org_id == ApplicationAgent.org_id,
                        User.status == "active",
                    )
                )
            ).first()
            if current is None:
                raise PreparedKnowledgeStoreUnavailable(
                    "application channel knowledge authorization unavailable"
                )
            agent, binding, _client, grant, _conversation, _user = current
            if "responses:create" not in set(grant.scopes or []):
                raise PreparedKnowledgeStoreUnavailable(
                    "application channel knowledge authorization unavailable"
                )
            try:
                expected_token = RuntimeApiKeyCodec().decrypt(
                    str(binding.api_key_encrypted)
                )
            except ValueError as exc:
                raise PreparedKnowledgeStoreUnavailable(
                    "application runtime credential is unavailable"
                ) from exc
            if not hmac.compare_digest(runtime_token, expected_token):
                raise PreparedKnowledgeStoreUnavailable(
                    "application runtime credential is unauthorized"
                )

            enterprise_enabled = _application_enterprise_enabled(agent)

            missing: dict[PreparedKnowledgeKind, dict[str, Any]] = {}
            if enterprise_cached is None:
                missing["enterprise"] = {
                    "enabled": enterprise_enabled,
                    "mode": "full",
                }
            if personal_cached is None:
                missing["personal"] = (
                    await _application_personal_prepared_state(
                        db=db,
                        agent=agent,
                    )
                )

        await self.store.put_bundle(key, missing)
        logger.info(
            "application channel prepared knowledge agent_id=%s "
            "session_id=%s runtime_run_id=%s duration_ms=%.1f",
            application_agent_id,
            runtime_session_id,
            runtime_run_id,
            (time.perf_counter() - started) * 1000.0,
        )

    async def prepare_application_agent_studio(
        self,
        *,
        application_agent_id: int,
        subject_user_id: int,
        runtime_session_id: str,
        runtime_token: str,
    ) -> None:
        """Prepare the Studio session owner's knowledge before issuing a Grant."""

        if not str(runtime_token):
            raise PreparedKnowledgeStoreUnavailable(
                "application runtime credential is unavailable"
            )
        key = RuntimePreparedKnowledgeKey.application_agent(
            application_agent_id=application_agent_id,
            runtime_session_id=runtime_session_id,
            runtime_token=runtime_token,
        )
        started = time.perf_counter()

        async with AsyncSessionLocal() as db:
            current = (
                await db.execute(
                    select(
                        ApplicationAgent,
                        ApplicationAgentRuntimeBinding,
                        ApplicationAgentStudioSession,
                        User,
                    )
                    .join(
                        ApplicationAgentRuntimeBinding,
                        ApplicationAgentRuntimeBinding.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .join(
                        ApplicationAgentStudioSession,
                        ApplicationAgentStudioSession.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .join(
                        User,
                        User.id == ApplicationAgentStudioSession.user_id,
                    )
                    .where(
                        ApplicationAgent.id == application_agent_id,
                        ApplicationAgentRuntimeBinding.status == "ready",
                        ApplicationAgentRuntimeBinding.org_id
                        == ApplicationAgent.org_id,
                        ApplicationAgentStudioSession.org_id
                        == ApplicationAgent.org_id,
                        ApplicationAgentStudioSession.user_id
                        == subject_user_id,
                        ApplicationAgentStudioSession.runtime_session_id
                        == runtime_session_id,
                        ApplicationAgentStudioSession.status == "active",
                        ApplicationAgent.created_by_user_id
                        == subject_user_id,
                        User.id == subject_user_id,
                        User.org_id == ApplicationAgent.org_id,
                        User.status == "active",
                    )
                )
            ).first()
            if current is None:
                raise PreparedKnowledgeStoreUnavailable(
                    "application studio knowledge authorization unavailable"
                )
            agent, binding, _studio_session, _user = current
            try:
                expected_token = RuntimeApiKeyCodec().decrypt(
                    str(binding.api_key_encrypted)
                )
            except ValueError as exc:
                raise PreparedKnowledgeStoreUnavailable(
                    "application runtime credential is unavailable"
                ) from exc
            if not hmac.compare_digest(runtime_token, expected_token):
                raise PreparedKnowledgeStoreUnavailable(
                    "application runtime credential is unauthorized"
                )

            states: dict[PreparedKnowledgeKind, dict[str, Any]] = {
                "enterprise": {
                    "enabled": _application_enterprise_enabled(agent),
                    "mode": "full",
                },
                "personal": await _application_personal_prepared_state(
                    db=db,
                    agent=agent,
                ),
            }

        # Studio configuration and user ACLs can change between grants, so a
        # fresh grant always replaces both cached states for this session.
        await self.store.put_bundle(key, states)
        logger.info(
            "application studio prepared knowledge agent_id=%s "
            "session_id=%s user_id=%s duration_ms=%.1f",
            application_agent_id,
            runtime_session_id,
            subject_user_id,
            (time.perf_counter() - started) * 1000.0,
        )


def _application_enterprise_enabled(agent: ApplicationAgent) -> bool:
    config = agent.config or {}
    return bool(config.get("enterprise_knowledge_enabled", False))


async def _application_personal_prepared_state(
    *,
    db: AsyncSession,
    agent: ApplicationAgent,
) -> dict[str, Any]:
    enabled = (
        await db.execute(
            select(ApplicationAgentPersonalKnowledgeBinding.id)
            .where(
                ApplicationAgentPersonalKnowledgeBinding.org_id
                == int(agent.org_id),
                ApplicationAgentPersonalKnowledgeBinding
                .application_agent_id == int(agent.id),
                ApplicationAgentPersonalKnowledgeBinding.status
                == "enabled",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "enabled": enabled is not None,
        "knowledge_bases": [],
        "revision": (
            "pkb:bindings:"
            + str(
                getattr(
                    agent,
                    "personal_knowledge_binding_digest",
                    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                )
            )
        ),
    }


@lru_cache(maxsize=1)
def shared_runtime_prepared_knowledge_service() -> (
    RuntimePreparedKnowledgeService
):
    return RuntimePreparedKnowledgeService(
        RedisRuntimePreparedKnowledgeStore(
            from_url(settings.REDIS_URL, decode_responses=True),
            ttl_seconds=settings.RUNTIME_PREPARED_KNOWLEDGE_TTL_SECONDS,
        )
    )
