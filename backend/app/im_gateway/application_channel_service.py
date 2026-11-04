from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentWeComChannel,
    ApplicationAgentWeComUserAccess,
    ApplicationClient,
    ImChannel,
    ImInboundMessage,
    ImOnboardingSession,
    OrganizationWeComIdentity,
    User,
)
from app.schemas.im_gateway import (
    ApplicationWeComChannelResponse,
    ApplicationWeComMessageListResponse,
    ApplicationWeComMessageSummary,
    ApplicationWeComUserListResponse,
    ApplicationWeComUserSummary,
    ImQrOnboardingCreated,
    ImQrOnboardingStatus,
    RegisterWeComChannelRequest,
)
from app.services.managed_secret import ManagedSecretCodec
from app.services.runtime_workspace import Principal

from .channel_service import (
    ImChannelConflictError,
    ImChannelNotConfiguredError,
    ImGatewayServiceError,
    ImOnboardingNotFoundError,
    ImOnboardingNotReadyError,
    ImUnsupportedConnectionModeError,
)
from .identity import opaque_digest
from .onboarding import WeComQrClient, WeComQrProtocolError


class ApplicationAgentUnavailableError(ImGatewayServiceError):
    code = "application_agent_unavailable"


@dataclass(frozen=True)
class RegisteredApplicationChannel:
    channel_id: int
    config_generation: int
    response: ApplicationWeComChannelResponse


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApplicationAgentImChannelService:
    """Application-target facade over the common durable IM Gateway."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        secret_codec: ManagedSecretCodec | None = None,
        qr_client: WeComQrClient | None = None,
        instance_id: str = "application-channel-api",
    ) -> None:
        self.db = db
        self.secret_codec = secret_codec or ManagedSecretCodec()
        self.qr_client = qr_client or WeComQrClient()
        self.instance_id = instance_id

    async def get_channel(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ApplicationWeComChannelResponse:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
        )
        channel = await self._channel(agent_id=agent_id)
        legacy = await self.db.scalar(
            select(ApplicationAgentWeComChannel.id)
            .where(
                ApplicationAgentWeComChannel.application_agent_id
                == agent_id,
            )
            .limit(1)
        )
        if channel is None:
            return ApplicationWeComChannelResponse(
                application_agent_id=int(agent.id),
                activation_allowed=str(agent.status) == "published",
                status=(
                    "legacy_migration_required"
                    if legacy is not None
                    else "unconfigured"
                ),
                legacy_migration_required=legacy is not None,
            )
        return self._response(
            channel,
            activation_allowed=str(agent.status) == "published",
            legacy_migration_required=False,
        )

    async def create_qr_onboarding(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ImQrOnboardingCreated:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
        )
        try:
            challenge = await self.qr_client.generate()
        except WeComQrProtocolError as exc:
            raise ImGatewayServiceError(str(exc)) from exc
        now = utc_now()
        expires_at = now + timedelta(
            seconds=settings.IM_GATEWAY_ONBOARDING_TTL_SECONDS
        )
        public_id = f"ima_{uuid4().hex}"
        self.db.add(
            ImOnboardingSession(
                public_id=public_id,
                org_id=int(agent.org_id),
                user_id=principal.user_id,
                target_type="application_agent",
                runtime_workspace_id=None,
                application_agent_id=int(agent.id),
                provider="wecom",
                method="qrcode",
                status="pending",
                qr_scode_encrypted=self.secret_codec.encrypt(
                    challenge.scode
                ),
                qr_scode_digest=hashlib.sha256(
                    challenge.scode.encode("utf-8")
                ).hexdigest(),
                qr_url=challenge.auth_url,
                expires_at=expires_at,
            )
        )
        await self.db.commit()
        return ImQrOnboardingCreated(
            onboarding_id=public_id,
            qr_content=challenge.auth_url,
            expires_at=expires_at,
            poll_after_ms=int(
                settings.IM_GATEWAY_ONBOARDING_POLL_SECONDS * 1000
            ),
        )

    async def get_qr_onboarding(
        self,
        *,
        principal: Principal,
        agent_id: int,
        onboarding_id: str,
    ) -> ImQrOnboardingStatus:
        await self._agent(principal=principal, agent_id=agent_id)
        row = await self._onboarding(
            principal=principal,
            agent_id=agent_id,
            onboarding_id=onboarding_id,
            for_update=True,
        )
        now = utc_now()
        if (
            row.status not in {"consumed", "failed", "expired"}
            and _aware(row.expires_at) <= now
        ):
            self._expire_onboarding(row)
            await self.db.commit()
            return self._onboarding_status(row)
        should_poll = (
            row.status in {"pending", "scanned"}
            and (
                row.poll_owner == self.instance_id
                or row.poll_lease_expires_at is None
                or _aware(row.poll_lease_expires_at) <= now
            )
        )
        if not should_poll:
            return self._onboarding_status(row)
        row.poll_owner = self.instance_id
        row.poll_lease_expires_at = now + timedelta(
            seconds=max(
                5.0,
                settings.IM_GATEWAY_ONBOARDING_POLL_SECONDS * 2,
            )
        )
        encrypted_scode = row.qr_scode_encrypted
        await self.db.commit()
        if not encrypted_scode:
            raise ImOnboardingNotReadyError(
                "onboarding challenge is unavailable"
            )
        try:
            polled = await self.qr_client.poll(
                self.secret_codec.decrypt(encrypted_scode)
            )
        except WeComQrProtocolError as exc:
            row = await self._onboarding(
                principal=principal,
                agent_id=agent_id,
                onboarding_id=onboarding_id,
                for_update=True,
            )
            row.poll_owner = None
            row.poll_lease_expires_at = None
            row.last_error_code = str(exc)
            row.last_error_message = "企微二维码状态暂时不可用，请稍后重试"
            await self.db.commit()
            return self._onboarding_status(row)
        row = await self._onboarding(
            principal=principal,
            agent_id=agent_id,
            onboarding_id=onboarding_id,
            for_update=True,
        )
        if row.poll_owner != self.instance_id:
            return self._onboarding_status(row)
        row.poll_owner = None
        row.poll_lease_expires_at = None
        row.last_error_code = None
        row.last_error_message = None
        if polled.status == "credentials_ready":
            if not polled.bot_id or not polled.secret:
                row.status = "failed"
                row.last_error_code = "wecom_qr_missing_credentials"
                row.last_error_message = "企微未返回完整的机器人凭据"
            else:
                row.status = "credentials_ready"
                row.bot_id = polled.bot_id
                row.credentials_encrypted = self.secret_codec.encrypt(
                    json.dumps(
                        {
                            "bot_id": polled.bot_id,
                            "secret": polled.secret,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
        elif polled.status == "expired":
            self._expire_onboarding(row)
        elif polled.status == "failed":
            self._expire_onboarding(row)
            row.status = "failed"
        else:
            row.status = polled.status
        await self.db.commit()
        return self._onboarding_status(row)

    async def register_wecom(
        self,
        *,
        principal: Principal,
        agent_id: int,
        request: RegisterWeComChannelRequest,
    ) -> RegisteredApplicationChannel:
        if request.connection_mode != "websocket":
            raise ImUnsupportedConnectionModeError(
                "URL callback is not available"
            )
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
        )
        onboarding: ImOnboardingSession | None = None
        if request.onboarding_id:
            onboarding = await self._onboarding(
                principal=principal,
                agent_id=agent_id,
                onboarding_id=request.onboarding_id,
                for_update=True,
            )
            if (
                onboarding.status != "credentials_ready"
                or not onboarding.credentials_encrypted
            ):
                raise ImOnboardingNotReadyError(
                    "wecom credentials are not ready"
                )
            credentials = json.loads(
                self.secret_codec.decrypt(
                    onboarding.credentials_encrypted
                )
            )
            bot_id = str(credentials.get("bot_id") or "").strip()
            secret = str(credentials.get("secret") or "").strip()
        else:
            bot_id = str(request.bot_id or "").strip()
            secret = str(request.secret or "").strip()
        if not bot_id or not secret:
            raise ImOnboardingNotReadyError(
                "complete wecom credentials are required"
            )
        collision = await self.db.scalar(
            select(ImChannel.id)
            .where(
                ImChannel.provider == "wecom",
                ImChannel.bot_id == bot_id,
                or_(
                    ImChannel.target_type != "application_agent",
                    ImChannel.application_agent_id != agent_id,
                ),
            )
            .limit(1)
        )
        if collision is not None:
            raise ImChannelConflictError(
                "this WeCom bot is already bound to another Agent"
            )
        channel = await self._channel(
            agent_id=agent_id,
            for_update=True,
        )
        if channel is None:
            client = await self._channel_client(
                principal=principal,
                agent=agent,
            )
            channel = ImChannel(
                target_type="application_agent",
                org_id=principal.org_id,
                created_by_user_id=principal.user_id,
                user_id=None,
                runtime_workspace_id=None,
                runtime_session_row_id=None,
                application_agent_id=int(agent.id),
                application_client_id=int(client.id),
                provider="wecom",
                connection_mode="websocket",
                enabled=str(agent.status) == "published",
                status=(
                    "connecting"
                    if str(agent.status) == "published"
                    else "disabled"
                ),
                config_generation=1,
                direct_enabled=True,
                group_enabled=True,
            )
            self.db.add(channel)
        else:
            channel.config_generation = int(
                channel.config_generation
            ) + 1
        channel.bot_id = bot_id
        channel.secret_encrypted = self.secret_codec.encrypt(secret)
        channel.last_error_code = None
        channel.last_error_message = None
        if onboarding is not None:
            onboarding.status = "consumed"
            onboarding.consumed_at = utc_now()
            onboarding.credentials_encrypted = None
            onboarding.qr_scode_encrypted = None
            onboarding.qr_url = None
            onboarding.poll_owner = None
            onboarding.poll_lease_expires_at = None
        legacy = (
            await self.db.execute(
                select(ApplicationAgentWeComChannel)
                .where(
                    ApplicationAgentWeComChannel.application_agent_id
                    == agent_id
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if legacy is not None:
            legacy.status = "disabled"
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ImChannelConflictError(
                "this WeCom bot or Agent is already bound"
            ) from exc
        await self.db.refresh(channel)
        response = self._response(
            channel,
            activation_allowed=str(agent.status) == "published",
        )
        return RegisteredApplicationChannel(
            channel_id=int(channel.id),
            config_generation=int(channel.config_generation),
            response=response,
        )

    async def set_enabled(
        self,
        *,
        principal: Principal,
        agent_id: int,
        enabled: bool,
    ) -> ApplicationWeComChannelResponse:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
        )
        channel = await self._required_channel(
            agent_id=agent_id,
            for_update=True,
        )
        if enabled and str(agent.status) != "published":
            raise ApplicationAgentUnavailableError(
                "application Agent must be published before enabling WeCom"
            )
        if enabled and (
            not channel.bot_id or not channel.secret_encrypted
        ):
            raise ImChannelNotConfiguredError(
                "wecom credentials are not configured"
            )
        channel.enabled = enabled
        channel.status = "connecting" if enabled else "disabled"
        channel.config_generation += 1
        channel.last_error_code = None
        channel.last_error_message = None
        await self.db.commit()
        await self.db.refresh(channel)
        return self._response(
            channel,
            activation_allowed=str(agent.status) == "published",
        )

    async def reconnect(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ApplicationWeComChannelResponse:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
        )
        channel = await self._required_channel(
            agent_id=agent_id,
            for_update=True,
        )
        if not channel.enabled:
            raise ImChannelNotConfiguredError(
                "enable the channel before reconnecting"
            )
        channel.status = "reconnecting"
        channel.config_generation += 1
        channel.last_error_code = None
        channel.last_error_message = None
        await self.db.commit()
        await self.db.refresh(channel)
        return self._response(
            channel,
            activation_allowed=str(agent.status) == "published",
        )

    async def rotate(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ApplicationWeComChannelResponse:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
        )
        channel = await self._required_channel(
            agent_id=agent_id,
            for_update=True,
        )
        channel.bot_id = None
        channel.secret_encrypted = None
        channel.enabled = False
        channel.status = "revoked"
        channel.config_generation += 1
        channel.last_error_code = None
        channel.last_error_message = None
        await self.db.commit()
        await self.db.refresh(channel)
        return self._response(
            channel,
            activation_allowed=str(agent.status) == "published",
        )

    async def update_settings(
        self,
        *,
        principal: Principal,
        agent_id: int,
        direct_enabled: bool,
        group_enabled: bool,
    ) -> ApplicationWeComChannelResponse:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
        )
        channel = await self._required_channel(
            agent_id=agent_id,
            for_update=True,
        )
        channel.direct_enabled = direct_enabled
        channel.group_enabled = group_enabled
        channel.config_generation += 1
        await self.db.commit()
        await self.db.refresh(channel)
        return self._response(
            channel,
            activation_allowed=str(agent.status) == "published",
        )

    async def users(
        self,
        *,
        principal: Principal,
        agent_id: int,
        query: str | None = None,
        limit: int = 100,
    ) -> ApplicationWeComUserListResponse:
        await self._agent(principal=principal, agent_id=agent_id)
        from .application_wecom_users import list_application_wecom_users

        return await list_application_wecom_users(
            self.db,
            agent_id=agent_id,
            org_id=principal.org_id,
            query=query,
            limit=limit,
            secret_codec=self.secret_codec,
        )

    async def messages(
        self,
        *,
        principal: Principal,
        agent_id: int,
        limit: int = 100,
    ) -> ApplicationWeComMessageListResponse:
        await self._agent(principal=principal, agent_id=agent_id)
        rows = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.target_type
                    == "application_agent",
                    ImInboundMessage.application_agent_id == agent_id,
                )
                .order_by(
                    ImInboundMessage.received_at.desc(),
                    ImInboundMessage.id.desc(),
                )
                .limit(max(1, min(limit, 200)))
            )
        ).scalars()
        return ApplicationWeComMessageListResponse(
            messages=[
                ApplicationWeComMessageSummary(
                    inbound_message_id=int(row.id),
                    chat_type=str(row.chat_type),
                    status=str(row.status),
                    identity_status=str(row.identity_status),
                    sender_platform_user_id=(
                        int(row.sender_platform_user_id)
                        if row.sender_platform_user_id is not None
                        else None
                    ),
                    received_at=row.received_at,
                    first_provider_dispatch_at=(
                        row.first_provider_dispatch_at
                    ),
                    completed_at=row.finished_at,
                )
                for row in rows
            ]
        )

    async def _agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
        for_update: bool = False,
    ) -> ApplicationAgent:
        statement = select(ApplicationAgent).where(
            ApplicationAgent.id == agent_id,
            ApplicationAgent.org_id == principal.org_id,
            ApplicationAgent.created_by_user_id == principal.user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).scalar_one_or_none()
        if row is None:
            raise ApplicationAgentUnavailableError(
                "application Agent not found"
            )
        return row

    async def _channel(
        self,
        *,
        agent_id: int,
        for_update: bool = False,
    ) -> ImChannel | None:
        statement = select(ImChannel).where(
            ImChannel.target_type == "application_agent",
            ImChannel.application_agent_id == agent_id,
            ImChannel.provider == "wecom",
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.db.execute(statement)).scalar_one_or_none()

    async def _required_channel(
        self,
        *,
        agent_id: int,
        for_update: bool,
    ) -> ImChannel:
        channel = await self._channel(
            agent_id=agent_id,
            for_update=for_update,
        )
        if channel is None:
            raise ImChannelNotConfiguredError(
                "wecom channel is not configured"
            )
        return channel

    async def _channel_client(
        self,
        *,
        principal: Principal,
        agent: ApplicationAgent,
    ) -> ApplicationClient:
        legacy = (
            await self.db.execute(
                select(
                    ApplicationAgentWeComChannel,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentWeComChannel.application_client_id,
                )
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_client_id
                    == ApplicationClient.id,
                )
                .where(
                    ApplicationAgentWeComChannel.application_agent_id
                    == agent.id,
                    ApplicationAgentClientGrant.application_agent_id
                    == agent.id,
                )
            )
        ).first()
        if legacy is not None:
            _legacy_channel, client, grant = legacy
            client.status = "active"
            grant.status = "active"
            grant.scopes = ["responses:create"]
            grant.allow_user_delegation = True
            grant.allow_service_invocation = False
            return client
        nonce = secrets.token_urlsafe(32)
        client = ApplicationClient(
            org_id=principal.org_id,
            client_id=f"wecom_ws_{agent.id}_{secrets.token_hex(8)}",
            name="企业微信 WebSocket 渠道",
            auth_method="internal_channel",
            credential_digest_or_reference=opaque_digest(nonce),
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
                application_agent_id=int(agent.id),
                application_client_id=int(client.id),
                scopes=["responses:create"],
                allow_user_delegation=True,
                allow_service_invocation=False,
                status="active",
            )
        )
        return client

    async def _onboarding(
        self,
        *,
        principal: Principal,
        agent_id: int,
        onboarding_id: str,
        for_update: bool,
    ) -> ImOnboardingSession:
        statement = select(ImOnboardingSession).where(
            ImOnboardingSession.public_id == onboarding_id,
            ImOnboardingSession.org_id == principal.org_id,
            ImOnboardingSession.user_id == principal.user_id,
            ImOnboardingSession.target_type == "application_agent",
            ImOnboardingSession.application_agent_id == agent_id,
            ImOnboardingSession.provider == "wecom",
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).scalar_one_or_none()
        if row is None:
            raise ImOnboardingNotFoundError(
                "wecom onboarding session not found"
            )
        return row

    @staticmethod
    def _expire_onboarding(row: ImOnboardingSession) -> None:
        row.status = "expired"
        row.qr_scode_encrypted = None
        row.qr_url = None
        row.credentials_encrypted = None
        row.bot_id = None
        row.poll_owner = None
        row.poll_lease_expires_at = None

    @staticmethod
    def _onboarding_status(
        row: ImOnboardingSession,
    ) -> ImQrOnboardingStatus:
        return ImQrOnboardingStatus(
            onboarding_id=str(row.public_id),
            status=str(row.status),
            expires_at=_aware(row.expires_at),
            bot_id=(
                str(row.bot_id)
                if row.status in {"credentials_ready", "consumed"}
                and row.bot_id
                else None
            ),
            secret_ready=bool(
                row.status == "credentials_ready"
                and row.credentials_encrypted
            ),
            error_code=(
                str(row.last_error_code)
                if row.last_error_code
                else None
            ),
            error_message=(
                str(row.last_error_message)
                if row.last_error_message
                else None
            ),
        )

    @staticmethod
    def _response(
        channel: ImChannel,
        *,
        activation_allowed: bool,
        legacy_migration_required: bool = False,
    ) -> ApplicationWeComChannelResponse:
        return ApplicationWeComChannelResponse(
            channel_id=int(channel.id),
            application_agent_id=int(channel.application_agent_id),
            activation_allowed=activation_allowed,
            enabled=bool(channel.enabled),
            status=str(channel.status),
            bot_id=str(channel.bot_id) if channel.bot_id else None,
            secret_configured=bool(channel.secret_encrypted),
            direct_enabled=bool(channel.direct_enabled),
            group_enabled=bool(channel.group_enabled),
            config_generation=int(channel.config_generation),
            last_connected_at=channel.last_connected_at,
            last_message_at=channel.last_message_at,
            last_error_code=(
                str(channel.last_error_code)
                if channel.last_error_code
                else None
            ),
            last_error_message=(
                str(channel.last_error_message)
                if channel.last_error_message
                else None
            ),
            legacy_migration_required=legacy_migration_required,
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mask_member_id(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]}"
