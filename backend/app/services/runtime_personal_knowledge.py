from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentPersonalKnowledgeBinding,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    CloudHermesProfile,
    HermesRuntimeBinding,
    KnowledgeBase,
    KnowledgeBaseFile,
    ProfileKnowledgeBinding,
    RuntimeAgentProfile,
    User,
)
from app.schemas.personal_knowledge import (
    RuntimePersonalKnowledgeCapabilities,
    RuntimePersonalKnowledgeHybridSearchResponse,
    RuntimePersonalKnowledgeItem,
    RuntimePersonalKnowledgeMatch,
    RuntimePersonalKnowledgeSearchRequest,
    RuntimePersonalKnowledgeSearchResponse,
    RuntimePersonalKnowledgeSourceFile,
    RuntimePersonalKnowledgeState,
    RuntimePersonalKnowledgeWikiMatch,
    RuntimePersonalKnowledgeWikiPage,
    RuntimePersonalKnowledgeWikiReadPageRequest,
    RuntimePersonalKnowledgeWikiSearchResponse,
)
from app.services.personal_knowledge_acl import PersonalKnowledgeAclService
from app.services.application_agent_personal_knowledge import (
    mark_creator_permission_revoked,
)
from app.services.personal_knowledge_provider import (
    FilebasePersonalKnowledgeClient,
    PersonalKnowledgeProviderError,
    WeKnoraPersonalKnowledgeClient,
    normalize_wiki_slug,
)
from app.services.weknora_rerank_settings import (
    WeKnoraRerankSettingStoreError,
)
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


class RuntimePersonalKnowledgeError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimePersonalKnowledgeIdentity:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_agent_profile_id: int
    plugin_active: bool
    allowed_knowledge_base_ids: frozenset[int] | None = None
    selection_owner_user_id: int | None = None
    search_denial_code: str | None = None


@dataclass(frozen=True)
class EffectivePersonalKnowledgeFile:
    file_id: int
    filename: str
    oss_url: str | None
    oss_key: str | None
    provider_document_id: str | None = None


@dataclass(frozen=True)
class EffectivePersonalKnowledge:
    knowledge_base_id: int
    name: str
    description: str | None
    dataset_id: str
    updated_at: datetime
    files: tuple[EffectivePersonalKnowledgeFile, ...]
    provider: str = "filebase"
    provider_capabilities: dict | None = None


class RuntimePersonalKnowledgeStore(Protocol):
    async def authenticate(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimePersonalKnowledgeIdentity:
        ...

    async def effective(
        self,
        identity: RuntimePersonalKnowledgeIdentity,
    ) -> list[EffectivePersonalKnowledge]:
        ...


class SqlAlchemyRuntimePersonalKnowledgeStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        codec: RuntimeApiKeyCodec | None = None,
        acl: PersonalKnowledgeAclService | None = None,
    ) -> None:
        self.session = session
        self.codec = codec or RuntimeApiKeyCodec()
        self.acl = acl or PersonalKnowledgeAclService(session)

    async def authenticate(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimePersonalKnowledgeIdentity:
        row = (
            await self.session.execute(
                select(
                    HermesRuntimeBinding,
                    CloudHermesProfile,
                    User,
                    RuntimeAgentProfile,
                )
                .join(
                    CloudHermesProfile,
                    CloudHermesProfile.id == HermesRuntimeBinding.runtime_workspace_id,
                )
                .join(User, User.id == HermesRuntimeBinding.user_id)
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.runtime_workspace_id
                    == HermesRuntimeBinding.runtime_workspace_id,
                )
                .where(
                    HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                    HermesRuntimeBinding.status.in_(("starting", "ready")),
                    CloudHermesProfile.status == "active",
                    User.status == "active",
                    RuntimeAgentProfile.org_id == HermesRuntimeBinding.org_id,
                    RuntimeAgentProfile.user_id == HermesRuntimeBinding.user_id,
                    *executable_runtime_agent_profile_clauses(),
                    (
                        RuntimeAgentProfile.id == runtime_agent_profile_id
                        if runtime_agent_profile_id is not None
                        else RuntimeAgentProfile.profile_kind == "default"
                    ),
                )
            )
        ).first()
        if row is None:
            raise RuntimePersonalKnowledgeError(
                "runtime_personal_knowledge_unauthorized",
                status_code=401,
            )
        binding, profile, user, agent_profile = row
        if not (
            binding.org_id == profile.org_id == user.org_id
            and binding.user_id == profile.user_id == user.id
        ):
            raise RuntimePersonalKnowledgeError(
                "runtime_personal_knowledge_unauthorized",
                status_code=401,
            )
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimePersonalKnowledgeError(
                "runtime_personal_knowledge_unauthorized",
                status_code=401,
            )
        return RuntimePersonalKnowledgeIdentity(
            org_id=int(user.org_id),
            user_id=int(user.id),
            runtime_workspace_id=int(profile.id),
            runtime_agent_profile_id=int(agent_profile.id),
            plugin_active=(
                agent_profile.profile_kind == "default"
                or "personal-knowledge"
                in set(
                    (agent_profile.capability_config or {}).get(
                        "plugin_ids",
                        [],
                    )
                )
            ),
        )

    async def effective(
        self,
        identity: RuntimePersonalKnowledgeIdentity,
    ) -> list[EffectivePersonalKnowledge]:
        if not identity.plugin_active:
            return []
        knowledge_bases = list(
            (
                await self.session.execute(
                    select(KnowledgeBase)
                    .join(
                        ProfileKnowledgeBinding,
                        ProfileKnowledgeBinding.knowledge_base_id == KnowledgeBase.id,
                    )
                    .where(
                        ProfileKnowledgeBinding.runtime_workspace_id
                        == identity.runtime_workspace_id,
                        ProfileKnowledgeBinding.runtime_agent_profile_id
                        == identity.runtime_agent_profile_id,
                        ProfileKnowledgeBinding.org_id == identity.org_id,
                        ProfileKnowledgeBinding.status == "enabled",
                        KnowledgeBase.org_id == identity.org_id,
                        KnowledgeBase.kind == "personal",
                        KnowledgeBase.status == "active",
                        KnowledgeBase.dataset_id.is_not(None),
                    )
                    .order_by(KnowledgeBase.id)
                )
            ).scalars()
        )
        principal = Principal(org_id=identity.org_id, user_id=identity.user_id)
        roles_by_knowledge_base_id = await self.acl.effective_roles(
            principal=principal,
            knowledge_bases=knowledge_bases,
        )
        accessible_ids = [
            int(knowledge_base.id)
            for knowledge_base in knowledge_bases
            if int(knowledge_base.id) in roles_by_knowledge_base_id
        ]
        if knowledge_bases and not accessible_ids:
            raise RuntimePersonalKnowledgeError(
                "KNOWLEDGE_BASE_NO_PERMISSION",
                status_code=403,
            )
        files_by_knowledge_base_id: dict[
            int,
            list[EffectivePersonalKnowledgeFile],
        ] = {}
        if accessible_ids:
            files = (
                await self.session.execute(
                    select(KnowledgeBaseFile).where(
                        KnowledgeBaseFile.org_id == identity.org_id,
                        KnowledgeBaseFile.knowledge_base_id.in_(accessible_ids),
                        KnowledgeBaseFile.file_status == 1,
                        KnowledgeBaseFile.is_deleted == 0,
                    )
                )
            ).scalars()
            for file in files:
                files_by_knowledge_base_id.setdefault(
                    int(file.knowledge_base_id),
                    [],
                ).append(
                    EffectivePersonalKnowledgeFile(
                        file_id=int(file.id),
                        filename=str(file.filename),
                        oss_url=str(file.oss_url) if file.oss_url else None,
                        oss_key=str(file.oss_key) if file.oss_key else None,
                        provider_document_id=(
                            str(file.provider_document_id)
                            if getattr(file, "provider_document_id", None)
                            else None
                        ),
                    )
                )

        effective: list[EffectivePersonalKnowledge] = []
        for knowledge_base in knowledge_bases:
            knowledge_base_id = int(knowledge_base.id)
            if knowledge_base_id not in roles_by_knowledge_base_id:
                continue
            ready_files = tuple(
                files_by_knowledge_base_id.get(knowledge_base_id, [])
            )
            if not ready_files:
                continue
            effective.append(
                EffectivePersonalKnowledge(
                    knowledge_base_id=knowledge_base_id,
                    name=str(knowledge_base.name),
                    description=knowledge_base.description,
                    dataset_id=str(knowledge_base.dataset_id),
                    updated_at=knowledge_base.updated_at,
                    files=ready_files,
                    provider=str(getattr(knowledge_base, "provider", "filebase")),
                    provider_capabilities=getattr(
                        knowledge_base,
                        "provider_capabilities",
                        None,
                    ),
                )
            )
        return effective


class SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore:
    """Resolve selected personal knowledge through the active Agent run."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        runtime_session_id: str,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
        codec: RuntimeApiKeyCodec | None = None,
        acl: PersonalKnowledgeAclService | None = None,
    ) -> None:
        self.session = session
        self.runtime_session_id = str(runtime_session_id).strip()
        self.turn_id = str(turn_id or "").strip() or None
        self.tool_call_id = str(tool_call_id or "").strip() or None
        self.codec = codec or RuntimeApiKeyCodec()
        self.acl = acl or PersonalKnowledgeAclService(session)

    async def authenticate(
        self,
        application_agent_id: int,
        _runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimePersonalKnowledgeIdentity:
        row = (
            await self.session.execute(
                select(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgent,
                    ApplicationAgentRun,
                    User,
                )
                .join(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ApplicationAgentRuntimeBinding.application_agent_id,
                )
                .join(
                    ApplicationAgentRun,
                    ApplicationAgentRun.runtime_binding_id
                    == ApplicationAgentRuntimeBinding.id,
                )
                .outerjoin(
                    User,
                    User.id == ApplicationAgentRun.subject_user_id,
                )
                .where(
                    ApplicationAgent.id == application_agent_id,
                    or_(
                        ApplicationAgent.status == "published",
                        ApplicationAgentRun.channel == "studio",
                    ),
                    ApplicationAgentRuntimeBinding.status == "ready",
                    ApplicationAgentRun.runtime_session_id
                    == self.runtime_session_id,
                    ApplicationAgentRun.status == "running",
                )
            )
        ).first()
        if row is None:
            raise RuntimePersonalKnowledgeError(
                "application_agent_active_user_run_required",
                status_code=403,
            )
        binding, agent, run, user = row
        # Runtime session + the unique running ApplicationAgentRun are the
        # authoritative identity boundary. Hermes currently emits different
        # turn-id shapes to pre_llm_call hooks and tool handlers, while the
        # platform stream persists the canonical turn only after those hooks
        # have run. Never bind or reject a Run using that transient header.
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimePersonalKnowledgeError(
                "runtime_personal_knowledge_unauthorized",
                status_code=401,
            )
        if run.subject_type == "user" and (
            user is None
            or user.status != "active"
            or int(user.org_id) != int(agent.org_id)
        ):
            raise RuntimePersonalKnowledgeError(
                "knowledge_subject_disabled",
                status_code=403,
            )
        if run.subject_type not in {"user", "service"}:
            raise RuntimePersonalKnowledgeError(
                "knowledge_subject_disabled",
                status_code=403,
            )
        selected_ids = frozenset(
            int(item)
            for item in (
                await self.session.execute(
                    select(
                        ApplicationAgentPersonalKnowledgeBinding
                        .knowledge_base_id
                    ).where(
                        ApplicationAgentPersonalKnowledgeBinding.org_id
                        == int(agent.org_id),
                        ApplicationAgentPersonalKnowledgeBinding
                        .application_agent_id == int(agent.id),
                        ApplicationAgentPersonalKnowledgeBinding.status
                        == "enabled",
                    )
                )
            ).scalars()
        )
        if run.subject_type == "service":
            return RuntimePersonalKnowledgeIdentity(
                org_id=int(agent.org_id),
                user_id=0,
                runtime_workspace_id=int(agent.id),
                runtime_agent_profile_id=0,
                plugin_active=False,
                allowed_knowledge_base_ids=frozenset(),
                selection_owner_user_id=int(agent.created_by_user_id),
                search_denial_code="knowledge_user_delegation_required",
            )
        return RuntimePersonalKnowledgeIdentity(
            org_id=int(agent.org_id),
            user_id=int(user.id),
            runtime_workspace_id=int(agent.id),
            runtime_agent_profile_id=0,
            plugin_active=bool(selected_ids),
            allowed_knowledge_base_ids=selected_ids,
            selection_owner_user_id=int(agent.created_by_user_id),
        )


    async def effective(
        self,
        identity: RuntimePersonalKnowledgeIdentity,
    ) -> list[EffectivePersonalKnowledge]:
        selected_ids = identity.allowed_knowledge_base_ids
        if not identity.plugin_active:
            return []
        if selected_ids is not None and not selected_ids:
            return []
        statement = select(KnowledgeBase).where(
            KnowledgeBase.org_id == identity.org_id,
            KnowledgeBase.kind == "personal",
            KnowledgeBase.status == "active",
            KnowledgeBase.dataset_id.is_not(None),
            KnowledgeBase.id.in_(selected_ids or ()),
        )
        if selected_ids is not None:
            statement = statement.where(
                KnowledgeBase.id.in_(selected_ids)
            )
        knowledge_bases = list(
            (
                await self.session.execute(
                    statement.order_by(KnowledgeBase.id)
                )
            ).scalars()
        )
        existing_ids = {
            int(knowledge_base.id) for knowledge_base in knowledge_bases
        }
        selection_owner_id = identity.selection_owner_user_id
        owner_roles = (
            await self.acl.effective_roles(
                principal=Principal(
                    org_id=identity.org_id,
                    user_id=selection_owner_id,
                ),
                knowledge_bases=knowledge_bases,
            )
            if selection_owner_id is not None
            else {}
        )
        revoked_ids = set(selected_ids or ()) - set(owner_roles)
        if revoked_ids:
            await mark_creator_permission_revoked(
                self.session,
                application_agent_id=identity.runtime_workspace_id,
                knowledge_base_ids=revoked_ids,
            )
        caller_roles = await self.acl.effective_roles(
            principal=Principal(
                org_id=identity.org_id,
                user_id=identity.user_id,
            ),
            knowledge_bases=[
                item
                for item in knowledge_bases
                if int(item.id) in owner_roles
            ],
        )
        accessible_ids = existing_ids.intersection(
            owner_roles,
            caller_roles,
        )
        if selected_ids and not accessible_ids:
            raise RuntimePersonalKnowledgeError(
                "KNOWLEDGE_BASE_NO_PERMISSION",
                status_code=403,
            )
        files_by_knowledge_base: dict[
            int, list[KnowledgeBaseFile]
        ] = {}
        if accessible_ids:
            files = (
                await self.session.execute(
                    select(KnowledgeBaseFile).where(
                        KnowledgeBaseFile.org_id == identity.org_id,
                        KnowledgeBaseFile.knowledge_base_id.in_(
                            accessible_ids
                        ),
                        KnowledgeBaseFile.file_status == 1,
                        KnowledgeBaseFile.is_deleted == 0,
                    )
                )
            ).scalars()
            for file in files:
                file_knowledge_base_id = getattr(
                    file,
                    "knowledge_base_id",
                    None,
                )
                if (
                    file_knowledge_base_id is None
                    and len(accessible_ids) == 1
                ):
                    file_knowledge_base_id = next(
                        iter(accessible_ids)
                    )
                if file_knowledge_base_id is None:
                    continue
                files_by_knowledge_base.setdefault(
                    int(file_knowledge_base_id),
                    [],
                ).append(file)
        effective: list[EffectivePersonalKnowledge] = []
        for knowledge_base in knowledge_bases:
            if int(knowledge_base.id) not in accessible_ids:
                continue
            ready_files = tuple(
                EffectivePersonalKnowledgeFile(
                    file_id=int(file.id),
                    filename=str(file.filename),
                    oss_url=str(file.oss_url) if file.oss_url else None,
                    oss_key=str(file.oss_key) if file.oss_key else None,
                    provider_document_id=(
                        str(file.provider_document_id)
                        if getattr(file, "provider_document_id", None)
                        else None
                    ),
                )
                for file in files_by_knowledge_base.get(
                    int(knowledge_base.id),
                    [],
                )
            )
            if not ready_files:
                continue
            effective.append(
                EffectivePersonalKnowledge(
                    knowledge_base_id=int(knowledge_base.id),
                    name=str(knowledge_base.name),
                    description=knowledge_base.description,
                    dataset_id=str(knowledge_base.dataset_id),
                    updated_at=knowledge_base.updated_at,
                    files=ready_files,
                    provider=str(getattr(knowledge_base, "provider", "filebase")),
                    provider_capabilities=getattr(
                        knowledge_base,
                        "provider_capabilities",
                        None,
                    ),
                )
            )
        return effective


class RuntimePersonalKnowledgeService:
    def __init__(
        self,
        store: RuntimePersonalKnowledgeStore,
        *,
        provider: FilebasePersonalKnowledgeClient | None = None,
        weknora_provider: WeKnoraPersonalKnowledgeClient | None = None,
        weknora_provider_factory: (
            Callable[[], WeKnoraPersonalKnowledgeClient] | None
        ) = None,
        weknora_rerank_enabled: (
            Callable[[], Awaitable[bool]] | None
        ) = None,
        min_similarity: float | None = None,
    ) -> None:
        self.store = store
        self.provider = provider or FilebasePersonalKnowledgeClient()
        self.weknora_provider = weknora_provider
        self.weknora_provider_factory = (
            weknora_provider_factory or WeKnoraPersonalKnowledgeClient
        )
        self.weknora_rerank_enabled = weknora_rerank_enabled
        self.min_similarity = (
            min_similarity
            if min_similarity is not None
            else settings.PERSONAL_KNOWLEDGE_MIN_SIMILARITY
        )

    def _get_weknora_provider(self) -> WeKnoraPersonalKnowledgeClient:
        if self.weknora_provider is None:
            self.weknora_provider = self.weknora_provider_factory()
        return self.weknora_provider

    async def _is_weknora_rerank_enabled(self) -> bool:
        if self.weknora_rerank_enabled is None:
            return True
        try:
            return await self.weknora_rerank_enabled()
        except WeKnoraRerankSettingStoreError as exc:
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            ) from exc

    async def _effective(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> tuple[
        RuntimePersonalKnowledgeIdentity,
        list[EffectivePersonalKnowledge],
    ]:
        identity = await self.store.authenticate(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
        )
        return identity, await self.store.effective(identity)

    @staticmethod
    def _narrow_requested(
        effective: list[EffectivePersonalKnowledge],
        requested_ids: list[int] | None,
    ) -> list[EffectivePersonalKnowledge]:
        if requested_ids is None:
            return effective
        requested = set(requested_ids)
        effective_ids = {
            knowledge_base.knowledge_base_id for knowledge_base in effective
        }
        if not requested.issubset(effective_ids):
            raise RuntimePersonalKnowledgeError(
                "KNOWLEDGE_BASE_NO_PERMISSION",
                status_code=403,
            )
        return [
            knowledge_base
            for knowledge_base in effective
            if knowledge_base.knowledge_base_id in requested
        ]

    def _scope_for_capability(
        self,
        effective: list[EffectivePersonalKnowledge],
        capability: str,
        *,
        explicit_request: bool,
    ) -> list[EffectivePersonalKnowledge]:
        capable = [
            knowledge_base
            for knowledge_base in effective
            if getattr(
                self._capabilities_for(knowledge_base),
                capability,
            )
        ]
        if explicit_request and len(capable) != len(effective):
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_capability_unavailable",
                status_code=409,
            )
        return capable

    @staticmethod
    def _capabilities_for(
        knowledge_base: EffectivePersonalKnowledge,
    ) -> RuntimePersonalKnowledgeCapabilities:
        if knowledge_base.provider == "filebase":
            return RuntimePersonalKnowledgeCapabilities(rag=True, wiki=False)
        snapshot = knowledge_base.provider_capabilities
        if not isinstance(snapshot, dict):
            return RuntimePersonalKnowledgeCapabilities(rag=True, wiki=False)
        return RuntimePersonalKnowledgeCapabilities(
            rag=(
                snapshot.get("vector") is True
                or snapshot.get("keyword") is True
            ),
            wiki=snapshot.get("wiki") is True,
        )

    async def state(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimePersonalKnowledgeState:
        identity, knowledge_bases = await self._effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
        )
        ordered_knowledge_bases = sorted(
            knowledge_bases,
            key=lambda knowledge_base: knowledge_base.knowledge_base_id,
        )
        revision_digest = hashlib.sha256(
            json.dumps(
                [
                    {
                        "capabilities": self._capabilities_for(
                            knowledge_base
                        ).model_dump(),
                        "description": knowledge_base.description or "",
                        "files": sorted(
                            file.file_id
                            for file in knowledge_base.files
                        ),
                        "id": knowledge_base.knowledge_base_id,
                        "name": knowledge_base.name,
                        "updated_at": (
                            knowledge_base.updated_at.isoformat()
                        ),
                    }
                    for knowledge_base in ordered_knowledge_bases
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        profile_revision = (
            str(identity.runtime_agent_profile_id)
            if identity.runtime_agent_profile_id
            else "none"
        )
        return RuntimePersonalKnowledgeState(
            enabled=bool(ordered_knowledge_bases),
            knowledge_bases=[
                RuntimePersonalKnowledgeItem(
                    id=knowledge_base.knowledge_base_id,
                    name=knowledge_base.name,
                    description=knowledge_base.description,
                    ready_file_count=len(knowledge_base.files),
                    capabilities=self._capabilities_for(knowledge_base),
                )
                for knowledge_base in ordered_knowledge_bases
            ],
            revision=(
                f"pkb:v2:{runtime_workspace_id}:"
                f"{profile_revision}:{revision_digest}"
            ),
        )

    @staticmethod
    def _matching_file(
        knowledge_base: EffectivePersonalKnowledge,
        chunk: dict,
    ) -> EffectivePersonalKnowledgeFile | None:
        keyword = str(chunk.get("document_keyword") or "")
        oss_path = str(chunk.get("oss_path") or "")
        for file in knowledge_base.files:
            if keyword and keyword == file.filename:
                return file
            if oss_path and oss_path in {file.oss_url, file.oss_key}:
                return file
            if oss_path and file.oss_key and oss_path.endswith(file.oss_key):
                return file
        return None

    @staticmethod
    def _source_url(
        knowledge_base: EffectivePersonalKnowledge,
        file: EffectivePersonalKnowledgeFile,
    ) -> str:
        return (
            "/?view=knowledge&tab=personal"
            f"&kb={knowledge_base.knowledge_base_id}&file={file.file_id}"
        )

    def _filebase_matches(
        self,
        effective: list[EffectivePersonalKnowledge],
        response: dict,
    ) -> list[RuntimePersonalKnowledgeMatch]:
        chunks = ((response.get("data") or {}).get("chunks") or [])
        by_dataset = {
            knowledge_base.dataset_id: knowledge_base
            for knowledge_base in effective
        }
        matches: list[RuntimePersonalKnowledgeMatch] = []
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            knowledge_base = by_dataset.get(str(chunk.get("dataset_id") or ""))
            if knowledge_base is None:
                continue
            file = self._matching_file(knowledge_base, chunk)
            if file is None:
                continue
            similarity = (
                float(chunk["similarity"])
                if chunk.get("similarity") is not None
                else None
            )
            if similarity is not None and similarity < self.min_similarity:
                continue
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            page = chunk.get("page")
            matches.append(
                RuntimePersonalKnowledgeMatch(
                    knowledge_base_id=knowledge_base.knowledge_base_id,
                    knowledge_base_name=knowledge_base.name,
                    file_id=file.file_id,
                    filename=file.filename,
                    chunk_id=str(chunk.get("id") or f"chunk-{index}"),
                    content=content,
                    score=similarity,
                    page=int(page) if isinstance(page, int) else None,
                    source_url=self._source_url(knowledge_base, file),
                    image_source_url=None,
                )
            )
        return matches

    @staticmethod
    def _weknora_page(locators: object) -> int | None:
        if not isinstance(locators, list):
            return None
        for locator in locators:
            if not isinstance(locator, dict) or locator.get("type") != "pdf":
                continue
            page = locator.get("page")
            if isinstance(page, int) and page > 0:
                return page
        return None

    def _weknora_matches(
        self,
        effective: list[EffectivePersonalKnowledge],
        response: dict,
    ) -> list[RuntimePersonalKnowledgeMatch]:
        by_dataset = {
            knowledge_base.dataset_id: knowledge_base
            for knowledge_base in effective
        }
        matches: list[RuntimePersonalKnowledgeMatch] = []
        for chunk in response.get("data") or []:
            if not isinstance(chunk, dict):
                continue
            knowledge_base = by_dataset.get(
                str(chunk.get("knowledge_base_id") or "")
            )
            if knowledge_base is None:
                continue
            provider_document_id = str(chunk.get("knowledge_id") or "")
            file = next(
                (
                    candidate
                    for candidate in knowledge_base.files
                    if candidate.provider_document_id
                    and candidate.provider_document_id == provider_document_id
                ),
                None,
            )
            if file is None:
                continue
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            raw_score = chunk.get("score")
            try:
                score = float(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                score = None
            matches.append(
                RuntimePersonalKnowledgeMatch(
                    knowledge_base_id=knowledge_base.knowledge_base_id,
                    knowledge_base_name=knowledge_base.name,
                    file_id=file.file_id,
                    filename=file.filename,
                    chunk_id=None,
                    content=content,
                    score=score,
                    page=self._weknora_page(chunk.get("source_locators")),
                    source_url=self._source_url(knowledge_base, file),
                    image_source_url=None,
                )
            )
        return matches

    async def _authorized_effective(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        requested_ids: list[int] | None,
    ) -> tuple[
        RuntimePersonalKnowledgeIdentity,
        list[EffectivePersonalKnowledge],
    ]:
        identity, effective = await self._effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
        )
        if identity.search_denial_code is not None:
            raise RuntimePersonalKnowledgeError(
                identity.search_denial_code,
                status_code=403,
            )
        return identity, self._narrow_requested(effective, requested_ids)

    async def search(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        request: RuntimePersonalKnowledgeSearchRequest,
    ) -> RuntimePersonalKnowledgeSearchResponse:
        _, effective = await self._authorized_effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
            request.knowledge_base_ids,
        )
        effective = self._scope_for_capability(
            effective,
            "rag",
            explicit_request=request.knowledge_base_ids is not None,
        )
        return await self._search_effective(request, effective)

    async def _search_effective(
        self,
        request: RuntimePersonalKnowledgeSearchRequest,
        effective: list[EffectivePersonalKnowledge],
    ) -> RuntimePersonalKnowledgeSearchResponse:
        searched_ids = [
            knowledge_base.knowledge_base_id for knowledge_base in effective
        ]
        if not effective:
            return RuntimePersonalKnowledgeSearchResponse(
                query=request.query,
                matches=[],
                low_confidence=True,
                searched_knowledge_base_ids=[],
            )
        filebase_effective = [
            knowledge_base
            for knowledge_base in effective
            if knowledge_base.provider != "weknora"
        ]
        weknora_effective = [
            knowledge_base
            for knowledge_base in effective
            if knowledge_base.provider == "weknora"
        ]
        rerank_enabled = (
            await self._is_weknora_rerank_enabled()
            if weknora_effective
            else True
        )
        try:
            calls = []
            if weknora_effective:
                calls.append(
                    (
                        "weknora",
                        self._get_weknora_provider().search(
                            question=request.query,
                            dataset_ids=[
                                knowledge_base.dataset_id
                                for knowledge_base in weknora_effective
                            ],
                            match_count=10,
                            rerank_enabled=rerank_enabled,
                        ),
                    )
                )
            if filebase_effective:
                calls.append(
                    (
                        "filebase",
                        self.provider.search(
                            question=request.query,
                            dataset_ids=[
                                knowledge_base.dataset_id
                                for knowledge_base in filebase_effective
                            ],
                        ),
                    )
                )
            results = await asyncio.gather(
                *(call for _, call in calls),
            )
        except PersonalKnowledgeProviderError as exc:
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            ) from exc
        responses = {
            name: result
            for (name, _), result in zip(calls, results, strict=True)
        }
        matches: list[RuntimePersonalKnowledgeMatch] = []
        if "filebase" in responses:
            matches.extend(
                self._filebase_matches(
                    filebase_effective,
                    responses["filebase"],
                )
            )
        if "weknora" in responses:
            matches.extend(
                self._weknora_matches(
                    weknora_effective,
                    responses["weknora"],
                )
            )
        return RuntimePersonalKnowledgeSearchResponse(
            query=request.query,
            matches=matches,
            low_confidence=not matches,
            searched_knowledge_base_ids=searched_ids,
        )

    async def _wiki_effective(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        requested_ids: list[int] | None,
    ) -> list[EffectivePersonalKnowledge]:
        identity, effective = await self._effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
        )
        if identity.search_denial_code is not None:
            raise RuntimePersonalKnowledgeError(
                identity.search_denial_code,
                status_code=403,
            )
        effective = self._narrow_requested(effective, requested_ids)
        return self._scope_for_capability(
            effective,
            "wiki",
            explicit_request=requested_ids is not None,
        )

    @staticmethod
    def _knowledge_base_source_url(
        knowledge_base: EffectivePersonalKnowledge,
    ) -> str:
        return (
            "/?view=knowledge&tab=personal"
            f"&kb={knowledge_base.knowledge_base_id}"
        )

    def _wiki_source_files(
        self,
        knowledge_base: EffectivePersonalKnowledge,
        source_refs: object,
    ) -> list[RuntimePersonalKnowledgeSourceFile]:
        if not isinstance(source_refs, list):
            return []
        files: list[RuntimePersonalKnowledgeSourceFile] = []
        seen: set[int] = set()
        for source_ref in source_refs:
            provider_document_id = str(source_ref).split("|", 1)[0].strip()
            file = next(
                (
                    candidate
                    for candidate in knowledge_base.files
                    if candidate.provider_document_id
                    and candidate.provider_document_id == provider_document_id
                ),
                None,
            )
            if file is None or file.file_id in seen:
                continue
            seen.add(file.file_id)
            files.append(
                RuntimePersonalKnowledgeSourceFile(
                    file_id=file.file_id,
                    filename=file.filename,
                    source_url=self._source_url(knowledge_base, file),
                )
            )
        return files

    async def wiki_search(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        request: RuntimePersonalKnowledgeSearchRequest,
    ) -> RuntimePersonalKnowledgeWikiSearchResponse:
        effective = await self._wiki_effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
            request.knowledge_base_ids,
        )
        return await self._wiki_search_effective(request, effective)

    async def _wiki_search_effective(
        self,
        request: RuntimePersonalKnowledgeSearchRequest,
        effective: list[EffectivePersonalKnowledge],
    ) -> RuntimePersonalKnowledgeWikiSearchResponse:
        searched_ids = [
            knowledge_base.knowledge_base_id for knowledge_base in effective
        ]
        if not effective:
            return RuntimePersonalKnowledgeWikiSearchResponse(
                query=request.query,
                matches=[],
                low_confidence=True,
                searched_knowledge_base_ids=[],
            )
        try:
            response = await self._get_weknora_provider().search_wiki(
                query=request.query,
                dataset_ids=[
                    knowledge_base.dataset_id for knowledge_base in effective
                ],
                limit=10,
            )
        except PersonalKnowledgeProviderError as exc:
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            ) from exc
        by_dataset = {
            knowledge_base.dataset_id: knowledge_base
            for knowledge_base in effective
        }
        matches: list[RuntimePersonalKnowledgeWikiMatch] = []
        for hit in response.get("data") or []:
            if not isinstance(hit, dict):
                continue
            knowledge_base = by_dataset.get(
                str(hit.get("knowledge_base_id") or "")
            )
            if knowledge_base is None:
                continue
            slug = str(hit.get("slug") or "").strip()
            if not slug:
                continue
            matches.append(
                RuntimePersonalKnowledgeWikiMatch(
                    knowledge_base_id=knowledge_base.knowledge_base_id,
                    knowledge_base_name=knowledge_base.name,
                    slug=slug,
                    title=str(hit.get("title") or slug),
                    summary=str(hit.get("summary") or ""),
                    match_snippet=str(hit.get("match_snippet") or ""),
                    source_url=self._knowledge_base_source_url(knowledge_base),
                    source_files=self._wiki_source_files(
                        knowledge_base,
                        hit.get("source_refs"),
                    ),
                )
            )
        return RuntimePersonalKnowledgeWikiSearchResponse(
            query=request.query,
            matches=matches,
            low_confidence=not matches,
            searched_knowledge_base_ids=searched_ids,
        )

    async def hybrid_search(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        request: RuntimePersonalKnowledgeSearchRequest,
    ) -> RuntimePersonalKnowledgeHybridSearchResponse:
        _, effective = await self._authorized_effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
            request.knowledge_base_ids,
        )
        rag_effective = [
            knowledge_base
            for knowledge_base in effective
            if self._capabilities_for(knowledge_base).rag
        ]
        wiki_effective = [
            knowledge_base
            for knowledge_base in effective
            if self._capabilities_for(knowledge_base).wiki
        ]
        rag_result, wiki_result = await asyncio.gather(
            self._search_effective(request, rag_effective),
            self._wiki_search_effective(request, wiki_effective),
        )
        return RuntimePersonalKnowledgeHybridSearchResponse(
            query=request.query,
            rag_results=rag_result.matches,
            wiki_results=wiki_result.matches,
            rag_searched_knowledge_base_ids=(
                rag_result.searched_knowledge_base_ids
            ),
            wiki_searched_knowledge_base_ids=(
                wiki_result.searched_knowledge_base_ids
            ),
            low_confidence=(
                not rag_result.matches and not wiki_result.matches
            ),
        )

    async def wiki_read_page(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        request: RuntimePersonalKnowledgeWikiReadPageRequest,
    ) -> RuntimePersonalKnowledgeWikiPage:
        effective = await self._wiki_effective(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
            [request.knowledge_base_id],
        )
        if not effective:
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_capability_unavailable",
                status_code=409,
            )
        knowledge_base = effective[0]
        try:
            slug = normalize_wiki_slug(request.slug)
            body = await self._get_weknora_provider().read_wiki_page(
                dataset_id=knowledge_base.dataset_id,
                slug=slug,
            )
        except PersonalKnowledgeProviderError as exc:
            if exc.status_code in {400, 404}:
                raise RuntimePersonalKnowledgeError(
                    "personal_knowledge_wiki_page_not_found",
                    status_code=404,
                ) from exc
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            ) from exc
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            )
        try:
            upstream_slug = normalize_wiki_slug(str(data.get("slug") or ""))
        except PersonalKnowledgeProviderError as exc:
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            ) from exc
        if (
            str(data.get("knowledge_base_id") or "")
            != knowledge_base.dataset_id
            or upstream_slug != slug
        ):
            raise RuntimePersonalKnowledgeError(
                "personal_knowledge_search_unavailable",
                status_code=503,
            )
        return RuntimePersonalKnowledgeWikiPage(
            knowledge_base_id=knowledge_base.knowledge_base_id,
            knowledge_base_name=knowledge_base.name,
            slug=slug,
            title=str(data.get("title") or slug),
            content=data["content"],
            source_url=self._knowledge_base_source_url(knowledge_base),
            source_files=self._wiki_source_files(
                knowledge_base,
                data.get("source_refs"),
            ),
        )
