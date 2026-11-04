from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    CloudHermesProfile,
    ImChannel,
    ImOnboardingSession,
    RuntimeAgentProfile,
    RuntimeAuditLog,
    RuntimeSession,
)
from app.schemas.im_gateway import (
    ImChannelCard,
    ImChannelListResponse,
    ImQrOnboardingCreated,
    ImQrOnboardingStatus,
    RegisterWeComChannelRequest,
    WeComChannelResponse,
)
from app.services.managed_secret import ManagedSecretCodec
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_sessions import generated_runtime_session_id
from app.services.runtime_workspace import Principal

from .onboarding import WeComQrClient, WeComQrProtocolError


_CARD_DEFINITIONS = (
    ("feishu", "飞书", False),
    ("wecom", "企微", True),
    ("wechat", "微信", False),
    ("dingtalk", "钉钉", False),
)


class ImGatewayServiceError(RuntimeError):
    code = "im_gateway_error"


class ImChannelNotConfiguredError(ImGatewayServiceError):
    code = "im_channel_not_configured"


class ImChannelConflictError(ImGatewayServiceError):
    code = "im_channel_conflict"


class ImUnsupportedConnectionModeError(ImGatewayServiceError):
    code = "im_url_callback_not_supported"


class ImOnboardingNotFoundError(ImGatewayServiceError):
    code = "im_onboarding_not_found"


class ImOnboardingNotReadyError(ImGatewayServiceError):
    code = "im_onboarding_not_ready"


class ImDefaultProfileNotReadyError(ImGatewayServiceError):
    code = "im_default_profile_not_ready"


@dataclass(frozen=True)
class RegisteredChannel:
    channel_id: int
    config_generation: int
    response: WeComChannelResponse


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class ImChannelService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        secret_codec: ManagedSecretCodec | None = None,
        qr_client: WeComQrClient | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.db = db
        self.secret_codec = secret_codec or ManagedSecretCodec()
        self.qr_client = qr_client or WeComQrClient()
        self.instance_id = instance_id or _instance_id()

    async def list_cards(
        self, *, principal: Principal, runtime_workspace_id: int
    ) -> ImChannelListResponse:
        await self._require_workspace(principal, runtime_workspace_id)
        rows = (
            await self.db.execute(
                select(ImChannel, RuntimeSession)
                .join(
                    RuntimeSession,
                    RuntimeSession.id == ImChannel.runtime_session_row_id,
                )
                .where(
                    ImChannel.org_id == principal.org_id,
                    ImChannel.user_id == principal.user_id,
                    ImChannel.runtime_workspace_id == runtime_workspace_id,
                )
            )
        ).all()
        by_provider = {
            str(channel.provider): (channel, session)
            for channel, session in rows
        }
        cards: list[ImChannelCard] = []
        for provider, display_name, available in _CARD_DEFINITIONS:
            current = by_provider.get(provider)
            if current is None:
                cards.append(
                    ImChannelCard(
                        provider=provider,
                        display_name=display_name,
                        available=available,
                    )
                )
                continue
            channel, session = current
            cards.append(
                self._card(
                    channel,
                    session,
                    display_name=display_name,
                    available=available,
                )
            )
        return ImChannelListResponse(channels=cards)

    async def create_qr_onboarding(
        self, *, principal: Principal, runtime_workspace_id: int
    ) -> ImQrOnboardingCreated:
        await self._require_workspace(principal, runtime_workspace_id)
        try:
            challenge = await self.qr_client.generate()
        except WeComQrProtocolError as exc:
            raise ImGatewayServiceError(str(exc)) from exc
        now = _now()
        expires_at = now + timedelta(
            seconds=settings.IM_GATEWAY_ONBOARDING_TTL_SECONDS
        )
        public_id = f"imo_{uuid4().hex}"
        row = ImOnboardingSession(
            public_id=public_id,
            org_id=principal.org_id,
            user_id=principal.user_id,
            target_type="personal_agent",
            runtime_workspace_id=runtime_workspace_id,
            provider="wecom",
            method="qrcode",
            status="pending",
            qr_scode_encrypted=self.secret_codec.encrypt(challenge.scode),
            qr_scode_digest=hashlib.sha256(
                challenge.scode.encode("utf-8")
            ).hexdigest(),
            qr_url=challenge.auth_url,
            expires_at=expires_at,
        )
        self.db.add(row)
        self.db.add(
            RuntimeAuditLog(
                org_id=principal.org_id,
                actor_user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                action="im.wecom.onboarding.created",
                target_type="im_onboarding",
                target_id=public_id,
                detail={"method": "qrcode"},
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
        runtime_workspace_id: int,
        onboarding_id: str,
    ) -> ImQrOnboardingStatus:
        row = await self._owned_onboarding(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            onboarding_id=onboarding_id,
            for_update=True,
        )
        now = _now()
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
                5.0, settings.IM_GATEWAY_ONBOARDING_POLL_SECONDS * 2
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
            # A transient upstream failure does not destroy a still-valid QR.
            row = await self._owned_onboarding(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                onboarding_id=onboarding_id,
                for_update=True,
            )
            row.poll_owner = None
            row.poll_lease_expires_at = None
            row.last_error_code = str(exc)
            row.last_error_message = "企微二维码状态暂时不可用，请稍后重试"
            await self.db.commit()
            return self._onboarding_status(row)
        row = await self._owned_onboarding(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
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
            row.status = "failed"
            row.qr_scode_encrypted = None
            row.qr_url = None
            row.credentials_encrypted = None
            row.bot_id = None
            row.poll_owner = None
            row.poll_lease_expires_at = None
        else:
            row.status = polled.status
        await self.db.commit()
        return self._onboarding_status(row)

    async def register_wecom(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        request: RegisterWeComChannelRequest,
    ) -> RegisteredChannel:
        if request.connection_mode != "websocket":
            raise ImUnsupportedConnectionModeError(
                "URL callback is not available"
            )
        workspace = await self._require_workspace(
            principal, runtime_workspace_id, for_update=True
        )
        onboarding: ImOnboardingSession | None = None
        if request.onboarding_id:
            onboarding = await self._owned_onboarding(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
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
                    ImChannel.org_id != principal.org_id,
                    ImChannel.user_id != principal.user_id,
                    ImChannel.runtime_workspace_id
                    != runtime_workspace_id,
                ),
            )
            .limit(1)
        )
        if collision is not None:
            raise ImChannelConflictError(
                "this WeCom bot is already bound to another workspace"
            )
        channel = (
            await self.db.execute(
                select(ImChannel)
                .where(
                    ImChannel.org_id == principal.org_id,
                    ImChannel.user_id == principal.user_id,
                    ImChannel.runtime_workspace_id
                    == runtime_workspace_id,
                    ImChannel.provider == "wecom",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if channel is None:
            agent_profile = await self._default_agent_profile(
                principal, runtime_workspace_id
            )
            session = RuntimeSession(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(agent_profile.id),
                runtime_session_id=generated_runtime_session_id(
                    runtime_workspace_id=runtime_workspace_id
                ),
                source_type="wecom",
                title_cache="企微会话",
                message_count_cache=0,
                last_active_at=_now(),
                chat_log_backfilled_at=_now(),
                status="active",
                session_model_entry_id=workspace.default_model_id,
            )
            self.db.add(session)
            await self.db.flush()
            channel = ImChannel(
                org_id=principal.org_id,
                target_type="personal_agent",
                created_by_user_id=principal.user_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                provider="wecom",
                connection_mode="websocket",
                enabled=True,
                status="connecting",
                config_generation=1,
                runtime_session_row_id=int(session.id),
            )
            self.db.add(channel)
        else:
            session = await self.db.get(
                RuntimeSession, channel.runtime_session_row_id
            )
            if (
                session is None
                or session.source_type != "wecom"
                or session.runtime_workspace_id != runtime_workspace_id
            ):
                raise ImChannelConflictError(
                    "fixed WeCom session invariant is broken"
                )
            channel.config_generation = int(
                channel.config_generation
            ) + 1
            if channel.bot_id and channel.bot_id != bot_id:
                channel.proactive_target_encrypted = None
                channel.proactive_target_digest = None
                channel.proactive_target_confirmed_at = None
        channel.connection_mode = "websocket"
        channel.bot_id = bot_id
        channel.secret_encrypted = self.secret_codec.encrypt(secret)
        channel.enabled = True
        channel.status = "connecting"
        channel.last_error_code = None
        channel.last_error_message = None
        if onboarding is not None:
            onboarding.status = "consumed"
            onboarding.consumed_at = _now()
            onboarding.credentials_encrypted = None
            onboarding.qr_scode_encrypted = None
            onboarding.qr_url = None
            onboarding.poll_owner = None
            onboarding.poll_lease_expires_at = None
        try:
            await self.db.flush()
            self.db.add(
                RuntimeAuditLog(
                    org_id=principal.org_id,
                    actor_user_id=principal.user_id,
                    runtime_workspace_id=runtime_workspace_id,
                    action="im.wecom.registered",
                    target_type="im_channel",
                    target_id=str(channel.id),
                    detail={
                        "provider": "wecom",
                        "connection_mode": "websocket",
                        "credential_source": (
                            "qrcode"
                            if onboarding is not None
                            else "manual"
                        ),
                    },
                )
            )
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ImChannelConflictError(
                "this WeCom bot or workspace is already bound"
            ) from exc
        await self.db.refresh(channel)
        await self.db.refresh(session)
        response = self._wecom_response(channel, session)
        return RegisteredChannel(
            channel_id=int(channel.id),
            config_generation=int(channel.config_generation),
            response=response,
        )

    async def set_wecom_enabled(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        enabled: bool,
    ) -> RegisteredChannel:
        channel, session = await self._owned_wecom(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            for_update=True,
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
        self.db.add(
            RuntimeAuditLog(
                org_id=principal.org_id,
                actor_user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                action=(
                    "im.wecom.enabled"
                    if enabled
                    else "im.wecom.disabled"
                ),
                target_type="im_channel",
                target_id=str(channel.id),
                detail={"provider": "wecom", "enabled": enabled},
            )
        )
        await self.db.commit()
        await self.db.refresh(channel)
        return RegisteredChannel(
            channel_id=int(channel.id),
            config_generation=int(channel.config_generation),
            response=self._wecom_response(channel, session),
        )

    async def revoke_wecom(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> RegisteredChannel:
        channel, session = await self._owned_wecom(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            for_update=True,
        )
        channel.bot_id = None
        channel.secret_encrypted = None
        channel.proactive_target_encrypted = None
        channel.proactive_target_digest = None
        channel.proactive_target_confirmed_at = None
        channel.enabled = False
        channel.status = "revoked"
        channel.config_generation += 1
        channel.last_error_code = None
        channel.last_error_message = None
        self.db.add(
            RuntimeAuditLog(
                org_id=principal.org_id,
                actor_user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                action="im.wecom.revoked",
                target_type="im_channel",
                target_id=str(channel.id),
                detail={
                    "provider": "wecom",
                    "session_retained": True,
                },
            )
        )
        await self.db.commit()
        await self.db.refresh(channel)
        return RegisteredChannel(
            channel_id=int(channel.id),
            config_generation=int(channel.config_generation),
            response=self._wecom_response(channel, session),
        )

    async def _owned_wecom(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        for_update: bool,
    ) -> tuple[ImChannel, RuntimeSession]:
        statement = (
            select(ImChannel, RuntimeSession)
            .join(
                RuntimeSession,
                RuntimeSession.id == ImChannel.runtime_session_row_id,
            )
            .where(
                ImChannel.org_id == principal.org_id,
                ImChannel.user_id == principal.user_id,
                ImChannel.runtime_workspace_id == runtime_workspace_id,
                ImChannel.provider == "wecom",
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).first()
        if row is None:
            raise ImChannelNotConfiguredError(
                "wecom channel is not configured"
            )
        return row[0], row[1]

    async def _owned_onboarding(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        onboarding_id: str,
        for_update: bool,
    ) -> ImOnboardingSession:
        statement = select(ImOnboardingSession).where(
            ImOnboardingSession.public_id == onboarding_id,
            ImOnboardingSession.org_id == principal.org_id,
            ImOnboardingSession.user_id == principal.user_id,
            ImOnboardingSession.runtime_workspace_id
            == runtime_workspace_id,
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

    async def _require_workspace(
        self,
        principal: Principal,
        runtime_workspace_id: int,
        *,
        for_update: bool = False,
    ) -> CloudHermesProfile:
        statement = select(CloudHermesProfile).where(
            CloudHermesProfile.id == runtime_workspace_id,
            CloudHermesProfile.org_id == principal.org_id,
            CloudHermesProfile.user_id == principal.user_id,
            CloudHermesProfile.status == "active",
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).scalar_one_or_none()
        if row is None:
            raise ImChannelNotConfiguredError(
                "runtime workspace not found"
            )
        return row

    async def _default_agent_profile(
        self, principal: Principal, runtime_workspace_id: int
    ) -> RuntimeAgentProfile:
        row = (
            await self.db.execute(
                select(RuntimeAgentProfile).where(
                    RuntimeAgentProfile.org_id == principal.org_id,
                    RuntimeAgentProfile.user_id == principal.user_id,
                    RuntimeAgentProfile.runtime_workspace_id
                    == runtime_workspace_id,
                    RuntimeAgentProfile.profile_kind == "default",
                    RuntimeAgentProfile.hermes_profile_name == "default",
                    *executable_runtime_agent_profile_clauses(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ImDefaultProfileNotReadyError(
                "default personal Agent profile is not ready"
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
            onboarding_id=row.public_id,
            status=row.status,
            expires_at=_aware(row.expires_at),
            bot_id=(
                row.bot_id
                if row.status in {"credentials_ready", "consumed"}
                else None
            ),
            secret_ready=bool(
                row.status == "credentials_ready"
                and row.credentials_encrypted
            ),
            error_code=row.last_error_code,
            error_message=row.last_error_message,
        )

    @staticmethod
    def _card(
        channel: ImChannel,
        session: RuntimeSession,
        *,
        display_name: str,
        available: bool,
    ) -> ImChannelCard:
        return ImChannelCard(
            provider=channel.provider,
            display_name=display_name,
            available=available,
            enabled=bool(channel.enabled),
            status=channel.status,
            connection_mode=channel.connection_mode,
            bot_id=channel.bot_id,
            secret_configured=bool(channel.secret_encrypted),
            runtime_session_id=session.runtime_session_id,
            config_generation=int(channel.config_generation),
            last_connected_at=channel.last_connected_at,
            last_message_at=channel.last_message_at,
            last_error_code=channel.last_error_code,
            last_error_message=channel.last_error_message,
        )

    @classmethod
    def _wecom_response(
        cls, channel: ImChannel, session: RuntimeSession
    ) -> WeComChannelResponse:
        return WeComChannelResponse(
            **cls._card(
                channel,
                session,
                display_name="企微",
                available=True,
            ).model_dump()
        )
