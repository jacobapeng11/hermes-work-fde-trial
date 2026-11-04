from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgentWeComCallbackEvent,
    ApplicationAgentWeComChannel,
)
from app.schemas.application_agent_invocation import (
    ApplicationAgentResponseRequest,
)
from app.services.application_agent_invocation import (
    InMemoryApplicationAgentInvocationService,
    SqlAlchemyApplicationAgentInvocationService,
)
from app.services.application_agent_wecom import (
    ApplicationAgentWeComError,
    InMemoryApplicationAgentWeComService,
    SqlAlchemyApplicationAgentWeComService,
    WeComApiClient,
    WeComChannelRecord,
    binding_web_url,
)
from app.services.application_agent_wecom_crypto import (
    WeComCryptoError,
    decrypt_message,
    extract_encrypted_xml,
    parse_text_message,
    verify_message_signature,
)
from app.services.managed_secret import ManagedSecretCodec
from app.services.runtime_prepared_knowledge import (
    shared_runtime_prepared_knowledge_service,
)


@dataclass(frozen=True)
class WeComAcceptedCallback:
    channel: WeComChannelRecord
    member_id: str | None = None
    passive_text: str | None = None
    event_id: int | None = None


class InMemoryApplicationAgentWeComCallbackService:
    def __init__(
        self,
        *,
        channels: InMemoryApplicationAgentWeComService,
        invocation: InMemoryApplicationAgentInvocationService,
    ) -> None:
        self.channels = channels
        self.invocation = invocation
        self.seen_messages: set[tuple[int, str]] = set()

    def reset(self) -> None:
        self.seen_messages.clear()

    async def verify_url(
        self,
        *,
        callback_key: str,
        signature: str,
        timestamp: str,
        nonce: str,
        echo: str,
    ) -> str:
        channel = await self.channels.find_channel_by_callback_key(callback_key)
        _validate_timestamp(timestamp)
        verify_message_signature(
            token=channel.callback_token,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=echo,
            signature=signature,
        )
        return decrypt_message(
            encoding_aes_key=channel.encoding_aes_key,
            encrypted=echo,
            receive_id=channel.corp_id,
        )

    async def accept(
        self,
        *,
        callback_key: str,
        signature: str,
        timestamp: str,
        nonce: str,
        raw_xml: str,
    ) -> WeComAcceptedCallback:
        channel, message = await _validated_message(
            channels=self.channels,
            callback_key=callback_key,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
            raw_xml=raw_xml,
        )
        message_key = (channel.channel_id, message.message_id)
        if message_key in self.seen_messages:
            return WeComAcceptedCallback(
                channel=channel,
                member_id=message.member_id,
            )
        self.seen_messages.add(message_key)
        principal = await self.channels.bound_principal(
            channel=channel,
            member_id=message.member_id,
        )
        if principal is None:
            ticket = await self.channels.create_binding_ticket(
                channel=channel,
                member_id=message.member_id,
            )
            return WeComAcceptedCallback(
                channel=channel,
                member_id=message.member_id,
                passive_text=(
                    "首次使用需要完成企业身份绑定，请打开："
                    f"{binding_web_url(ticket)}"
                ),
            )
        result = await self.invocation.invoke(
            slug=channel.agent_slug,
            client_id=channel.internal_client_id,
            client_secret=channel.internal_client_secret,
            delegated_principal=principal,
            payload=ApplicationAgentResponseRequest(
                input=message.content,
                conversation_key=(
                    f"wecom:direct:{channel.channel_id}:{message.member_id}"
                ),
                stream=False,
                request_id=f"wecom_{message.message_id}",
            ),
            channel="wecom",
        )
        return WeComAcceptedCallback(
            channel=channel,
            member_id=message.member_id,
            passive_text=result.output_text,
        )


class SqlAlchemyApplicationAgentWeComCallbackService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        channels: SqlAlchemyApplicationAgentWeComService | None = None,
        codec: ManagedSecretCodec | None = None,
    ) -> None:
        self.db = db
        self.codec = codec or ManagedSecretCodec()
        self.channels = channels or SqlAlchemyApplicationAgentWeComService(
            db,
            codec=self.codec,
        )

    async def verify_url(
        self,
        *,
        callback_key: str,
        signature: str,
        timestamp: str,
        nonce: str,
        echo: str,
    ) -> str:
        channel = await self.channels.find_channel_by_callback_key(callback_key)
        _validate_timestamp(timestamp)
        try:
            verify_message_signature(
                token=channel.callback_token,
                timestamp=timestamp,
                nonce=nonce,
                encrypted=echo,
                signature=signature,
            )
            return decrypt_message(
                encoding_aes_key=channel.encoding_aes_key,
                encrypted=echo,
                receive_id=channel.corp_id,
            )
        except WeComCryptoError as exc:
            raise ApplicationAgentWeComError(exc.code, status_code=401) from exc

    async def accept(
        self,
        *,
        callback_key: str,
        signature: str,
        timestamp: str,
        nonce: str,
        raw_xml: str,
    ) -> WeComAcceptedCallback:
        channel, message = await _validated_message(
            channels=self.channels,
            callback_key=callback_key,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
            raw_xml=raw_xml,
        )
        principal = await self.channels.bound_principal(
            channel=channel,
            member_id=message.member_id,
        )
        if principal is None:
            ticket = await self.channels.create_binding_ticket(
                channel=channel,
                member_id=message.member_id,
            )
            return WeComAcceptedCallback(
                channel=channel,
                member_id=message.member_id,
                passive_text=(
                    "首次使用需要完成企业身份绑定，请打开："
                    f"{binding_web_url(ticket)}"
                ),
            )
        event = ApplicationAgentWeComCallbackEvent(
            org_id=channel.org_id,
            channel_id=channel.channel_id,
            message_id=message.message_id,
            wecom_member_id=message.member_id,
            message_ciphertext=self.codec.encrypt(message.content),
            status="queued",
        )
        self.db.add(event)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            return WeComAcceptedCallback(
                channel=channel,
                member_id=message.member_id,
            )
        await self.db.refresh(event)
        return WeComAcceptedCallback(
            channel=channel,
            member_id=message.member_id,
            event_id=int(event.id),
        )


async def process_wecom_callback_event(
    *,
    db: AsyncSession,
    event_id: int,
    api_client: WeComApiClient | None = None,
    codec: ManagedSecretCodec | None = None,
) -> None:
    secret_codec = codec or ManagedSecretCodec()
    channels = SqlAlchemyApplicationAgentWeComService(db, codec=secret_codec)
    event = (
        await db.execute(
            select(ApplicationAgentWeComCallbackEvent)
            .where(
                ApplicationAgentWeComCallbackEvent.id == event_id,
                ApplicationAgentWeComCallbackEvent.status == "queued",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if event is None:
        await db.rollback()
        return
    event.status = "running"
    event.started_at = _now()
    await db.commit()
    try:
        channel = await channels.find_channel_by_callback_key(
            str(
                (
                    await db.execute(
                        select(ApplicationAgentWeComChannel.callback_key).where(
                            ApplicationAgentWeComChannel.id == event.channel_id
                        )
                    )
                ).scalar_one()
            )
        )
        principal = await channels.bound_principal(
            channel=channel,
            member_id=str(event.wecom_member_id),
        )
        if principal is None:
            raise ApplicationAgentWeComError(
                "wecom_binding_required",
                status_code=403,
            )
        message = secret_codec.decrypt(str(event.message_ciphertext))
        result = await SqlAlchemyApplicationAgentInvocationService(
            db,
            prepared_knowledge_service=(
                shared_runtime_prepared_knowledge_service()
            ),
        ).invoke(
            slug=channel.agent_slug,
            client_id=channel.internal_client_id,
            client_secret=channel.internal_client_secret,
            delegated_principal=principal,
            payload=ApplicationAgentResponseRequest(
                input=message,
                conversation_key=(
                    f"wecom:direct:{channel.channel_id}:{event.wecom_member_id}"
                ),
                stream=False,
                request_id=f"wecom_{event.message_id}",
            ),
            channel="wecom",
        )
        await (api_client or WeComApiClient()).send_text(
            channel=channel,
            member_id=str(event.wecom_member_id),
            content=result.output_text or "处理完成。",
        )
        event.status = "completed"
        event.error_code = None
    except Exception as exc:
        event.status = "failed"
        event.error_code = (
            exc.code
            if isinstance(exc, ApplicationAgentWeComError)
            else "wecom_callback_processing_failed"
        )
    finally:
        event.message_ciphertext = None
        event.finished_at = _now()
        await db.commit()


async def queued_wecom_callback_event_ids(
    db: AsyncSession,
    *,
    limit: int = 20,
) -> list[int]:
    rows = (
        await db.execute(
            select(ApplicationAgentWeComCallbackEvent.id)
            .where(ApplicationAgentWeComCallbackEvent.status == "queued")
            .order_by(ApplicationAgentWeComCallbackEvent.created_at.asc())
            .limit(limit)
        )
    ).scalars()
    return [int(value) for value in rows]


async def _validated_message(
    *,
    channels,
    callback_key: str,
    signature: str,
    timestamp: str,
    nonce: str,
    raw_xml: str,
):
    channel = await channels.find_channel_by_callback_key(callback_key)
    _validate_timestamp(timestamp)
    try:
        encrypted = extract_encrypted_xml(raw_xml)
        verify_message_signature(
            token=channel.callback_token,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=encrypted,
            signature=signature,
        )
        plaintext = decrypt_message(
            encoding_aes_key=channel.encoding_aes_key,
            encrypted=encrypted,
            receive_id=channel.corp_id,
        )
        message = parse_text_message(plaintext)
    except WeComCryptoError as exc:
        status_code = (
            422
            if exc.code
            in {
                "wecom_message_type_unsupported",
                "wecom_message_content_invalid",
            }
            else 401
        )
        raise ApplicationAgentWeComError(
            exc.code,
            status_code=status_code,
        ) from exc
    if (
        message.corp_id != channel.corp_id
        or message.agent_id != channel.wecom_agent_id
    ):
        raise ApplicationAgentWeComError(
            "wecom_channel_identity_mismatch",
            status_code=401,
        )
    return channel, message


def _validate_timestamp(timestamp: str) -> None:
    try:
        value = int(timestamp)
    except ValueError as exc:
        raise ApplicationAgentWeComError(
            "wecom_timestamp_invalid",
            status_code=401,
        ) from exc
    if abs(int(time.time()) - value) > (
        settings.APPLICATION_AGENT_WECOM_CALLBACK_MAX_SKEW_SECONDS
    ):
        raise ApplicationAgentWeComError(
            "wecom_timestamp_expired",
            status_code=401,
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)
