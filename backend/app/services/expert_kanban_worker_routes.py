from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import HermesRuntimeBinding, ModelEntry, RuntimeRun
from app.models import ModelProvider
from app.schemas.model_runtime_capabilities import normalize_runtime_capabilities
from app.schemas.runtime_internal import (
    KanbanWorkerRouteActivateCommandPayload,
    KanbanWorkerRouteRevokeCommandPayload,
)
from app.services.expert_kanban_worker_runs import ExpertKanbanWorkerRunService
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
    build_runtime_command,
    deterministic_command_id,
    load_runtime_command_admission_generation,
    canonical_payload_digest,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec
from app.services.runtime_provider_routes import (
    ProviderRouteBindingIdentity,
    RuntimeProviderRouteError,
    RuntimeProviderRouteResolver,
)


class ExpertKanbanWorkerRouteService:
    """Converge Supervisor model leases for task-scoped expert workers."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        client: RuntimeCommandClient | None = None,
        codec: RuntimeApiKeyCodec | None = None,
    ) -> None:
        self.session = session
        self.client = client or RuntimeCommandClient()
        self._owns_client = client is None
        self.codec = codec or RuntimeApiKeyCodec()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()

    async def activate_for_task(
        self,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
    ) -> bool:
        context = await self._context(
            runtime_workspace_id=runtime_workspace_id,
            board=board,
            task_id=task_id,
        )
        if context is None:
            return False
        run, binding, model, _provider, profile_id = context
        provider_route = None
        upstream_model_id = str(model.model_key)
        model_context_length = getattr(model, "context_length", None)
        context_length = (
            int(model_context_length)
            if int(model_context_length or 0) > 0
            else 128_000
        )
        runtime_capabilities = normalize_runtime_capabilities(
            model.runtime_capabilities
        )
        if settings.provider_route_issuance_enabled(str(binding.runtime_id)):
            try:
                resolved_route = await RuntimeProviderRouteResolver(
                    self.session
                ).resolve(
                    org_id=int(run.org_id),
                    model_entry_id=int(model.id),
                    binding=ProviderRouteBindingIdentity(
                        runtime_id=str(binding.runtime_id),
                        binding_type="personal_runtime",
                        binding_id=int(binding.id),
                        pvc_storage_identity_hash=str(
                            binding.pvc_storage_identity_hash or ""
                        ),
                    ),
                )
            except (RuntimeProviderRouteError, ValueError):
                return False
            upstream_model_id = resolved_route.upstream_model_id
            context_length = resolved_route.context_length
            runtime_capabilities = resolved_route.runtime_capabilities
            provider_route = resolved_route.provider_route
        payload = KanbanWorkerRouteActivateCommandPayload(
            run_id=str(run.runtime_run_id),
            task_id=task_id,
            board=board,
            profile_id=profile_id,
            org_id=int(run.org_id),
            model_id=str(model.id),
            upstream_model_id=upstream_model_id,
            context_length=context_length,
            runtime_capabilities=runtime_capabilities,
            provider_route=provider_route,
            max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
        )
        return await self._send(
            binding=binding,
            payload=payload,
            operation="activate",
        )

    async def revoke_for_task(
        self,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
    ) -> bool:
        context = await self._context(
            runtime_workspace_id=runtime_workspace_id,
            board=board,
            task_id=task_id,
        )
        if context is None:
            return False
        run, binding, _model, _provider, profile_id = context
        payload = KanbanWorkerRouteRevokeCommandPayload(
            run_id=str(run.runtime_run_id),
            task_id=task_id,
            board=board,
            profile_id=profile_id,
        )
        return await self._send(
            binding=binding,
            payload=payload,
            operation="revoke",
        )

    async def _context(
        self,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
        ) -> tuple[
            RuntimeRun,
            HermesRuntimeBinding,
            ModelEntry,
            ModelProvider,
            str,
        ] | None:
        run_id = ExpertKanbanWorkerRunService.runtime_run_id(
            runtime_workspace_id=runtime_workspace_id,
            board=board,
            task_id=task_id,
        )
        row = (
            await self.session.execute(
                select(
                    RuntimeRun,
                    HermesRuntimeBinding,
                    ModelEntry,
                    ModelProvider,
                )
                .join(
                    HermesRuntimeBinding,
                    HermesRuntimeBinding.runtime_workspace_id
                    == RuntimeRun.runtime_workspace_id,
                )
                .join(ModelEntry, ModelEntry.id == RuntimeRun.model_entry_id)
                .join(
                    ModelProvider,
                    ModelProvider.id == ModelEntry.provider_id,
                )
                .where(
                    RuntimeRun.runtime_run_id == run_id,
                    RuntimeRun.source_type
                    == ExpertKanbanWorkerRunService.SOURCE_TYPE,
                    HermesRuntimeBinding.status == "ready",
                    HermesRuntimeBinding.endpoint_host_header.is_not(None),
                    ModelEntry.status == "active",
                    ModelEntry.org_id == RuntimeRun.org_id,
                    ModelProvider.status == "active",
                )
            )
        ).first()
        if row is None:
            return None
        run, binding, model, provider = row
        profile_id = str(
            (run.request_payload or {}).get("expert_profile_name") or ""
        )
        if not profile_id.startswith("expert-"):
            return None
        return run, binding, model, provider, profile_id

    async def _send(self, *, binding, payload, operation: str) -> bool:
        admission_generation = await load_runtime_command_admission_generation(
            self.session,
            require_open=operation != "revoke",
        )
        target = RuntimeCommandTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_server_key=self.codec.decrypt(str(binding.api_key_encrypted)),
            admission_generation=admission_generation,
        )
        envelope = build_runtime_command(
            target=target,
            command_id=deterministic_command_id(
                "kanban_worker_route",
                operation,
                target.runtime_id,
                payload.profile_id,
                payload.run_id,
                # Provider route capsules are immutable per route digest but
                # may be reissued after a failed delivery. Include the
                # canonical payload identity so SQLite idempotency does not
                # reject a legitimate retry as a different payload under the
                # same command id.
                canonical_payload_digest(payload.model_dump(mode="json")),
            ),
            payload=payload,
            deadline_seconds=30,
        )
        try:
            ack = await self.client.send(target=target, envelope=envelope)
        except RuntimeCommandError as exc:
            logger = logging.getLogger(__name__)
            logger.warning("kanban worker route %s failed: %s", operation, exc)
            return False
        return ack.state == "completed"
