from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import HermesRuntimeBinding, ModelEntry
from app.schemas.runtime_internal import (
    InternalTurnCommandPayload,
    RuntimeTurnIdentity,
)
from app.schemas.model_runtime_capabilities import normalize_runtime_capabilities
from app.services.runtime_chat import CreatedRuntimeRun
from app.services.profile_execution_identity import (
    ProfileExecutionIdentityError,
    resolve_profile_execution_identity,
)
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
    build_runtime_command,
    deterministic_command_id,
    load_runtime_command_admission_generation,
)
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec
from app.services.runtime_provider_routes import (
    ProviderRouteBindingIdentity,
    RuntimeProviderRouteError,
    RuntimeProviderRouteResolver,
)


class RuntimeInternalTurnError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable_before_admission: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable_before_admission = retryable_before_admission


@dataclass(frozen=True)
class PersonalInternalTurnContext:
    target: RuntimeCommandTarget
    identity: RuntimeTurnIdentity


async def resolve_personal_internal_turn(
    db: AsyncSession,
    *,
    principal: Principal,
    created: CreatedRuntimeRun,
    endpoint: RuntimeEndpoint,
    codec: RuntimeApiKeyCodec | None = None,
) -> PersonalInternalTurnContext:
    binding = await db.scalar(
        select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.org_id == principal.org_id,
            HermesRuntimeBinding.user_id == principal.user_id,
            HermesRuntimeBinding.runtime_workspace_id
            == created.runtime_workspace_id,
            HermesRuntimeBinding.runtime_id == endpoint.runtime_id,
            HermesRuntimeBinding.status == "ready",
        )
    )
    if (
        binding is None
        or not binding.endpoint_host_header
        or created.model_entry_id is None
    ):
        raise RuntimeInternalTurnError("RUNTIME_NOT_READY")
    provider_route = None
    if settings.provider_route_issuance_enabled(str(binding.runtime_id)):
        try:
            resolved_route = await RuntimeProviderRouteResolver(db).resolve(
                org_id=principal.org_id,
                model_entry_id=int(created.model_entry_id),
                binding=ProviderRouteBindingIdentity(
                    runtime_id=str(binding.runtime_id),
                    binding_type="personal_runtime",
                    binding_id=int(binding.id),
                    pvc_storage_identity_hash=str(
                        binding.pvc_storage_identity_hash or ""
                    ),
                ),
            )
        except (RuntimeProviderRouteError, ValueError) as exc:
            raise RuntimeInternalTurnError(
                "RUNTIME_MODEL_PROVIDER_UNAVAILABLE"
            ) from exc
        upstream_model_id = resolved_route.upstream_model_id
        context_length = resolved_route.context_length
        runtime_capabilities = resolved_route.runtime_capabilities
        provider_route = resolved_route.provider_route
    else:
        model_route = (
            await db.execute(
                select(
                    ModelEntry.model_key,
                        ModelEntry.context_length,
                    ModelEntry.runtime_capabilities,
                ).where(
                    ModelEntry.id == int(created.model_entry_id),
                    ModelEntry.org_id == principal.org_id,
                    ModelEntry.status == "active",
                )
            )
        ).one_or_none()
        if model_route is None or not model_route[0]:
            raise RuntimeInternalTurnError("RUNTIME_MODEL_UNAVAILABLE")
        upstream_model_id = str(model_route[0])
        context_length = (
            int(model_route[1]) if int(model_route[1] or 0) > 0 else 128_000
        )
        runtime_capabilities = normalize_runtime_capabilities(model_route[2])
    key_codec = codec or RuntimeApiKeyCodec()
    admission_generation = await load_runtime_command_admission_generation(db)
    try:
        profile_identity = await resolve_profile_execution_identity(
            db,
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=created.runtime_workspace_id,
            runtime_agent_profile_id=created.runtime_agent_profile_id,
            hermes_profile_name=str(created.hermes_profile_name or "default"),
        )
    except ProfileExecutionIdentityError as exc:
        raise RuntimeInternalTurnError("RUNTIME_PROFILE_UNAVAILABLE") from exc
    return PersonalInternalTurnContext(
        target=RuntimeCommandTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_server_key=key_codec.decrypt(str(binding.api_key_encrypted)),
            admission_generation=admission_generation,
        ),
        identity=RuntimeTurnIdentity(
            user_id=principal.user_id,
            org_id=principal.org_id,
            session_id=created.runtime_session_id,
            profile_id=str(created.hermes_profile_name or "default"),
            runtime_agent_profile_id=(
                profile_identity.runtime_agent_profile_id
            ),
            profile_desired_revision=profile_identity.desired_revision,
            profile_applied_revision=profile_identity.applied_revision,
            profile_digest=profile_identity.profile_digest,
            skill_digest=profile_identity.skill_digest,
            mcp_digest=profile_identity.mcp_digest,
            knowledge_digest=profile_identity.knowledge_digest,
            model_id=str(created.model_entry_id),
            upstream_model_id=upstream_model_id,
            context_length=context_length,
            runtime_capabilities=runtime_capabilities,
            provider_route=provider_route,
            config_revision=str(
                binding.config_hash
                or f"generation-{int(binding.config_generation or 0)}"
            )[:128],
            max_prompt_bytes=settings.RUNTIME_EDGE_MAX_PROMPT_BYTES,
            max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
            admission_generation=admission_generation,
        ),
    )


async def execute_personal_internal_turn(
    *,
    context: PersonalInternalTurnContext,
    created: CreatedRuntimeRun,
    prompt: str,
    source: str,
    execution_key: str,
    timeout_seconds: float = 600,
    capacity_wait_seconds: float = 60,
    event_sink: Callable[
        [str, dict[str, object]], Awaitable[None]
    ]
    | None = None,
    client: RuntimeCommandClient | None = None,
) -> dict[str, Any]:
    command_client = client or RuntimeCommandClient()
    owns_client = client is None
    payload = InternalTurnCommandPayload(
        source=source,
        run_id=created.runtime_run_id,
        client_message_id=deterministic_command_id(
            "internal-turn-message",
            source,
            execution_key,
        ),
        identity=context.identity,
        prompt=prompt,
    )
    envelope = build_runtime_command(
        target=context.target,
        command_id=deterministic_command_id(
            "internal-turn",
            source,
            context.target.binding_id,
            execution_key,
        ),
        payload=payload,
        deadline_seconds=max(
            1,
            int(timeout_seconds + capacity_wait_seconds),
        ),
    )
    loop = asyncio.get_running_loop()
    capacity_deadline = loop.time() + max(0.0, capacity_wait_seconds)
    capacity_attempt = 0
    try:
        while True:
            ack = await command_client.send(
                target=context.target,
                envelope=envelope,
            )
            if (
                ack.state == "accepted"
                and ack.run_id == created.runtime_run_id
            ):
                break
            if (
                ack.state == "rejected"
                and ack.error_code
                in {
                    "APPLICATION_CAPACITY_BUSY",
                    "PERSONAL_CAPACITY_BUSY",
                    "SESSION_BUSY",
                    "RUNTIME_DRAINING",
                }
                and loop.time() < capacity_deadline
            ):
                delays = (1, 2, 4, 8, 15)
                delay = delays[min(capacity_attempt, len(delays) - 1)]
                capacity_attempt += 1
                await asyncio.sleep(
                    min(delay, max(0.0, capacity_deadline - loop.time()))
                )
                continue
            raise RuntimeInternalTurnError(
                ack.error_code or "RUNTIME_TURN_REJECTED",
                retryable_before_admission=ack.state == "rejected",
            )
        return await command_client.wait_run(
            target=context.target,
            run_id=created.runtime_run_id,
            event_sink=event_sink,
            timeout_seconds=timeout_seconds,
        )
    except RuntimeCommandError as exc:
        raise RuntimeInternalTurnError(
            "RUNTIME_TURN_SUBMISSION_UNCERTAIN"
        ) from exc
    finally:
        if owns_client:
            await command_client.close()


async def submit_personal_internal_turn(
    *,
    context: PersonalInternalTurnContext,
    created: CreatedRuntimeRun,
    prompt: str,
    source: str,
    execution_key: str,
    source_detail: dict[str, Any] | None = None,
    scheduled_task_id: int | None = None,
    scheduled_occurrence_key: str | None = None,
    command_id_override: str | None = None,
    client: RuntimeCommandClient | None = None,
) -> dict[str, Any]:
    """Admit a turn without waiting for streamed output.

    A Web clarify answer may cold-start its Runtime.  Once admission succeeds,
    normal run projection and the browser reconnect path own the output.
    """

    command_client = client or RuntimeCommandClient()
    owns_client = client is None
    try:
        payload = InternalTurnCommandPayload(
            source=source,
            run_id=created.runtime_run_id,
            client_message_id=deterministic_command_id(
                "internal-turn-message", source, execution_key
            ),
            identity=context.identity,
            prompt=prompt,
            source_detail=source_detail,
            scheduled_task_id=scheduled_task_id,
            scheduled_occurrence_key=scheduled_occurrence_key,
        )
        envelope = build_runtime_command(
            target=context.target,
            command_id=(
                command_id_override
                or deterministic_command_id(
                    "internal-turn",
                    source,
                    context.target.binding_id,
                    execution_key,
                )
            ),
            payload=payload,
            deadline_seconds=90,
        )
        ack = await command_client.send(target=context.target, envelope=envelope)
        result = dict(ack.result or {})
        canonical_run_id = str(
            result.get("canonical_run_id") or ack.run_id or ""
        ).strip()
        coalesced = result.get("coalesced") is True
        accepted_identity = (
            ack.run_id == created.runtime_run_id
            or (
                coalesced
                and bool(ack.run_id)
                and canonical_run_id == ack.run_id
            )
        )
        if ack.state != "accepted" or not accepted_identity:
            raise RuntimeInternalTurnError(
                ack.error_code or "RUNTIME_TURN_REJECTED",
                retryable_before_admission=ack.state == "rejected",
            )
        return result
    except RuntimeCommandError as exc:
        raise RuntimeInternalTurnError("RUNTIME_TURN_SUBMISSION_UNCERTAIN") from exc
    finally:
        if owns_client:
            await command_client.close()
