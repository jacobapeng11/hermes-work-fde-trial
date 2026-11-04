from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentWeComBindingTicket,
    ApplicationAgentWeComChannel,
    ApplicationAgentWeComOAuthState,
    ApplicationAgentWeComUserBinding,
    ApplicationClient,
    User,
)
from app.schemas.application_agent_wecom import (
    ApplicationAgentWeComChannelUpsertRequest,
)
from app.services.application_agent_wecom_crypto import (
    WeComCryptoError,
    encrypt_message,
)
from app.services.application_agents import (
    InMemoryApplicationAgentService,
    _secret_digest,
)
from app.services.managed_secret import ManagedSecretCodec
from app.services.runtime_workspace import Principal


class ApplicationAgentWeComError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WeComChannelRecord:
    channel_id: int
    org_id: int
    application_agent_id: int
    application_client_id: int
    agent_slug: str
    agent_name: str
    agent_status: str
    name: str
    corp_id: str
    wecom_agent_id: int
    callback_key: str
    access_key: str
    callback_token: str
    encoding_aes_key: str
    agent_secret: str
    internal_client_id: str
    internal_client_secret: str
    status: str
    updated_at: datetime | None = None

    @property
    def callback_url(self) -> str:
        return (
            f"{settings.PUBLIC_API_BASE.rstrip('/')}/channels/wecom/"
            f"{self.callback_key}/callback"
        )

    @property
    def entry_url(self) -> str:
        return (
            f"{settings.PUBLIC_API_BASE.rstrip('/')}/channels/wecom/"
            f"{self.access_key}/entry"
        )


@dataclass(frozen=True)
class WeComBindingRecord:
    binding_id: int
    platform_user_id: int
    member_id: str
    status: str
    bound_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class WeComBindingResult:
    application_agent_name: str
    channel_name: str
    member_id: str
    bound_at: datetime


class InMemoryApplicationAgentWeComService:
    def __init__(self, management: InMemoryApplicationAgentService) -> None:
        self.management = management
        self.channels: dict[int, WeComChannelRecord] = {}
        self.bindings: dict[tuple[int, str], WeComBindingRecord] = {}
        self.tickets: dict[str, tuple[int, str, datetime, bool]] = {}
        self.oauth_states: dict[str, tuple[int, datetime, bool]] = {}
        self._next_channel_id = 1
        self._next_binding_id = 1

    def reset(self) -> None:
        self.channels.clear()
        self.bindings.clear()
        self.tickets.clear()
        self.oauth_states.clear()
        self._next_channel_id = 1
        self._next_binding_id = 1

    def _agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
        require_owner: bool = False,
    ):
        agent = self.management.agents.get(agent_id)
        if agent is None or agent.org_id != principal.org_id:
            raise ApplicationAgentWeComError(
                "application_agent_not_found",
                status_code=404,
            )
        if require_owner and agent.created_by_user_id != principal.user_id:
            raise ApplicationAgentWeComError(
                "application_agent_owner_required",
                status_code=403,
            )
        return agent

    async def get_channel(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> WeComChannelRecord | None:
        self._agent(principal=principal, agent_id=agent_id)
        return self.channels.get(agent_id)

    async def upsert_channel(
        self,
        *,
        principal: Principal,
        agent_id: int,
        payload: ApplicationAgentWeComChannelUpsertRequest,
    ) -> WeComChannelRecord:
        agent = self._agent(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        existing = self.channels.get(agent_id)
        callback_token = payload.callback_token or (
            existing.callback_token if existing else ""
        )
        encoding_aes_key = payload.encoding_aes_key or (
            existing.encoding_aes_key if existing else ""
        )
        agent_secret = payload.agent_secret or (
            existing.agent_secret if existing else ""
        )
        _validate_channel_secrets(
            callback_token=callback_token,
            encoding_aes_key=encoding_aes_key,
            agent_secret=agent_secret,
            corp_id=payload.corp_id,
        )
        if payload.enabled and agent.status != "published":
            raise ApplicationAgentWeComError(
                "application_agent_must_be_published",
                status_code=409,
            )
        if existing is None:
            access_key = f"wca_{secrets.token_urlsafe(24)}"
            internal_secret = f"wcic_{secrets.token_urlsafe(32)}"
            client = await self.management.create_client(
                principal=principal,
                agent_id=agent_id,
                payload=_internal_client_payload(),
            )
            existing = WeComChannelRecord(
                channel_id=self._next_channel_id,
                org_id=principal.org_id,
                application_agent_id=agent_id,
                application_client_id=client.application_client_id,
                agent_slug=agent.slug,
                agent_name=agent.name,
                agent_status=agent.status,
                name=payload.name,
                corp_id=payload.corp_id,
                wecom_agent_id=payload.wecom_agent_id,
                callback_key=secrets.token_urlsafe(24),
                access_key=access_key,
                callback_token=callback_token,
                encoding_aes_key=encoding_aes_key,
                agent_secret=agent_secret,
                internal_client_id=client.client_id,
                internal_client_secret=client.client_secret or internal_secret,
                status="active" if payload.enabled else "disabled",
                updated_at=_now(),
            )
            self._next_channel_id += 1
        else:
            existing = replace(
                existing,
                name=payload.name,
                corp_id=payload.corp_id,
                wecom_agent_id=payload.wecom_agent_id,
                callback_token=callback_token,
                encoding_aes_key=encoding_aes_key,
                agent_secret=agent_secret,
                status="active" if payload.enabled else "disabled",
                updated_at=_now(),
            )
        self.channels[agent_id] = existing
        return existing

    async def list_bindings(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[WeComBindingRecord]:
        channel = await self.get_channel(principal=principal, agent_id=agent_id)
        if channel is None:
            return []
        return [
            row
            for (channel_id, _), row in self.bindings.items()
            if channel_id == channel.channel_id
        ]

    async def rotate_entry(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> WeComChannelRecord:
        self._agent(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        channel = await self.get_channel(principal=principal, agent_id=agent_id)
        if channel is None:
            raise ApplicationAgentWeComError(
                "wecom_channel_not_found",
                status_code=404,
            )
        updated = replace(
            channel,
            access_key=f"wca_{secrets.token_urlsafe(24)}",
            updated_at=_now(),
        )
        self.channels[agent_id] = updated
        return updated

    async def revoke_binding(
        self,
        *,
        principal: Principal,
        agent_id: int,
        binding_id: int,
    ) -> None:
        self._agent(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        channel = await self.get_channel(principal=principal, agent_id=agent_id)
        if channel is None:
            raise ApplicationAgentWeComError(
                "wecom_channel_not_found",
                status_code=404,
            )
        for key, binding in self.bindings.items():
            if (
                key[0] == channel.channel_id
                and binding.binding_id == binding_id
            ):
                self.bindings[key] = replace(binding, status="revoked")
                return
        raise ApplicationAgentWeComError(
            "wecom_binding_not_found",
            status_code=404,
        )

    async def find_channel_by_access_key(
        self,
        access_key: str,
    ) -> WeComChannelRecord:
        for channel in self.channels.values():
            if secrets.compare_digest(channel.access_key, access_key):
                return _active_channel(channel)
        raise ApplicationAgentWeComError(
            "wecom_channel_not_found",
            status_code=404,
        )

    async def find_channel_by_callback_key(
        self,
        callback_key: str,
    ) -> WeComChannelRecord:
        for channel in self.channels.values():
            if secrets.compare_digest(channel.callback_key, callback_key):
                return _active_channel(channel)
        raise ApplicationAgentWeComError(
            "wecom_channel_not_found",
            status_code=404,
        )

    async def create_oauth_state(self, channel_id: int) -> tuple[str, int]:
        state = f"wcos_{secrets.token_urlsafe(24)}"
        expires_in = settings.APPLICATION_AGENT_WECOM_OAUTH_STATE_TTL_SECONDS
        self.oauth_states[_digest(state)] = (
            channel_id,
            _now() + timedelta(seconds=expires_in),
            False,
        )
        return state, expires_in

    async def consume_oauth_state(self, state: str) -> WeComChannelRecord:
        row = self.oauth_states.get(_digest(state))
        if row is None or row[2] or row[1] <= _now():
            raise ApplicationAgentWeComError(
                "wecom_oauth_state_invalid",
                status_code=401,
            )
        self.oauth_states[_digest(state)] = (row[0], row[1], True)
        for channel in self.channels.values():
            if channel.channel_id == row[0]:
                return _active_channel(channel)
        raise ApplicationAgentWeComError(
            "wecom_channel_not_found",
            status_code=404,
        )

    async def create_binding_ticket(
        self,
        *,
        channel: WeComChannelRecord,
        member_id: str,
    ) -> str:
        ticket = f"wcb_{secrets.token_urlsafe(32)}"
        self.tickets[_digest(ticket)] = (
            channel.channel_id,
            member_id,
            _now()
            + timedelta(
                seconds=settings.APPLICATION_AGENT_WECOM_BINDING_TICKET_TTL_SECONDS
            ),
            False,
        )
        return ticket

    async def bind(
        self,
        *,
        principal: Principal,
        ticket: str,
    ) -> WeComBindingResult:
        digest = _digest(ticket)
        row = self.tickets.get(digest)
        if row is None or row[3] or row[2] <= _now():
            raise ApplicationAgentWeComError(
                "wecom_binding_ticket_invalid",
                status_code=401,
            )
        channel = next(
            (
                item
                for item in self.channels.values()
                if item.channel_id == row[0]
            ),
            None,
        )
        if channel is None or channel.org_id != principal.org_id:
            raise ApplicationAgentWeComError(
                "wecom_binding_identity_mismatch",
                status_code=403,
            )
        key = (channel.channel_id, row[1])
        existing = self.bindings.get(key)
        if existing is not None and existing.platform_user_id != principal.user_id:
            raise ApplicationAgentWeComError(
                "wecom_binding_conflict",
                status_code=409,
            )
        bound_at = _now()
        binding = (
            replace(existing, status="active", bound_at=bound_at)
            if existing is not None
            else WeComBindingRecord(
                binding_id=self._next_binding_id,
                platform_user_id=principal.user_id,
                member_id=row[1],
                status="active",
                bound_at=bound_at,
                last_used_at=None,
            )
        )
        if existing is None:
            self._next_binding_id += 1
        self.bindings[key] = binding
        self.tickets[digest] = (row[0], row[1], row[2], True)
        return WeComBindingResult(
            application_agent_name=channel.agent_name,
            channel_name=channel.name,
            member_id=row[1],
            bound_at=bound_at,
        )

    async def bound_principal(
        self,
        *,
        channel: WeComChannelRecord,
        member_id: str,
    ) -> Principal | None:
        binding = self.bindings.get((channel.channel_id, member_id))
        if binding is None or binding.status != "active":
            return None
        self.bindings[(channel.channel_id, member_id)] = replace(
            binding,
            last_used_at=_now(),
        )
        return Principal(user_id=binding.platform_user_id, org_id=channel.org_id)


class SqlAlchemyApplicationAgentWeComService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        codec: ManagedSecretCodec | None = None,
    ) -> None:
        self.db = db
        self.codec = codec or ManagedSecretCodec()

    async def get_channel(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> WeComChannelRecord | None:
        agent = await self._agent(principal=principal, agent_id=agent_id)
        row = (
            await self.db.execute(
                select(ApplicationAgentWeComChannel, ApplicationClient)
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentWeComChannel.application_client_id,
                )
                .where(
                    ApplicationAgentWeComChannel.application_agent_id == agent.id,
                    ApplicationAgentWeComChannel.org_id == principal.org_id,
                )
            )
        ).first()
        if row is None:
            return None
        return self._record(row[0], row[1], agent)

    async def upsert_channel(
        self,
        *,
        principal: Principal,
        agent_id: int,
        payload: ApplicationAgentWeComChannelUpsertRequest,
    ) -> WeComChannelRecord:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        existing = (
            await self.db.execute(
                select(ApplicationAgentWeComChannel).where(
                    ApplicationAgentWeComChannel.application_agent_id == agent.id
                )
            )
        ).scalar_one_or_none()
        callback_token = payload.callback_token or (
            self.codec.decrypt(str(existing.callback_token_encrypted))
            if existing is not None
            else ""
        )
        encoding_aes_key = payload.encoding_aes_key or (
            self.codec.decrypt(str(existing.encoding_aes_key_encrypted))
            if existing is not None
            else ""
        )
        agent_secret = payload.agent_secret or (
            self.codec.decrypt(str(existing.agent_secret_encrypted))
            if existing is not None
            else ""
        )
        _validate_channel_secrets(
            callback_token=callback_token,
            encoding_aes_key=encoding_aes_key,
            agent_secret=agent_secret,
            corp_id=payload.corp_id,
        )
        if payload.enabled and str(agent.status) != "published":
            raise ApplicationAgentWeComError(
                "application_agent_must_be_published",
                status_code=409,
            )
        if existing is None:
            access_key = f"wca_{secrets.token_urlsafe(24)}"
            internal_secret = f"wcic_{secrets.token_urlsafe(32)}"
            client = ApplicationClient(
                org_id=principal.org_id,
                client_id=f"wecom_{agent.id}_{secrets.token_hex(8)}",
                name=f"{payload.name}（渠道）",
                auth_method="internal_channel",
                credential_digest_or_reference=_secret_digest(internal_secret),
                status="active",
                allowed_redirect_uris=[],
                rate_limit_policy={},
                created_by_user_id=principal.user_id,
            )
            self.db.add(client)
            await self.db.flush()
            self.db.add(
                ApplicationAgentClientGrant(
                    org_id=principal.org_id,
                    application_agent_id=agent.id,
                    application_client_id=client.id,
                    scopes=["responses:create"],
                    allow_user_delegation=True,
                    allow_service_invocation=False,
                    status="active",
                )
            )
            existing = ApplicationAgentWeComChannel(
                org_id=principal.org_id,
                application_agent_id=agent.id,
                application_client_id=client.id,
                name=payload.name,
                corp_id=payload.corp_id,
                wecom_agent_id=payload.wecom_agent_id,
                callback_key=secrets.token_urlsafe(24),
                access_key_digest=_digest(access_key),
                access_key_encrypted=self.codec.encrypt(access_key),
                internal_client_secret_encrypted=self.codec.encrypt(
                    internal_secret
                ),
                callback_token_encrypted=self.codec.encrypt(callback_token),
                encoding_aes_key_encrypted=self.codec.encrypt(encoding_aes_key),
                agent_secret_encrypted=self.codec.encrypt(agent_secret),
                status="active" if payload.enabled else "disabled",
            )
            self.db.add(existing)
        else:
            client = (
                await self.db.execute(
                    select(ApplicationClient).where(
                        ApplicationClient.id == existing.application_client_id
                    )
                )
            ).scalar_one()
            existing.name = payload.name
            existing.corp_id = payload.corp_id
            existing.wecom_agent_id = payload.wecom_agent_id
            existing.callback_token_encrypted = self.codec.encrypt(callback_token)
            existing.encoding_aes_key_encrypted = self.codec.encrypt(
                encoding_aes_key
            )
            existing.agent_secret_encrypted = self.codec.encrypt(agent_secret)
            existing.status = "active" if payload.enabled else "disabled"
        await self.db.commit()
        await self.db.refresh(existing)
        return self._record(existing, client, agent)

    async def list_bindings(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[WeComBindingRecord]:
        channel = await self.get_channel(principal=principal, agent_id=agent_id)
        if channel is None:
            return []
        rows = (
            await self.db.execute(
                select(ApplicationAgentWeComUserBinding)
                .where(
                    ApplicationAgentWeComUserBinding.channel_id
                    == channel.channel_id
                )
                .order_by(
                    ApplicationAgentWeComUserBinding.bound_at.desc(),
                    ApplicationAgentWeComUserBinding.id.desc(),
                )
            )
        ).scalars()
        return [self._binding_record(row) for row in rows]

    async def rotate_entry(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> WeComChannelRecord:
        await self._agent(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        channel = (
            await self.db.execute(
                select(ApplicationAgentWeComChannel).where(
                    ApplicationAgentWeComChannel.application_agent_id == agent_id,
                    ApplicationAgentWeComChannel.org_id == principal.org_id,
                )
            )
        ).scalar_one_or_none()
        if channel is None:
            raise ApplicationAgentWeComError(
                "wecom_channel_not_found",
                status_code=404,
            )
        access_key = f"wca_{secrets.token_urlsafe(24)}"
        channel.access_key_digest = _digest(access_key)
        channel.access_key_encrypted = self.codec.encrypt(access_key)
        await self.db.commit()
        return await self.get_channel(principal=principal, agent_id=agent_id)

    async def revoke_binding(
        self,
        *,
        principal: Principal,
        agent_id: int,
        binding_id: int,
    ) -> None:
        await self._agent(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        channel = await self.get_channel(principal=principal, agent_id=agent_id)
        if channel is None:
            raise ApplicationAgentWeComError(
                "wecom_channel_not_found",
                status_code=404,
            )
        binding = (
            await self.db.execute(
                select(ApplicationAgentWeComUserBinding).where(
                    ApplicationAgentWeComUserBinding.id == binding_id,
                    ApplicationAgentWeComUserBinding.channel_id
                    == channel.channel_id,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            raise ApplicationAgentWeComError(
                "wecom_binding_not_found",
                status_code=404,
            )
        binding.status = "revoked"
        await self.db.commit()

    async def find_channel_by_access_key(
        self,
        access_key: str,
    ) -> WeComChannelRecord:
        row = await self._public_channel(
            ApplicationAgentWeComChannel.access_key_digest == _digest(access_key)
        )
        channel = self._record(*row)
        return _active_channel(channel)

    async def find_channel_by_callback_key(
        self,
        callback_key: str,
    ) -> WeComChannelRecord:
        row = await self._public_channel(
            ApplicationAgentWeComChannel.callback_key == callback_key
        )
        channel = self._record(*row)
        return _active_channel(channel)

    async def create_oauth_state(self, channel_id: int) -> tuple[str, int]:
        state = f"wcos_{secrets.token_urlsafe(24)}"
        expires_in = settings.APPLICATION_AGENT_WECOM_OAUTH_STATE_TTL_SECONDS
        self.db.add(
            ApplicationAgentWeComOAuthState(
                channel_id=channel_id,
                state_digest=_digest(state),
                expires_at=_now() + timedelta(seconds=expires_in),
            )
        )
        await self.db.commit()
        return state, expires_in

    async def consume_oauth_state(self, state: str) -> WeComChannelRecord:
        row = (
            await self.db.execute(
                select(ApplicationAgentWeComOAuthState)
                .where(
                    ApplicationAgentWeComOAuthState.state_digest
                    == _digest(state)
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.consumed_at is not None or row.expires_at <= _now():
            await self.db.rollback()
            raise ApplicationAgentWeComError(
                "wecom_oauth_state_invalid",
                status_code=401,
            )
        row.consumed_at = _now()
        await self.db.commit()
        public_row = await self._public_channel(
            ApplicationAgentWeComChannel.id == row.channel_id
        )
        return _active_channel(self._record(*public_row))

    async def create_binding_ticket(
        self,
        *,
        channel: WeComChannelRecord,
        member_id: str,
    ) -> str:
        ticket = f"wcb_{secrets.token_urlsafe(32)}"
        self.db.add(
            ApplicationAgentWeComBindingTicket(
                org_id=channel.org_id,
                channel_id=channel.channel_id,
                wecom_member_id=member_id,
                ticket_digest=_digest(ticket),
                expires_at=_now()
                + timedelta(
                    seconds=(
                        settings.APPLICATION_AGENT_WECOM_BINDING_TICKET_TTL_SECONDS
                    )
                ),
            )
        )
        await self.db.commit()
        return ticket

    async def bind(
        self,
        *,
        principal: Principal,
        ticket: str,
    ) -> WeComBindingResult:
        ticket_row = (
            await self.db.execute(
                select(ApplicationAgentWeComBindingTicket)
                .where(
                    ApplicationAgentWeComBindingTicket.ticket_digest
                    == _digest(ticket)
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            ticket_row is None
            or ticket_row.consumed_at is not None
            or ticket_row.expires_at <= _now()
        ):
            await self.db.rollback()
            raise ApplicationAgentWeComError(
                "wecom_binding_ticket_invalid",
                status_code=401,
            )
        channel_row = await self._public_channel(
            ApplicationAgentWeComChannel.id == ticket_row.channel_id
        )
        channel = self._record(*channel_row)
        if channel.org_id != principal.org_id:
            await self.db.rollback()
            raise ApplicationAgentWeComError(
                "wecom_binding_identity_mismatch",
                status_code=403,
            )
        user_exists = (
            await self.db.execute(
                select(User.id).where(
                    User.id == principal.user_id,
                    User.org_id == principal.org_id,
                    User.status == "active",
                )
            )
        ).scalar_one_or_none()
        if user_exists is None:
            await self.db.rollback()
            raise ApplicationAgentWeComError(
                "wecom_binding_identity_mismatch",
                status_code=403,
            )
        binding = (
            await self.db.execute(
                select(ApplicationAgentWeComUserBinding).where(
                    ApplicationAgentWeComUserBinding.channel_id
                    == channel.channel_id,
                    ApplicationAgentWeComUserBinding.wecom_member_id
                    == ticket_row.wecom_member_id,
                )
            )
        ).scalar_one_or_none()
        if binding is not None and binding.platform_user_id != principal.user_id:
            await self.db.rollback()
            raise ApplicationAgentWeComError(
                "wecom_binding_conflict",
                status_code=409,
            )
        bound_at = _now()
        if binding is None:
            binding = ApplicationAgentWeComUserBinding(
                org_id=principal.org_id,
                channel_id=channel.channel_id,
                wecom_member_id=ticket_row.wecom_member_id,
                platform_user_id=principal.user_id,
                status="active",
                bound_at=bound_at,
            )
            self.db.add(binding)
        else:
            binding.status = "active"
            binding.bound_at = bound_at
        ticket_row.consumed_at = bound_at
        await self.db.commit()
        return WeComBindingResult(
            application_agent_name=channel.agent_name,
            channel_name=channel.name,
            member_id=str(ticket_row.wecom_member_id),
            bound_at=bound_at,
        )

    async def bound_principal(
        self,
        *,
        channel: WeComChannelRecord,
        member_id: str,
    ) -> Principal | None:
        binding = (
            await self.db.execute(
                select(ApplicationAgentWeComUserBinding, User)
                .join(User, User.id == ApplicationAgentWeComUserBinding.platform_user_id)
                .where(
                    ApplicationAgentWeComUserBinding.channel_id
                    == channel.channel_id,
                    ApplicationAgentWeComUserBinding.wecom_member_id == member_id,
                    ApplicationAgentWeComUserBinding.status == "active",
                    User.org_id == channel.org_id,
                    User.status == "active",
                )
            )
        ).first()
        if binding is None:
            return None
        binding[0].last_used_at = _now()
        await self.db.commit()
        return Principal(
            user_id=int(binding[0].platform_user_id),
            org_id=channel.org_id,
        )

    async def _agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
        require_owner: bool = False,
    ) -> ApplicationAgent:
        agent = (
            await self.db.execute(
                select(ApplicationAgent).where(
                    ApplicationAgent.id == agent_id,
                    ApplicationAgent.org_id == principal.org_id,
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise ApplicationAgentWeComError(
                "application_agent_not_found",
                status_code=404,
            )
        if require_owner and int(agent.created_by_user_id) != principal.user_id:
            raise ApplicationAgentWeComError(
                "application_agent_owner_required",
                status_code=403,
            )
        return agent

    async def _public_channel(self, condition):
        row = (
            await self.db.execute(
                select(
                    ApplicationAgentWeComChannel,
                    ApplicationClient,
                    ApplicationAgent,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentWeComChannel.application_client_id,
                )
                .join(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ApplicationAgentWeComChannel.application_agent_id,
                )
                .where(condition)
            )
        ).first()
        if row is None:
            raise ApplicationAgentWeComError(
                "wecom_channel_not_found",
                status_code=404,
            )
        return row

    def _record(
        self,
        channel: ApplicationAgentWeComChannel,
        client: ApplicationClient,
        agent: ApplicationAgent,
    ) -> WeComChannelRecord:
        return WeComChannelRecord(
            channel_id=int(channel.id),
            org_id=int(channel.org_id),
            application_agent_id=int(channel.application_agent_id),
            application_client_id=int(channel.application_client_id),
            agent_slug=str(agent.slug),
            agent_name=str(agent.name),
            agent_status=str(agent.status),
            name=str(channel.name),
            corp_id=str(channel.corp_id),
            wecom_agent_id=int(channel.wecom_agent_id),
            callback_key=str(channel.callback_key),
            access_key=self.codec.decrypt(str(channel.access_key_encrypted)),
            callback_token=self.codec.decrypt(
                str(channel.callback_token_encrypted)
            ),
            encoding_aes_key=self.codec.decrypt(
                str(channel.encoding_aes_key_encrypted)
            ),
            agent_secret=self.codec.decrypt(str(channel.agent_secret_encrypted)),
            internal_client_id=str(client.client_id),
            internal_client_secret=self.codec.decrypt(
                str(channel.internal_client_secret_encrypted)
            ),
            status=str(channel.status),
            updated_at=channel.updated_at,
        )

    @staticmethod
    def _binding_record(
        row: ApplicationAgentWeComUserBinding,
    ) -> WeComBindingRecord:
        return WeComBindingRecord(
            binding_id=int(row.id),
            platform_user_id=int(row.platform_user_id),
            member_id=str(row.wecom_member_id),
            status=str(row.status),
            bound_at=row.bound_at,
            last_used_at=row.last_used_at,
        )


class WeComApiClient:
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def resolve_oauth_member(
        self,
        *,
        channel: WeComChannelRecord,
        code: str,
    ) -> str:
        access_token = await self._access_token(channel)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{settings.WECOM_API_BASE_URL.rstrip('/')}/cgi-bin/auth/getuserinfo",
                params={"access_token": access_token, "code": code},
            )
        payload = _wecom_json(response)
        member_id = payload.get("UserId")
        if not isinstance(member_id, str) or not member_id.strip():
            raise ApplicationAgentWeComError(
                "wecom_oauth_user_not_found",
                status_code=401,
            )
        return member_id.strip()

    async def send_text(
        self,
        *,
        channel: WeComChannelRecord,
        member_id: str,
        content: str,
    ) -> None:
        access_token = await self._access_token(channel)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{settings.WECOM_API_BASE_URL.rstrip('/')}/cgi-bin/message/send",
                params={"access_token": access_token},
                json={
                    "touser": member_id,
                    "msgtype": "text",
                    "agentid": channel.wecom_agent_id,
                    "text": {"content": content[:2048]},
                    "safe": 0,
                },
            )
        _wecom_json(response)

    async def _access_token(self, channel: WeComChannelRecord) -> str:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{settings.WECOM_API_BASE_URL.rstrip('/')}/cgi-bin/gettoken",
                params={
                    "corpid": channel.corp_id,
                    "corpsecret": channel.agent_secret,
                },
            )
        payload = _wecom_json(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ApplicationAgentWeComError(
                "wecom_access_token_failed",
                status_code=502,
            )
        return access_token


def oauth_authorize_url(
    *,
    channel: WeComChannelRecord,
    state: str,
) -> str:
    redirect_uri = (
        f"{settings.PUBLIC_API_BASE.rstrip('/')}/channels/wecom/"
        f"{channel.callback_key}/oauth"
    )
    query = urlencode(
        {
            "appid": channel.corp_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
        },
        quote_via=quote,
    )
    return f"{settings.WECOM_OAUTH_AUTHORIZE_URL}?{query}#wechat_redirect"


def binding_web_url(ticket: str) -> str:
    return (
        f"{settings.PUBLIC_WEB_BASE.rstrip('/')}/user/wecom-bind?"
        f"{urlencode({'ticket': ticket})}"
    )


def mask_member_id(member_id: str) -> str:
    if len(member_id) <= 4:
        return "*" * len(member_id)
    return f"{member_id[:2]}{'*' * (len(member_id) - 4)}{member_id[-2:]}"


def _validate_channel_secrets(
    *,
    callback_token: str,
    encoding_aes_key: str,
    agent_secret: str,
    corp_id: str,
) -> None:
    if not callback_token or not agent_secret:
        raise ApplicationAgentWeComError(
            "wecom_secrets_required",
            status_code=422,
        )
    try:
        encrypt_message(
            encoding_aes_key=encoding_aes_key,
            plaintext="channel-preflight",
            receive_id=corp_id,
        )
    except WeComCryptoError as exc:
        raise ApplicationAgentWeComError(
            exc.code,
            status_code=422,
        ) from exc


def _active_channel(channel: WeComChannelRecord) -> WeComChannelRecord:
    if channel.status != "active":
        raise ApplicationAgentWeComError(
            "wecom_channel_disabled",
            status_code=403,
        )
    if channel.agent_status != "published":
        raise ApplicationAgentWeComError(
            "application_agent_not_published",
            status_code=409,
        )
    return channel


def _internal_client_payload():
    from app.schemas.application_agent import ApplicationClientCreateRequest

    return ApplicationClientCreateRequest(
        name="企业微信渠道",
        allow_user_delegation=True,
        allow_service_invocation=False,
        scopes=["responses:create"],
    )


def _wecom_json(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise ApplicationAgentWeComError(
            "wecom_api_unavailable",
            status_code=502,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApplicationAgentWeComError(
            "wecom_api_invalid_response",
            status_code=502,
        ) from exc
    if not isinstance(payload, dict) or int(payload.get("errcode", 0)) != 0:
        raise ApplicationAgentWeComError(
            "wecom_api_rejected",
            status_code=502,
        )
    return payload


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
