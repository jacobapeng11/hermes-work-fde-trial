from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentAuthorizationCode,
    ApplicationAgentClientGrant,
    ApplicationClient,
    User,
)
from app.services.application_agents import _secret_digest
from app.services.runtime_workspace import Principal


class ApplicationAgentAuthorizationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int,
        redirect_uri: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.redirect_uri = redirect_uri


@dataclass(frozen=True)
class AuthorizationCodeRecord:
    code_digest: str
    org_id: int
    agent_id: int
    agent_slug: str
    client_pk: int
    client_id: str
    user_id: int
    expires_at: datetime
    consumed: bool = False


@dataclass(frozen=True)
class ApplicationAgentTokenExchangeResult:
    access_token: str
    expires_in: int
    user_code: str


@dataclass(frozen=True)
class ApplicationAgentTokenSubject:
    org_id: int
    agent_id: int
    agent_slug: str
    client_id: str
    user_id: int


class InMemoryApplicationAgentAuthorizationService:
    def __init__(
        self,
        management,
        *,
        user_codes: dict[tuple[int, int], str | None] | None = None,
    ) -> None:
        self.management = management
        self.user_codes = user_codes
        self.codes: dict[str, AuthorizationCodeRecord] = {}

    def reset(self) -> None:
        self.codes.clear()

    async def authorize(
        self,
        *,
        principal: Principal,
        agent_slug: str,
        client_id: str,
        redirect_uri: str | None = None,
    ) -> tuple[str, int]:
        validated_redirect_uri: str | None = None
        if redirect_uri is not None:
            redirect_client = next(
                (
                    client
                    for candidate in self.management.agents.values()
                    if candidate.org_id == principal.org_id
                    for client in self.management.clients.get(
                        candidate.application_agent_id,
                        [],
                    )
                    if client.client_id == client_id
                    and client.status == "active"
                ),
                None,
            )
            if (
                redirect_client is None
                or redirect_uri not in redirect_client.allowed_redirect_uris
            ):
                raise ApplicationAgentAuthorizationError(
                    "application_redirect_uri_not_allowed",
                    status_code=403,
                )
            validated_redirect_uri = redirect_uri
        agent = next(
            (
                item
                for item in self.management.agents.values()
                if item.org_id == principal.org_id
                and item.slug == agent_slug
                and item.status == "published"
            ),
            None,
        )
        if agent is None:
            raise ApplicationAgentAuthorizationError(
                "application_agent_not_found",
                status_code=404,
                redirect_uri=validated_redirect_uri,
            )
        client = next(
            (
                item
                for item in self.management.clients.get(
                    agent.application_agent_id,
                    [],
                )
                if item.client_id == client_id
                and item.status == "active"
                and item.allow_user_delegation
                and "responses:create" in item.scopes
            ),
            None,
        )
        if client is None:
            raise ApplicationAgentAuthorizationError(
                "application_client_unauthorized",
                status_code=403,
                redirect_uri=validated_redirect_uri,
            )
        code = f"hca_code_{secrets.token_urlsafe(32)}"
        digest = _digest(code)
        self.codes[digest] = AuthorizationCodeRecord(
            code_digest=digest,
            org_id=principal.org_id,
            agent_id=agent.application_agent_id,
            agent_slug=agent.slug,
            client_pk=client.application_client_id,
            client_id=client.client_id,
            user_id=principal.user_id,
            expires_at=_now()
            + timedelta(
                seconds=settings.APPLICATION_AGENT_AUTHORIZATION_CODE_TTL_SECONDS
            ),
        )
        return code, settings.APPLICATION_AGENT_AUTHORIZATION_CODE_TTL_SECONDS

    async def exchange(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
    ) -> ApplicationAgentTokenExchangeResult:
        digest = _digest(code)
        record = self.codes.get(digest)
        if (
            record is None
            or record.consumed
            or record.expires_at <= _now()
            or record.client_id != client_id
        ):
            raise ApplicationAgentAuthorizationError(
                "authorization_code_invalid",
                status_code=401,
            )
        client = next(
            (
                item
                for item in self.management.clients.get(record.agent_id, [])
                if item.client_id == client_id
            ),
            None,
        )
        if (
            client is None
            or not client.credential_digest
            or not hmac.compare_digest(
                client.credential_digest,
                _secret_digest(client_secret),
            )
        ):
            raise ApplicationAgentAuthorizationError(
                "application_client_unauthorized",
                status_code=401,
            )
        configured_user_code = (
            str(record.user_id)
            if self.user_codes is None
            else self.user_codes.get((record.org_id, record.user_id))
        )
        user_code = str(configured_user_code or "").strip()
        if not user_code:
            raise ApplicationAgentAuthorizationError(
                "delegated_user_identity_unavailable",
                status_code=403,
            )
        self.codes[digest] = AuthorizationCodeRecord(
            **{**record.__dict__, "consumed": True}
        )
        subject = _token_subject_from_authorization_code(record)
        return ApplicationAgentTokenExchangeResult(
            access_token=_issue_token(
                subject,
                ttl_seconds=settings.APPLICATION_AGENT_TOKEN_TTL_SECONDS,
            ),
            expires_in=settings.APPLICATION_AGENT_TOKEN_TTL_SECONDS,
            user_code=user_code,
        )

    async def issue_trusted_user_token(
        self,
        *,
        agent_slug: str,
        user_code: str,
        client_id: str,
        client_secret: str,
    ) -> ApplicationAgentTokenExchangeResult:
        candidates = [
            (agent, client)
            for agent in self.management.agents.values()
            if agent.slug == agent_slug and agent.status == "published"
            for client in self.management.clients.get(
                agent.application_agent_id,
                [],
            )
            if client.client_id == client_id
            and client.status == "active"
            and client.allow_user_delegation
            and "responses:create" in client.scopes
        ]
        if not candidates:
            raise ApplicationAgentAuthorizationError(
                "application_agent_trusted_user_delegation_not_allowed",
                status_code=403,
            )
        credential_digest = _secret_digest(client_secret)
        authenticated = [
            (agent, client)
            for agent, client in candidates
            if client.credential_digest
            and hmac.compare_digest(
                client.credential_digest,
                credential_digest,
            )
        ]
        if len(authenticated) != 1:
            raise ApplicationAgentAuthorizationError(
                "application_client_unauthorized",
                status_code=401,
            )
        agent, client = authenticated[0]
        normalized_user_code = user_code.strip()
        if self.user_codes is None:
            try:
                user_id = int(normalized_user_code)
            except ValueError:
                user_id = 0
        else:
            matching_user_ids = [
                candidate_user_id
                for (org_id, candidate_user_id), candidate_user_code in (
                    self.user_codes.items()
                )
                if org_id == agent.org_id
                and str(candidate_user_code or "").strip()
                == normalized_user_code
            ]
            user_id = (
                matching_user_ids[0]
                if len(matching_user_ids) == 1
                else 0
            )
        if user_id <= 0:
            raise ApplicationAgentAuthorizationError(
                "delegated_user_identity_unavailable",
                status_code=403,
            )
        ttl_seconds = (
            settings.APPLICATION_AGENT_SQLBOT2_TRUSTED_TOKEN_TTL_SECONDS
        )
        subject = ApplicationAgentTokenSubject(
            org_id=agent.org_id,
            agent_id=agent.application_agent_id,
            agent_slug=agent.slug,
            client_id=client.client_id,
            user_id=user_id,
        )
        return ApplicationAgentTokenExchangeResult(
            access_token=_issue_token(subject, ttl_seconds=ttl_seconds),
            expires_in=ttl_seconds,
            user_code=normalized_user_code,
        )

    async def issue_sqlbot2_trusted_user_token(
        self,
        *,
        user_code: str,
        client_id: str,
        client_secret: str,
    ) -> ApplicationAgentTokenExchangeResult:
        """Compatibility wrapper for the historical SQLBot2-only route."""
        try:
            return await self.issue_trusted_user_token(
                agent_slug="sqlbot2",
                user_code=user_code,
                client_id=client_id,
                client_secret=client_secret,
            )
        except ApplicationAgentAuthorizationError as exc:
            if exc.code != "application_agent_trusted_user_delegation_not_allowed":
                raise
            raise ApplicationAgentAuthorizationError(
                "sqlbot2_trusted_user_delegation_not_allowed",
                status_code=exc.status_code,
            ) from exc


class SqlAlchemyApplicationAgentAuthorizationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def authorize(
        self,
        *,
        principal: Principal,
        agent_slug: str,
        client_id: str,
        redirect_uri: str | None = None,
    ) -> tuple[str, int]:
        validated_redirect_uri: str | None = None
        if redirect_uri is not None:
            redirect_client = (
                await self.db.execute(
                    select(ApplicationClient).where(
                        ApplicationClient.org_id == principal.org_id,
                        ApplicationClient.client_id == client_id,
                        ApplicationClient.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if (
                redirect_client is None
                or redirect_uri
                not in set(redirect_client.allowed_redirect_uris or [])
            ):
                raise ApplicationAgentAuthorizationError(
                    "application_redirect_uri_not_allowed",
                    status_code=403,
                )
            validated_redirect_uri = redirect_uri
        row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
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
                .where(
                    ApplicationAgent.org_id == principal.org_id,
                    ApplicationAgent.slug == agent_slug,
                    ApplicationAgent.status == "published",
                    ApplicationClient.client_id == client_id,
                    ApplicationClient.status == "active",
                    ApplicationAgentClientGrant.status == "active",
                    ApplicationAgentClientGrant.allow_user_delegation.is_(
                        True
                    ),
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentAuthorizationError(
                "application_authorization_not_allowed",
                status_code=403,
                redirect_uri=validated_redirect_uri,
            )
        agent, client, grant = row
        if "responses:create" not in set(grant.scopes or []):
            raise ApplicationAgentAuthorizationError(
                "application_authorization_not_allowed",
                status_code=403,
                redirect_uri=validated_redirect_uri,
            )
        code = f"hca_code_{secrets.token_urlsafe(32)}"
        self.db.add(
            ApplicationAgentAuthorizationCode(
                org_id=principal.org_id,
                application_agent_id=agent.id,
                application_client_id=client.id,
                user_id=principal.user_id,
                code_digest=_digest(code),
                expires_at=_now()
                + timedelta(
                    seconds=(
                        settings.APPLICATION_AGENT_AUTHORIZATION_CODE_TTL_SECONDS
                    )
                ),
            )
        )
        await self.db.commit()
        return code, settings.APPLICATION_AGENT_AUTHORIZATION_CODE_TTL_SECONDS

    async def exchange(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
    ) -> ApplicationAgentTokenExchangeResult:
        row = (
            await self.db.execute(
                select(
                    ApplicationAgentAuthorizationCode,
                    ApplicationAgent,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
                    User,
                )
                .join(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ApplicationAgentAuthorizationCode.application_agent_id,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentAuthorizationCode.application_client_id,
                )
                .join(
                    ApplicationAgentClientGrant,
                    (
                        ApplicationAgentClientGrant.application_agent_id
                        == ApplicationAgent.id
                    )
                    & (
                        ApplicationAgentClientGrant.application_client_id
                        == ApplicationClient.id
                    ),
                )
                .join(
                    User,
                    (User.id == ApplicationAgentAuthorizationCode.user_id)
                    & (
                        User.org_id
                        == ApplicationAgentAuthorizationCode.org_id
                    ),
                )
                .where(
                    ApplicationAgentAuthorizationCode.code_digest
                    == _digest(code),
                    ApplicationAgentAuthorizationCode.consumed_at.is_(None),
                    ApplicationAgentAuthorizationCode.expires_at > _now(),
                    ApplicationAgent.status == "published",
                    ApplicationClient.client_id == client_id,
                    ApplicationClient.status == "active",
                    ApplicationAgentClientGrant.status == "active",
                    ApplicationAgentClientGrant.allow_user_delegation.is_(
                        True
                    ),
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ApplicationAgentAuthorizationError(
                "authorization_code_invalid",
                status_code=401,
            )
        authorization, agent, client, grant, user = row
        if (
            not hmac.compare_digest(
                str(client.credential_digest_or_reference),
                _secret_digest(client_secret),
            )
            or "responses:create" not in set(grant.scopes or [])
        ):
            raise ApplicationAgentAuthorizationError(
                "application_client_unauthorized",
                status_code=401,
            )
        user_code = str(user.user_code or "").strip()
        if user.status != "active" or not user_code:
            raise ApplicationAgentAuthorizationError(
                "delegated_user_identity_unavailable",
                status_code=403,
            )
        authorization.consumed_at = _now()
        await self.db.commit()
        record = AuthorizationCodeRecord(
            code_digest=str(authorization.code_digest),
            org_id=int(authorization.org_id),
            agent_id=int(agent.id),
            agent_slug=str(agent.slug),
            client_pk=int(client.id),
            client_id=str(client.client_id),
            user_id=int(authorization.user_id),
            expires_at=authorization.expires_at,
            consumed=True,
        )
        subject = _token_subject_from_authorization_code(record)
        return ApplicationAgentTokenExchangeResult(
            access_token=_issue_token(
                subject,
                ttl_seconds=settings.APPLICATION_AGENT_TOKEN_TTL_SECONDS,
            ),
            expires_in=settings.APPLICATION_AGENT_TOKEN_TTL_SECONDS,
            user_code=user_code,
        )

    async def issue_trusted_user_token(
        self,
        *,
        agent_slug: str,
        user_code: str,
        client_id: str,
        client_secret: str,
    ) -> ApplicationAgentTokenExchangeResult:
        rows = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
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
                .where(
                    ApplicationAgent.slug == agent_slug,
                    ApplicationAgent.status == "published",
                    ApplicationClient.client_id == client_id,
                    ApplicationClient.status == "active",
                    ApplicationClient.auth_method == "client_secret",
                    ApplicationAgentClientGrant.status == "active",
                    ApplicationAgentClientGrant.allow_user_delegation.is_(
                        True
                    ),
                )
            )
        ).all()
        if not rows:
            raise ApplicationAgentAuthorizationError(
                "application_agent_trusted_user_delegation_not_allowed",
                status_code=403,
            )
        credential_digest = _secret_digest(client_secret)
        authenticated = [
            (agent, client, grant)
            for agent, client, grant in rows
            if hmac.compare_digest(
                str(client.credential_digest_or_reference),
                credential_digest,
            )
        ]
        if len(authenticated) != 1:
            raise ApplicationAgentAuthorizationError(
                "application_client_unauthorized",
                status_code=401,
            )
        agent, client, grant = authenticated[0]
        if "responses:create" not in set(grant.scopes or []):
            raise ApplicationAgentAuthorizationError(
                "application_agent_trusted_user_delegation_not_allowed",
                status_code=403,
            )
        normalized_user_code = user_code.strip()
        user = (
            await self.db.execute(
                select(User).where(
                    User.org_id == agent.org_id,
                    User.user_code == normalized_user_code,
                    User.status == "active",
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise ApplicationAgentAuthorizationError(
                "delegated_user_identity_unavailable",
                status_code=403,
            )
        ttl_seconds = (
            settings.APPLICATION_AGENT_SQLBOT2_TRUSTED_TOKEN_TTL_SECONDS
        )
        subject = ApplicationAgentTokenSubject(
            org_id=int(agent.org_id),
            agent_id=int(agent.id),
            agent_slug=str(agent.slug),
            client_id=str(client.client_id),
            user_id=int(user.id),
        )
        return ApplicationAgentTokenExchangeResult(
            access_token=_issue_token(subject, ttl_seconds=ttl_seconds),
            expires_in=ttl_seconds,
            user_code=normalized_user_code,
        )

    async def issue_sqlbot2_trusted_user_token(
        self,
        *,
        user_code: str,
        client_id: str,
        client_secret: str,
    ) -> ApplicationAgentTokenExchangeResult:
        """Compatibility wrapper for the historical SQLBot2-only route."""
        try:
            return await self.issue_trusted_user_token(
                agent_slug="sqlbot2",
                user_code=user_code,
                client_id=client_id,
                client_secret=client_secret,
            )
        except ApplicationAgentAuthorizationError as exc:
            if exc.code != "application_agent_trusted_user_delegation_not_allowed":
                raise
            raise ApplicationAgentAuthorizationError(
                "sqlbot2_trusted_user_delegation_not_allowed",
                status_code=exc.status_code,
            ) from exc


def decode_agent_token(
    token: str,
    *,
    client_id: str,
    agent_slug: str,
) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.RUNTIME_TOKEN_SECRET,
            algorithms=["HS256"],
            audience="hermes-application-agent",
            options={
                "require": [
                    "aud",
                    "sub",
                    "org_id",
                    "client_id",
                    "application_agent",
                    "scope",
                    "iat",
                    "exp",
                    "jti",
                ]
            },
        )
    except jwt.PyJWTError as exc:
        raise ApplicationAgentAuthorizationError(
            "agent_token_invalid",
            status_code=401,
        ) from exc
    if (
        claims.get("client_id") != client_id
        or claims.get("application_agent") != agent_slug
        or "responses:create" not in set(claims.get("scope") or [])
    ):
        raise ApplicationAgentAuthorizationError(
            "agent_token_scope_mismatch",
            status_code=403,
        )
    return Principal(
        org_id=int(claims["org_id"]),
        user_id=int(claims["sub"]),
    )


def _token_subject_from_authorization_code(
    record: AuthorizationCodeRecord,
) -> ApplicationAgentTokenSubject:
    return ApplicationAgentTokenSubject(
        org_id=record.org_id,
        agent_id=record.agent_id,
        agent_slug=record.agent_slug,
        client_id=record.client_id,
        user_id=record.user_id,
    )


def _issue_token(
    subject: ApplicationAgentTokenSubject,
    *,
    ttl_seconds: int,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "hermes-work",
            "aud": "hermes-application-agent",
            "sub": str(subject.user_id),
            "org_id": subject.org_id,
            "client_id": subject.client_id,
            "application_agent_id": subject.agent_id,
            "application_agent": subject.agent_slug,
            "subject_type": "user",
            "scope": ["responses:create"],
            "jti": secrets.token_urlsafe(18),
            "iat": now,
            "exp": now + ttl_seconds,
        },
        settings.RUNTIME_TOKEN_SECRET,
        algorithm="HS256",
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
