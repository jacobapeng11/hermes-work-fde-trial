from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgentWeComUserAccess,
    ImChannel,
    OrganizationWeComIdentity,
    User,
)
from app.services.runtime_workspace import Principal


class WeComIdentityError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ResolvedWeComIdentity:
    identity_id: int | None
    principal: Principal
    user_name: str
    user_code: str | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def wecom_member_digest(member_id: str) -> str:
    normalized = _member_id(member_id)
    return hmac.new(
        settings.AES_ENCRYPTION_KEY.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def opaque_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OrganizationWeComIdentityService:
    """Resolve and bind WeCom members without exposing their provider ID."""

    def __init__(
        self,
        db: AsyncSession,
    ) -> None:
        self.db = db

    async def resolve(
        self,
        *,
        org_id: int,
        member_id: str,
    ) -> ResolvedWeComIdentity | None:
        normalized_member_id = _member_id(member_id)
        direct_users = list(
            (
                await self.db.execute(
                    select(User)
                    .where(
                        User.org_id == org_id,
                        User.qw_token == normalized_member_id,
                        User.status == "active",
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(direct_users) == 1:
            user = direct_users[0]
            return ResolvedWeComIdentity(
                identity_id=None,
                principal=Principal(
                    org_id=int(user.org_id),
                    user_id=int(user.id),
                ),
                user_name=str(user.name),
                user_code=(
                    str(user.user_code)
                    if user.user_code is not None
                    else None
                ),
            )
        if len(direct_users) > 1:
            return None

        # Fall back to historical OrganizationWeComIdentity mappings for members
        # that were bound before the qw_token field existed on User.
        digest = wecom_member_digest(normalized_member_id)
        row = (
            await self.db.execute(
                select(OrganizationWeComIdentity, User)
                .join(
                    User,
                    User.id
                    == OrganizationWeComIdentity.platform_user_id,
                )
                .where(
                    OrganizationWeComIdentity.org_id == org_id,
                    OrganizationWeComIdentity.member_id_digest == digest,
                    OrganizationWeComIdentity.status == "active",
                    User.org_id == org_id,
                    User.status == "active",
                )
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        identity, user = row
        return ResolvedWeComIdentity(
            identity_id=int(identity.id),
            principal=Principal(
                org_id=int(user.org_id),
                user_id=int(user.id),
            ),
            user_name=str(user.name),
            user_code=(
                str(user.user_code) if user.user_code is not None else None
            ),
        )

    @staticmethod
    def shared_binding_url(*, member_id: str) -> str:
        query = urlencode({"qw_token": _member_id(member_id)})
        return f"{settings.PUBLIC_WEB_BASE.rstrip('/')}/user/wecom-bind?{query}"

    async def bind_qw_token(
        self,
        *,
        principal: Principal,
        qw_token: str,
    ) -> None:
        """Bind a WeCom qw_token to the authenticated platform user.

        Raises WeComIdentityError with codes:
          - wecom_identity_qw_token_empty: qw_token is blank
          - wecom_identity_qw_token_invalid: qw_token exceeds the database contract
          - wecom_identity_qw_token_conflict: another user in the same org already owns this qw_token
        """
        normalized = qw_token.strip()
        if not normalized:
            raise WeComIdentityError("wecom_identity_qw_token_empty", status_code=422)
        if len(normalized) > 100:
            raise WeComIdentityError(
                "wecom_identity_qw_token_invalid",
                status_code=422,
            )

        user = (
            await self.db.execute(
                select(User)
                .where(
                    User.id == principal.user_id,
                    User.org_id == principal.org_id,
                    User.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if user is None:
            raise WeComIdentityError("wecom_identity_user_not_found", status_code=404)

        conflict = (
            await self.db.execute(
                select(User)
                .where(
                    User.org_id == principal.org_id,
                    User.qw_token == normalized,
                    User.id != principal.user_id,
                    User.status == "active",
                )
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise WeComIdentityError(
                "wecom_identity_qw_token_conflict",
                status_code=409,
            )

        user.qw_token = normalized
        await self.db.commit()

    async def record_access(
        self,
        *,
        channel: ImChannel,
        identity_id: int,
        chat_type: str,
        external_conversation_id: str,
    ) -> None:
        if channel.application_agent_id is None:
            return
        now = utc_now()
        identity = await self.db.get(
            OrganizationWeComIdentity, identity_id
        )
        if identity is None:
            return
        identity.last_seen_at = now
        access = (
            await self.db.execute(
                select(ApplicationAgentWeComUserAccess)
                .where(
                    ApplicationAgentWeComUserAccess.application_agent_id
                    == channel.application_agent_id,
                    ApplicationAgentWeComUserAccess.organization_wecom_identity_id
                    == identity_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        conversation_digest = opaque_digest(external_conversation_id)
        if access is None:
            access = ApplicationAgentWeComUserAccess(
                application_agent_id=int(channel.application_agent_id),
                channel_id=int(channel.id),
                organization_wecom_identity_id=identity_id,
                first_seen_at=now,
                last_seen_at=now,
                last_chat_type=chat_type,
                last_external_conversation_digest=conversation_digest,
            )
            self.db.add(access)
        else:
            access.channel_id = int(channel.id)
            access.last_seen_at = now
            access.last_chat_type = chat_type
            access.last_external_conversation_digest = conversation_digest
        await self.db.commit()

    async def revoke(
        self,
        *,
        principal: Principal,
        identity_id: int,
    ) -> None:
        identity = (
            await self.db.execute(
                select(OrganizationWeComIdentity)
                .where(
                    OrganizationWeComIdentity.id == identity_id,
                    OrganizationWeComIdentity.org_id == principal.org_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if identity is None:
            raise WeComIdentityError(
                "wecom_identity_not_found",
                status_code=404,
            )
        identity.status = "revoked"
        identity.revoked_at = utc_now()
        await self.db.commit()


def _member_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 255:
        raise WeComIdentityError(
            "wecom_member_id_invalid",
            status_code=400,
        )
    return normalized
