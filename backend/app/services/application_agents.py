from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentAuthorizationCode,
    ApplicationAgentClientGrant,
    ApplicationAgentConfigRevision,
    ApplicationAgentConversation,
    ApplicationAgentConversationFileArtifact,
    ApplicationAgentMemory,
    ApplicationAgentPersonalKnowledgeBinding,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentSkill,
    ApplicationAgentStudioFileArtifact,
    ApplicationAgentStudioSession,
    ApplicationAgentWeComBindingTicket,
    ApplicationAgentWeComCallbackEvent,
    ApplicationAgentWeComChannel,
    ApplicationAgentWeComOAuthState,
    ApplicationAgentWeComUserBinding,
    ApplicationAgentWeComUserAccess,
    ApplicationClient,
    ImChannel,
    ImChannelLease,
    ImDeliveryCheckpoint,
    ImInboundMessage,
    ImMessageAttachment,
    ImOnboardingSession,
    ModelUsageLog,
    ProfileSkillBinding,
    RuntimeAuditLog,
    Skill,
    SkillVersion,
    UserSkillHubInstallation,
    User,
    WeComIdentityBindingTicket,
)
from app.schemas.application_agent import (
    ApplicationAgentConfig,
    ApplicationAgentCreateRequest,
    ApplicationAgentUpdateRequest,
    ApplicationClientCreateRequest,
    ApplicationClientUpdateRequest,
)
from app.services.runtime_workspace import Principal
from app.services.runtime_models import SqlAlchemyRuntimeModelStore
from app.services.application_agent_asset_snapshot import (
    ApplicationAgentAssetSnapshotBuilder,
    ApplicationAgentAssetSnapshotError,
)
from app.hermes_runtime.skill_visibility_policy import (
    INITIAL_SKILL_POLICY_CONFIG_KEY,
    SKILL_POLICY_CONFIG_KEY,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
)


class ApplicationAgentError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


_LOOPBACK_REDIRECT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _with_initial_skill_policy(
    config: dict,
    *,
    existing: dict | None = None,
    default_policy_version: int = SKILL_VISIBILITY_POLICY_V1,
    desired_policy_version: int = SKILL_VISIBILITY_POLICY_V3,
) -> dict:
    """Persist the one-time default Skill policy without exposing it as user config."""
    result = dict(config)
    prior = (existing or {}).get(INITIAL_SKILL_POLICY_CONFIG_KEY)
    result[INITIAL_SKILL_POLICY_CONFIG_KEY] = (
        prior
        if prior in {SKILL_VISIBILITY_POLICY_V1, SKILL_VISIBILITY_POLICY_V2, SKILL_VISIBILITY_POLICY_V3}
        else default_policy_version
    )
    desired = (existing or {}).get(SKILL_POLICY_CONFIG_KEY)
    result[SKILL_POLICY_CONFIG_KEY] = (
        desired
        if desired in {SKILL_VISIBILITY_POLICY_V1, SKILL_VISIBILITY_POLICY_V2, SKILL_VISIBILITY_POLICY_V3}
        else desired_policy_version
    )
    return result


def _validate_redirect_uri_policy(
    redirect_uris: list[str],
    *,
    insecure_redirect_uri_allowlist: frozenset[str],
) -> None:
    for redirect_uri in redirect_uris:
        parsed = urlsplit(redirect_uri)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "http"
            and host not in _LOOPBACK_REDIRECT_HOSTS
            and redirect_uri not in insecure_redirect_uri_allowlist
        ):
            raise ApplicationAgentError(
                "application_redirect_uri_insecure",
                status_code=422,
            )


@dataclass(frozen=True)
class ApplicationAgentRecord:
    application_agent_id: int
    org_id: int
    created_by_user_id: int
    slug: str
    name: str
    avatar_url: str | None
    status: str
    runtime_status: str
    config: dict
    config_generation: int
    applied_generation: int | None
    apply_status: str
    last_apply_error_code: str | None
    client_count: int
    active_run_count: int
    queued_run_count: int
    published_at: datetime | None
    updated_at: datetime | None
    lifecycle_status: str = "active"
    delete_requested_at: datetime | None = None
    delete_last_error_code: str | None = None
    access_mode: str = "owner"
    creator_name: str | None = None
    creator_user_code: str | None = None
    creator_status: str | None = None
    personal_knowledge_enabled_count: int = 0
    personal_knowledge_revoked_count: int = 0


@dataclass(frozen=True)
class ApplicationAgentRevisionRecord:
    revision_id: int
    generation: int
    config_hash: str
    apply_status: str
    applied_at: datetime | None
    error_code: str | None
    created_by_user_id: int
    created_at: datetime | None


@dataclass(frozen=True)
class ApplicationClientRecord:
    application_client_id: int
    client_id: str
    name: str
    status: str
    allow_user_delegation: bool
    allow_service_invocation: bool
    auth_mode: str
    scopes: list[str]
    allowed_redirect_uris: list[str]
    created_at: datetime | None
    client_secret: str | None = None
    credential_digest: str | None = None


class InMemoryApplicationAgentService:
    def __init__(
        self,
        *,
        insecure_redirect_uri_allowlist: list[str] | None = None,
    ) -> None:
        configured_allowlist = (
            settings.APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST
            if insecure_redirect_uri_allowlist is None
            else insecure_redirect_uri_allowlist
        )
        self.insecure_redirect_uri_allowlist = frozenset(configured_allowlist)
        self.agents: dict[int, ApplicationAgentRecord] = {}
        self.revisions: dict[int, list[ApplicationAgentRevisionRecord]] = {}
        self.clients: dict[int, list[ApplicationClientRecord]] = {}
        self.next_agent_id = 1
        self.next_revision_id = 1
        self.next_client_id = 1

    def reset(self) -> None:
        self.agents.clear()
        self.revisions.clear()
        self.clients.clear()
        self.next_agent_id = 1
        self.next_revision_id = 1
        self.next_client_id = 1

    async def list_agents(self, *, principal: Principal) -> list[ApplicationAgentRecord]:
        return sorted(
            (
                self._with_access(agent, principal)
                for agent in self.agents.values()
                if agent.org_id == principal.org_id
            ),
            key=lambda agent: (agent.updated_at or datetime.min, agent.application_agent_id),
            reverse=True,
        )

    async def get_agent(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        agent = self.agents.get(agent_id)
        if agent is None or agent.org_id != principal.org_id:
            raise ApplicationAgentError("application_agent_not_found", status_code=404)
        return self._with_access(agent, principal)

    async def get_owned_agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ApplicationAgentRecord:
        agent = await self.get_agent(principal=principal, agent_id=agent_id)
        if agent.created_by_user_id != principal.user_id:
            raise ApplicationAgentError(
                "application_agent_owner_required",
                status_code=403,
            )
        return agent

    async def create_agent(
        self,
        *,
        principal: Principal,
        payload: ApplicationAgentCreateRequest,
    ) -> ApplicationAgentRecord:
        if any(agent.org_id == principal.org_id and agent.slug == payload.slug for agent in self.agents.values()):
            raise ApplicationAgentError("application_agent_slug_conflict")
        now = _now()
        agent = ApplicationAgentRecord(
            application_agent_id=self.next_agent_id,
            org_id=principal.org_id,
            created_by_user_id=principal.user_id,
            slug=payload.slug,
            name=payload.name.strip(),
            avatar_url=payload.avatar_url,
            status="unpublished",
            runtime_status="absent",
            config=_with_initial_skill_policy(payload.config.model_dump(mode="json")),
            config_generation=1,
            applied_generation=None,
            apply_status="applying",
            last_apply_error_code=None,
            lifecycle_status="active",
            client_count=0,
            active_run_count=0,
            queued_run_count=0,
            published_at=None,
            updated_at=now,
        )
        self.next_agent_id += 1
        self.agents[agent.application_agent_id] = agent
        self._append_revision(agent, principal.user_id)
        return agent

    async def update_agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
        payload: ApplicationAgentUpdateRequest,
    ) -> ApplicationAgentRecord:
        current = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        if (
            payload.expected_config_generation is not None
            and payload.expected_config_generation
            != current.config_generation
        ):
            raise ApplicationAgentError(
                "application_agent_config_conflict",
                status_code=409,
            )
        generation = current.config_generation + 1
        published = current.status == "published"
        updated = replace(
            current,
            name=payload.name.strip(),
            avatar_url=payload.avatar_url,
            config=_with_initial_skill_policy(
                payload.config.model_dump(mode="json"),
                existing=current.config,
                default_policy_version=SKILL_VISIBILITY_POLICY_V1,
                desired_policy_version=SKILL_VISIBILITY_POLICY_V3,
            ),
            config_generation=generation,
            applied_generation=generation if published else current.applied_generation,
            apply_status="applying",
            runtime_status="ready" if published else current.runtime_status,
            updated_at=_now(),
        )
        self.agents[agent_id] = updated
        self._append_revision(updated, principal.user_id)
        return updated

    async def update_skill_policy(
        self, *, principal: Principal, agent_id: int, policy_version: int,
        expected_config_generation: int,
    ) -> ApplicationAgentRecord:
        if policy_version not in {SKILL_VISIBILITY_POLICY_V1, SKILL_VISIBILITY_POLICY_V2, SKILL_VISIBILITY_POLICY_V3}:
            raise ApplicationAgentError("unsupported_skill_policy", status_code=422)
        current = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        if current.config_generation != expected_config_generation:
            raise ApplicationAgentError("application_agent_config_conflict", status_code=409)
        config = dict(current.config)
        config[SKILL_POLICY_CONFIG_KEY] = policy_version
        published = current.status == "published"
        updated = replace(
            current,
            config=config,
            config_generation=current.config_generation + 1,
            applied_generation=(current.config_generation + 1 if published else current.applied_generation),
            apply_status="applying",
            updated_at=_now(),
        )
        self.agents[agent_id] = updated
        self._append_revision(updated, principal.user_id)
        return updated

    async def list_revisions(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[ApplicationAgentRevisionRecord]:
        await self.get_agent(principal=principal, agent_id=agent_id)
        return list(reversed(self.revisions.get(agent_id, [])))

    async def list_clients(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[ApplicationClientRecord]:
        await self.get_agent(principal=principal, agent_id=agent_id)
        return [
            client
            for client in self.clients.get(agent_id, [])
            if client.status == "active"
        ]

    async def create_client(
        self,
        *,
        principal: Principal,
        agent_id: int,
        payload: ApplicationClientCreateRequest,
    ) -> ApplicationClientRecord:
        agent = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        _validate_redirect_uri_policy(
            payload.allowed_redirect_uris,
            insecure_redirect_uri_allowlist=(
                self.insecure_redirect_uri_allowlist
            ),
        )
        secret = f"hca_{secrets.token_urlsafe(32)}"
        client = ApplicationClientRecord(
            application_client_id=self.next_client_id,
            client_id=f"app_{agent_id}_{secrets.token_hex(6)}",
            name=payload.name.strip(),
            status="active",
            allow_user_delegation=payload.allow_user_delegation,
            allow_service_invocation=payload.allow_service_invocation,
            auth_mode=_client_auth_mode(
                payload.allow_user_delegation,
                payload.allow_service_invocation,
            ),
            scopes=list(dict.fromkeys(payload.scopes)),
            allowed_redirect_uris=list(payload.allowed_redirect_uris),
            created_at=_now(),
            client_secret=secret,
            credential_digest=_secret_digest(secret),
        )
        self.next_client_id += 1
        self.clients.setdefault(agent_id, []).append(
            replace(client, client_secret=None)
        )
        self.agents[agent_id] = replace(
            agent,
            client_count=sum(
                item.status == "active"
                for item in self.clients[agent_id]
            ),
            updated_at=_now(),
        )
        return client

    async def update_client(
        self,
        *,
        principal: Principal,
        agent_id: int,
        client_pk: int,
        payload: ApplicationClientUpdateRequest,
    ) -> ApplicationClientRecord:
        agent = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        _validate_redirect_uri_policy(
            payload.allowed_redirect_uris,
            insecure_redirect_uri_allowlist=(
                self.insecure_redirect_uri_allowlist
            ),
        )
        clients = self.clients.get(agent_id, [])
        for index, client in enumerate(clients):
            if (
                client.application_client_id != client_pk
                or client.status != "active"
            ):
                continue
            updated = replace(
                client,
                name=payload.name.strip(),
                allow_user_delegation=payload.allow_user_delegation,
                allow_service_invocation=payload.allow_service_invocation,
                auth_mode=_client_auth_mode(
                    payload.allow_user_delegation,
                    payload.allow_service_invocation,
                ),
                allowed_redirect_uris=list(payload.allowed_redirect_uris),
            )
            clients[index] = updated
            self.agents[agent_id] = replace(agent, updated_at=_now())
            return updated
        raise ApplicationAgentError(
            "application_client_not_found",
            status_code=404,
        )

    async def delete_client(
        self,
        *,
        principal: Principal,
        agent_id: int,
        client_pk: int,
    ) -> None:
        agent = await self.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        clients = self.clients.get(agent_id, [])
        for index, client in enumerate(clients):
            if (
                client.application_client_id == client_pk
                and client.status == "active"
            ):
                clients[index] = replace(client, status="disabled")
                self.agents[agent_id] = replace(
                    agent,
                    client_count=sum(
                        item.status == "active"
                        for item in clients
                    ),
                    updated_at=_now(),
                )
                return
        raise ApplicationAgentError(
            "application_client_not_found",
            status_code=404,
        )

    async def publish(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        agent = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        if not any(
            client.status == "active"
            for client in self.clients.get(agent_id, [])
        ):
            raise ApplicationAgentError("application_agent_client_required")
        now = _now()
        updated = replace(
            agent,
            status="published",
            runtime_status="ready",
            applied_generation=agent.config_generation,
            apply_status="applied",
            published_at=agent.published_at or now,
            updated_at=now,
        )
        self.agents[agent_id] = updated
        return updated

    async def disable(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        agent = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        updated = replace(agent, status="disabled", runtime_status="stopped", updated_at=_now())
        self.agents[agent_id] = updated
        return updated

    async def prepare_delete(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> None:
        agent = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        self.agents[agent_id] = replace(
            agent,
            status="disabled",
            lifecycle_status="deleting",
            runtime_status="stopped",
            delete_requested_at=agent.delete_requested_at or _now(),
            delete_last_error_code=None,
            updated_at=_now(),
        )
        self.clients[agent_id] = [
            replace(client, status="disabled")
            for client in self.clients.get(agent_id, [])
        ]

    async def delete_agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> None:
        await self.get_owned_agent(principal=principal, agent_id=agent_id)
        self.clients.pop(agent_id, None)
        self.revisions.pop(agent_id, None)
        self.agents.pop(agent_id, None)

    async def resume(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        agent = await self.get_owned_agent(principal=principal, agent_id=agent_id)
        if agent.published_at is None:
            raise ApplicationAgentError("application_agent_never_published")
        updated = replace(agent, status="published", runtime_status="ready", updated_at=_now())
        self.agents[agent_id] = updated
        return updated

    def _append_revision(self, agent: ApplicationAgentRecord, user_id: int) -> None:
        applied = agent.apply_status == "applied"
        revision = ApplicationAgentRevisionRecord(
            revision_id=self.next_revision_id,
            generation=agent.config_generation,
            config_hash=_config_hash(agent.config),
            apply_status=agent.apply_status,
            applied_at=_now() if applied else None,
            error_code=agent.last_apply_error_code,
            created_by_user_id=user_id,
            created_at=_now(),
        )
        self.next_revision_id += 1
        self.revisions.setdefault(agent.application_agent_id, []).append(revision)

    @staticmethod
    def _with_access(
        agent: ApplicationAgentRecord,
        principal: Principal,
    ) -> ApplicationAgentRecord:
        return replace(
            agent,
            access_mode=(
                "owner"
                if agent.created_by_user_id == principal.user_id
                else "viewer"
            ),
        )


class SqlAlchemyApplicationAgentService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        insecure_redirect_uri_allowlist: list[str] | None = None,
    ) -> None:
        self.db = db
        configured_allowlist = (
            settings.APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST
            if insecure_redirect_uri_allowlist is None
            else insecure_redirect_uri_allowlist
        )
        self.insecure_redirect_uri_allowlist = frozenset(configured_allowlist)

    async def _validate_model_visibility(
        self,
        *,
        principal: Principal,
        config: ApplicationAgentConfig,
    ) -> None:
        model_entry_id = config.model_entry_id
        if model_entry_id is None:
            return
        visible_models = await SqlAlchemyRuntimeModelStore(self.db).list_visible_models(
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
        if int(model_entry_id) not in {
            int(model.model_entry_id) for model in visible_models
        }:
            raise ApplicationAgentError(
                "application_agent_model_unavailable",
                status_code=409,
            )

    async def list_agents(self, *, principal: Principal) -> list[ApplicationAgentRecord]:
        rows = list(
            (
                await self.db.execute(
                    select(ApplicationAgent)
                    .where(
                        ApplicationAgent.org_id == principal.org_id,
                    )
                    .order_by(ApplicationAgent.updated_at.desc(), ApplicationAgent.id.desc())
                )
            ).scalars()
        )
        return [
            await self._record(row, principal=principal)
            for row in rows
        ]

    async def get_agent(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        row = await self._agent_row(principal=principal, agent_id=agent_id)
        return await self._record(row, principal=principal)

    async def get_owned_agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ApplicationAgentRecord:
        row = await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        return await self._record(row, principal=principal)

    async def create_agent(
        self,
        *,
        principal: Principal,
        payload: ApplicationAgentCreateRequest,
    ) -> ApplicationAgentRecord:
        existing = (
            await self.db.execute(
                select(ApplicationAgent.id).where(
                    ApplicationAgent.org_id == principal.org_id,
                    ApplicationAgent.slug == payload.slug,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ApplicationAgentError("application_agent_slug_conflict")
        await self._validate_model_visibility(
            principal=principal,
            config=payload.config,
        )
        config = _with_initial_skill_policy(payload.config.model_dump(mode="json"))
        row = ApplicationAgent(
            org_id=principal.org_id,
            slug=payload.slug,
            name=payload.name.strip(),
            avatar_url=payload.avatar_url,
            status="unpublished",
            created_by_user_id=principal.user_id,
            updated_by_user_id=principal.user_id,
            config=config,
            config_generation=1,
            apply_status="applying",
            lifecycle_status="active",
        )
        self.db.add(row)
        await self.db.flush()
        try:
            snapshot = await ApplicationAgentAssetSnapshotBuilder(self.db).build(
                org_id=int(row.org_id),
                application_agent_id=int(row.id),
                config=config,
            )
        except ApplicationAgentAssetSnapshotError as exc:
            await self.db.rollback()
            raise ApplicationAgentError(exc.code, status_code=422) from exc
        self.db.add(
            self._revision(
                row=row,
                principal=principal,
                config_snapshot=snapshot,
            )
        )
        await self.db.commit()
        await self.db.refresh(row)
        return await self._record(row, principal=principal)

    async def update_agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
        payload: ApplicationAgentUpdateRequest,
    ) -> ApplicationAgentRecord:
        row = await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        if (
            payload.expected_config_generation is not None
            and payload.expected_config_generation
            != int(row.config_generation)
        ):
            raise ApplicationAgentError(
                "application_agent_config_conflict",
                status_code=409,
            )
        await self._validate_model_visibility(
            principal=principal,
            config=payload.config,
        )
        row.name = payload.name.strip()
        row.avatar_url = payload.avatar_url
        row.config = _with_initial_skill_policy(
            payload.config.model_dump(mode="json"),
            existing=dict(row.config or {}),
            default_policy_version=SKILL_VISIBILITY_POLICY_V1,
            desired_policy_version=SKILL_VISIBILITY_POLICY_V3,
        )
        row.config_generation += 1
        row.updated_by_user_id = principal.user_id
        if row.lifecycle_status == "active":
            row.apply_status = "applying"
        try:
            snapshot = await ApplicationAgentAssetSnapshotBuilder(self.db).build(
                org_id=int(row.org_id),
                application_agent_id=int(row.id),
                config=row.config,
            )
        except ApplicationAgentAssetSnapshotError as exc:
            await self.db.rollback()
            raise ApplicationAgentError(exc.code, status_code=422) from exc
        self.db.add(
            self._revision(
                row=row,
                principal=principal,
                config_snapshot=snapshot,
            )
        )
        await self.db.commit()
        await self.db.refresh(row)
        return await self._record(row, principal=principal)

    async def update_skill_policy(
        self, *, principal: Principal, agent_id: int, policy_version: int,
        expected_config_generation: int,
    ) -> ApplicationAgentRecord:
        if policy_version not in {SKILL_VISIBILITY_POLICY_V1, SKILL_VISIBILITY_POLICY_V2, SKILL_VISIBILITY_POLICY_V3}:
            raise ApplicationAgentError("unsupported_skill_policy", status_code=422)
        row = await self._agent_row(
            principal=principal, agent_id=agent_id, for_update=True, require_owner=True
        )
        if int(row.config_generation) != expected_config_generation:
            await self.db.rollback()
            raise ApplicationAgentError("application_agent_config_conflict", status_code=409)
        config = dict(row.config or {})
        config[SKILL_POLICY_CONFIG_KEY] = policy_version
        row.config = config
        row.config_generation += 1
        row.updated_by_user_id = principal.user_id
        if row.lifecycle_status == "active":
            row.apply_status = "applying"
        try:
            snapshot = await ApplicationAgentAssetSnapshotBuilder(self.db).build(
                org_id=int(row.org_id), application_agent_id=int(row.id), config=row.config
            )
        except ApplicationAgentAssetSnapshotError as exc:
            await self.db.rollback()
            raise ApplicationAgentError(exc.code, status_code=422) from exc
        self.db.add(self._revision(row=row, principal=principal, config_snapshot=snapshot))
        await self.db.commit()
        await self.db.refresh(row)
        return await self._record(row, principal=principal)

    async def list_revisions(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[ApplicationAgentRevisionRecord]:
        await self._agent_row(principal=principal, agent_id=agent_id)
        rows = list(
            (
                await self.db.execute(
                    select(ApplicationAgentConfigRevision)
                    .where(ApplicationAgentConfigRevision.application_agent_id == agent_id)
                    .order_by(
                        ApplicationAgentConfigRevision.generation.desc(),
                        ApplicationAgentConfigRevision.id.desc(),
                    )
                )
            ).scalars()
        )
        return [
            ApplicationAgentRevisionRecord(
                revision_id=int(row.id),
                generation=int(row.generation),
                config_hash=str(row.config_hash),
                apply_status=str(row.apply_status),
                applied_at=row.applied_at,
                error_code=str(row.error_code) if row.error_code else None,
                created_by_user_id=int(row.created_by_user_id),
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def list_clients(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[ApplicationClientRecord]:
        await self._agent_row(principal=principal, agent_id=agent_id)
        rows = (
            await self.db.execute(
                select(ApplicationClient, ApplicationAgentClientGrant)
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_client_id == ApplicationClient.id,
                )
                .where(
                    ApplicationAgentClientGrant.application_agent_id == agent_id,
                    ApplicationAgentClientGrant.org_id == principal.org_id,
                    ApplicationClient.org_id == principal.org_id,
                    ApplicationAgentClientGrant.status == "active",
                    ApplicationClient.status == "active",
                    ApplicationClient.auth_method == "client_secret",
                )
                .order_by(ApplicationClient.created_at.desc(), ApplicationClient.id.desc())
            )
        ).all()
        return [self._client_record(client, grant) for client, grant in rows]

    async def create_client(
        self,
        *,
        principal: Principal,
        agent_id: int,
        payload: ApplicationClientCreateRequest,
    ) -> ApplicationClientRecord:
        _validate_redirect_uri_policy(
            payload.allowed_redirect_uris,
            insecure_redirect_uri_allowlist=(
                self.insecure_redirect_uri_allowlist
            ),
        )
        await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            require_owner=True,
        )
        raw_secret = f"hca_{secrets.token_urlsafe(32)}"
        client = ApplicationClient(
            org_id=principal.org_id,
            client_id=f"app_{agent_id}_{secrets.token_hex(8)}",
            name=payload.name.strip(),
            auth_method="client_secret",
            credential_digest_or_reference=_secret_digest(raw_secret),
            status="active",
            allowed_redirect_uris=list(payload.allowed_redirect_uris),
            rate_limit_policy={},
            created_by_user_id=principal.user_id,
        )
        self.db.add(client)
        await self.db.flush()
        grant = ApplicationAgentClientGrant(
            org_id=principal.org_id,
            application_agent_id=agent_id,
            application_client_id=client.id,
            scopes=list(dict.fromkeys(payload.scopes)),
            allow_user_delegation=payload.allow_user_delegation,
            allow_service_invocation=payload.allow_service_invocation,
            status="active",
        )
        self.db.add(grant)
        await self.db.commit()
        await self.db.refresh(client)
        return replace(self._client_record(client, grant), client_secret=raw_secret)

    async def update_client(
        self,
        *,
        principal: Principal,
        agent_id: int,
        client_pk: int,
        payload: ApplicationClientUpdateRequest,
    ) -> ApplicationClientRecord:
        _validate_redirect_uri_policy(
            payload.allowed_redirect_uris,
            insecure_redirect_uri_allowlist=(
                self.insecure_redirect_uri_allowlist
            ),
        )
        await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        row = (
            await self.db.execute(
                select(ApplicationClient, ApplicationAgentClientGrant)
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_client_id
                    == ApplicationClient.id,
                )
                .where(
                    ApplicationClient.id == client_pk,
                    ApplicationClient.org_id == principal.org_id,
                    ApplicationAgentClientGrant.org_id == principal.org_id,
                    ApplicationAgentClientGrant.application_agent_id
                    == agent_id,
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ApplicationAgentError(
                "application_client_not_found",
                status_code=404,
            )
        client, grant = row
        client.name = payload.name.strip()
        client.allowed_redirect_uris = list(payload.allowed_redirect_uris)
        grant.allow_user_delegation = payload.allow_user_delegation
        grant.allow_service_invocation = payload.allow_service_invocation
        await self.db.commit()
        await self.db.refresh(client)
        return self._client_record(client, grant)

    async def delete_client(
        self,
        *,
        principal: Principal,
        agent_id: int,
        client_pk: int,
    ) -> None:
        await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        row = (
            await self.db.execute(
                select(ApplicationClient, ApplicationAgentClientGrant)
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_client_id
                    == ApplicationClient.id,
                )
                .where(
                    ApplicationClient.id == client_pk,
                    ApplicationClient.org_id == principal.org_id,
                    ApplicationClient.status == "active",
                    ApplicationAgentClientGrant.org_id == principal.org_id,
                    ApplicationAgentClientGrant.application_agent_id
                    == agent_id,
                    ApplicationAgentClientGrant.status == "active",
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ApplicationAgentError(
                "application_client_not_found",
                status_code=404,
            )
        client, grant = row
        client.status = "disabled"
        grant.status = "disabled"
        await self.db.commit()

    async def publish(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        row = await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        grant_count = int(
            (
                await self.db.execute(
                    select(func.count(ApplicationAgentClientGrant.id)).where(
                        ApplicationAgentClientGrant.application_agent_id == agent_id,
                        ApplicationAgentClientGrant.org_id == principal.org_id,
                        ApplicationAgentClientGrant.status == "active",
                    )
                )
            ).scalar_one()
        )
        if grant_count == 0:
            raise ApplicationAgentError("application_agent_client_required")
        binding = await self._binding(agent_id=agent_id)
        if binding is None or binding.status != "ready":
            raise ApplicationAgentError("application_agent_runtime_not_ready", status_code=503)
        now = _now()
        row.status = "published"
        row.published_at = row.published_at or now
        row.disabled_at = None
        row.applied_generation = row.config_generation
        row.apply_status = "applied"
        await self.db.commit()
        await self.db.refresh(row)
        return await self._record(row, principal=principal)

    async def disable(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        row = await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        row.status = "disabled"
        row.disabled_at = _now()
        # Disabling is reversible and must not create a deletion transition.
        # Preserve an existing lifecycle tombstone if a concurrent DELETE has
        # already been accepted; the reconciler remains authoritative there.
        await self.db.execute(
            update(ApplicationAgentWeComChannel)
            .where(
                ApplicationAgentWeComChannel.application_agent_id
                == agent_id
            )
            .values(status="disabled")
        )
        await self.db.commit()
        await self.db.refresh(row)
        return await self._record(row, principal=principal)

    async def prepare_delete(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> None:
        row = await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        row.status = "disabled"
        row.disabled_at = _now()
        row.lifecycle_status = "deleting"
        row.delete_requested_at = row.delete_requested_at or _now()
        row.delete_last_error_code = None
        client_ids = select(
            ApplicationAgentClientGrant.application_client_id
        ).where(
            ApplicationAgentClientGrant.application_agent_id == agent_id
        )
        await self.db.execute(
            update(ApplicationAgentClientGrant)
            .where(
                ApplicationAgentClientGrant.application_agent_id == agent_id
            )
            .values(status="disabled")
        )
        await self.db.execute(
            update(ApplicationClient)
            .where(ApplicationClient.id.in_(client_ids))
            .values(status="disabled")
        )
        await self.db.execute(
            update(ApplicationAgentWeComChannel)
            .where(
                ApplicationAgentWeComChannel.application_agent_id == agent_id
            )
            .values(status="disabled")
        )
        await self.db.execute(
            update(ImChannel)
            .where(
                ImChannel.target_type == "application_agent",
                ImChannel.application_agent_id == agent_id,
            )
            .values(
                enabled=False,
                status="disabled",
                config_generation=ImChannel.config_generation + 1,
            )
        )
        await self.db.execute(
            update(ApplicationAgentRuntimeBinding)
            .where(
                ApplicationAgentRuntimeBinding.application_agent_id
                == agent_id
            )
            .values(status="stopped")
        )
        await self.db.commit()

    async def delete_agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> None:
        await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        client_ids = list(
            (
                await self.db.execute(
                    select(
                        ApplicationAgentClientGrant.application_client_id
                    ).where(
                        ApplicationAgentClientGrant.application_agent_id
                        == agent_id
                    )
                )
            ).scalars()
        )
        runtime_run_ids = list(
            (
                await self.db.execute(
                    select(ApplicationAgentRun.runtime_run_id).where(
                        ApplicationAgentRun.application_agent_id == agent_id
                    )
                )
            ).scalars()
        )
        im_channel_ids = list(
            (
                await self.db.execute(
                    select(ImChannel.id).where(
                        ImChannel.target_type
                        == "application_agent",
                        ImChannel.application_agent_id == agent_id,
                    )
                )
            ).scalars()
        )
        inbound_message_ids = list(
            (
                await self.db.execute(
                    select(ImInboundMessage.id).where(
                        ImInboundMessage.application_agent_id == agent_id
                    )
                )
            ).scalars()
        )
        channel_ids = list(
            (
                await self.db.execute(
                    select(ApplicationAgentWeComChannel.id).where(
                        ApplicationAgentWeComChannel.application_agent_id
                        == agent_id
                    )
                )
            ).scalars()
        )
        if channel_ids:
            for model in (
                ApplicationAgentWeComCallbackEvent,
                ApplicationAgentWeComUserBinding,
                ApplicationAgentWeComBindingTicket,
                ApplicationAgentWeComOAuthState,
            ):
                await self.db.execute(
                    delete(model).where(model.channel_id.in_(channel_ids))
                )
        await self.db.execute(
            delete(ApplicationAgentWeComChannel).where(
                ApplicationAgentWeComChannel.application_agent_id == agent_id
            )
        )
        if inbound_message_ids:
            await self.db.execute(
                delete(ImMessageAttachment).where(
                    ImMessageAttachment.inbound_message_id.in_(
                        inbound_message_ids
                    )
                )
            )
            await self.db.execute(
                delete(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id.in_(
                        inbound_message_ids
                    )
                )
            )
            await self.db.execute(
                delete(ImInboundMessage).where(
                    ImInboundMessage.id.in_(inbound_message_ids)
                )
            )
        await self.db.execute(
            delete(ApplicationAgentWeComUserAccess).where(
                ApplicationAgentWeComUserAccess.application_agent_id
                == agent_id
            )
        )
        await self.db.execute(
            delete(WeComIdentityBindingTicket).where(
                WeComIdentityBindingTicket.source_application_agent_id
                == agent_id
            )
        )
        await self.db.execute(
            delete(ImOnboardingSession).where(
                ImOnboardingSession.target_type
                == "application_agent",
                ImOnboardingSession.application_agent_id == agent_id,
            )
        )
        application_skill_ids = list(
            (
                await self.db.execute(
                    select(Skill.id).where(
                        Skill.application_agent_id == agent_id,
                    )
                )
            ).scalars()
        )
        if application_skill_ids:
            # Remove all rows that keep restrictive references to the
            # agent-owned catalog before deleting immutable versions and
            # finally the Skill rows themselves.
            await self.db.execute(
                delete(ProfileSkillBinding).where(
                    ProfileSkillBinding.skill_id.in_(application_skill_ids)
                )
            )
            await self.db.execute(
                delete(UserSkillHubInstallation).where(
                    UserSkillHubInstallation.skill_id.in_(application_skill_ids)
                )
            )
            await self.db.execute(
                delete(SkillVersion).where(
                    SkillVersion.skill_id.in_(application_skill_ids)
                )
            )
            await self.db.execute(
                delete(Skill).where(Skill.id.in_(application_skill_ids))
            )
        if im_channel_ids:
            await self.db.execute(
                delete(ImChannelLease).where(
                    ImChannelLease.channel_id.in_(im_channel_ids)
                )
            )
            await self.db.execute(
                delete(ImChannel).where(
                    ImChannel.id.in_(im_channel_ids)
                )
            )
        await self.db.execute(
            delete(ApplicationAgentStudioFileArtifact).where(
                ApplicationAgentStudioFileArtifact.studio_session_id.in_(
                    select(ApplicationAgentStudioSession.id).where(
                        ApplicationAgentStudioSession.application_agent_id
                        == agent_id
                    )
                )
            )
        )
        for model in (
            ApplicationAgentAuthorizationCode,
            ApplicationAgentConversationFileArtifact,
            ApplicationAgentRun,
            ApplicationAgentMemory,
            ApplicationAgentConversation,
            ApplicationAgentClientGrant,
            ApplicationAgentConfigRevision,
            ApplicationAgentPersonalKnowledgeBinding,
            ApplicationAgentSkill,
            ApplicationAgentStudioSession,
        ):
            await self.db.execute(
                delete(model).where(model.application_agent_id == agent_id)
            )
        await self.db.execute(
            delete(RuntimeAuditLog).where(
                RuntimeAuditLog.org_id == principal.org_id,
                RuntimeAuditLog.target_type == "application_agent",
                RuntimeAuditLog.target_id == str(agent_id),
            )
        )
        if runtime_run_ids:
            await self.db.execute(
                delete(ModelUsageLog).where(
                    ModelUsageLog.runtime_run_id.in_(runtime_run_ids)
                )
            )
        await self.db.execute(
            delete(ApplicationAgentRuntimeBinding).where(
                ApplicationAgentRuntimeBinding.application_agent_id
                == agent_id
            )
        )
        for client_id in client_ids:
            remaining_grant = (
                await self.db.execute(
                    select(ApplicationAgentClientGrant.id)
                    .where(
                        ApplicationAgentClientGrant.application_client_id
                        == client_id
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if remaining_grant is None:
                await self.db.execute(
                    delete(ApplicationClient).where(
                        ApplicationClient.id == client_id
                    )
                )
        await self.db.execute(
            delete(ApplicationAgent).where(
                ApplicationAgent.id == agent_id,
                ApplicationAgent.org_id == principal.org_id,
            )
        )
        await self.db.commit()

    async def resume(self, *, principal: Principal, agent_id: int) -> ApplicationAgentRecord:
        row = await self._agent_row(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        if row.published_at is None:
            raise ApplicationAgentError("application_agent_never_published")
        binding = await self._binding(agent_id=agent_id)
        if binding is None or binding.status != "ready":
            raise ApplicationAgentError("application_agent_runtime_not_ready", status_code=503)
        row.status = "published"
        row.disabled_at = None
        await self.db.commit()
        await self.db.refresh(row)
        return await self._record(row, principal=principal)

    async def _agent_row(
        self,
        *,
        principal: Principal,
        agent_id: int,
        for_update: bool = False,
        require_owner: bool = False,
    ) -> ApplicationAgent:
        statement = select(ApplicationAgent).where(
            ApplicationAgent.id == agent_id,
            ApplicationAgent.org_id == principal.org_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).scalar_one_or_none()
        if row is None:
            raise ApplicationAgentError("application_agent_not_found", status_code=404)
        if require_owner and int(row.created_by_user_id) != principal.user_id:
            raise ApplicationAgentError(
                "application_agent_owner_required",
                status_code=403,
            )
        return row

    async def _binding(self, *, agent_id: int) -> ApplicationAgentRuntimeBinding | None:
        return (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding).where(
                    ApplicationAgentRuntimeBinding.application_agent_id == agent_id
                )
            )
        ).scalar_one_or_none()

    async def _record(
        self,
        row: ApplicationAgent,
        *,
        principal: Principal,
    ) -> ApplicationAgentRecord:
        binding = await self._binding(agent_id=int(row.id))
        creator = (
            await self.db.execute(
                select(User).where(
                    User.id == row.created_by_user_id,
                    User.org_id == row.org_id,
                )
            )
        ).scalar_one_or_none()
        client_count = int(
            (
                await self.db.execute(
                    select(func.count(ApplicationAgentClientGrant.id)).where(
                        ApplicationAgentClientGrant.application_agent_id == row.id,
                        ApplicationAgentClientGrant.status == "active",
                    )
                )
            ).scalar_one()
        )
        personal_knowledge_counts = {
            str(status): int(count)
            for status, count in (
                await self.db.execute(
                    select(
                        ApplicationAgentPersonalKnowledgeBinding.status,
                        func.count(
                            ApplicationAgentPersonalKnowledgeBinding.id
                        ),
                    )
                    .where(
                        ApplicationAgentPersonalKnowledgeBinding
                        .application_agent_id == row.id
                    )
                    .group_by(
                        ApplicationAgentPersonalKnowledgeBinding.status
                    )
                )
            ).all()
        }
        return ApplicationAgentRecord(
            application_agent_id=int(row.id),
            org_id=int(row.org_id),
            created_by_user_id=int(row.created_by_user_id),
            slug=str(row.slug),
            name=str(row.name),
            avatar_url=str(row.avatar_url) if row.avatar_url else None,
            status=str(row.status),
            runtime_status=str(binding.status) if binding else "absent",
            config=dict(row.config or {}),
            config_generation=int(row.config_generation),
            applied_generation=int(row.applied_generation) if row.applied_generation is not None else None,
            apply_status=str(row.apply_status),
            last_apply_error_code=str(row.last_apply_error_code) if row.last_apply_error_code else None,
            lifecycle_status=str(row.lifecycle_status),
            delete_requested_at=row.delete_requested_at,
            delete_last_error_code=(
                str(row.delete_last_error_code)
                if row.delete_last_error_code
                else None
            ),
            client_count=client_count,
            active_run_count=int(binding.active_run_count) if binding else 0,
            queued_run_count=int(binding.queued_run_count) if binding else 0,
            published_at=row.published_at,
            updated_at=row.updated_at,
            access_mode=(
                "owner"
                if int(row.created_by_user_id) == principal.user_id
                else "viewer"
            ),
            creator_name=str(creator.name) if creator else None,
            creator_user_code=(
                str(creator.user_code)
                if creator and creator.user_code
                else None
            ),
            creator_status=str(creator.status) if creator else None,
            personal_knowledge_enabled_count=(
                personal_knowledge_counts.get("enabled", 0)
            ),
            personal_knowledge_revoked_count=(
                personal_knowledge_counts.get("permission_revoked", 0)
            ),
        )

    @staticmethod
    def _revision(
        *,
        row: ApplicationAgent,
        principal: Principal,
        config_snapshot: dict | None = None,
    ) -> ApplicationAgentConfigRevision:
        snapshot = dict(config_snapshot or row.config or {})
        return ApplicationAgentConfigRevision(
            org_id=principal.org_id,
            application_agent_id=row.id,
            generation=row.config_generation,
            config_snapshot=snapshot,
            config_hash=_config_hash(snapshot),
            created_by_user_id=principal.user_id,
            apply_status=row.apply_status,
        )

    @staticmethod
    def _client_record(
        client: ApplicationClient,
        grant: ApplicationAgentClientGrant,
    ) -> ApplicationClientRecord:
        return ApplicationClientRecord(
            application_client_id=int(client.id),
            client_id=str(client.client_id),
            name=str(client.name),
            status=str(client.status),
            allow_user_delegation=bool(grant.allow_user_delegation),
            allow_service_invocation=bool(grant.allow_service_invocation),
            auth_mode=_client_auth_mode(
                bool(grant.allow_user_delegation),
                bool(grant.allow_service_invocation),
            ),
            scopes=[str(scope) for scope in (grant.scopes or [])],
            allowed_redirect_uris=[
                str(uri) for uri in (client.allowed_redirect_uris or [])
            ],
            created_at=client.created_at,
            credential_digest=str(client.credential_digest_or_reference),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _config_hash(config: dict) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _client_auth_mode(
    allow_user_delegation: bool,
    allow_service_invocation: bool,
) -> str:
    if allow_user_delegation and allow_service_invocation:
        return "legacy_dual"
    if allow_user_delegation:
        return "delegated_user"
    if allow_service_invocation:
        return "service"
    return "invalid"
