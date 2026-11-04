from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import secrets
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioSession,
    HermesRuntimeBinding,
    ModelEntry,
)
from app.schemas.runtime_edge import (
    RUNTIME_EDGE_WEBSOCKET_PROTOCOL,
    RuntimeEdgeBootstrapResponse,
    RuntimeEdgeGrantRequest,
    RuntimeEdgeSessionGrantResponse,
)
from app.schemas.provider_routes import ProviderRouteGrantV1
from app.schemas.model_runtime_capabilities import (
    normalize_runtime_capabilities,
    supports_native_vision,
)
from app.services.runtime_chat import (
    RuntimeChatRequestIntent,
    RuntimeChatService,
    SqlAlchemyRuntimeChatStore,
)
from app.services.chat_files import (
    ChatFileIndexService,
    ChatFilePermissionError,
    SqlAlchemyChatFileStore,
)
from app.services.application_agent_studio_chat_files import (
    ApplicationAgentStudioChatFileService,
)
from app.services.runtime_ticket_issuer import RuntimeTicketIssuer
from app.services.profile_execution_identity import (
    EMPTY_ASSET_DIGEST,
    ProfileExecutionIdentityError,
    resolve_profile_execution_identity,
)
from app.services.runtime_provider_routes import (
    ProviderRouteBindingIdentity,
    RuntimeProviderRouteError,
    RuntimeProviderRouteResolver,
)
from app.services.runtime_maintenance import (
    RuntimeMaintenanceError,
    RuntimeMaintenanceService,
    RuntimeMaintenanceSnapshot,
)
from app.services.application_agent_execution_policy import (
    ApplicationAgentExecutionPolicyError,
    ApplicationAgentExecutionPolicyResolver,
    resolve_application_execution_config,
)
from app.services.application_agent_execution_identity import application_asset_digests
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


_RUNTIME_HANDLE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,126}[a-z0-9])?$")
_EMPTY_ASSET_DIGEST = EMPTY_ASSET_DIGEST


class RuntimeEdgeGrantError(PermissionError):
    pass


class RuntimeEdgeGrantControl(Protocol):
    async def require_grant_issuance(
        self,
        *,
        lock: bool = False,
    ) -> RuntimeMaintenanceSnapshot: ...


@dataclass(frozen=True)
class RuntimeEdgeGrantTarget:
    runtime_id: str
    binding_id: str
    endpoint_host_header: str
    api_server_key: str
    runtime_session_id: str
    model_entry_id: int
    upstream_model_id: str
    profile_id: str
    runtime_agent_profile_id: int
    config_revision: str
    profile_desired_revision: int
    profile_applied_revision: int
    profile_digest: str
    skill_digest: str
    mcp_digest: str
    knowledge_digest: str
    application_agent_id: int | None = None
    context_length: int = 128_000
    runtime_capabilities: dict[str, str] | None = None
    provider_route: ProviderRouteGrantV1 | None = None
    attachments: tuple[dict[str, str | None], ...] = ()
    admission_generation: int = 1
    grant_issuance_enabled: bool = True


class RuntimeEdgeGrantService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        codec: RuntimeApiKeyCodec | None = None,
        maintenance_control: RuntimeEdgeGrantControl | None = None,
    ) -> None:
        self.db = db
        self.codec = codec or RuntimeApiKeyCodec()
        self.maintenance_control = maintenance_control or RuntimeMaintenanceService(db)

    async def target(
        self,
        *,
        principal: Principal,
        request: RuntimeEdgeGrantRequest,
    ) -> RuntimeEdgeGrantTarget:
        admission = await self._grant_admission()
        prepared = await RuntimeChatService(
            SqlAlchemyRuntimeChatStore(self.db)
        ).prepare_run(
            principal=principal,
            intent=RuntimeChatRequestIntent(
                runtime_workspace_id=request.runtime_workspace_id,
                runtime_session_id=request.runtime_session_id,
                model_entry_id=request.model_entry_id,
                message="_",
                source_type="web",
                channel="web",
            ),
        )
        if prepared.model_entry_id is None:
            raise RuntimeEdgeGrantError("Runtime model is unavailable")
        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.org_id == principal.org_id,
                HermesRuntimeBinding.user_id == principal.user_id,
                HermesRuntimeBinding.runtime_workspace_id
                == request.runtime_workspace_id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        if (
            binding is None
            or not binding.endpoint_host_header
            or not _RUNTIME_HANDLE.fullmatch(str(binding.runtime_id))
        ):
            raise RuntimeEdgeGrantError("Runtime Edge is unavailable")
        config_revision = str(
            binding.config_hash
            or f"generation-{int(binding.config_generation or 0)}"
        )[:128]
        upstream_model_id, context_length, runtime_capabilities, provider_route = await self._model_route(
            org_id=principal.org_id,
            model_entry_id=int(prepared.model_entry_id),
            runtime_id=str(binding.runtime_id),
            binding_type="personal_runtime",
            binding_id=int(binding.id),
            pvc_storage_identity_hash=binding.pvc_storage_identity_hash,
        )
        attachments = await self._attachments(
            principal=principal,
            runtime_workspace_id=request.runtime_workspace_id,
            runtime_session_id=prepared.runtime_session_id,
            file_ids=request.attachment_file_ids,
        )
        self._require_vision_support(
            runtime_capabilities=runtime_capabilities,
            attachments=attachments,
        )
        (
            profile_desired_revision,
            profile_applied_revision,
            profile_digest,
            skill_digest,
            mcp_digest,
            knowledge_digest,
        ) = await self._profile_execution_identity(
            principal=principal,
            runtime_workspace_id=request.runtime_workspace_id,
            runtime_agent_profile_id=prepared.runtime_agent_profile_id,
            hermes_profile_name=prepared.hermes_profile_name,
        )
        return RuntimeEdgeGrantTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_server_key=self.codec.decrypt(str(binding.api_key_encrypted)),
            runtime_session_id=prepared.runtime_session_id,
            model_entry_id=int(prepared.model_entry_id),
            upstream_model_id=upstream_model_id,
            context_length=context_length,
            runtime_capabilities=runtime_capabilities,
            provider_route=provider_route,
            profile_id=str(prepared.hermes_profile_name or "default"),
            runtime_agent_profile_id=int(prepared.runtime_agent_profile_id),
            application_agent_id=None,
            config_revision=config_revision,
            profile_desired_revision=profile_desired_revision,
            profile_applied_revision=profile_applied_revision,
            profile_digest=profile_digest,
            skill_digest=skill_digest,
            mcp_digest=mcp_digest,
            knowledge_digest=knowledge_digest,
            attachments=attachments,
            admission_generation=admission.credential_admission_generation,
            grant_issuance_enabled=admission.grant_issuance_enabled,
        )

    async def application_target(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        request: RuntimeEdgeGrantRequest,
    ) -> RuntimeEdgeGrantTarget:
        admission = await self._grant_admission()
        if request.runtime_workspace_id != application_agent_id:
            raise RuntimeEdgeGrantError("Application Runtime does not match")
        row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationAgentRuntimeBinding,
                )
                .join(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgentRuntimeBinding.application_agent_id
                    == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.id == application_agent_id,
                    ApplicationAgent.org_id == principal.org_id,
                    ApplicationAgentRuntimeBinding.status == "ready",
                )
            )
        ).first()
        if row is None:
            raise RuntimeEdgeGrantError("Application Runtime is unavailable")
        agent, binding = row
        try:
            applied_config = await resolve_application_execution_config(
                self.db,
                agent=agent,
                binding=binding,
            )
        except ApplicationAgentExecutionPolicyError as exc:
            raise RuntimeEdgeGrantError(
                exc.code
            ) from exc
        session_prefix = f"studio-{application_agent_id}-{principal.user_id}-"
        session_id = request.runtime_session_id
        if (
            len(session_id) > 128
            or not session_id.startswith(session_prefix)
            or re.fullmatch(r"[A-Za-z0-9_-]+", session_id) is None
        ):
            raise RuntimeEdgeGrantError("Application Studio session is invalid")
        owned_session = await self.db.scalar(
            select(ApplicationAgentStudioSession.id).where(
                ApplicationAgentStudioSession.org_id == principal.org_id,
                ApplicationAgentStudioSession.application_agent_id
                == application_agent_id,
                ApplicationAgentStudioSession.user_id == principal.user_id,
                ApplicationAgentStudioSession.runtime_session_id == session_id,
                ApplicationAgentStudioSession.status == "active",
            )
        )
        if owned_session is None:
            raise RuntimeEdgeGrantError("Application Studio session is invalid")
        try:
            model_policy = await ApplicationAgentExecutionPolicyResolver(
                self.db
            ).resolve_model(
                org_id=int(agent.org_id),
                owner_user_id=int(agent.created_by_user_id),
                config=applied_config.config,
            )
        except ApplicationAgentExecutionPolicyError as exc:
            raise RuntimeEdgeGrantError(
                exc.code
            ) from exc
        model_entry_id = model_policy.model_entry_id
        if (
            binding.endpoint_host_header is None
            or not _RUNTIME_HANDLE.fullmatch(str(binding.runtime_id))
        ):
            raise RuntimeEdgeGrantError("Application Runtime Edge is unavailable")
        upstream_model_id, context_length, runtime_capabilities, provider_route = await self._model_route(
            org_id=principal.org_id,
            model_entry_id=model_entry_id,
            runtime_id=str(binding.runtime_id),
            binding_type="application_runtime",
            binding_id=int(binding.id),
            pvc_storage_identity_hash=binding.pvc_storage_identity_hash,
        )
        attachments = await self._application_attachments(
            principal=principal,
            application_agent_id=application_agent_id,
            runtime_session_id=session_id,
            file_ids=request.attachment_file_ids,
        )
        self._require_vision_support(
            runtime_capabilities=runtime_capabilities,
            attachments=attachments,
        )
        profile_digest, skill_digest, mcp_digest, knowledge_digest = (
            application_asset_digests(applied_config.config)
        )
        return RuntimeEdgeGrantTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_server_key=self.codec.decrypt(str(binding.api_key_encrypted)),
            runtime_session_id=session_id,
            model_entry_id=model_entry_id,
            upstream_model_id=upstream_model_id,
            context_length=context_length,
            runtime_capabilities=runtime_capabilities,
            provider_route=provider_route,
            profile_id=f"application-{application_agent_id}",
            runtime_agent_profile_id=0,
            application_agent_id=application_agent_id,
            config_revision=f"generation-{applied_config.generation}",
            profile_desired_revision=int(agent.config_generation or 0),
            profile_applied_revision=int(applied_config.generation),
            profile_digest=profile_digest,
            skill_digest=skill_digest,
            mcp_digest=mcp_digest,
            knowledge_digest=knowledge_digest,
            attachments=attachments,
            admission_generation=admission.credential_admission_generation,
            grant_issuance_enabled=admission.grant_issuance_enabled,
        )

    def issue_bootstrap(
        self,
        *,
        principal: Principal,
        target: RuntimeEdgeGrantTarget,
        allowed_origin: str,
        now: int | None = None,
    ) -> RuntimeEdgeBootstrapResponse:
        self._require_target_grant_issuance(target)
        issued_at = (
            int(datetime.now(timezone.utc).timestamp())
            if now is None
            else now
        )
        issuer = RuntimeTicketIssuer(
            runtime_id=target.runtime_id,
            secret=target.api_server_key,
            admission_generation=target.admission_generation,
            grant_issuance_enabled=target.grant_issuance_enabled,
        )
        access = issuer.runtime_access(
            sub=str(principal.user_id),
            user_id=principal.user_id,
            org_id=principal.org_id,
            allowed_origin=allowed_origin,
            connection_nonce=secrets.token_urlsafe(24),
            issued_at=issued_at,
        )
        grant = self._session_grant(
            issuer=issuer,
            principal=principal,
            target=target,
            issued_at=issued_at,
        )
        response_values = grant.model_dump()
        if "admission_generation" in getattr(
            RuntimeEdgeBootstrapResponse,
            "model_fields",
            {},
        ):
            response_values["admission_generation"] = target.admission_generation
        return RuntimeEdgeBootstrapResponse(
            **response_values,
            runtime_handle=target.runtime_id,
            websocket_path=f"/runtime-edge/{target.runtime_id}/ws",
            websocket_protocol=RUNTIME_EDGE_WEBSOCKET_PROTOCOL,
            runtime_access=access,
            runtime_access_expires_at=datetime.fromtimestamp(
                issued_at + 300,
                tz=timezone.utc,
            ),
        )

    def issue_session_grant(
        self,
        *,
        principal: Principal,
        target: RuntimeEdgeGrantTarget,
        now: int | None = None,
    ) -> RuntimeEdgeSessionGrantResponse:
        self._require_target_grant_issuance(target)
        issued_at = (
            int(datetime.now(timezone.utc).timestamp())
            if now is None
            else now
        )
        return self._session_grant(
            issuer=RuntimeTicketIssuer(
                runtime_id=target.runtime_id,
                secret=target.api_server_key,
                admission_generation=target.admission_generation,
                grant_issuance_enabled=target.grant_issuance_enabled,
            ),
            principal=principal,
            target=target,
            issued_at=issued_at,
        )

    @staticmethod
    def _session_grant(
        *,
        issuer: RuntimeTicketIssuer,
        principal: Principal,
        target: RuntimeEdgeGrantTarget,
        issued_at: int,
    ) -> RuntimeEdgeSessionGrantResponse:
        token = issuer.session_grant(
            user_id=principal.user_id,
            org_id=principal.org_id,
            session_id=target.runtime_session_id,
            profile_id=target.profile_id,
            runtime_agent_profile_id=target.runtime_agent_profile_id,
            application_agent_id=target.application_agent_id,
            model_id=str(target.model_entry_id),
            upstream_model_id=target.upstream_model_id,
            context_length=target.context_length,
            runtime_capabilities=target.runtime_capabilities,
            config_revision=target.config_revision,
            profile_desired_revision=target.profile_desired_revision,
            profile_applied_revision=target.profile_applied_revision,
            profile_digest=target.profile_digest,
            skill_digest=target.skill_digest,
            mcp_digest=target.mcp_digest,
            knowledge_digest=target.knowledge_digest,
            max_prompt_bytes=settings.RUNTIME_EDGE_MAX_PROMPT_BYTES,
            max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
            attachments=list(target.attachments),
            provider_route=target.provider_route,
            issued_at=issued_at,
        )
        response_values: dict[str, object] = {
            "runtime_id": target.runtime_id,
            "runtime_session_id": target.runtime_session_id,
            "model_entry_id": target.model_entry_id,
            "profile_id": target.profile_id,
            "runtime_agent_profile_id": target.runtime_agent_profile_id,
            "application_agent_id": target.application_agent_id,
            "config_revision": target.config_revision,
            "profile_desired_revision": target.profile_desired_revision,
            "profile_applied_revision": target.profile_applied_revision,
            "profile_digest": target.profile_digest,
            "skill_digest": target.skill_digest,
            "mcp_digest": target.mcp_digest,
            "knowledge_digest": target.knowledge_digest,
            "session_grant": token,
            "session_grant_expires_at": datetime.fromtimestamp(
                issued_at + 1800,
                tz=timezone.utc,
            ),
        }
        if "admission_generation" in getattr(
            RuntimeEdgeSessionGrantResponse,
            "model_fields",
            {},
        ):
            response_values["admission_generation"] = target.admission_generation
        return RuntimeEdgeSessionGrantResponse(**response_values)

    async def _profile_execution_identity(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        hermes_profile_name: str,
    ) -> tuple[int, int, str, str, str, str]:
        if runtime_agent_profile_id is None:
            raise RuntimeEdgeGrantError("Runtime Agent Profile is unavailable")
        try:
            identity = await resolve_profile_execution_identity(
                self.db,
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
                hermes_profile_name=hermes_profile_name,
            )
        except ProfileExecutionIdentityError as exc:
            if str(exc) == "PROFILE_REVISION_NOT_FOUND":
                raise RuntimeEdgeGrantError(
                    "Runtime Agent Profile revision is unavailable"
                ) from exc
            raise RuntimeEdgeGrantError(
                "Runtime Agent Profile is unavailable"
            ) from exc
        return (
            identity.desired_revision,
            identity.applied_revision,
            identity.profile_digest,
            identity.skill_digest,
            identity.mcp_digest,
            identity.knowledge_digest,
        )

    async def _grant_admission(self) -> RuntimeMaintenanceSnapshot:
        try:
            return await self.maintenance_control.require_grant_issuance(lock=True)
        except RuntimeMaintenanceError as exc:
            raise RuntimeEdgeGrantError(exc.code) from exc

    @staticmethod
    def _require_target_grant_issuance(target: RuntimeEdgeGrantTarget) -> None:
        if not target.grant_issuance_enabled or target.admission_generation <= 0:
            raise RuntimeEdgeGrantError("RUNTIME_GRANT_ISSUANCE_DISABLED")

    async def _model_route(
        self,
        *,
        org_id: int,
        model_entry_id: int,
        runtime_id: str,
        binding_type: str,
        binding_id: int,
        pvc_storage_identity_hash: str | None,
    ) -> tuple[str, int, dict[str, str], ProviderRouteGrantV1 | None]:
        if settings.provider_route_issuance_enabled(runtime_id):
            try:
                resolved = await RuntimeProviderRouteResolver(self.db).resolve(
                    org_id=org_id,
                    model_entry_id=model_entry_id,
                    binding=ProviderRouteBindingIdentity(
                        runtime_id=runtime_id,
                        binding_type=binding_type,
                        binding_id=binding_id,
                        pvc_storage_identity_hash=str(
                            pvc_storage_identity_hash or ""
                        ),
                    ),
                )
            except (RuntimeProviderRouteError, ValueError) as exc:
                raise RuntimeEdgeGrantError(
                    "Runtime model Provider route is unavailable"
                ) from exc
            return (
                resolved.upstream_model_id,
                resolved.context_length,
                resolved.runtime_capabilities,
                resolved.provider_route,
            )
        row = (
            await self.db.execute(
                select(
                    ModelEntry.model_key,
                    ModelEntry.context_length,
                    ModelEntry.runtime_capabilities,
                ).where(
                ModelEntry.id == model_entry_id,
                ModelEntry.org_id == org_id,
                ModelEntry.status == "active",
            )
            )
        ).one_or_none()
        if row is None or not row[0]:
            raise RuntimeEdgeGrantError("Runtime model route is unavailable")
        context_length = int(row[1]) if int(row[1] or 0) > 0 else 128_000
        return (
            str(row[0]),
            context_length,
            normalize_runtime_capabilities(row[2]),
            None,
        )

    async def _attachments(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
        file_ids: list[int],
    ) -> tuple[dict[str, str | None], ...]:
        ordered_ids = list(dict.fromkeys(file_ids))
        if len(ordered_ids) != len(file_ids):
            raise RuntimeEdgeGrantError("Attachment identifiers must be unique")
        if not ordered_ids:
            return ()
        try:
            records = await ChatFileIndexService(
                SqlAlchemyChatFileStore(self.db)
            ).get_files(
                principal=principal,
                file_ids=ordered_ids,
            )
        except ChatFilePermissionError as exc:
            raise RuntimeEdgeGrantError("Attachment is unavailable") from exc
        if any(
            record.runtime_workspace_id != runtime_workspace_id
            or record.runtime_session_id != runtime_session_id
            for record in records
        ):
            raise RuntimeEdgeGrantError(
                "Attachment does not belong to the Runtime session"
            )
        return tuple(
            {
                "file_id": str(record.file_id),
                "display_name": record.display_name,
                "relative_path": record.relative_path,
                "mime_type": record.mime_type,
            }
            for record in records
        )

    @staticmethod
    def _require_vision_support(
        *,
        runtime_capabilities: dict[str, str] | None,
        attachments: tuple[dict[str, str | None], ...],
    ) -> None:
        if (
            any(
                str(attachment.get("mime_type") or "").startswith("image/")
                for attachment in attachments
            )
            and not supports_native_vision(runtime_capabilities)
        ):
            raise RuntimeEdgeGrantError("MODEL_VISION_UNSUPPORTED")

    async def _application_attachments(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        runtime_session_id: str,
        file_ids: list[int],
    ) -> tuple[dict[str, str | None], ...]:
        ordered_ids = list(dict.fromkeys(file_ids))
        if len(ordered_ids) != len(file_ids):
            raise RuntimeEdgeGrantError("Attachment identifiers must be unique")
        if not ordered_ids:
            return ()
        try:
            return await ApplicationAgentStudioChatFileService(
                self.db
            ).resolve_attachments(
                principal=principal,
                application_agent_id=application_agent_id,
                runtime_session_id=runtime_session_id,
                file_ids=ordered_ids,
            )
        except ChatFilePermissionError as exc:
            raise RuntimeEdgeGrantError("Attachment is unavailable") from exc
